import json

import pytest

from trading.research.joint_portfolio_paper_package import (
    STATUS,
    build_joint_paper_package,
)


def _account(path, day, symbol, price):
    path.write_text(json.dumps({
        "day": day,
        "cash": 14_000_000,
        "positions": {},
        "prices": {symbol: price},
        "targets": {symbol: 0.16},
        "orders": {},
        "fills": {},
        "execution_admitted": False,
        "portfolio_comparison": {"orders_created": False, "ledger_mutated": False},
    }), encoding="utf-8")


def test_joint_target_reaches_shared_paper_package(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _account(first, "2026-10-13", "000001.SZSE", 1000)
    _account(second, "2026-10-13", "000002.SZSE", 500)
    result = build_joint_paper_package(
        {"first": first, "second": second},
        execution_day="2026-10-14", settlement_day="2026-10-15",
        initial_cash_cny=100000, output_dir=tmp_path / "run",
    )
    assert result["status"] == STATUS
    assert result["execution_admitted"] is False
    assert result["paper_package"]["paper_order_count"] == 2
    assert result["paper_package"]["result"]["cost_model"]["id"] == "cn_a_share_standard_ledger_costs_v1"
    assert result["paper_package"]["result"]["benchmark_binding"]["id"] == "paper_target_projection_no_market_benchmark_v1"
    assert result["target_payload"]["quantities"] == {
        "000001.SZSE": 800,
        "000002.SZSE": 1600,
    }
    assert (tmp_path / "run" / "joint_paper_receipt.json").is_file()
    assert (tmp_path / "run" / "paper_package" / "projected_account.json").is_file()


def test_joint_paper_rejects_execution_before_terminal_day(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _account(first, "2026-10-13", "000001.SZSE", 1000)
    _account(second, "2026-10-13", "000002.SZSE", 500)
    with pytest.raises(ValueError, match="execution_day must be later"):
        build_joint_paper_package(
            {"first": first, "second": second},
            execution_day="2026-10-13", settlement_day="2026-10-14",
            initial_cash_cny=100000,
        )
