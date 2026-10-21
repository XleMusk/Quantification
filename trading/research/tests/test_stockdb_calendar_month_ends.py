"""Opt-in calendar scheduling on tiny synthetic histories, never market replays."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.research import experiment
from trading.research import stockdb_rolling_low_volatility as m


VARIANT = "low_volatility_252_20"
START, MONTH_END, NEXT = "2026-01-29", "2026-01-30", "2026-02-02"


def raw_frame(end):
    days = pd.bdate_range("2024-11-01", end).strftime("%Y-%m-%d")
    days = [d for d in days if not "2026-10-01" <= d <= "2026-10-07"]
    values = 10 + np.arange(len(days)) * .002 + np.sin(np.arange(len(days)) / 5) * .05
    return pd.DataFrame([dict(code="600001", symbol="600001.SSE", date=day,
        open=float(values[i]), high=float(values[i]) + .05, low=float(values[i]) - .05,
        close=float(values[i]), pre_close=float(values[max(0, i-1)]),
        volume=10_000_000., amount=100_000_000., is_st=False) for i, day in enumerate(days)])


def snapshot(path, frame):
    path.mkdir()
    bars = path / "bars.parquet"
    frame[list(m.SOURCE_COLUMNS)].to_parquet(bars, index=False)
    manifest = dict(status="complete_inventory_only", execution_admitted=False,
        chunks_expected=1, chunks=[dict(parquet_file=bars.name,
                                      parquet_sha256=sha256(bars.read_bytes()).hexdigest())])
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "ROOT", tmp_path / "project")
    monkeypatch.setattr(m, "DATA_ROOT", tmp_path / "data")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schema="cn_a_share_exchange_calendar_v1", year=2026,
        closed_ranges=["2026-10-01/2026-10-07"], open_sessions=[])), encoding="utf-8")
    frame = raw_frame(NEXT)
    old = snapshot(tmp_path / "old", frame.loc[frame.date.le(MONTH_END)])
    new = snapshot(tmp_path / "new", frame)
    return dict(calendar=calendar, frame=frame, old=old, new=new, root=tmp_path)


def execute(e, source=None, *, end=MONTH_END, resume=None, calendar=None, enabled=True, start=START):
    return m.run(e["old"] if source is None else source, start, end, variants=(VARIANT,),
        calendars=[e["calendar"] if calendar is None else calendar], resume_run=resume,
        calendar_driven_month_ends=enabled)


def saved_account(record):
    return Path(record["artifacts"]) / VARIANT / "account.json"


def test_month_end_generates_exact_weekend_successor_without_future_raw(evidence):
    e = evidence
    out, record = execute(e)
    assert record["status"] == "completed_research_only"
    assert record["sessions"] == 2 and record["source_end"] == MONTH_END
    account = json.loads(saved_account(record).read_text(encoding="utf-8"))
    assert account["pending_targets"] and not account["orders"] and not account["fills"]
    assert next(x for x in account["events"] if x["kind"] == "targets_generated")["execution_not_before"] == NEXT
    protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["month_end_schedule"] == record["month_end_schedule"]
    assert protocol["month_end_schedule"]["method_id"] == m.CALENDAR_MONTH_END_METHOD
    fixed = record["calendar_verification"]["fixed_open_sessions"]
    feed = m.RollingStockDBFeed(e["frame"], MONTH_END, variant=VARIANT, fixed_calendar=fixed)
    assert feed.next_day(MONTH_END) == NEXT and MONTH_END in feed.ends
    assert feed.calendar[-1] == NEXT and feed.rows.get(NEXT) is None
    assert NEXT not in feed.volumes.index and NEXT not in feed.closes.index
    assert all(table.index.max() == MONTH_END for table in feed._features.values())
    # A known future session is not an executable session; reject before any
    # pending target, price, order, cash or account cursor can be mutated.
    before = deepcopy(account)
    with pytest.raises(ValueError, match="not an observed raw execution session"):
        advance(account, feed, NEXT, m.portfolio(), 2,
                context=RunContext("synthetic-calendar-test", TradingMode.REPLAY))
    assert account == before


def test_pinned_holiday_skips_weekend_and_closure_without_fabricating_quotes(evidence):
    frame = raw_frame("2026-09-30")
    verification = m._calendar_verification([evidence["calendar"]], frame,
        "2026-09-30", "2026-09-30", include_fixed_sessions=True)
    feed = m.RollingStockDBFeed(frame, "2026-09-30", variant=VARIANT,
                                fixed_calendar=verification["fixed_open_sessions"])
    assert "2026-09-30" in feed.ends
    assert feed.next_day("2026-09-30") == "2026-10-08"
    assert feed.rows.get("2026-10-08") is None
    assert feed._features["momentum"].index.max() == "2026-09-30"


def test_next_month_append_keeps_old_month_end_target_and_account_prefix(evidence, monkeypatch):
    e = evidence
    out, original = execute(e)
    account_bytes = saved_account(original).read_bytes()
    monkeypatch.setattr(experiment, "new_state", lambda *args: pytest.fail("No account reset on continuation"))
    _, continued = execute(e, e["new"], end=NEXT, resume=out / "run.json")
    result = continued["results"][0]
    assert result["reused_sessions"] == 2 and result["newly_completed_sessions"] == 1
    assert continued["month_end_schedule"] == original["month_end_schedule"]
    assert continued["account_reset"] is False and saved_account(original).read_bytes() == account_bytes
    after = json.loads(saved_account(continued).read_text(encoding="utf-8"))
    assert after["day"] == NEXT and after["fills"]
    assert {fill["day"] for fill in after["fills"].values()} == {NEXT}
    assert all(lot["available"] == "2026-02-03" for lots in after["positions"].values() for lot in lots)
    a = pd.read_csv(Path(original["artifacts"]) / VARIANT / "daily.csv")
    b = pd.read_csv(Path(continued["artifacts"]) / VARIANT / "daily.csv")
    pd.testing.assert_frame_equal(a, b.iloc[:len(a)].reset_index(drop=True))
    old_ranks = pd.read_csv(Path(original["artifacts"]) / f"{VARIANT}_ranks.csv")
    new_ranks = pd.read_csv(Path(continued["artifacts"]) / f"{VARIANT}_ranks.csv")
    pd.testing.assert_frame_equal(old_ranks, new_ranks)


def test_legacy_mode_remains_unmodified_and_cannot_switch_on_resume(evidence):
    e = evidence
    out, legacy = execute(e, enabled=False)
    protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    assert "month_end_schedule" not in protocol and "month_end_schedule" not in legacy
    assert "fixed_open_sessions" not in legacy["calendar_verification"]
    feed = m.RollingStockDBFeed(e["frame"], MONTH_END, variant=VARIANT)
    assert MONTH_END not in feed.ends and feed.next_day(MONTH_END) == MONTH_END
    with pytest.raises(ValueError, match="month-end schedule mode"):
        execute(e, e["new"], end=NEXT, resume=out / "run.json")
    new_out, _ = execute(e)
    with pytest.raises(ValueError, match="month-end schedule mode"):
        execute(e, e["new"], end=NEXT, resume=new_out / "run.json", enabled=False)


@pytest.mark.parametrize("change", ["path", "bytes"])
def test_calendar_binding_cannot_be_replaced_during_resume(evidence, change):
    e = evidence
    out, saved = execute(e)
    original = saved_account(saved).read_bytes()
    calendar = e["calendar"]
    if change == "path":
        calendar = e["root"] / "calendar_same_bytes.json"
        calendar.write_bytes(e["calendar"].read_bytes())
    else:
        value = json.loads(calendar.read_text(encoding="utf-8"))
        value["synthetic_revision"] = True
        calendar.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed calendar binding"):
        execute(e, e["new"], end=NEXT, resume=out / "run.json", calendar=calendar)
    assert saved_account(saved).read_bytes() == original


def test_missing_replay_year_is_rejected_before_any_output(evidence):
    with pytest.raises(ValueError, match="every replay year"):
        execute(evidence, start="2025-12-31")
    assert not (evidence["root"] / "project").exists()
    assert not (evidence["root"] / "data").exists()


def test_year_end_without_exact_next_year_successor_is_rejected(evidence):
    source = snapshot(evidence["root"] / "year_end", raw_frame("2026-12-31"))
    with pytest.raises(ValueError, match="exact fixed-calendar successor"):
        execute(evidence, source, start="2026-12-30", end="2026-12-31")
    assert not (evidence["root"] / "project").exists()
    assert not (evidence["root"] / "data").exists()


def test_cli_explicitly_forwards_calendar_driven_flag(monkeypatch, capsys):
    observed = {}
    def run(*args, **kwargs):
        observed.update(kwargs)
        return Path("synthetic_only"), {"status": "completed_research_only"}
    monkeypatch.setattr(m, "run", run)
    assert m.main(["--snapshot", "synthetic_snapshot", "--calendar", "synthetic_calendar.json",
                   "--calendar-driven-month-ends"]) == 0
    assert observed["calendar_driven_month_ends"] is True
    assert observed["calendars"] == [Path("synthetic_calendar.json")]
