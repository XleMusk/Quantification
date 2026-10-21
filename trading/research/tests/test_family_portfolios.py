from types import SimpleNamespace
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trading.contracts import PortfolioIntent, RunContext, SignalFrame, TradingMode
from trading.research.family_portfolios import (FamilyFeed, RECIPES, bind_stock_inputs, common_pool,
                                                ranked_recipe, save_diagnostics, training_directions,
                                                validate_family_resume)
from trading.research.low_risk import VARIANTS, portfolio, rank
from trading.research.stock_defensive import DEFENSIVE, StockDefensiveFeed, mixed_config


def sample_pool():
    return [{"symbol": f"00000{i+1}.SZSE", "volatility": .1 * (i+1), "momentum": .1 * i,
             "median_amount": 30000000., "RSQR30": .1 * (i+1), "WVMA20": float(i+1)} for i in range(4)]


def test_action_override_requires_a_pair_and_preserves_all_other_library_inputs(tmp_path):
    inputs = {"price_view": "prices", "native_view": "native", "listing_master": "listing",
              "event_view": "old_events", "reference_view": "old_references"}
    with pytest.raises(ValueError, match="together"):
        bind_stock_inputs(inputs, tmp_path / "events")
    with pytest.raises(ValueError, match="together"):
        bind_stock_inputs(inputs, reference_view=tmp_path / "references")
    bound = bind_stock_inputs(inputs, tmp_path / "events", tmp_path / "references")
    assert bound == {**inputs, "event_view": str((tmp_path / "events").resolve()),
                     "reference_view": str((tmp_path / "references").resolve())}
    assert bind_stock_inputs(inputs) == inputs and inputs["event_view"] == "old_events"


def test_family_continuation_cannot_change_recipe_fund_or_date_contract():
    previous = {"status": "failed_family_portfolio_diagnostics", "research_family": "fixed_family_incremental",
                "end": "2026-09-30", "results": [{"signal": "stock_low_vol_control"}],
                "protocol": {"signals": ["stock_low_vol_control"], "stock_manifest": {},
                             "fund_dataset": "fixed_funds", "family_weights": {"risk_trend_quality": [.5, .5]}}}
    current = deepcopy(previous)
    current["status"] = "running"
    validate_family_resume(previous, current)
    for key, value in (("fund_dataset", "different_funds"), ("family_weights", {"risk_trend_quality": [.8, .2]})):
        changed = deepcopy(current)
        changed["protocol"][key] = value
        with pytest.raises(ValueError, match="contract changed"):
            validate_family_resume(previous, changed)
    changed = {**current, "end": "2026-10-01"}
    with pytest.raises(ValueError, match="calendar"):
        validate_family_resume(previous, changed)


def test_continuation_diagnostics_retain_successful_prefix_not_failed_attempt(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    pd.DataFrame([{"date": "2025-01-31", "symbol": "000001.SZSE", "rank": 1},
                  {"date": "2025-02-28", "symbol": "000002.SZSE", "rank": 1}]).to_csv(source / "rankings.csv", index=False)
    checkpoint = {"source": str(source), "state": {"day": "2025-02-27"}}
    save_diagnostics(dest, "rankings.csv", [{"date": "2025-02-28", "symbol": "000003.SZSE", "rank": 1}], checkpoint)
    rows = pd.read_csv(dest / "rankings.csv")
    assert rows.symbol.tolist() == ["000001.SZSE", "000003.SZSE"]


def test_only_training_determines_direction_even_if_later_periods_disagree():
    rows = [{"segment": segment, "horizon": 20, "factor": "Alpha158/" + name,
             "ic_days": 221, "train_direction": direction, "mean_rank_ic": value}
            for segment, direction, value in (("train", "low", -.02), ("valid", "high", .2), ("test", "high", .4))
            for name in ("RSQR30", "WVMA20")]
    assert all(row["sign"] == -1 for row in training_directions(pd.DataFrame(rows)).values())
    for row in rows:
        if row["segment"] == "train":
            row["ic_days"] = 0
    with pytest.raises(ValueError, match="training-only direction"):
        training_directions(pd.DataFrame(rows))


def test_common_missing_feature_pool_and_controls_are_shared():
    pool = sample_pool()
    native = pd.DataFrame(pool).set_index("symbol")[["RSQR30", "WVMA20"]]
    native.iloc[0, 0] = np.nan
    common = common_pool(pool, native)
    assert len(common) == 3 and all(row["symbol"] != pool[0]["symbol"] for row in common)
    assert ranked_recipe(common, RECIPES[0], {}) == rank(common, VARIANTS[1])
    assert ranked_recipe(common, RECIPES[1], {}) == rank(common, VARIANTS[2])
    with pytest.raises(ValueError, match="omitted"):
        common_pool(pool, native.iloc[1:])


def test_two_trend_features_get_one_family_vote_not_two():
    directions = {"RSQR30": {"sign": 1}, "WVMA20": {"sign": -1}}
    result = ranked_recipe(sample_pool(), "equal_three_families", directions)
    first = next(row for row in result if row["symbol"] == "000001.SZSE")
    assert first["score"] == pytest.approx((1 + .25 + 1) / 3)
    assert first["trend_rank"] == .25
    assert ranked_recipe([], "equal_three_families", directions) == []


def test_no_training_period_or_non_replay_trading_of_fitted_direction():
    feed = object.__new__(FamilyFeed)
    feed.helper = SimpleNamespace(end="2026-09-30")
    config = portfolio()
    with pytest.raises(ValueError, match="training period"):
        feed.signal_frame("2024-12-31", config, RunContext("test", TradingMode.REPLAY))
    with pytest.raises(ValueError, match="replay-only"):
        feed.signal_frame("2025-01-31", config, RunContext("test", TradingMode.PAPER))


def test_existing_mixed_allocator_accepts_new_stock_producer_without_curve_blending():
    context = RunContext("test", TradingMode.REPLAY)
    class Producer:
        def signal_frame(self, day, config, supplied_context):
            assert config == portfolio(140000) and supplied_context == context
            return SignalFrame.from_legacy([{"symbol": "000001.SZSE", "score": 2.},
                                            {"symbol": "000002.SZSE", "score": 1.}], day, "fixed_family", context)
    feed = StockDefensiveFeed(SimpleNamespace(end="2026-09-30"), {}, "new_recipe", 140000, stock_signal=Producer())
    signal = feed.signal_frame("2025-01-31", mixed_config(), context)
    assert [row["symbol"] for row in signal.ranked_candidates()][:2] == list(DEFENSIVE)
    intent = PortfolioIntent.full_target({s: .2 for s in [*DEFENSIVE, "000001.SZSE", "000002.SZSE"]},
                                         context=context, decided_at="2025-01-31T15:00:00+08:00")
    weights = dict(feed.allocate(intent).weights)
    assert weights == {DEFENSIVE[0]: .2, DEFENSIVE[1]: .2, "000001.SZSE": .02, "000002.SZSE": .02}
