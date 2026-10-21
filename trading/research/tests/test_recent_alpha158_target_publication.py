"""Publication clocks and frozen capacity, not Alpha return tests."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.contracts import PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from trading.research import recent_alpha158_target_publication as pub
from trading.research.baostock_daily_slice import digest
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, PREVIOUS, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
SYMBOLS = [f"{i:06d}.SZSE" for i in range(1, 7)]


def selected(days, symbols=SYMBOLS[:5]):
    return pd.DataFrame([{"date": day, "symbol": symbol, "score": 1 - i / 10,
        "score_seed17": .9, "score_seed29": .8, "score_seed43": .7}
        for day in days for i, symbol in enumerate(symbols)])


def raw(days=DAYS):
    return pd.DataFrame([{"date": day, "code": "sz." + symbol[:6], "open": "10", "high": "10.1",
        "low": "9.9", "close": "10", "preclose": "10", "volume": "10000", "amount": "100000",
        "tradestatus": "1", "isST": "0", "pctChg": "0"} for day in days for symbol in SYMBOLS])


def qualification(missing=()):
    return IpoQualification({symbol: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T12:00:00+08:00",
        "board": "main", "source_basis": "synthetic"} for symbol in SYMBOLS if symbol not in missing},
        tuple(SESSIONS), {"synthetic": True})


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    contract = {key: key for key in pub.MODEL_FIELDS}
    contract["seed_models"] = {"17": "a", "29": "b", "43": "c"}
    records = {}
    loaded = {}
    source_proofs = [{"day": day, "raw_daily_sha256": "raw" + day,
        "source_updated_at": (day + "T17:35:00+08:00") if day == DAYS[-1] else "2026-10-15T11:00:00+08:00",
        "source_completed_at": None} for day in DAYS]
    anchor = {"day": "2026-10-09", "closes": dict.fromkeys(SYMBOLS, 10.)}
    for name, score_days, provider_days, computed, ready in (
            ("prefix", DAYS[:3], DAYS[:3], "2026-10-15T12:00:00+08:00", "2026-10-15T11:30:00+08:00"),
            ("current", [DAYS[-1]], DAYS, "2026-10-15T18:10:00+08:00", "2026-10-15T18:00:00+08:00")):
        path = tmp_path / (name + ".json")
        record = {**deepcopy(contract), "score_dates": score_days}
        path.write_text(json.dumps(record), encoding="utf-8")
        records[name] = path
        proof = {"batch_run": str(path), "batch_sha256": digest(path), "score_dates": score_days,
            "score_sha256": name, "source_proofs": deepcopy(source_proofs[:len(provider_days)]),
            "score_computed_at": computed, "source_available_at": ready}
        loaded[path] = (selected(score_days), raw(provider_days), provider_days, SESSIONS, PREVIOUS, proof, anchor)
    monkeypatch.setattr(pub, "_load", lambda batch, cal: deepcopy(loaded[batch]))
    monkeypatch.setattr(pub, "_model_contract", lambda record: {key: record[key] for key in pub.MODEL_FIELDS})
    monkeypatch.setattr(pub, "load_ipo_qualification", lambda *a, **k: qualification())
    monkeypatch.setattr(pub, "_now", lambda: datetime.fromisoformat("2026-10-15T18:45:00+08:00"))
    return {"calendar": calendar, "records": records, "loaded": loaded, "out": tmp_path / "publication"}


def run(values, prefix=None):
    return pub.publish(values["records"]["current"], prefix or [values["records"]["prefix"]],
        values["calendar"], "ipo", "master", values["out"])


def test_real_late_evening_clock_two_typed_targets_no_successor_or_state(inputs, monkeypatch):
    # Any accidental accounting use is a failure; only the pure selector is reused.
    import trading.ledger
    monkeypatch.setattr(trading.ledger, "new_state", lambda *a, **k: pytest.fail("publication created state"))
    result = run(inputs)
    assert result["source_close_day"] == "2026-10-15" and result["proposed_execution_day"] == "2026-10-16"
    assert result["recorded_at"] == "2026-10-15T18:45:00+08:00"
    assert result["source_ready_at"] == "2026-10-15T17:35:00+08:00"
    assert result["score_computed_at"] == "2026-10-15T18:10:00+08:00"
    assert result["next_open_deadline"] == "2026-10-16T09:30:00+08:00"
    assert result["capacity_prefix"]["score_dates"] == DAYS[:3]
    assert result["holding_policy"].endswith("no_fixed_session_expiry")
    assert result["protocol"] == pub.PROTOCOL and result["config"] == pub._config(100000, 5).to_dict()
    assert [case["case"] for case in result["cases"]] == ["causal_first_seen_cap20", "uncapped"]
    for case in result["cases"]:
        intent = PortfolioIntent.from_dict(case["portfolio_intent"])
        assert intent.context is None and intent.decided_at == result["recorded_at"]
        assert intent.execution_not_before == "2026-10-16" and len(intent.weights) == 5
        assert set(dict(intent.weights).values()) == {.16} and case["target_cash_weight"] == pytest.approx(.2)
    assert result["successor_raw_used"] is result["account_created"] is result["orders_generated"] is False
    assert set(path.name for path in inputs["out"].iterdir()) == {"result.json"}
    assert all(result[key] is expected for key, expected in pub.FLAGS.items())


def test_unknown_selected_member_blocks_whole_case_and_preserves_raw_weights(inputs, monkeypatch):
    monkeypatch.setattr(pub, "load_ipo_qualification", lambda *a, **k: qualification(missing=(SYMBOLS[1],)))
    result = run(inputs)
    assert result["target_holdings_generated"] is False and result["research_rule_weights_generated"] is True
    for case in result["cases"]:
        intent = PortfolioIntent.from_dict(case["portfolio_intent"])
        assert intent.kind.value == "NO_CHANGE" and not intent.weights
        assert intent.execution_not_before == "2026-10-16"  # same metadata as FULL_TARGET
        assert case["target_weights"] is case["target_cash_weight"] is None
        assert len(case["raw_rule_weights_before_qualification"]) == 5
        assert len(case["selected_events"]) == 5 and SYMBOLS[-1] not in case["raw_rule_weights_before_qualification"]


def test_capacity_uses_all_old_top5_identities_no_refill_and_keeps_both_cases():
    old = selected(["2026-10-12"], [f"{i:06d}.SZSE" for i in range(1, 21)])
    current = selected([DAYS[-1]], [SYMBOLS[0], "000021.SZSE", "000022.SZSE"])
    base = pub.ExactSourceRawFeed(raw(), DAYS, SYMBOLS)
    bounded = pub._prepare_case(current, old, base, DAYS[-1], PREVIOUS, qualification(), pub.PROTOCOL["cases"][0])
    assert len(bounded["cap_symbols_before"]) == len(bounded["cap_symbols_after"]) == 20
    assert bounded["causal_cap_dropped_current_rows"] == 2
    assert bounded["raw_rule_weights_before_qualification"] == {SYMBOLS[0]: .16}
    assert len(bounded["ranked_top5_before_cap"]) == 3 and len(bounded["selected_events"]) == 1


def test_capacity_removing_all_current_events_preserves_frozen_no_change():
    prior_days = ["2026-10-09", *DAYS[:3]]
    prefix = pd.concat([selected([day], [f"{i:06d}.SZSE" for i in range(n * 5 + 1, n * 5 + 6)])
                        for n, day in enumerate(prior_days)], ignore_index=True)
    current = selected([DAYS[-1]], [f"{i:06d}.SZSE" for i in range(21, 26)])
    base = pub.ExactSourceRawFeed(raw(), DAYS, SYMBOLS)
    bounded = pub._prepare_case(current, prefix, base, DAYS[-1], PREVIOUS, qualification(), pub.PROTOCOL["cases"][0])
    assert len(bounded["cap_symbols_before"]) == 20 and bounded["causal_cap_dropped_current_rows"] == 5
    assert bounded["selected_events"] == [] and bounded["raw_rule_weights_before_qualification"] == {}
    assert bounded["reasons"] == ["no_selected_events_after_fixed_capacity_NO_CHANGE"]


def test_published_unbound_weights_bind_explicitly_then_reuse_shared_engine_next_open(inputs, monkeypatch):
    from trading import engine
    from trading.ledger import Ledger, new_state

    publication = run(inputs)
    saved = PortfolioIntent.from_dict(publication["cases"][0]["portfolio_intent"])
    context = RunContext("synthetic_published_alpha_path", TradingMode.REPLAY)
    bound = replace(saved, context=context)
    assert saved.context is None and bound.decided_at == publication["recorded_at"]
    execution, following = "2026-10-16", "2026-10-19"
    days = [*DAYS, execution, following]
    base = pub.ExactSourceRawFeed(raw([*DAYS, execution]), days, SYMBOLS)
    for instrument in base.instruments.values():
        instrument["ipoDate"] = "1999-01-01"
    base.scores = lambda *args: SignalUnavailable("no_new_publication")
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(execution + "T20:00:00+08:00"))
    config, state = pub._config(100000, 5), new_state(100000)
    engine.advance(state, base, DAYS[-1], config, 0, context=context, published_close_intent=bound)
    assert state["pending_targets"] == dict(saved.weights) and not state["orders"]
    restored = json.loads(json.dumps(state))
    row = engine.advance(restored, base, execution, config, 1, context=context)
    assert row["new_orders"] == 5 and len(restored["fills"]) == 5 and row["fees"] == pytest.approx(25.05)
    book = Ledger(restored, config, context)
    assert all(book.quantity(symbol) == 100 and book.available(symbol, execution) == 0
               and book.available(symbol, following) == 100 for symbol, _ in bound.weights)
    assert "pending_published_close_intent" not in restored


@pytest.mark.parametrize("stamp,status,reason", [
    ("2026-10-16T00:10:00+08:00", "unbound_recording_date_context_only", "actual_recording_date"),
    ("2026-10-16T09:30:00+08:00", "late_context_only", "late_for_original_next_open")])
def test_midnight_or_late_recording_never_backdates_or_rolls_execution(inputs, monkeypatch, stamp, status, reason):
    monkeypatch.setattr(pub, "_now", lambda: datetime.fromisoformat(stamp))
    result = run(inputs)
    assert result["status"] == status and result["proposed_execution_day"] == "2026-10-16"
    for case in result["cases"]:
        intent = PortfolioIntent.from_dict(case["portfolio_intent"])
        assert intent.kind.value == "NO_CHANGE" and intent.execution_not_before is None
        assert intent.decided_at == stamp and reason in intent.reason
        assert len(case["raw_rule_weights_before_qualification"]) == 5


@pytest.mark.parametrize("change", ["prefix_gap", "prefix_overlap", "model", "source", "future_raw", "current_multiple", "future_computed"])
def test_bad_prefix_model_future_or_clock_fails_before_output(inputs, change):
    current = inputs["loaded"][inputs["records"]["current"]]
    old = inputs["loaded"][inputs["records"]["prefix"]]
    if change == "prefix_gap":
        path = inputs["records"]["prefix"]
        record = json.loads(path.read_text())
        record["score_dates"] = DAYS[:2]
        path.write_text(json.dumps(record))
    elif change == "prefix_overlap":
        with pytest.raises(ValueError, match="overlap"):
            run(inputs, [inputs["records"]["prefix"]] * 2)
        assert not inputs["out"].exists()
        return
    elif change == "model":
        path = inputs["records"]["prefix"]
        record = json.loads(path.read_text())
        record["seed_models"]["17"] = "different"
        path.write_text(json.dumps(record))
    elif change == "source":
        old[5]["source_proofs"][1]["raw_daily_sha256"] = "changed"
    elif change == "future_raw":
        current[1].loc[len(current[1])] = raw(["2026-10-16"]).iloc[0]
    elif change == "current_multiple":
        path = inputs["records"]["current"]
        record = json.loads(path.read_text())
        record["score_dates"] = DAYS[2:]
        path.write_text(json.dumps(record))
    else:
        current[5]["score_computed_at"] = "2026-10-16T18:00:00+08:00"
    with pytest.raises(ValueError):
        run(inputs)
    assert not inputs["out"].exists()


def test_multiple_prior_batches_cover_every_prior_session_without_capacity_reset(inputs):
    # Move D to Friday 16th; append the already stored Thursday-15 batch.
    original_current = inputs["records"]["current"]
    current = inputs["loaded"][original_current]
    next_path = original_current.with_name("next.json")
    record = json.loads(original_current.read_text())
    record["score_dates"] = ["2026-10-16"]
    next_path.write_text(json.dumps(record))
    days = DAYS + ["2026-10-16"]
    source = deepcopy(current[5])
    source["source_proofs"].append({"day": "2026-10-16", "source_updated_at": "2026-10-16T17:30:00+08:00"})
    source.update(score_computed_at="2026-10-16T18:10:00+08:00", source_available_at="2026-10-16T18:00:00+08:00")
    inputs["loaded"][next_path] = (selected(["2026-10-16"]), raw(days), days, SESSIONS, PREVIOUS, source, current[-1])
    inputs["records"]["current"] = next_path
    # Real wallclock seam only; no caller-supplied backdating API exists.
    from unittest.mock import patch
    with patch.object(pub, "_now", return_value=datetime.fromisoformat("2026-10-16T18:45:00+08:00")):
        result = run(inputs, [inputs["records"]["prefix"], original_current])
    assert result["capacity_prefix"]["score_dates"] == DAYS and result["proposed_execution_day"] == "2026-10-19"
    assert result["capacity_prefix"]["capacity_reset"] is False


def test_frozen_model_proof_checks_actual_model_preprocessing_and_catalog_bytes(tmp_path, monkeypatch):
    wrapper = tmp_path / "run.json"
    wrapper.write_text("{}")
    (tmp_path / "alpha158_catalog.csv").write_text("name,formula\nx,$close\n")
    models, saved = {}, {}
    for seed in pub.SEEDS:
        folder = tmp_path / f"B0_seed{seed}"
        folder.mkdir()
        (folder / "model.txt").write_text(str(seed))
        (folder / "preprocessing.csv").write_text("f,median\nx,0\n")
        models[seed] = folder
        saved[str(seed)] = {"folder": str(folder), "model_sha256": digest(folder / "model.txt"),
            "preprocessing_sha256": digest(folder / "preprocessing.csv")}
    record = {"method_id": "frozen", "model_run": str(wrapper), "model_run_sha256": digest(wrapper),
        "seed_models": saved, "training_segments": {"train": ["2024", "2024"]},
        "catalog_sha256": digest(tmp_path / "alpha158_catalog.csv"),
        "frozen_training_catalog_sha256": digest(tmp_path / "alpha158_catalog.csv"),
        "score_aggregation": "within_each_date_seed_percentile_mean_no_cross_date_ranking"}
    monkeypatch.setattr(pub, "_resolve_model", lambda value: (wrapper, {"models_refit": False,
        "config": {"segments": record["training_segments"]}}, models))
    assert pub._model_contract(record)["seed_models"] == saved
    (models[17] / "preprocessing.csv").write_text("modified")
    with pytest.raises(ValueError, match="preprocessing bytes"):
        pub._model_contract(record)


def test_existing_output_is_not_overwritten(inputs):
    inputs["out"].mkdir()
    with pytest.raises(FileExistsError):
        run(inputs)
