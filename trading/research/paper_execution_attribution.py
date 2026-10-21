"""Aggregate offline parent/child execution and paper-cost attribution."""

import argparse
import csv
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


SCHEMA = "paper_execution_attribution_v1"


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _decimal(value, default="0"):
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _money(value):
    return f"{Decimal(value).quantize(Decimal('0.01')):.2f}"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _key(row):
    return (
        row.get("strategy_id", ""),
        row.get("symbol", ""),
        row.get("side", ""),
        row.get("parent_sequence", row.get("sequence", "")),
    )


def _load_paper_fill_map(path):
    if not path or not Path(path).exists():
        return {}
    return {
        (row.get("strategy_id", ""), row.get("symbol", ""), row.get("side", "")): row
        for row in _read_csv(path)
    }


def _load_metric_map(path):
    if not path or not Path(path).exists():
        return {}
    return {row.get("metric", ""): row.get("value", "") for row in _read_csv(path)}


def _load_scenarios(path):
    if not path or not Path(path).exists():
        return []
    rows = []
    for row in _read_csv(path):
        rows.append({
            "scenario_id": row.get("scenario_id", ""),
            "total_reference_impact_rmb": row.get("total_reference_impact_rmb", ""),
            "adverse_price_impact_rmb": row.get("adverse_price_impact_rmb", ""),
            "estimated_fees_slippage_rmb": row.get("estimated_fees_slippage_rmb", ""),
            "basis": row.get("basis", ""),
        })
    return rows


def _aggregate_children(rows, fill_map):
    grouped = defaultdict(list)
    for row in rows:
        grouped[_key(row)].append(row)
    parents = []
    for key, children in grouped.items():
        children = sorted(children, key=lambda row: _int(row.get("child_sequence")))
        first = children[0]
        last = children[-1]
        strategy_id, symbol, side, parent_sequence = key
        parent_qty = _int(first.get("parent_qty"))
        planned_qty = sum(_int(row.get("planned_qty")) for row in children)
        filled_qty = sum(_int(row.get("synthetic_fill_qty")) for row in children)
        remaining_qty = _int(last.get("synthetic_remaining_qty"))
        price_row = fill_map.get((strategy_id, symbol, side), {})
        price = _decimal(price_row.get("reference_price_rmb"))
        full_notional = _decimal(price_row.get("gross_notional_rmb"))
        fees = _decimal(price_row.get("fees_rmb"))
        synthetic_notional = price * filled_qty
        residual_notional = price * remaining_qty
        parents.append({
            "parent_sequence": parent_sequence,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "first_planned_time": first.get("planned_time", ""),
            "last_planned_time": last.get("planned_time", ""),
            "child_count": len(children),
            "parent_qty": parent_qty,
            "planned_qty": planned_qty,
            "synthetic_fill_qty": filled_qty,
            "synthetic_remaining_qty": remaining_qty,
            "synthetic_fill_ratio": f"{(Decimal(filled_qty) / Decimal(parent_qty) if parent_qty else Decimal('0')):.6f}",
            "child_execution_status": last.get("parent_execution_status", ""),
            "reference_price_rmb": _money(price),
            "paper_full_parent_notional_rmb": _money(full_notional),
            "synthetic_fill_notional_rmb": _money(synthetic_notional),
            "synthetic_remaining_notional_rmb": _money(residual_notional),
            "paper_parent_fees_rmb": _money(fees),
            "allocation_method": "frozen_reference_price_times_synthetic_child_fills",
            "real_broker_state": "false",
            "execution_admitted": "false",
        })
    return sorted(parents, key=lambda row: (_int(row["parent_sequence"]), row["symbol"]))


