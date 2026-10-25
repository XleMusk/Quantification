import json
from pathlib import Path

import pytest

from trading.research.virtual_strategy_sleeves import build_virtual_sleeves


def _account(path: Path, *, day: str, target: dict[str, float], cash: int = 7_000,
             symbol: str = "600000.SSE") -> None:
    payload = {
        "day": day,
        "cash": cash,
        "receivables": {},
        "prices": {symbol: 1000},
        "positions": {symbol: [{"qty": 100, "available": day}]},
        "targets": target,
        "orders": {},
        "fills": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_virtual_sleeves_conserve_cash_and_equity(tmp_path: Path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _account(first, day="2026-10-23", target={"600000.SSE": .2, "000001.SZSE": .2})
    _account(second, day="2026-10-23", target={"600000.SSE": .1, "000002.SZSE": .2})
    result = build_virtual_sleeves({"first": first, "second": second}, single_limit=.4)
    assert result["status"] == "VERIFIED_RESEARCH_ONLY_VIRTUAL_SLEEVES"
    assert result["common_terminal_day"] is True
    assert result["conservation_checks"]["initial_cash_conserved_cents"] is True
    assert result["conservation_checks"]["equity_attribution_conserved"] is True
    assert result["virtual_pool"]["target_weights"]["600000.SSE"] == pytest.approx(.15)
    assert result["execution_admitted"] is False
    assert result["orders_created"] is False and result["ledger_mutated"] is False


def test_mixed_terminal_dates_are_explicit_research_only(tmp_path: Path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _account(first, day="2026-10-23", target={"600000.SSE": .2})
    _account(second, day="2026-10-13", target={"600000.SSE": .2})
    result = build_virtual_sleeves({"first": first, "second": second}, single_limit=.4)
    assert result["common_terminal_day"] is False
    assert result["alignment_status"] == "MIXED_TERMINAL_DATES_RESEARCH_ONLY"
    assert result["research_only"] is True


def test_virtual_sleeves_reject_default_single_name_breach(tmp_path: Path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _account(first, day="2026-10-23", target={"600000.SSE": .3})
    _account(second, day="2026-10-23", target={"600000.SSE": .3})
    with pytest.raises(ValueError, match="exposure limits"):
        build_virtual_sleeves({"first": first, "second": second})
