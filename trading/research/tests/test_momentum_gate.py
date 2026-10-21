from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance, choose_targets
from trading.ledger import new_state
from trading.research import momentum_gate as gate
from trading.research.monthly_timing import index_inputs
from trading.research.tests.test_monthly_timing import base_feed, table


CONTEXT = RunContext("momentum-gate-test", TradingMode.REPLAY)
CONFIG = gate.portfolio(140000)


def wrapper():
    base = base_feed()
    base.day_index = {day: i for i, day in enumerate(base.calendar)}
    ranks = {"2024-01-31": ({"intent": "FULL_TARGET", "candidate_count": 1234},
                            [{"symbol": "600000.SSE", "score": 1.}])}
    return gate.MomentumGateFeed(base, ranks, table(), CONFIG, "2024-03-04")


def test_cached_top30_preserves_the_full_pool_selector():
    full = [{"symbol": f"{600000 + i}.SSE", "score": float(100 - i)} for i in range(100)]
    for held in ([], [row["symbol"] for row in full[10:35]], [full[-1]["symbol"]]):
        assert choose_targets(full, held, CONFIG) == choose_targets(full[:30], held, CONFIG)


def test_fixed_gate_and_cash_persistence_do_not_invent_missing_rank_targets():
    feed = wrapper()
    assert len(feed.signal_frame("2024-01-31", CONFIG, CONTEXT).signals) == 1
    assert feed.audit[-1]["full_source_candidate_count"] == 1234
    assert feed.signal_frame("2024-02-01", CONFIG, CONTEXT).reason == "not_month_end"
    assert feed.signal_frame("2024-02-29", CONFIG, CONTEXT).signals == ()
    assert feed.signal_frame("2024-03-01", CONFIG, CONTEXT).signals == ()
    assert feed.audit[-1]["decision"] == "carry_cash_target"
    feed.gates["2024-02-29"]["gate"] = "equal_no_change"
    assert feed.signal_frame("2024-02-29", CONFIG, CONTEXT).reason == "equal_no_change"
    feed.gates["2024-02-29"]["gate"] = "risk_on"
    assert feed.signal_frame("2024-02-29", CONFIG, CONTEXT).reason == "cached_rank_batch_missing"
    with pytest.raises(ValueError, match="replay-only"):
        feed.signal_frame("2024-01-31", CONFIG, RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="unchanged costs"):
        feed.signal_frame("2024-01-31", replace(CONFIG, retain_rank=31), CONTEXT)


def test_new_wrapper_uses_shared_engine_and_retries_limit_down_exit():
    feed = wrapper()
    feed.base.rows["2024-03-01"]["600000.SSE"].update(open=9., close=9.)
    feed.base.rows["2024-03-04"]["600000.SSE"].update(open=9.1, close=9.1, preclose=9.)
    state = new_state(140000)
    for count, day in enumerate(d for d in feed.calendar if "2024-01-31" <= d <= "2024-03-04"):
        advance(state, feed, day, CONFIG, count)
        if day == "2024-01-31":
            assert not state["fills"]
        if day == "2024-03-01":
            assert any(state["positions"].values()) and state["pending_targets"] == {}
    assert any(e["kind"] == "order_blocked" and e["reason"] == "limit_down_queue_unknown" for e in state["events"])
    assert state["orders"]["2024-03-04:600000.SSE:sell"]["status"] == "filled"
    assert not any(state["positions"].values())


def test_new_wrapper_does_not_suppress_daily_st_exit():
    feed = wrapper()
    state = new_state(140000)
    for count, day in enumerate(("2024-01-31", "2024-02-01", "2024-02-02")):
        if day == "2024-02-02":
            feed.base.rows[day]["600000.SSE"]["isST"] = 1
        advance(state, feed, day, CONFIG, count)
    assert len(state["orders"]) == 2 and not any(state["positions"].values())


def test_saved_rank_count_and_protocol_must_match(tmp_path):
    folder, data = tmp_path / "output", tmp_path / "data"
    artifacts = data / "research/market_replay/source"
    folder.mkdir()
    artifacts.mkdir(parents=True)
    days = ["2024-01-31", "2024-02-01"]
    inputs = {key: key for key in ("price_view", "raw_view", "listing_master", "source_codes", "canonical_codes")}
    inputs["unsupported_instruments"] = {}
    record = {"status": "completed_low_risk_diagnostic", "research_family": "low_risk_stock_selection",
        "protocol": {"id": "low_risk_price_volume_ablation_v1", "portfolio": CONFIG.to_dict()},
        "inputs": inputs, "start": days[0], "end": days[-1], "sessions": 2,
        "results": [{"signal": gate.CONTROL, "status": "completed", "completed_sessions": 2}],
        "artifacts": str(artifacts)}
    (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
    dp = artifacts / (gate.CONTROL + "_decisions.csv")
    rp = artifacts / (gate.CONTROL + "_ranks_1_30.csv")
    pd.DataFrame([{"date": days[0], "variant": gate.CONTROL, "decision": "monthly_ranking",
                   "eligible_count": 2, "candidate_count": 2, "intent": "FULL_TARGET"}]).to_csv(dp, index=False)
    rows = [{"date": days[0], "rank": i + 1, "symbol": f"{600000+i}.SSE",
        "score": 1. - i/10, "momentum": 1. - i/10, "volatility": .2, "median_amount": 3e7} for i in range(2)]
    pd.DataFrame(rows).to_csv(rp, index=False)
    base = SimpleNamespace(manifest=inputs, calendar=days, symbols=[r["symbol"] for r in rows], unsupported_instruments={})
    with patch.object(gate, "DATA_ROOT", data):
        cache, receipts = gate.cached_ranks(folder, base, CONFIG, days[0], days[-1])
        assert len(cache[days[0]][1]) == 2 and len(receipts) == 1
        pd.DataFrame(rows[:1]).to_csv(rp, index=False)
        with pytest.raises(ValueError, match="unchanged selector"):
            gate.cached_ranks(folder, base, CONFIG, days[0], days[-1])
        record["protocol"]["portfolio"]["holdings"] = 5
        (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(ValueError, match="unchanged full-source"):
            gate.cached_ranks(folder, base, CONFIG, days[0], days[-1])


def test_terminal_index_calendar_requires_explicit_opt_in_and_no_last_signal(tmp_path):
    days = ["2024-01-31", "2024-02-01", "2024-02-29"]
    with patch("trading.research.monthly_timing.load_indices", return_value=({"receipts": {}}, days)):
        with pytest.raises(ValueError, match="next session"):
            index_inputs(tmp_path, days, days[-1])
        with pytest.raises(ValueError, match="Missing CSI300"):
            index_inputs(tmp_path, days, days[-1], terminal_calendar=True)
    assert gate.month_ends(days, days[-1]) == ["2024-01-31"]