def _child_pressure_scenarios(rows, fill_map):
    """Project cash and residual pressure when only part of each schedule runs."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[_key(row)].append(row)

    scenarios = (("first_child_only", 1), ("first_two_children", 2), ("full_schedule", None))
    output = []
    for scenario_id, limit in scenarios:
        child_rows_used = 0
        scheduled_qty_used = 0
        assumed_filled_qty = 0
        assumed_remaining_qty = 0
        residual_parent_count = 0
        buy_cash_required = Decimal("0")
        sell_cash_release = Decimal("0")
        residual_notional = Decimal("0")
        buy_parent_count = 0
        sell_parent_count = 0

        for key, parent_children in grouped.items():
            children = sorted(parent_children, key=lambda row: _int(row.get("child_sequence")))
            used = children if limit is None else children[: min(limit, len(children))]
            first = children[0]
            strategy_id, symbol, side, _ = key
            parent_qty = _int(first.get("parent_qty"))
            scheduled_qty = sum(_int(row.get("planned_qty")) for row in used)
            remaining_qty = max(0, parent_qty - scheduled_qty)
            price_row = fill_map.get((strategy_id, symbol, side), {})
            price = _decimal(price_row.get("reference_price_rmb"))
            used_notional = price * scheduled_qty
            remaining_notional = price * remaining_qty

            child_rows_used += len(used)
            scheduled_qty_used += scheduled_qty
            assumed_filled_qty += scheduled_qty
            assumed_remaining_qty += remaining_qty
            residual_parent_count += remaining_qty > 0
            residual_notional += remaining_notional
            if side == "buy":
                buy_parent_count += 1
                buy_cash_required += used_notional
            elif side == "sell":
                sell_parent_count += 1
                sell_cash_release += used_notional

        output.append({
            "scenario_id": scenario_id,
            "parent_count": len(grouped),
            "buy_parent_count": buy_parent_count,
            "sell_parent_count": sell_parent_count,
            "child_rows_used": child_rows_used,
            "scheduled_qty_used": scheduled_qty_used,
            "assumed_filled_qty": assumed_filled_qty,
            "assumed_remaining_qty": assumed_remaining_qty,
            "residual_parent_count": residual_parent_count,
            "buy_cash_required_rmb": _money(buy_cash_required),
            "sell_cash_release_rmb": _money(sell_cash_release),
            "residual_notional_rmb": _money(residual_notional),
            "basis": "frozen reference price times scheduled child quantities; pressure projection only",
            "real_broker_state": "false",
            "execution_admitted": "false",
        })
    return output


def _side_summary(parents):
    groups = defaultdict(list)
    for row in parents:
        groups[row["side"]].append(row)
    summary = []
    for side, rows in sorted(groups.items()):
        summary.append({
            "side": side,
            "parent_count": len(rows),
            "child_count": sum(_int(row["child_count"]) for row in rows),
            "planned_qty": sum(_int(row["planned_qty"]) for row in rows),
            "synthetic_fill_qty": sum(_int(row["synthetic_fill_qty"]) for row in rows),
            "synthetic_remaining_qty": sum(_int(row["synthetic_remaining_qty"]) for row in rows),
            "completed_parent_count": sum(_int(row["synthetic_remaining_qty"]) == 0 for row in rows),
            "paper_full_parent_notional_rmb": _money(sum(_decimal(row["paper_full_parent_notional_rmb"]) for row in rows)),
            "synthetic_fill_notional_rmb": _money(sum(_decimal(row["synthetic_fill_notional_rmb"]) for row in rows)),
            "synthetic_remaining_notional_rmb": _money(sum(_decimal(row["synthetic_remaining_notional_rmb"]) for row in rows)),
            "paper_parent_fees_rmb": _money(sum(_decimal(row["paper_parent_fees_rmb"]) for row in rows)),
        })
    return summary


def _execution_cost_stress(parents):
    """Estimate incremental cost on synthetic fills under fixed slippage stress."""
    by_strategy = defaultdict(list)
    for row in parents:
        by_strategy[row.get("strategy_id", "")].append(row)
    output = []
    for strategy_id, strategy_parents in sorted(by_strategy.items()):
        filled_notional = sum(
            _decimal(row["reference_price_rmb"]) * _int(row["synthetic_fill_qty"])
            for row in strategy_parents
        )
        residual_notional = sum(
            _decimal(row["reference_price_rmb"]) * _int(row["synthetic_remaining_qty"])
            for row in strategy_parents
        )
        baseline_fees = sum(
            _decimal(row["paper_parent_fees_rmb"]) for row in strategy_parents
        )
        for stress_bps in (0, 5, 10, 20, 50):
            incremental = filled_notional * Decimal(stress_bps) / Decimal("10000")
            output.append({
                "strategy_id": strategy_id,
                "stress_id": f"{strategy_id}_additional_slippage_{stress_bps}bps",
                "additional_slippage_bps": stress_bps,
                "parent_count": len(strategy_parents),
                "synthetic_filled_notional_rmb": _money(filled_notional),
                "synthetic_residual_notional_rmb": _money(residual_notional),
                "baseline_paper_fees_rmb": _money(baseline_fees),
                "incremental_slippage_cost_rmb": _money(incremental),
                "estimated_total_execution_cost_rmb": _money(baseline_fees + incremental),
                "basis": "candidate-specific frozen reference price times synthetic fills; stress is diagnostic only",
                "real_broker_state": "false",
                "execution_admitted": "false",
            })
    return output


def build(child_projection, paper_fill=None, tca=None, scenarios=None, out_dir=None):
    child_projection = Path(child_projection)
    child_rows = _read_csv(child_projection)
    fill_map = _load_paper_fill_map(paper_fill)
    tca_metrics = _load_metric_map(tca)
    scenario_rows = _load_scenarios(scenarios)
    parents = _aggregate_children(child_rows, fill_map)
    pressure_rows = _child_pressure_scenarios(child_rows, fill_map)
    stress_rows = _execution_cost_stress(parents)
    side_summary = _side_summary(parents)
    out = Path(out_dir) if out_dir else child_projection.parent
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "status": "offline_reference_attribution",
        "source_artifacts": {
            "child_projection": str(child_projection),
            "paper_fill_projection": str(paper_fill) if paper_fill else None,
            "tca_pretrade": str(tca) if tca else None,
            "scenario_analysis": str(scenarios) if scenarios else None,
            "child_pressure_scenarios": str(Path(out_dir) / "paper_child_pressure_scenarios_v1.csv") if out_dir else str(child_projection.parent / "paper_child_pressure_scenarios_v1.csv"),
            "execution_cost_stress": str(Path(out_dir) / "paper_execution_cost_stress_v1.csv") if out_dir else str(child_projection.parent / "paper_execution_cost_stress_v1.csv"),
        },
        "parent_count": len(parents),
        "child_count": len(child_rows),
        "completed_parent_count": sum(_int(row["synthetic_remaining_qty"]) == 0 for row in parents),
        "residual_parent_count": sum(_int(row["synthetic_remaining_qty"]) > 0 for row in parents),
        "real_broker_state": False,
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "tca_basis": "frozen reference price and configured paper cost; no observed fill benchmark",
        "tca_metrics": tca_metrics,
        "side_summary": side_summary,
        "scenario_summary": scenario_rows,
        "child_pressure_scenarios": pressure_rows,
        "execution_cost_stress": stress_rows,
        "parents": parents,
        "not_attempted": [
            "No live quote, broker order, broker fill or account state was read.",
            "No synthetic residual was promoted to a broker residual or retry instruction.",
            "No automatic child-order release or submission was attempted.",
        ],
    }
    json_path = out / "paper_execution_attribution_v1.json"
    csv_path = out / "paper_execution_attribution_v1.csv"
    pressure_csv_path = out / "paper_child_pressure_scenarios_v1.csv"
    stress_csv_path = out / "paper_execution_cost_stress_v1.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = list(parents[0]) if parents else ["parent_sequence", "strategy_id", "symbol", "side"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(parents)
    pressure_fields = list(pressure_rows[0]) if pressure_rows else ["scenario_id"]
    with pressure_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=pressure_fields)
        writer.writeheader()
        writer.writerows(pressure_rows)
    stress_fields = list(stress_rows[0]) if stress_rows else ["stress_id"]
    with stress_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=stress_fields)
        writer.writeheader()
        writer.writerows(stress_rows)
    return json_path, csv_path, pressure_csv_path, stress_csv_path, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-projection", required=True)
    parser.add_argument("--paper-fill")
    parser.add_argument("--tca")
    parser.add_argument("--scenarios")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    json_path, csv_path, pressure_csv_path, stress_csv_path, payload = build(
        args.child_projection, args.paper_fill, args.tca, args.scenarios, args.out
    )
    print(json.dumps({
        "json": str(json_path),
        "csv": str(csv_path),
        "pressure_csv": str(pressure_csv_path),
        "stress_csv": str(stress_csv_path),
        "parent_count": payload["parent_count"],
        "child_count": payload["child_count"],
        "residual_parent_count": payload["residual_parent_count"],
        "execution_admitted": payload["execution_admitted"],
    }, ensure_ascii=False))
    return 0
