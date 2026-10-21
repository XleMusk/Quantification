from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.ledger import new_state
from trading.research import leader_daily_replay as runner
from trading.research.baostock_all_stock_probe import EXPECTED_FIELDS
from trading.research.daily_source_probe import WHEEL_SHA256
from trading.research.dated_nontrading_coverage import candidate_coverage, load_dated_status
from trading.research.shortline_decision_v2 import BATCH_SCHEMA


DAY, NEXT = "2026-01-05", "2026-01-06"
A, B, C, D = "600001.SSE", "600002.SSE", "600003.SSE", "600004.SSE"


def source(tmp_path, *, day=DAY, statuses=None, legacy=False):
    folder = tmp_path / day
    folder.mkdir()
    raw = folder / ("query_all_stock_" + day + ".csv")
    pd.DataFrame(statuses or [["sh.600001", "1", "A"], ["sh.600002", "0", "ST B"]],
                 columns=EXPECTED_FIELDS).to_csv(raw, index=False, encoding="utf-8-sig")
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    document = docs / "StockBasicInfoAPI.md"
    document.write_text("tradeStatus | 交易状态，其中1：正常交易，0：停牌", encoding="utf-8")
    digest = sha256(document.read_bytes()).hexdigest()
    (docs / "receipt.json").write_text(json.dumps({"sha256": digest, "source": "https://www.baostock.com/mainContent?file=StockBasicInfoAPI.md",
                                                 "retrieval_endpoint": "https://www.baostock.com/helpdocs/api/markdown/StockBasicInfoAPI.md"}), encoding="utf-8")
    receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "day": day, "response_day": day,
               "method": "query_all_stock", "kwargs": {"day": day}, "status": "completed_provider_status_list",
               "quality_issues": [], "provider_error_code": "0", "fields_returned": EXPECTED_FIELDS,
               "sdk": {"sha256": WHEEL_SHA256}, "execution_admitted": False, "price_bars_generated": False,
               "raw_csv": str(raw), "rows_preserved": len(pd.read_csv(raw)), "completed_at": "2026-10-15T02:46:40+08:00",
               "provider_semantics_verified": True,
               "provider_documentation": {"sha256": digest, "provider_semantics_verified": True, "local_document": str(document)}}
    raw_hash = sha256(raw.read_bytes()).hexdigest()
    if not legacy:
        receipt["raw_sha256"] = raw_hash
    (folder / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    pin = tmp_path / "pins.json"
    pin.write_text(json.dumps([{"day": day, "original_receipt": str(folder / "receipt.json"), "raw_csv": str(raw),
                                "verified_current_raw_sha256": raw_hash,
                                "raw_hash_origin": "post_acquisition_pin_now_original_receipt_unmodified",
                                "original_acquisition_hash_verified": False, "reacquired": False, "execution_admitted": False}]), encoding="utf-8")
    return folder, receipt, pin


def facts(*, missing=None, invalid=None, observed=1, day=DAY):
    missing = [B] if missing is None else missing
    return {"day": day, "expected_cohort_rows": observed + len(missing), "observed_cohort_rows": observed,
            "missing_symbols": missing, "invalid_symbols": invalid or [], "status": "incomplete"}


def test_only_exact_dated_unheld_nontrading_rows_explain_candidate_coverage(tmp_path):
    folder, _, _ = source(tmp_path)
    evidence = load_dated_status(folder, DAY)
    raw = facts()
    before = deepcopy(raw)
    result = candidate_coverage(raw, evidence)
    assert result["status"] == "complete" and result["candidate_nontrading_observed_symbols"] == [B]
    assert raw == before and result["raw_coverage_status"] == "incomplete"
    assert result["price_bars_created"] is False and result["pit_verified"] is False
    assert result["temporal_basis"] == "retrospective_retrieval_not_point_in_time"
    for required in ({"held_symbols": [B]}, {"pending_symbols": [B]}):
        blocked = candidate_coverage(raw, evidence, **required)
        assert blocked["status"] == "incomplete" and blocked["candidate_nontrading_observed_symbols"] == []
        assert blocked["required_held_or_pending_missing_symbols"] == [B]
    with pytest.raises(ValueError, match="exact raw day"):
        candidate_coverage(facts(day=NEXT), evidence)


def test_normal_trading_no_provider_row_invalid_and_whole_missing_day_stay_unknown(tmp_path):
    folder, _, _ = source(tmp_path, statuses=[["sh.600001", "1", "A"], ["sh.600002", "0", "B"], ["sh.600003", "1", "C"]])
    evidence = load_dated_status(folder, DAY)
    result = candidate_coverage(facts(missing=[B, C, D]), evidence)
    assert result["status"] == "incomplete"
    assert result["candidate_nontrading_observed_symbols"] == [B]
    assert result["provider_status1_missing_symbols"] == [C]
    assert result["absent_provider_row_unknown_symbols"] == [D]
    assert candidate_coverage(facts(invalid=[A]), evidence)["status"] == "incomplete"
    whole = candidate_coverage(facts(observed=0), evidence)
    assert whole["whole_raw_day_absent"] and whole["status"] == "incomplete"
    assert whole["candidate_nontrading_observed_symbols"] == []


@pytest.mark.parametrize("change", ["wrong_day", "schema", "hash", "duplicate", "unverified_docs", "late_publication", "late_available_at"])
def test_invalid_or_late_evidence_is_rejected(tmp_path, change):
    folder, receipt, _ = source(tmp_path)
    raw = next(folder.glob("*.csv"))
    if change == "wrong_day":
        receipt["response_day"] = NEXT
    elif change == "schema":
        receipt["schema"] = "unknown"
    elif change == "hash":
        raw.write_bytes(raw.read_bytes() + b"\n")
    elif change == "duplicate":
        data = pd.read_csv(raw, dtype=str)
        pd.concat([data, data.iloc[:1]]).to_csv(raw, index=False, encoding="utf-8-sig")
        receipt.update(raw_sha256=sha256(raw.read_bytes()).hexdigest(), rows_preserved=3)
    elif change == "unverified_docs":
        receipt["provider_semantics_verified"] = False
    elif change == "late_publication":
        receipt["public_available_at"] = DAY + "T16:00:00+08:00"
    else:
        receipt["available_at"] = DAY + "T16:00:00+08:00"
    (folder / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError):
        evidence = load_dated_status(folder, DAY)
        candidate_coverage(facts(), evidence)


def test_legacy_raw_requires_separate_matching_hash_pin_and_keeps_original_receipt(tmp_path):
    folder, _, pin = source(tmp_path, legacy=True)
    receipt_path = folder / "receipt.json"
    before = receipt_path.read_bytes()
    with pytest.raises(ValueError, match="no acquisition raw hash"):
        load_dated_status(folder, DAY)
    evidence = load_dated_status(folder, DAY, legacy_hash_pin=pin)
    assert candidate_coverage(facts(), evidence)["status"] == "complete"
    assert evidence.verification["original_acquisition_hash_verified"] is False
    assert receipt_path.read_bytes() == before
    pins = json.loads(pin.read_text())
    pins[0]["verified_current_raw_sha256"] = "bad"
    pin.write_text(json.dumps(pins))
    with pytest.raises(ValueError, match="source bytes"):
        load_dated_status(folder, DAY, legacy_hash_pin=pin)


def test_optional_runner_input_does_not_price_or_allow_missing_held_symbol(tmp_path):
    folder, _, _ = source(tmp_path)
    evidence = load_dated_status(folder, DAY)
    raw = pd.DataFrame([{"date": day, "symbol": symbol, "open": 10., "high": 10.1, "low": 9.9,
                         "close": 10., "volume": 5_000_000., "amount": 50_000_000., "pct_chg": 0.,
                         "preclose_for_limits": 10., "is_st": False}
                        for day, symbols in [("2025-12-31", [A, B]), (DAY, [A]), (NEXT, [A, B])]
                        for symbol in symbols])
    raw_before = raw.copy(deep=True)
    coverage = runner.dated_coverage(raw, [DAY, NEXT], [A, B])
    batch = {"schema": BATCH_SCHEMA, "day": DAY, "status": "observed_source_complete", "source": "fixed",
             "candidates": [{"symbol": A, "score": 1.}]}
    default, _ = runner.scope_batches({DAY: batch}, coverage, [A, B], raw)
    revised, _ = runner.scope_batches({DAY: batch}, coverage, [A, B], raw, status_evidence={DAY: evidence})
    assert default[DAY]["status"] == "incomplete" and "candidate_coverage" not in default[DAY]
    assert revised[DAY]["status"] == "complete" and revised[DAY]["cohort_coverage"]["status"] == "incomplete"
    pd.testing.assert_frame_equal(raw, raw_before)
    base = runner.CommonCohortRawFeed(raw, [DAY, NEXT], [A, B])
    state = new_state(100000)
    feed = runner.CommonCohortDecisionFeed(base, revised, state)
    context = RunContext("test", TradingMode.REPLAY)
    config = runner._config(100000, 5)
    assert not isinstance(feed.signal_frame(DAY, config, context), SignalUnavailable)
    state["positions"][B] = [{"qty": 100}]
    blocked = feed.signal_frame(DAY, config, context)
    assert isinstance(blocked, SignalUnavailable) and blocked.reason == "shortline_v2_current_raw_panel_incomplete"
