from hashlib import sha256

import pandas as pd

from trading.research.leader_daily_replay import CommonCohortRawFeed, EXIT_POLICIES, _case
from trading.research.leader_expiry_hook_diagnostic import _return_label, read_frozen_member_raw
from trading.research.shortline_decision_v2 import BATCH_SCHEMA
from trading.research.shortline_decision_v3 import FiveSessionExpiryPolicy


def test_original_case_path_default_retains_missing_batch_position_but_explicit_hook_exits():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
            "2026-01-12", "2026-01-13", "2026-01-14"]
    symbols = ["000001.SZSE", "000016.SZSE"]
    rows = [{"date": day, "symbol": symbol, "open": 10., "high": 10.2, "low": 9.8,
             "close": 10., "volume": 1e9, "amount": 1e10, "pct_chg": 0.,
             "preclose_for_limits": 10., "is_st": False}
            for symbol in symbols for day in ["2024-01-02", *days]
            if day == "2024-01-02" or symbol == symbols[0]]
    base = CommonCohortRawFeed(pd.DataFrame(rows), days, symbols)
    assert base.instruments[symbols[0]]["ipoDate"] == "2024-01-02"
    batches = {day: {"schema": BATCH_SCHEMA, "day": day, "status": "incomplete", "candidates": [],
                     "source": "fixed-test", "incomplete_reason": "unheld_member_missing"} for day in days}
    batches[days[0]].update(status="complete", candidates=[{"symbol": symbols[0], "score": 1.}])
    args = (base, batches, "stage1_amount_baseline", "common_pre_window_symbol20", EXIT_POLICIES[0], days[0], days[-1])
    old, old_evidence = _case(*args)
    new, new_evidence = _case(*args, replay_risk_exit_policy=FiveSessionExpiryPolicy())
    assert old["error"] is new["error"] is None
    assert old["open_positions"][symbols[0]] > 0
    assert new["open_positions"] == {}
    sales = [fill for fill in new_evidence["state"]["fills"].values() if fill["side"] == "sell"]
    assert sales and {fill["day"] for fill in sales} == {"2026-01-13"}
    assert not any(fill["side"] == "sell" for fill in old_evidence["state"]["fills"].values())
    assert batches[days[1]]["status"] == "incomplete"


def test_raw_member_filter_keeps_pre_window_origin_and_separate_tail_partition(tmp_path):
    def row(code, day):
        return {"code": code, "date": day, "open": 10., "high": 10.1, "low": 9.9,
                "close": 10., "volume": 1000., "amount": 10000., "pct_chg": 0.,
                "is_st": False, "pre_close": 10.}
    chunks = []
    for filename, day, records in [("old.parquet", 20240102, [row("000001", 20240102), row("000002", 20240102)]),
                                   ("separate-tail.parquet", 20261013, [row("000001", 20261013)])]:
        path = tmp_path / filename
        pd.DataFrame(records).to_parquet(path, index=False)
        chunks.append({"prefix": "0", "lower_inclusive": str(day), "rows": len(records),
                       "parquet_file": filename, "parquet_sha256": sha256(path.read_bytes()).hexdigest()})
    raw, proofs = read_frozen_member_raw(tmp_path, {"chunks": chunks}, ["000001.SZSE"], "2026-10-13")
    assert raw.date.tolist() == ["2024-01-02", "2026-10-13"]
    assert raw.symbol.unique().tolist() == ["000001.SZSE"]
    assert len(proofs) == 2 and proofs[0]["original_partition_rows"] == 2


def test_cash_only_trace_is_not_reported_as_strategy_zero_return():
    assert _return_label({"fills": 0, "metrics": {"net_return": 0.0}}) == "未成交，不评收益"
    assert _return_label({"fills": 2, "metrics": None}) == "不可用"
    assert _return_label({"fills": 2, "metrics": {"net_return": 0.0}}) == "0.0000%"
