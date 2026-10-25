from __future__ import annotations

import csv
from pathlib import Path

import pytest

from trading.research import common_window_strategy_comparison as comparison


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_common_window_uses_date_intersection_and_rmb_fee_conversion(tmp_path, monkeypatch):
    entries = []
    for name, end_equity, fee_cents in (("half", 110.0, 100), ("ridge", 108.0, 250)):
        daily = tmp_path / f"{name}_daily.csv"
        orders = tmp_path / f"{name}_orders.csv"
        fills = tmp_path / f"{name}_fills.csv"
        daily_rows = [
            {"date": "2024-01-02", "equity": 100.0, "turnover_value": 10},
            {"date": "2024-01-04", "equity": end_equity, "turnover_value": 20},
        ]
        if name == "half":
            daily_rows.insert(0, {"date": "2024-01-01", "equity": 95.0, "turnover_value": 0})
        _write_csv(daily, ["date", "equity", "turnover_value"], daily_rows)
        _write_csv(
            orders,
            ["day", "fees"],
            [{"day": "2024-01-02", "fees": fee_cents}, {"day": "2024-01-04", "fees": 0}],
        )
        _write_csv(fills, ["day"], [{"day": "2024-01-02"}])
        entries.append(
            {
                "id": "low_vol_half_momentum_20" if name == "half" else "mature_factor_balanced_ridge",
                "label": name,
                "candidate_spec_id": name,
                "stockdb_snapshot": "snapshot",
                "source_paths": {"daily": str(daily), "orders": str(orders), "fills": str(fills)},
            }
        )
    monkeypatch.setattr(comparison, "STRATEGY_IDS", tuple(entry["id"] for entry in entries))
    monkeypatch.setattr(comparison, "_resolve_entries", lambda: entries)

    receipt = comparison.build_receipt()

    assert receipt["common_window"] == {
        "start": "2024-01-02",
        "end": "2024-01-04",
        "sessions": 2,
        "basis": "intersection_of_saved_daily_account_dates",
    }
    assert all(row["metrics"]["sessions"] == 2 for row in receipt["strategies"])
    assert receipt["strategies"][0]["metrics"]["net_return"] == pytest.approx(0.1)
    assert receipt["strategies"][0]["metrics"]["fees_rmb"] == pytest.approx(1.0)
    assert receipt["strategies"][1]["metrics"]["fees_rmb"] == pytest.approx(2.5)
    assert receipt["execution_admitted"] is False
    assert receipt["ledger_mutated"] is False
