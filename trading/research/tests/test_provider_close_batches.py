"""Synthetic source/consumer behavior only, never current-market evidence."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import baostock_full_close as source
from trading.research import provider_close_batches as consumer
from trading.research.leader_daily_replay import CommonCohortRawFeed, CommonCohortDecisionFeed
from trading.research.shortline_decision_v3 import FiveSessionExpiryPolicy
from trading.research.shortline_panel_replay import _config


DAY, NEXT = "2026-10-14", "2026-10-15"
A, HALT, ABSENT = "000001.SZSE", "000002.SZSE", "000003.SZSE"
SDK = {"sha256": source.WHEEL_SHA256, "wheel": "synthetic-unused-wheel"}


@pytest.fixture
def assets(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    history_days = pd.bdate_range(end="2026-10-13", periods=70).strftime("%Y-%m-%d").tolist()
    historical = pd.DataFrame([{"date": day, "code": symbol[:6], "open": 8 + index * .02,
        "high": 8.05 + index * .02, "low": 7.95 + index * .02, "close": 8 + index * .02,
        "volume": 10_000_000., "amount": (8 + index * .02) * 10_000_000.,
        "pct_chg": .2, "pre_close": 8., "is_st": False}
        for symbol in (A, HALT) for index, day in enumerate(history_days)])
    metadata = {"snapshot": "synthetic_old_inventory", "prefixes": ["0"], "partitions": [], "source_verified": False}
    monkeypatch.setattr(consumer, "read_physical_snapshot", lambda *args, **kwargs: (historical.copy(deep=True), deepcopy(metadata)))
    monkeypatch.setattr(source.daily.time, "sleep", lambda _: None)

    def package(day=DAY, budget=20):
        root = tmp_path / ("package_" + day)
        roster = root / "roster"
        roster.mkdir(parents=True)
        frame = pd.DataFrame([["sz.000001", "1", "issuer A"], ["sz.000002", "0", "issuer B"],
                              ["sh.000001", "1", "index"]], columns=["code", "tradeStatus", "code_name"])
        raw_path = roster / f"query_all_stock_{day}.csv"
        frame.to_csv(raw_path, index=False)
        receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "day": day,
            "response_day": day, "method": "query_all_stock", "kwargs": {"day": day},
            "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
            "fields_returned": list(frame.columns), "sdk": SDK, "execution_admitted": False,
            "price_bars_generated": False, "raw_csv": str(raw_path), "raw_sha256": source.daily.digest(raw_path),
            "rows_preserved": len(frame)}
        (roster / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        @contextmanager
        def session():
            def query(**arguments):
                halted = arguments["code"] == "sz.000002"
                price = 10. if day == DAY or halted else 10.1
                row = [day, arguments["code"], str(price), str(price), str(price), str(price),
                       str(price if halted or day == DAY else 10.), "" if halted else "10000000",
                       "" if halted else str(price * 10000000), "3", "0" if halted else "1",
                       "" if halted else ("0" if day == DAY else "1"), "0"]
                raw = pd.DataFrame([row], columns=source.daily.FIELDS.split(","))
                return SimpleNamespace(error_code="0", error_msg="success", get_data=lambda: raw)
            yield SimpleNamespace(query_history_k_data_plus=query)
        now = datetime.fromisoformat(day + "T16:00:00+08:00")
        source.collect(day, calendar, SDK, root, now=now, max_new_queries=budget, session_factory=session)
        return root
    return calendar, historical, metadata, package


def test_new_complete_source_contract_keeps_old_metadata_and_nullable_raw_unchanged(assets):
    calendar, history, metadata, package = assets
    root = package()
    original_history, original_metadata = history.copy(deep=True), deepcopy(metadata)
    bundle = consumer.build_provider_close_bundle(root, "unused_snapshot", calendar)
    assert bundle["source_days_complete"] == [DAY]
    assert bundle["input_source_contract"] == consumer.CONTRACT
    assert set(bundle["batches"]) == set(consumer.FAMILIES)
    assert {bundle["batches"][family][DAY]["status"] for family in consumer.FAMILIES} == {"complete"}
    assert {bundle["batches"][family][DAY]["original_producer_batch_status"] for family in consumer.FAMILIES} == {"incomplete"}
    assert bundle["source_metadata"]["history_metadata"] == metadata == original_metadata
    assert bundle["original_producer_coverage"][0]["source_query_partitions_complete"] is False
    view = bundle["provider_inventory_by_day"][DAY]
    assert view["ordinary_inventory_rows"] == 2 and view["nullable_status0_symbols"] == [HALT]
    assert view["candidate_inventory_complete"] is True and view["raw_valuation_coverage_complete"] is False
    halted = bundle["raw_bars"].loc[lambda f: f.date.eq(DAY) & f.symbol.eq(HALT)].iloc[0]
    assert pd.isna(halted.volume) and pd.isna(halted.amount) and pd.isna(halted.pct_chg)
    assert halted.tradestatus == 0 and halted.preclose == 10 and bool(halted.provider_raw_observation)
    old = bundle["raw_bars"].loc[lambda f: f.date.lt(DAY)]
    assert old.provider_raw_observation.isna().all() and old.tradestatus.isna().all()
    assert NEXT in bundle["calendar"] and NEXT not in set(bundle["raw_bars"].date)
    assert bundle["batches"]["trend_leader_20_60"][DAY]["candidates"][0]["symbol"] == A
    assert all(bundle[key] is False for key in ("strategy_validated", "selection_enabled", "signal_admitted", "paper_admitted", "execution_admitted", "model_refit", "future_labels_used_for_score"))
    pd.testing.assert_frame_equal(history, original_history)


def test_fixed_cohort_absence_unknown_halt_candidate_only_and_held_price_block(assets):
    calendar, _, _, package = assets
    bundle = consumer.build_provider_close_bundle(package(), "unused", calendar)
    scoped, coverage = consumer.scope_provider_batches(bundle, [A, HALT])
    assert coverage[DAY]["status"] == "complete"
    assert coverage[DAY]["candidate_status0_explained_symbols"] == [HALT]
    assert coverage[DAY]["raw_valuation_unusable_symbols"] == [HALT]
    assert scoped["trend_leader_20_60"][DAY]["candidates"][0]["symbol"] == A
    absent, absent_coverage = consumer.scope_provider_batches(bundle, [A, HALT, ABSENT])
    assert absent_coverage[DAY]["historical_cohort_missing_current_roster"] == [ABSENT]
    assert absent_coverage[DAY]["expected_cohort_rows"] == 3
    assert absent["trend_leader_20_60"][DAY]["status"] == "incomplete"
    for kwargs in ({"held_symbols": [HALT]}, {"pending_symbols": [HALT]}):
        blocked, facts = consumer.scope_provider_batches(bundle, [A, HALT], **kwargs)
        assert facts[DAY]["required_held_or_pending_unpriceable_symbols"] == [HALT]
        assert blocked["trend_leader_20_60"][DAY]["status"] == "incomplete"
        assert facts[DAY]["held_pending_inventory_modified"] is False
    outside, outside_facts = consumer.scope_provider_batches(bundle, [A, HALT], held_symbols=[ABSENT])
    assert outside_facts[DAY]["required_held_or_pending_unpriceable_symbols"] == [ABSENT]
    assert outside["trend_leader_20_60"][DAY]["status"] == "incomplete"
    tail, _ = consumer.scope_provider_batches(bundle, [A, HALT], decision_end="2026-10-13")
    assert tail["trend_leader_20_60"][DAY]["candidates"] == []
    assert tail["trend_leader_20_60"][DAY]["status"] == "incomplete"


def test_complete_candidate_contract_enters_signalframe_then_sole_engine_ledger_with_real_next_bar(assets):
    calendar, _, _, package = assets
    bundle = consumer.build_provider_close_bundle([package(DAY), package(NEXT)], "unused", calendar)
    scoped, _ = consumer.scope_provider_batches(bundle, [A, HALT])
    base = CommonCohortRawFeed(bundle["raw_bars"], bundle["calendar"], [A, HALT])
    state, config = new_state(100000), _config(100000, 5)
    feed = CommonCohortDecisionFeed(base, scoped["trend_leader_20_60"], state)
    context = RunContext("synthetic_provider_contract", TradingMode.REPLAY)
    frame = feed.signal_frame(DAY, config, context)
    assert isinstance(frame, SignalFrame)
    advance(state, feed, DAY, config, base.calendar.index(DAY), context=context,
            replay_risk_exit_policy=FiveSessionExpiryPolicy())
    advance(state, feed, NEXT, config, base.calendar.index(NEXT), context=context,
            replay_risk_exit_policy=FiveSessionExpiryPolicy())
    assert any(fill["symbol"] == A and fill["side"] == "buy" for fill in state["fills"].values())
    assert HALT not in state["positions"]
    assert all(order["symbol"] == A for order in state["orders"].values())
    # Unknown successor raw remains unavailable even though its calendar date is
    # known. A nullable halt cannot become a fake owned valuation.
    assert isinstance(feed.signal_frame(NEXT, config, context), SignalUnavailable)
    state["positions"][HALT] = [{"qty": 100}]
    assert isinstance(feed.signal_frame(DAY, config, context), SignalUnavailable)
    with pytest.raises(ValueError, match="Unpriceable risk-exit holding"):
        FiveSessionExpiryPolicy().validate_session(state, feed, DAY, context)


def test_partial_budget_checkpoint_refused_before_historical_feature_work(assets, monkeypatch):
    calendar, _, _, package = assets
    root = package(budget=1)
    monkeypatch.setattr(consumer, "read_physical_snapshot", lambda *args, **kwargs: pytest.fail("partial source must fail first"))
    with pytest.raises(ValueError, match="not a complete"):
        consumer.build_provider_close_bundle(root, "unused", calendar)


def test_source_hash_damage_active_blank_and_mixed_source_dates_are_not_completed(assets):
    calendar, _, _, package = assets
    root = package()
    raw, metadata = source.load_complete(root)
    damaged = raw.copy(deep=True)
    damaged.loc[damaged.tradestatus.eq("1"), "amount"] = ""
    with pytest.raises(ValueError, match="Every active"):
        consumer._inventory(damaged, metadata, "pinned_static_exchange_calendar")
    damaged = raw.copy(deep=True)
    damaged.loc[0, "date"] = NEXT
    with pytest.raises(ValueError, match="identities/statuses"):
        consumer._inventory(damaged, metadata, "pinned_static_exchange_calendar")
    path = root / "raw_daily.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="raw hash"):
        consumer.build_provider_close_bundle(root, "unused", calendar)


def test_source_acquisition_timestamps_propagate_without_signal_publication_inference(assets):
    calendar, _, _, package = assets
    root = package()
    receipt_path, roster_path = root / "receipt.json", root / "roster/receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recorded = {"created_at": "2026-10-15T08:00:00+08:00", "run_started_at": "2026-10-15T08:02:00+08:00",
                "updated_at": "2026-10-15T08:40:00+08:00", "completed_at": "2026-10-15T08:39:59+08:00"}
    receipt.update(recorded)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster["completed_at"] = "2026-10-15T08:01:00+08:00"
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    result = consumer.build_provider_close_bundle(root, "unused", calendar)
    pin = result["source_packages"][0]
    assert pin["acquisition_timestamps"] == recorded
    assert pin["roster_receipt_completed_at"] == roster["completed_at"]
    assert pin["terminal_completion_observed_at"] == recorded["completed_at"]
    assert pin["terminal_completion_time_basis"] == "explicit_source_receipt_completed_at"
    assert pin["captured_timestamp_not_signal_publication"] is True
    assert pin["actual_signal_publication_verified"] is result["actual_signal_publication_verified"] is False
    assert pin["pit_verified"] is False and "15:05" not in json.dumps(pin)
    del receipt["completed_at"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    fallback = consumer.build_provider_close_bundle(root, "unused", calendar)["source_packages"][0]
    assert "completed_at" not in fallback["acquisition_timestamps"]
    assert fallback["terminal_completion_observed_at"] == recorded["updated_at"]
    assert fallback["terminal_completion_time_basis"] == "terminal_inventory_receipt_updated_at_after_full_assembly"
