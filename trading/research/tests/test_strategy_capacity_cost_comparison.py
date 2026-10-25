import json
from pathlib import Path

import pytest

from trading.research.strategy_capacity_cost_comparison import build_comparison


def _account(path: Path) -> None:
    payload = {
        "day": "2026-10-23",
        "orders": {
            "a": {"qty": 200, "filled": 100, "limit_price": 1000, "fees": 10},
            "b": {"qty": 100, "filled": 100, "limit_price": 2000, "fees": 20},
        },
        "fills": {
            "fa": {"order_id": "a", "qty": 100, "price": 1000},
            "fb": {"order_id": "b", "qty": 100, "price": 2000},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_saved_order_capacity_and_fixed_cost_bands(tmp_path: Path):
    path = tmp_path / "account.json"
    _account(path)
    result = build_comparison({"toy": path}, cost_bands_bps=(0, 50))
    row = result["strategy_rows"][0]
    assert row["requested_qty"] == 300
    assert row["filled_qty"] == 200
    assert row["unfilled_qty"] == 100
    assert row["unfilled_exposure_cny"] == 1000.0
    assert row["capacity_utilization"] == pytest.approx(2 / 3)
    assert row["cost_stress"][1]["slippage_bps"] == 50
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_order_fill_quantity_mismatch_is_rejected(tmp_path: Path):
    path = tmp_path / "account.json"
    _account(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fills"]["fb"]["qty"] = 50
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="quantities disagree"):
        build_comparison({"toy": path})
