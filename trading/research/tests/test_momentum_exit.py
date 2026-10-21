from dataclasses import replace
import json

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode, decision_time
from trading.engine import reduce_replay_target
from trading.research.experiment import load_replay_checkpoint, replay
from trading.research.momentum_exit import PositiveMomentumExit
from trading.research.tests import test_replay_resume


def test_positive_only_gate_keeps_identity_zero_slots_and_cash(tmp_path):
    rows = [{"date": "2024-01-02", "symbol": symbol, "momentum": value,
             "source_pub_date": "2023-10-20", "source_stat_date": "2023-09-30"}
            for symbol, value in (("600000.SSE", .2), ("600001.SSE", 0.), ("600002.SSE", -.1))]
    pd.DataFrame(rows).to_csv(tmp_path / "rankings.csv", index=False)
    config = StrategyConfig(holdings=20, retain_rank=30, lookback=252, skip=21)
    policy = PositiveMomentumExit(tmp_path, config, "fixed-source")
    context = RunContext(account_id="test-gate", mode=TradingMode.REPLAY)
    intent = PortfolioIntent.full_target({row["symbol"]: .04 for row in rows}, context=context,
                                        decided_at=decision_time("2024-01-02"), execution_not_before="2024-01-03")
    gated = reduce_replay_target(intent, policy)
    assert dict(gated.weights) == {"600000.SSE": .04, "600001.SSE": 0., "600002.SSE": 0.}
    assert gated.context == intent.context
    assert gated.decided_at == intent.decided_at
    assert gated.execution_not_before == intent.execution_not_before
    assert len(policy.audit) == 3
    with pytest.raises(ValueError, match="unknown"):
        policy(replace(intent, weights=(("600003.SSE", .04),)))


def test_gate_rejects_missing_values_or_future_report(tmp_path):
    row = {"date": "2024-01-02", "symbol": "600000.SSE", "momentum": .1,
           "source_pub_date": "2023-10-20", "source_stat_date": "2023-09-30"}
    config = StrategyConfig(holdings=20, retain_rank=30, lookback=252, skip=21)
    for changed in ({**row, "momentum": float("nan")}, {**row, "source_pub_date": row["date"]}):
        pd.DataFrame([changed]).to_csv(tmp_path / "rankings.csv", index=False)
        with pytest.raises(ValueError, match="finite, publication-lagged"):
            PositiveMomentumExit(tmp_path, config, "fixed-source")


def test_pinned_close_target_policy_continues_exact_ledger(tmp_path):
    fixture = test_replay_resume.ReplayResumeTests()
    feed, config = fixture.feed(), StrategyConfig(initial_cash=140000)
    contract = {"id": "test_reduce_only_v1", "config": config.to_dict(), "threshold": 0}

    def policy(intent):
        weight = .04 if pd.Timestamp(intent.decided_at).tz_convert("Asia/Shanghai").date().isoformat() < "2024-01-09" else 0.
        return replace(intent, weights=tuple((symbol, weight) for symbol, _ in intent.weights))

    expected = replay(feed, config, feed.calendar, tmp_path / "full", target_policy=policy,
                      target_policy_contract=contract)
    with pytest.raises(ValueError, match="source gap"):
        replay(fixture.feed(feed.calendar[3]), config, feed.calendar, tmp_path / "prefix",
               target_policy=policy, target_policy_contract=contract)
    checkpoint = load_replay_checkpoint(tmp_path / "prefix", config, feed.calendar, target_policy_contract=contract)
    actual = replay(feed, config, feed.calendar, tmp_path / "continued", target_policy=policy,
                    target_policy_contract=contract, checkpoint=checkpoint)
    assert actual["reused_sessions"] == 3
    assert actual["net_return"] == expected["net_return"]
    assert json.loads((tmp_path / "continued/account.json").read_text()) == json.loads((tmp_path / "full/account.json").read_text())
    for changed in (None, {**contract, "threshold": .01}):
        with pytest.raises(ValueError, match="configuration changed"):
            load_replay_checkpoint(tmp_path / "prefix", config, feed.calendar, target_policy_contract=changed)
    with pytest.raises(ValueError, match="requires its reduction callable"):
        replay(feed, config, feed.calendar, tmp_path / "missing_policy", target_policy_contract=contract)
    with pytest.raises(ValueError, match="mismatch"):
        replay(feed, config, feed.calendar, tmp_path / "changed", target_policy=policy,
               target_policy_contract={**contract, "threshold": .01}, checkpoint=checkpoint)


def test_legacy_unpinned_overlay_still_cannot_resume(tmp_path):
    feed, config = test_replay_resume.ReplayResumeTests().feed(), StrategyConfig()
    replay(feed, config, feed.calendar, tmp_path / "legacy", target_policy=lambda intent: intent)
    with pytest.raises(ValueError, match="unpinned target policy"):
        load_replay_checkpoint(tmp_path / "legacy", config, feed.calendar)
