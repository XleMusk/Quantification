"""Persistent close-to-next-open mechanics; no model fitting or market returns."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.ledger import Ledger
from trading.research import recent_alpha158_forward_run as forward
from trading.research import recent_alpha158_target_publication as publication
from trading.research.baostock_daily_slice import digest
from trading.research.ipo_qualification import IpoQualification


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
SYMBOLS = [f"{i:06d}.SZSE" for i in range(1, 7)]


def selected(days):
    return pd.DataFrame([{"date": day, "symbol": symbol, "score": 1 - i / 10,
        "score_seed17": .9, "score_seed29": .8, "score_seed43": .7}
        for day in days for i, symbol in enumerate(SYMBOLS[:5])])


def raw(days):
    return pd.DataFrame([{"date": day, "code": "sz." + symbol[:6], "open": "10", "high": "10.1",
        "low": "9.9", "close": "10", "preclose": "10", "volume": "10000", "amount": "100000",
        "tradestatus": "1", "isST": "0", "pctChg": "0"} for day in days for symbol in SYMBOLS])


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps(CALENDAR), encoding="utf-8")
    sessions, previous = forward._sessions(cal)
    qualification = IpoQualification({s: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T12:00:00+08:00",
        "board": "main", "source_basis": "synthetic"} for s in SYMBOLS}, tuple(sessions), {"synthetic": True})
    contract = {key: key for key in publication.MODEL_FIELDS}
    contract["seed_models"] = {"17": "a", "29": "b", "43": "c"}
    anchor = {"day": "2026-10-09", "closes": dict.fromkeys(SYMBOLS, 10.)}
    proofs = [{"day": day, "raw_daily_sha256": "raw-" + day, "ordinary_inventory_rows": 6,
        "source_completed_at": ("2026-10-15T11:00:00+08:00" if day <= DAYS[2] else day + "T17:35:00+08:00"),
        "read_only_verified": True, "source_inventory_complete": True,
        "full_dated_roster_inventory_complete": True,
        "receipt_status": "complete_full_dated_close_inventory_not_admitted", "calendar_sha256": digest(cal)} for day in DAYS]
    loaded, batches, providers = {}, {}, {}
    for end in (2, 3, 4, 5):
        days, day = DAYS[:end + 1], DAYS[end]
        folder = tmp_path / ("provider_" + str(end))
        folder.mkdir()
        raw_path = folder / "raw.parquet"
        raw(days).to_parquet(raw_path, index=False)
        ready = "2026-10-15T11:30:00+08:00" if end == 2 else day + "T18:00:00+08:00"
        provider_record = {"schema": forward.PROVIDER_SCHEMA, "provider_method_id": forward.PROVIDER_METHOD,
            "status": "research_provider_completed", "created_at": ready,
            "calendar_sha256": digest(cal), "recent_source_days": days, "source_proofs": proofs[:end + 1],
            "recent_source_inventory_complete": True, "recent_raw_original_fields_unchanged": True,
            "recent_raw_observations_file": str(raw_path), "recent_raw_observations_sha256": digest(raw_path),
            "recent_raw_observation_rows": len(raw(days)), "raw_prices_imputed": False,
            **{k: False for k in ("model_refit", "model_trained", "future_labels_used_for_score", "execution_admitted", "selection_enabled", "strategy_validated")}}
        (folder / "result.json").write_text(json.dumps(provider_record), encoding="utf-8")
        providers[day] = folder
        if end == 5:
            continue
        batch_path = tmp_path / ("score_" + str(end) + ".json")
        score_days = days if end == 2 else [day]
        batch_path.write_text(json.dumps({**contract, "score_dates": score_days}), encoding="utf-8")
        source = {"batch_run": str(batch_path), "batch_sha256": digest(batch_path), "score_dates": score_days,
            "score_sha256": "score-" + day, "source_proofs": proofs[:end + 1],
            "source_available_at": ready, "score_computed_at": "2026-10-15T12:00:00+08:00" if end == 2 else day + "T18:10:00+08:00"}
        loaded[str(batch_path)] = (selected(score_days), raw(days), days, sessions, previous, source, anchor)
        batches[day] = batch_path
    load = lambda batch, calendar: deepcopy(loaded[str(Path(batch).resolve())])
    monkeypatch.setattr(publication, "_load", load)
    monkeypatch.setattr(forward, "_load", load)
    model = lambda record: {key: record[key] for key in publication.MODEL_FIELDS}
    monkeypatch.setattr(publication, "_model_contract", model)
    monkeypatch.setattr(forward, "_model_contract", model)
    monkeypatch.setattr(publication, "load_ipo_qualification", lambda *a, **kw: qualification)
    monkeypatch.setattr(forward, "load_ipo_qualification", lambda *a, **kw: qualification)
    monkeypatch.setattr(forward, "_context_anchor", lambda record: (anchor, {"synthetic_anchor": "fixed"}))
    monkeypatch.setattr(forward, "_now", lambda: datetime.fromisoformat("2026-10-19T20:00:00+08:00"))
    monkeypatch.setattr(engine, "_published_now", forward._now)
    pubs = []
    for end in (3, 4):
        day = DAYS[end]
        monkeypatch.setattr(publication, "_now", lambda day=day: datetime.fromisoformat(day + "T18:45:00+08:00"))
        folder = tmp_path / ("publication_" + str(end))
        publication.publish(batches[day], [batches[DAYS[2]], *([batches[DAYS[3]]] if end == 4 else [])],
            cal, "ipo", "master", folder)
        pubs.append(folder / "result.json")
    return {"calendar": cal, "providers": providers, "pubs": pubs, "run_dir": tmp_path / "persistent",
        "loaded": loaded, "qualification": qualification}


def execute(evidence, *, publications=None, provider_day="2026-10-16", through=None):
    return forward.run(publications or evidence["pubs"][:1], evidence["providers"][provider_day],
        evidence["calendar"], "ipo", "master", evidence["run_dir"], through=through)


def head(evidence):
    return json.loads((evidence["run_dir"] / "head.json").read_text(encoding="utf-8"))


def test_missing_exact_successor_waits_before_directory_lock_or_new_state(evidence, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No persistent side effects while the exact E is absent")
    monkeypatch.setattr(forward, "new_state", forbidden)
    monkeypatch.setattr(forward, "lock", forbidden)
    result = execute(evidence, provider_day="2026-10-15")
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert result["required_successor_day"] == "2026-10-16"
    assert not evidence["run_dir"].exists()


def test_exact_E_reference_unknown_does_not_create_books(evidence, monkeypatch):
    original = forward._reference
    monkeypatch.setattr(forward, "_reference", lambda base, day, symbol, previous:
        {"status": "unknown", "day": day} if day == "2026-10-16" else original(base, day, symbol, previous))
    monkeypatch.setattr(forward, "new_state", lambda *args: pytest.fail("No new_state before quality gate"))
    result = execute(evidence)
    assert result["status"] == "waiting_for_required_execution_quality_evidence"
    assert not evidence["run_dir"].exists()


def test_two_real_close_publications_append_same_Ledgers_context_pending_and_fills(evidence, monkeypatch):
    result = execute(evidence)
    assert result["newly_committed_case_sessions"] == 2
    first = head(evidence)
    for key, case in first["cases"].items():
        saved = PortfolioIntent.from_dict(json.loads(evidence["pubs"][0].read_text())["cases"][[c["id"] for c in forward.PROTOCOL["cases"]].index(key)]["portfolio_intent"])
        assert saved.context is None
        assert not case["state"]["orders"] and case["state"]["day"] == "2026-10-15"
        bound = PortfolioIntent.from_dict(case["journal"][0]["bound_intent"])
        assert bound.context == RunContext.from_dict(case["context"])
        assert (bound.decided_at, bound.weights, bound.execution_not_before) == (saved.decided_at, saved.weights, saved.execution_not_before)
        assert case["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-16"
    monkeypatch.setattr(forward, "new_state", lambda *args: pytest.fail("Continuation must not reset cash or state"))
    # Complete 16 source + actual 16 publication suffice.  19 raw is absent;
    # only its exact calendar date is needed for the new typed pending target.
    result = execute(evidence, publications=evidence["pubs"], provider_day="2026-10-16")
    assert result["newly_committed_case_sessions"] == 2
    second = head(evidence)
    for key, case in second["cases"].items():
        state = case["state"]
        assert case["context"] == first["cases"][key]["context"]
        assert case["daily"][:1] == first["cases"][key]["daily"]
        assert [r["date"] for r in case["daily"]] == DAYS[3:5]
        assert len(state["orders"]) == len(state["fills"]) == 5
        assert case["daily"][-1]["fees"] == pytest.approx(25.05)
        assert state["pending_published_close_intent"]["execution_not_before"] == "2026-10-19"
        context = RunContext.from_dict(case["context"])
        book = Ledger(state, forward._config(100000, 5), context)
        assert all(book.quantity(s) == 100 and book.available(s, "2026-10-16") == 0 for s in SYMBOLS[:5])
        assert "replay_risk_exit_policy" not in state
        assert case["journal"][0] == first["cases"][key]["journal"][0]
    original_bytes = (evidence["run_dir"] / "head.json").read_bytes()
    again = execute(evidence, publications=evidence["pubs"], provider_day="2026-10-16")
    assert again["newly_committed_case_sessions"] == 0
    assert (evidence["run_dir"] / "head.json").read_bytes() == original_bytes


@pytest.mark.parametrize("change", ["weights", "bound_context", "recording_date", "source", "model", "capacity"])
def test_changed_publication_rejected_before_new_directory(evidence, change):
    path = evidence["pubs"][0]
    record = json.loads(path.read_text())
    if change == "weights":
        record["cases"][0]["portfolio_intent"]["weights"][SYMBOLS[0]] = .01
    elif change == "bound_context":
        record["cases"][0]["portfolio_intent"]["context"] = RunContext("other", TradingMode.REPLAY).to_dict()
    elif change == "recording_date":
        record["recorded_at"] = "2026-10-16T00:10:00+08:00"
    elif change == "source":
        record["source"]["batch_sha256"] = "changed"
    elif change == "model":
        record["frozen_model_contract"]["model_run_sha256"] = "changed"
    else:
        record["capacity_prefix"]["top5_events"].pop()
    path.write_text(json.dumps(record), encoding="utf-8")
    if change == "recording_date":
        result = execute(evidence)
        assert "unbindable" in result["status"]
    else:
        with pytest.raises(ValueError):
            execute(evidence)
    assert not evidence["run_dir"].exists()


def test_missing_16_is_not_substituted_by_19(evidence, monkeypatch):
    original = forward._provider
    def gap(*args):
        value = original(*args)
        value["days"] = [d for d in value["days"] if d != "2026-10-16"]
        return value
    monkeypatch.setattr(forward, "_provider", gap)
    result = execute(evidence, provider_day="2026-10-19")
    assert result["required_successor_day"] == "2026-10-16" and not evidence["run_dir"].exists()


def test_consumed_prefix_cannot_be_dropped_or_rewritten(evidence):
    execute(evidence)
    original = (evidence["run_dir"] / "head.json").read_bytes()
    with pytest.raises(ValueError, match="prefix"):
        execute(evidence, publications=evidence["pubs"][1:], provider_day="2026-10-19")
    assert (evidence["run_dir"] / "head.json").read_bytes() == original
    with pytest.raises(ValueError):
        execute(evidence, through="2026-10-14")


def test_engine_failed_trial_preserves_cash_orders_cursor_and_is_not_resubmitted(evidence, monkeypatch):
    original = forward.advance
    calls = []
    def fail(trial, *args, **kwargs):
        calls.append(1)
        trial["cash"] = -123
        raise ValueError("synthetic terminal failure")
    monkeypatch.setattr(forward, "advance", fail)
    execute(evidence)
    saved = head(evidence)
    assert calls == [1, 1]
    assert all(c["state"]["cash"] == 10000000 and c["state"]["day"] is None and not c["state"]["orders"] for c in saved["cases"].values())
    assert len(list((evidence["run_dir"] / "failed_attempts").glob("*.json"))) == 2
    monkeypatch.setattr(forward, "advance", lambda *a, **kw: pytest.fail("Identical terminal attempt must not retry"))
    execute(evidence)
    assert head(evidence) == saved
    monkeypatch.setattr(forward, "advance", original)


def test_unknown_new_candidate_is_published_NO_CHANGE_not_cash_liquidation(evidence, monkeypatch):
    """A missing reference in a new candidate does not change held weights."""
    old_pub = json.loads(evidence["pubs"][1].read_text())
    batch = old_pub["source"]["batch_run"]
    loaded = evidence["loaded"][batch]
    current = loaded[0].copy()
    current.loc[current.symbol.eq(SYMBOLS[4]), "symbol"] = SYMBOLS[5]
    values = list(loaded)
    values[0] = current
    values[1] = loaded[1].copy()
    mask = values[1].date.eq("2026-10-16") & values[1].code.eq("sz." + SYMBOLS[5][:6])
    values[1].loc[mask, "preclose"] = "9"
    evidence["loaded"][batch] = tuple(values)
    provider = evidence["providers"]["2026-10-16"]
    provider_result_path = provider / "result.json"
    provider_record = json.loads(provider_result_path.read_text())
    raw_file = Path(provider_record["recent_raw_observations_file"])
    values[1].to_parquet(raw_file, index=False)
    provider_record["recent_raw_observations_sha256"] = digest(raw_file)
    provider_result_path.write_text(json.dumps(provider_record), encoding="utf-8")
    new_out = evidence["pubs"][1].parent.parent / "unknown_new_candidate_pub"
    monkeypatch.setattr(publication, "_now", lambda: datetime.fromisoformat("2026-10-16T18:45:00+08:00"))
    result = publication.publish(batch, [p["batch_run"] for p in old_pub["capacity_prefix"]["batches"]],
        evidence["calendar"], "ipo", "master", new_out)
    assert all(case["target_weights"] is None and case["portfolio_intent"]["kind"] == "NO_CHANGE" for case in result["cases"])
    execute(evidence)
    execute(evidence, publications=[evidence["pubs"][0], new_out / "result.json"])
    for case in head(evidence)["cases"].values():
        state = case["state"]
        assert len(state["fills"]) == 5 and state["day"] == "2026-10-16"
        assert not state.get("pending_published_close_intent") and state["pending_targets"] is None
        assert all(sum(lot["qty"] for lot in state["positions"][s]) == 100 for s in SYMBOLS[:5])
        assert case["journal"][-1]["bound_intent"]["kind"] == "NO_CHANGE"
        assert case["daily"][-1]["events"][-1]["kind"] == "no_decision"
