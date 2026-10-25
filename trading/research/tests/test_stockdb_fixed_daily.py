"""Daily orchestration uses the existing replay, with one current pointer."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import stockdb_fixed_daily as m
from trading.research import stockdb_rolling_low_volatility as rolling
from trading.research.tests.test_stockdb_calendar_month_ends import (
    evidence, START, MONTH_END, NEXT, VARIANT, saved_account)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def daily(evidence, monkeypatch):
    e = evidence
    monkeypatch.setattr(m, "DATA_ROOT", e["root"])
    # Its fixed original snapshot already contains the successor bar. The
    # seed decision still uses only rows through the month-end cutoff.
    manifest_path = e["new"] / "manifest.json"
    manifest = read(manifest_path)
    manifest.update(start="2024-11-01", end=NEXT)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    native = e["frame"].rename(columns={"pre_close": "preclose", "is_st": "isST"})[
        ["date", "code", "open", "high", "low", "close", "preclose",
         "volume", "amount", "isST"]].copy()
    native["tradestatus"] = "1"
    native["adjustflag"] = "3"
    supplemental = e["root"] / "synthetic_native_raw_daily.parquet"
    native.to_parquet(supplemental, index=False)
    out, record = rolling.run(e["new"], START, MONTH_END, variants=(VARIANT,),
        calendars=[e["calendar"]], calendar_driven_month_ends=True,
        persistent_full_exits=True, supplemental_raw_view=supplemental)
    package = e["root"] / "complete_successor"
    package.mkdir()
    raw = pd.DataFrame([{"date": NEXT, "code": "sh.600001"}])
    calendar_sha = sha256(e["calendar"].read_bytes()).hexdigest()
    source = {"read_only_verified": True, "source_folder": str(package.resolve()),
        "plan": {"day": NEXT, "calendar_sha256": calendar_sha, "roster_raw_sha256": "synthetic_roster"},
        "receipt": {"day": NEXT, "status": "complete_full_dated_close_inventory_not_admitted",
            "full_dated_roster_inventory_complete": True, "raw_daily_sha256": "synthetic_raw",
            "schema": "synthetic_fixture_only", "completed_at": "2026-02-02T16:00:00+08:00"}}
    (package/"plan.json").write_text(json.dumps(source["plan"]), encoding="utf-8")
    (package/"receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
    # Loader boundaries are mocked only for synthetic package metadata; the
    # strategy, checkpoint restore, orders and account path are real.
    monkeypatch.setattr(m, "load_complete", lambda path: (raw.copy(), deepcopy(source)))
    return dict(e=e, source=source, seed=out/"run.json", seed_record=record,
                directory=e["root"]/"daily_workflow", package=package)


def test_registered_saved_account_advances_once_and_duplicate_is_read_only(daily, monkeypatch):
    d = daily
    old_account = saved_account(d["seed_record"])
    old_bytes = old_account.read_bytes()
    m.register(d["seed"], VARIANT, d["directory"])
    head_before = (d["directory"]/"head.json").read_bytes()
    monkeypatch.setattr(m.rolling, "portfolio", rolling.portfolio)
    result = m.run(d["directory"], NEXT, packages=[d["package"]])
    assert result["execution_admitted"] is False
    head_after = (d["directory"]/"head.json").read_bytes()
    assert head_after != head_before and old_account.read_bytes() == old_bytes
    observed = m.status(d["directory"])
    assert observed["last_completed_day"] == NEXT
    assert observed["terminal_account"] == read(d["directory"] / "head.json")["source"]["account"]["path"]
    assert observed["cash"] is not None
    monkeypatch.setattr(m.rolling, "run", lambda *a, **kw: pytest.fail("No replay on an already committed cutoff"))
    monkeypatch.setattr(m, "load_complete", lambda *a: pytest.fail("No source reload on duplicate cutoff"))
    repeated = m.run(d["directory"], NEXT, packages=[d["package"]])
    assert repeated["status"] == "no_new_session"
    assert (d["directory"]/"head.json").read_bytes() == head_after
    assert m.status(d["directory"])["last_completed_day"] == NEXT


def test_missing_complete_day_waits_without_advancing_or_rebuilding(daily, monkeypatch):
    d = daily
    m.register(d["seed"], VARIANT, d["directory"])
    head = (d["directory"]/"head.json").read_bytes()
    monkeypatch.setattr(m.rolling, "run", lambda *a, **kw: pytest.fail("Missing source must not invoke replay"))
    result = m.run(d["directory"], NEXT)
    assert result["status"] == "waiting_for_complete_sources"
    assert result["missing_days"] == [NEXT]
    assert (d["directory"]/"head.json").read_bytes() == head


def test_failed_attempt_keeps_head_and_same_input_is_not_automatically_retried(daily, monkeypatch):
    d = daily
    m.register(d["seed"], VARIANT, d["directory"])
    head = (d["directory"]/"head.json").read_bytes()
    calls = []
    def fail(*a, **kw):
        calls.append(kw)
        raise ValueError("synthetic unprocessed source failure")
    monkeypatch.setattr(m.rolling, "run", fail)
    with pytest.raises(ValueError, match="synthetic unprocessed"):
        m.run(d["directory"], NEXT, packages=[d["package"]])
    assert len(calls) == 1 and (d["directory"]/"head.json").read_bytes() == head
    with pytest.raises(ValueError, match="failed|retry|attempt"):
        m.run(d["directory"], NEXT, packages=[d["package"]])
    assert len(calls) == 1 and (d["directory"]/"head.json").read_bytes() == head


def test_package_calendar_mismatch_cannot_advance_a_valid_saved_account(daily):
    d = daily
    m.register(d["seed"], VARIANT, d["directory"])
    head = (d["directory"]/"head.json").read_bytes()
    d["source"]["plan"]["calendar_sha256"] = "different_calendar"
    with pytest.raises(ValueError, match="calendar|source"):
        m.run(d["directory"], NEXT, packages=[d["package"]])
    assert (d["directory"]/"head.json").read_bytes() == head


def test_status_rejects_changed_authoritative_account_instead_of_falling_back(daily):
    d = daily
    m.register(d["seed"], VARIANT, d["directory"])
    account = saved_account(d["seed_record"])
    original = read(account)
    original["cash"] += 1
    account.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="changed|hash|pin|checkpoint"):
        m.status(d["directory"])


def test_legacy_seed_is_not_silently_upgraded(daily):
    d = daily
    old_out, record = rolling.run(d["e"]["new"], START, MONTH_END, variants=(VARIANT,),
        calendars=[d["e"]["calendar"]], calendar_driven_month_ends=True)
    old_bytes = saved_account(record).read_bytes()
    with pytest.raises(ValueError, match="lifecycle|method|persistent"):
        m.register(old_out/"run.json", VARIANT, d["directory"])
    assert saved_account(record).read_bytes() == old_bytes
    assert not (d["directory"]/"head.json").exists()


def test_completed_child_recovers_head_publication_without_replaying(daily, monkeypatch):
    d = daily
    m.register(d["seed"], VARIANT, d["directory"])
    before = (d["directory"]/"head.json").read_bytes()
    atomic = m._atomic
    def interrupt(path, value):
        if Path(path).name == "head.json" and value["generation"] == 1:
            raise OSError("synthetic interruption before head replacement")
        return atomic(path, value)
    monkeypatch.setattr(m, "_atomic", interrupt)
    with pytest.raises(OSError, match="head replacement"):
        m.run(d["directory"], NEXT, packages=[d["package"]])
    assert (d["directory"]/"head.json").read_bytes() == before
    attempts = list((d["directory"]/"attempts").glob("*.json"))
    assert len(attempts) == 1 and read(attempts[0])["result"]["status"] == "completed_research_only"
    monkeypatch.setattr(m, "_atomic", atomic)
    monkeypatch.setattr(m.rolling, "run", lambda *a, **kw: pytest.fail("Recover must reuse saved successful output"))
    result = m.recover(d["directory"], attempts[0].stem)
    assert result["status"] == "recovered_completed_result" and result["replay_repeated"] is False
    assert result["last_completed_day"] == NEXT and result["order_count"] > 0
    restored = (d["directory"]/"head.json").read_bytes()
    assert m.recover(d["directory"], attempts[0].stem)["status"] == "already_committed"
    assert (d["directory"]/"head.json").read_bytes() == restored
