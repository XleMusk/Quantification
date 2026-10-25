from trading.research.paper_exposure_stress import build_stress_view


def _inputs():
    plan = {
        "execution_admitted": False,
        "account": {"cash_cny": 3000.0, "equity_cny": 10000.0},
    }
    reconciliation = {
        "execution_admitted": False,
        "status": "PAPER_CLOSE_REFERENCE_READY",
        "day": "2026-10-23",
        "target_count": 2,
        "rows": [
            {
                "symbol": "600000.SSE", "paper_close_rmb": 10.0,
                "current_qty": 100, "sellable_qty": 100, "estimated_target_qty": 0,
                "target_weight": 0.1, "estimated_buy_fee_cny": 0.0,
            },
            {
                "symbol": "000001.SZSE", "paper_close_rmb": 20.0,
                "current_qty": 0, "sellable_qty": 0, "estimated_target_qty": 100,
                "target_weight": 0.2, "estimated_buy_fee_cny": 0.0,
            },
        ],
    }
    native = {
        "execution_admitted": False,
        "reference_date": "2026-10-23",
        "comparison": {"close_mismatches": 0},
        "rows": [{"symbol": "600000.SSE"}, {"symbol": "000001.SZSE"}],
    }
    return plan, reconciliation, native


def test_t1_and_cash_stress_report_actions_and_residual_exposure():
    result = build_stress_view(*_inputs(), cash_haircut=0.5)
    assert result["status"] == "VERIFIED_LOCAL_PAPER_EXPOSURE_STRESS"
    t1 = next(row for row in result["scenarios"] if row["scenario_id"] == "t1_sell_blocked")
    assert t1["blocked_order_count"] == 1
    assert t1["blocked_qty"] == 100
    assert t1["residual_exposure_cny"] == 1000.0
    cash = next(row for row in result["scenarios"] if row["scenario_id"] == "cash_haircut_50pct")
    assert cash["blocked_order_count"] == 1
    assert cash["blocked_qty"] == 100
    assert result["orders_created"] is False
    assert result["ledger_mutated"] is False


def test_input_day_or_close_mismatch_is_rejected():
    plan, reconciliation, native = _inputs()
    native["reference_date"] = "2026-10-22"
    try:
        build_stress_view(plan, reconciliation, native)
    except ValueError as exc:
        assert "days must match" in str(exc)
    else:
        raise AssertionError("mismatched close day must be rejected")

