"""Saved-path exposure/fee identities only; no market data or Engine replay."""
from copy import deepcopy

import pytest

from trading.research.leader_environment_attribution import diagnose_pair


D1, D2 = "2026-03-02", "2026-03-03"


def fixture_pair():
    config = {"initial_cash": 1000, "commission": .0003, "minimum_commission": 5,
        "sell_tax": .0005, "transfer_fee": .00001, "slippage_bps": 10,
        "participation": .01, "holdings": 5}

    def case(guard, equity, cash, daily_fees, fill_fees):
        return {"case_id": f"trend_leader__{guard}__uncapped", "type": "trend_leader",
            "guard": guard, "capacity": "uncapped", "config": deepcopy(config),
            "status": "completed_conditional_mechanical_replay", "complete_PnL_available": True,
            "blocking_evidence": None, "residual_positions": [], "engine_sessions_committed": 2,
            "metrics": {"net_return": equity[-1] / 1000 - 1,
                "max_drawdown": equity[0] / 1000 - 1, "final_equity": equity[-1], "fees": sum(daily_fees)},
            "daily": [{"date": day, "equity": e, "cash": c, "receivables": 100.,
                "holdings": 1 if i == 0 else 0, "fees": fee, "stale_symbols": [], "events": []}
                for i, (day, e, c, fee) in enumerate(zip((D1, D2), equity, cash, daily_fees))],
            "orders": [{"id": "buy", "day": D1, "fees": sum(fill_fees[:2])},
                {"id": "sell", "day": D2, "fees": fill_fees[2]}],
            "fills": [{"id": f"fill-{i}", "order_id": "buy" if i < 2 else "sell",
                "day": D1 if i < 2 else D2, "fees": fee}
                for i, fee in enumerate(fill_fees)]}

    unguarded = case("unguarded", (994., 1010.), (394., 910.), (6., 4.), (200, 400, 400))
    guarded = case("two_close_envguard", (999., 1018.), (649., 918.), (1., 1.), (40, 60, 100))
    return unguarded, guarded


def test_matched_exposure_and_net_fee_addback_identity_preserve_saved_paths():
    unguarded, guarded = fixture_pair()
    original = deepcopy((unguarded, guarded))
    saved = diagnose_pair(unguarded, guarded)
    assert saved["status"] == "comparable_posthoc_math_diagnostic", saved
    # Stocks = equity - cash - receivables. The 100 receivable in both
    # accounts must not be counted as stock exposure: 250 / 500 == .5.
    assert saved["lambda"] == pytest.approx(.5)
    comparison = saved["comparison"]
    assert comparison["unguarded_saved_net_return"] == pytest.approx(.01)
    assert comparison["guarded_saved_net_return"] == pytest.approx(.018)
    assert comparison["matched_unguarded_net_return"] == pytest.approx(.005)
    assert comparison["net_advantage_return"] == pytest.approx(.013)
    assert comparison["gross_advantage_return"] == pytest.approx(.01)
    assert comparison["fee_saving_cny"] == pytest.approx(3.)
    assert comparison["net_advantage_return"] == pytest.approx(
        comparison["gross_advantage_return"] + comparison["fee_saving_cny"] / 1000)
    assert (unguarded, guarded) == original
    assert saved["execution_admitted"] is False


def test_missing_trades_null_metrics_blocks_residuals_stale_or_different_days_are_not_zero_returns():
    for mutation in ("no_fills", "null_metrics", "blocking", "residual", "stale", "different_days"):
        unguarded, guarded = fixture_pair()
        if mutation == "no_fills":
            unguarded["fills"] = []
        elif mutation == "null_metrics":
            unguarded["metrics"] = None
        elif mutation == "blocking":
            unguarded["blocking_evidence"] = {"day": D2, "reason": "saved source unresolved"}
        elif mutation == "residual":
            unguarded["residual_positions"] = ["600000.SSE"]
        elif mutation == "stale":
            guarded["daily"][0]["stale_symbols"] = ["600000.SSE"]
        else:
            guarded["daily"][1]["date"] = "2026-03-04"
        original = deepcopy((unguarded, guarded))
        saved = diagnose_pair(unguarded, guarded)
        assert saved["status"] == "not_comparable" and saved["reason_codes"], mutation
        assert saved["lambda"] is None and saved["comparison"] is None, mutation
        assert (unguarded, guarded) == original


def test_exposure_above_one_and_invalid_fee_evidence_are_refused_without_clipping():
    for mutation in ("lambda_above_one", "negative_fee", "fractional_cent_fee", "daily_fee_mismatch"):
        unguarded, guarded = fixture_pair()
        if mutation == "lambda_above_one":
            guarded["daily"][0]["cash"] = 149.  # 750 / 500 == 1.5, never clip to one.
        elif mutation == "negative_fee":
            guarded["fills"][0]["fees"] = -1
        elif mutation == "fractional_cent_fee":
            guarded["fills"][0]["fees"] = 40.5
        else:
            guarded["daily"][0]["fees"] = 2.
        saved = diagnose_pair(unguarded, guarded)
        assert saved["status"] == "not_comparable" and saved["reason_codes"], mutation
        assert saved["lambda"] is None and saved["comparison"] is None, mutation
