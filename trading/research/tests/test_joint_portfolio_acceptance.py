import json
from pathlib import Path

import pytest

from trading.research.joint_portfolio_acceptance import build_joint_portfolio


def _account(path: Path, day: str, targets: dict[str, float], prices: dict[str, int]):
    path.write_text(json.dumps({
        "day": day, "cash": 14_000_000, "positions": {}, "prices": prices,
        "targets": targets, "orders": {}, "fills": {}, "execution_admitted": False,
        "portfolio_comparison": {"orders_created": False, "ledger_mutated": False},
    }), encoding="utf-8")


def test_aligned_accounts_produce_integer_lot_joint_target(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _account(a, "2026-10-13", {"000001.SZSE": .16}, {"000001.SZSE": 1000})
    _account(b, "2026-10-13", {"000002.SZSE": .16}, {"000002.SZSE": 500})
    result = build_joint_portfolio({"a": a, "b": b})
    assert result["common_terminal_day"] == "2026-10-13"
    assert result["acceptance"]["integer_lots"] is True
    assert result["acceptance"]["all_positive_targets_lot_feasible"] is True
    assert result["acceptance"]["cash_nonnegative"] is True
    assert result["orders_created"] is False


def test_mixed_terminal_days_are_rejected(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _account(a, "2026-10-13", {"000001.SZSE": .16}, {"000001.SZSE": 1000})
    _account(b, "2026-10-23", {"000002.SZSE": .16}, {"000002.SZSE": 500})
    with pytest.raises(ValueError, match="common terminal day"):
        build_joint_portfolio({"a": a, "b": b})


def test_default_aligned_research_accounts_generate_receipt():
    result = build_joint_portfolio()
    assert result["status"] == "VERIFIED_RESEARCH_ONLY_JOINT_PORTFOLIO"
    assert result["common_terminal_day"] == "2026-10-13"
    assert result["acceptance"]["no_execution_side_effect"] is True
    assert result["constraints"]["gross_pass"] is True
    assert result["acceptance"]["all_positive_targets_lot_feasible"] is True
    assert next(row for row in result["target_rows"] if row["symbol"] == "301686.SZSE")["target_quantity"] == 100
