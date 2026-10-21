"""Saved holdings keep their FIFO/risk/account facts when public quotes attach."""
from copy import deepcopy
import json

import pytest

from trading.research import leader_position_intraday_view as view


D, E = "2026-10-16", "2026-10-19"
NOW = E + "T10:00:30+08:00"
HELD, CANDIDATE = "000001.SZSE", "600000.SSE"


def fixture():
    anchor = dict(status="matched_saved_FIFO_fill", fill_id="oldest-surviving-buy",
        fill_day="2026-10-15", decision_day="2026-10-14", available_day=D,
        fill_price_cny=10., remaining_earliest_fill_shares=800,
        setup_low_cny=9.2, setup_high_cny=10.4, evidence={"original_entry_only": True})
    queue = dict(execution_on=E, decided_on=D, reason="saved_structural_failure",
        source="original_saved_close_risk", execution_day_elapsed=False)
    held = dict(symbol=HELD, quantity_shares=1600, sellable_on_book_day_shares=800,
        t1_locked_shares=800, sell_reserved_shares=0, mark_price_cny=10.,
        marked_value_cny=16000., mark_is_latest_market_quote=False, mark_stale=False,
        anchor=anchor, assessment={"day": D, "status": "queued_exit",
            "facts": {"original_close": 10.}, "reasons": ["original_close_condition"]},
        queued_risk_exit=queue)
    rows = [dict(case_id=kind + "__unguarded__uncapped", type=kind,
        processed_day=D, next_session_after_book_day=E, cash_cny=5000., equity_cny=21000.,
        positions=[deepcopy(held)], pending_decision={"kind": "NO_CHANGE", "weights": None},
        latest_order_blocks=[{"symbol": HELD, "reason": "saved_limit_queue"}])
        for kind in ("space_height_leader", "relay_continuation_leader", "liquidity_capacity_proxy")]
    positions = dict(status="saved_research_positions", current_candidate_symbols=[CANDIDATE], rows=rows)
    raw, quotes = [], []
    for symbol, last, previous in ((CANDIDATE, "30", "30"), (HELD, "10.5", "10")):
        raw.append(dict(symbol=symbol, source_date=E, source_quote_time=E + "T10:00:00+08:00",
            received_at=E + "T10:00:01+08:00", current_quote=True, source_status="00",
            last_price_rmb=last, previous_close_price_rmb=previous))
        quotes.append(dict(symbol=symbol, source_quote_time=E + "T10:00:00+08:00",
            current_at_read=True, read_status="current", last_price_rmb=last))
    cohort = dict(status="within_current_saved_cohort_observation", source_file="saved/latest_receipt.json",
        session={"session_day": E}, quotes=quotes)
    receipt = dict(session_day=E, finished_at=E + "T10:00:02+08:00", quotes=raw)
    return positions, cohort, receipt


def strip_observations(result):
    original = deepcopy(result)
    original.pop("intraday_tracking", None)
    for account in original["rows"]:
        for held in account["positions"]:
            held.pop("public_quote_tracking", None)
    return original


def test_held_symbol_outside_candidates_keeps_fifo_exit_financials_and_saved_sellable():
    positions, cohort, receipt = fixture()
    before = deepcopy((positions, cohort, receipt))
    result = view.build_position_tracking(positions, cohort, receipt, NOW)
    assert HELD not in positions["current_candidate_symbols"]
    assert strip_observations(result) == positions
    assert before == (positions, cohort, receipt)
    for account in result["rows"]:
        held = account["positions"][0]
        assert held["quantity_shares"] == 1600
        assert held["anchor"]["remaining_earliest_fill_shares"] == 800
        assert held["sellable_on_book_day_shares"] == held["t1_locked_shares"] == 800
        assert held["queued_risk_exit"] == positions["rows"][0]["positions"][0]["queued_risk_exit"]
        tracked = held["public_quote_tracking"]
        assert tracked["snapshot_price_cny"] == 10.5 and tracked["fresh_at_read"] is True
        assert tracked["book_reference_status"] == "matched_book_close"
        assert tracked["price_change_from_book_percent"] == pytest.approx(5.)
        assert tracked["price_change_is_strategy_return"] is False
        assert tracked["fixed_intraday_stop_cny"] is None and tracked["risk_recomputed"] is False
        assert tracked["broker_sellable_shares"] is None
        assert tracked["current_rule_status"] == "not_evaluated_from_public_snapshot"
    summary = result["intraday_tracking"]
    assert summary["unique_position_symbols"] == summary["observed_unique"] == summary["current_unique"] == 1
    assert summary["queued_exit_position_occurrences"] == 3
    assert summary["account_cash_equity_unchanged"] is summary["actual_entry_anchors_unchanged"] is True


def test_stale_is_historical_only_and_wrong_raw_day_adjacent_day_or_book_reference_never_fills_gaps():
    positions, cohort, receipt = fixture()
    stale = deepcopy(cohort)
    for quote in stale["quotes"]:
        quote.update(current_at_read=False, read_status="stale")
    result = view.build_position_tracking(positions, stale, receipt, E + "T10:03:00+08:00")
    tracked = result["rows"][0]["positions"][0]["public_quote_tracking"]
    assert tracked["snapshot_price_cny"] == 10.5 and tracked["fresh_at_read"] is False
    assert tracked["status"] == "saved_snapshot_not_current"
    for fault in ("raw_date", "nonadjacent_day", "reference_change", "stale_book_mark"):
        p, c, r = deepcopy((positions, cohort, receipt))
        if fault == "raw_date":
            next(q for q in r["quotes"] if q["symbol"] == HELD)["source_date"] = D
        elif fault == "nonadjacent_day":
            for account in p["rows"]:
                account["next_session_after_book_day"] = "2026-10-20"
        elif fault == "reference_change":
            next(q for q in r["quotes"] if q["symbol"] == HELD)["previous_close_price_rmb"] = "9.99"
        else:
            for account in p["rows"]:
                account["positions"][0]["mark_stale"] = True
        result = view.build_position_tracking(p, c, r, NOW)
        assert strip_observations(result) == p
        for account in result["rows"]:
            tracked = account["positions"][0]["public_quote_tracking"]
            assert tracked["price_change_from_book_percent"] is None, fault
            if fault in {"raw_date", "nonadjacent_day"}:
                assert tracked["snapshot_price_cny"] is None and tracked["fresh_at_read"] is False, fault
            elif fault == "reference_change":
                assert tracked["snapshot_price_cny"] == 10.5
                assert tracked["book_reference_status"] == "changed_or_incompatible"
            else:
                assert tracked["book_reference_status"] == "unknown"


def test_failed_latest_or_reader_error_preserves_holdings_and_exits_without_fallback_or_execution(tmp_path, monkeypatch):
    import requests
    from trading import engine
    positions, cohort, receipt = fixture()
    original = deepcopy(positions)
    path = tmp_path / "latest_failed_receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    attempts = []
    def failed_reader(*args, **kwargs):
        attempts.append("latest_failed")
        return {**cohort, "source_file": str(path), "status": "unavailable_latest_observation"}
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved held-position reader must not execute or collect")
    monkeypatch.setattr(view, "leader_cohort_intraday_view", failed_reader)
    monkeypatch.setattr(engine, "advance", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    before = path.read_bytes()
    failed = view.attach_saved_position_quotes(positions, tmp_path, now=NOW)
    assert strip_observations(failed) == positions == original
    assert failed["intraday_tracking"]["observed_unique"] == failed["intraday_tracking"]["current_unique"] == 0
    for account in failed["rows"]:
        tracked = account["positions"][0]["public_quote_tracking"]
        assert tracked["status"] == "unavailable_latest_observation"
        assert tracked["snapshot_price_cny"] is None and tracked["fresh_at_read"] is False
    def broken_reader(*args, **kwargs):
        attempts.append("reader_exception")
        raise ValueError("synthetic newest evidence is corrupt")
    monkeypatch.setattr(view, "leader_cohort_intraday_view", broken_reader)
    unavailable = view.attach_saved_position_quotes(positions, tmp_path, now=NOW)
    assert strip_observations(unavailable) == positions == original
    assert unavailable["intraday_tracking"]["status"] == "unavailable"
    assert unavailable["intraday_tracking"]["saved_positions_and_exits_preserved"] is True
    assert attempts == ["latest_failed", "reader_exception"] and path.read_bytes() == before
