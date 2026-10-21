"""Create one explicitly selected, paper-only strategy plan from an opening pack."""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..routing import route_order


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _price_cents(row: dict[str, str]) -> int:
    if row.get("price_cents") not in (None, ""):
        return int(row["price_cents"])
    return int((Decimal(str(row.get("price_rmb") or "0")) * 100).quantize(Decimal("1")))


def _write_offline_prefill_payloads(
    out: Path,
    orders: list[dict[str, str]],
    preflight: list[dict[str, str]],
    execution_day: str,
) -> tuple[Path, Path, dict]:
    """Materialize frozen order fields for later THS mapping, never submission."""
    preflight_by_key = {
        (row.get("symbol"), row.get("side"), row.get("qty"), _price_cents(row)): row
        for row in preflight
    }
    rows = []
    for sequence, order in enumerate(orders, start=1):
        key = (order.get("symbol"), order.get("side"), order.get("qty"), _price_cents(order))
        check = preflight_by_key.get(key, {})
        route = route_order(order.get("symbol", ""))
        accepted = check.get("preflight_status") == "accepted" and route.status == "route_available"
        price = f"{Decimal(_price_cents(order)) / 100:.2f}"
        rows.append({
            "sequence": sequence,
            "strategy_id": order.get("strategy_id", ""),
            "symbol": order.get("symbol", ""),
            "side": order.get("side", ""),
            "qty": order.get("qty", "") if accepted else "",
            "price_rmb": price if accepted else "",
            "symbol_field": order.get("symbol", "").split(".")[0] if accepted else "",
            "price_field": price if accepted else "",
            "qty_field": order.get("qty", "") if accepted else "",
            "preflight_status": check.get("preflight_status", "missing"),
            "route_id": route.route_id,
            "route_channel": route.channel,
            "route_mode": route.mode,
            "route_status": route.status if check.get("preflight_status") == "accepted" else "not_routable_preflight_rejected",
            "route_reason": route.reason,
            "prefill_status": "reference_payload_only" if accepted else (
                "not_prefillable_route_unavailable" if check.get("preflight_status") == "accepted"
                else "not_prefillable_preflight_rejected"
            ),
            "price_basis": "frozen_opening_pack_reference_not_current_rmb",
            "execution_admitted": "false",
        })
    csv_path = out / "ths_prefill_payloads.csv"
    _write_csv(csv_path, rows)
    summary = {
        "schema": "ths_prefill_payloads_preview_v1",
        "status": "offline_reference_payload_only",
        "execution_day": execution_day,
        "price_basis": "frozen_opening_pack_reference_not_current_rmb",
        "order_count": len(orders),
        "accepted_payload_count": sum(row["prefill_status"] == "reference_payload_only" for row in rows),
        "rejected_or_missing_count": sum(row["prefill_status"] != "reference_payload_only" for row in rows),
        "route_available_count": sum(row["route_status"] == "route_available" for row in rows),
        "route_unavailable_count": sum(row["route_status"] != "route_available" for row in rows),
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "submitted": False,
        "execution_admitted": False,
        "route_policy": "single_manual_ths_mainboard_route; unsupported instruments remain visible and produce no payload",
        "rows": rows,
        "not_attempted": [
            "Frozen reference prices are not current RMB quotes.",
            "No THS field mapping, client write, readback, submission or cancellation was attempted.",
        ],
        "source_artifacts": {"payload_csv": str(csv_path)},
    }
    json_path = out / "ths_prefill_payloads.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    readback_template_path = out / "ths_prefill_readback_template.json"
    readback_rows = []
    for row in rows:
        readback_applicable = row["route_status"] == "route_available" and row["prefill_status"] == "reference_payload_only"
        readback_rows.append({
            "sequence": row["sequence"],
            "symbol": row["symbol"],
            "side": row["side"],
            "route_status": row["route_status"],
            "readback_applicable": readback_applicable,
            "expected": {
                "symbol_field": row["symbol_field"] or None,
                "price_field": row["price_field"] or None,
                "qty_field": row["qty_field"] or None,
            },
            "readback": {
                "symbol_field": None,
                "price_field": None,
                "qty_field": None,
            },
            "field_status": {
                "symbol_field": "pending_authorized_client_readback" if readback_applicable else "not_applicable",
                "price_field": "pending_authorized_client_readback" if readback_applicable else "not_applicable",
                "qty_field": "pending_authorized_client_readback" if readback_applicable else "not_applicable",
            },
            "readback_status": "pending_authorized_client_readback" if readback_applicable else "not_applicable_route_unavailable",
        })
    applicable_count = sum(row["readback_applicable"] for row in readback_rows)
    route_unavailable_count = sum(row["route_status"] != "route_available" for row in readback_rows)
    readback_template = {
        "schema": "ths_prefill_readback_template_v1",
        "status": "offline_reference_readback_template",
        "execution_day": execution_day,
        "strategy_id": next((row.get("strategy_id") for row in rows if row.get("strategy_id")), None),
        "basis": "frozen paper target and offline THS payload only; prices are not current RMB quotes",
        "client_interacted": False,
        "submitted": False,
        "execution_admitted": False,
        "readback_diagnostics": {
            "status": "pending_authorized_client_readback",
            "applicable_row_count": applicable_count,
            "pending_row_count": applicable_count,
            "pending_field_count": applicable_count * 3,
            "route_unavailable_count": route_unavailable_count,
            "mismatch_count": 0,
            "readback_complete": False,
            "submitted": False,
            "execution_admitted": False,
        },
        "instructions": [
            "Only fill readback values from an authorized, manually observed THS form.",
            "Keep route-unavailable rows pending; do not substitute another instrument or price.",
            "Do not treat this template as a client readback, order receipt or fill receipt.",
        ],
        "rows": readback_rows,
        "source_artifacts": {
            "payload_csv": str(csv_path),
            "payload_summary": str(json_path),
        },
    }
    readback_template_path.write_text(
        json.dumps(readback_template, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary["source_artifacts"]["readback_template"] = str(readback_template_path)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path, summary


def validate_prefill_readback_template(payload: dict) -> dict:
    """Validate a filled batch THS form-readback template without side effects."""
    if not isinstance(payload, dict) or payload.get("schema") != "ths_prefill_readback_template_v1":
        raise ValueError("Expected ths_prefill_readback_template_v1")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Readback template rows must be a list")
    sequences = [row.get("sequence") for row in rows if isinstance(row, dict)]
    if len(sequences) != len(rows) or sequences != list(range(1, len(rows) + 1)):
        raise ValueError("Readback template sequences must be contiguous starting at 1")

    checked = []
    applicable_count = verified_count = pending_count = mismatch_count = 0
    pending_field_count = mismatch_field_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each readback row must be an object")
        expected = row.get("expected") or {}
        received = row.get("readback") or {}
        applicable = bool(row.get("readback_applicable"))
        if not applicable:
            checked.append({"sequence": row["sequence"], "status": "not_applicable"})
            continue
        applicable_count += 1
        statuses = {}
        pending_fields = []
        mismatched_fields = []
        for field in ("symbol_field", "price_field", "qty_field"):
            expected_value = expected.get(field)
            received_value = received.get(field)
            if received_value is None or str(received_value).strip() == "":
                statuses[field] = "pending"
                pending_fields.append(field)
            elif str(received_value).strip() != str(expected_value).strip():
                statuses[field] = "mismatch"
                mismatched_fields.append(field)
            else:
                statuses[field] = "matched"
        pending_field_count += len(pending_fields)
        mismatch_field_count += len(mismatched_fields)
        if mismatched_fields:
            status = "readback_mismatch"
            mismatch_count += 1
        elif pending_fields:
            status = "pending_authorized_client_readback"
            pending_count += 1
        else:
            status = "readback_verified"
            verified_count += 1
        checked.append({
            "sequence": row["sequence"],
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "status": status,
            "field_status": statuses,
            "pending_fields": pending_fields,
            "mismatched_fields": mismatched_fields,
        })
    status = (
        "readback_mismatch" if mismatch_count else
        "pending_authorized_client_readback" if pending_count else
        "readback_verified"
    )
    return {
        "schema": "ths_prefill_readback_validation_v1",
        "status": status,
        "execution_day": payload.get("execution_day"),
        "strategy_id": payload.get("strategy_id"),
        "applicable_row_count": applicable_count,
        "verified_row_count": verified_count,
        "pending_row_count": pending_count,
        "mismatch_row_count": mismatch_count,
        "pending_field_count": pending_field_count,
        "mismatch_field_count": mismatch_field_count,
        "readback_complete": applicable_count == verified_count,
        "submitted": False,
        "execution_admitted": False,
        "client_interacted": bool(payload.get("client_interacted")),
        "rows": checked,
    }


def _money(rows: list[dict[str, str]]) -> str:
    total = sum(Decimal(str(row.get("estimated_notional_rmb") or "0")) for row in rows)
    return f"{total:.2f}"


def _write_paper_account_sizing_projection(
    out: Path,
    orders: list[dict[str, str]],
    execution_config: dict,
    paper_fill_path: Path,
) -> tuple[Path, Path, dict]:
    """Project account-sized limits from declared paper cash, never live cash."""
    fill_rows = _read_csv(paper_fill_path)
    accepted = [
        row for row in fill_rows
        if row.get("execution_status") == "synthetic_full_fill_reference_price"
    ]
    initial_cash = Decimal(str(execution_config.get("initial_cash") or "0")).quantize(Decimal("0.01"))
    buy_gross = sum(
        (Decimal(str(row.get("gross_notional_rmb") or "0")) for row in accepted if row.get("side") == "buy"),
        Decimal("0"),
    )
    sell_gross = sum(
        (Decimal(str(row.get("gross_notional_rmb") or "0")) for row in accepted if row.get("side") == "sell"),
        Decimal("0"),
    )
    fees = sum(
        (Decimal(str(row.get("fees_rmb") or "0")) for row in accepted),
        Decimal("0"),
    )
    cash_delta = sum(
        (Decimal(str(row.get("cash_delta_rmb") or "0")) for row in accepted),
        Decimal("0"),
    )
    buy_cash_delta = sum(
        (Decimal(str(row.get("cash_delta_rmb") or "0")) for row in accepted if row.get("side") == "buy"),
        Decimal("0"),
    )
    cash_after_buys_only = (initial_cash + buy_cash_delta).quantize(Decimal("0.01"))
    projected_cash = (initial_cash + cash_delta).quantize(Decimal("0.01"))
    max_single_buy = max(
        (Decimal(str(row.get("gross_notional_rmb") or "0")) for row in accepted if row.get("side") == "buy"),
        default=Decimal("0"),
    )
    gross_limit = Decimal(str(execution_config.get("gross_limit") or "0"))
    single_limit = Decimal(str(execution_config.get("single_limit") or "0"))
    gross_cap = (initial_cash * gross_limit).quantize(Decimal("0.01"))
    single_cap = (initial_cash * single_limit).quantize(Decimal("0.01"))
    buy_rows = [row for row in orders if row.get("side") == "buy"]
    buy_lot_valid = sum(
        int(int(row.get("qty") or 0) > 0 and int(row.get("qty") or 0) % 100 == 0)
        for row in buy_rows
    )
    checks = [
        {"metric": "paper_initial_cash_rmb", "value": f"{initial_cash:.2f}", "limit": "", "status": "assumption",
         "basis": "strategy execution_config.initial_cash; broker balance not observed"},
        {"metric": "accepted_buy_gross_rmb", "value": f"{buy_gross:.2f}", "limit": f"{gross_cap:.2f}",
         "status": "pass" if buy_gross <= gross_cap else "review",
         "basis": "shared paper-fill reference notional; gross_limit applied to buy exposure"},
        {"metric": "max_single_buy_gross_rmb", "value": f"{max_single_buy:.2f}", "limit": f"{single_cap:.2f}",
         "status": "pass" if max_single_buy <= single_cap else "review",
         "basis": "shared paper-fill reference notional; single_limit applied to one buy"},
        {"metric": "accepted_sell_gross_rmb", "value": f"{sell_gross:.2f}", "limit": "", "status": "informational",
         "basis": "paper sell proceeds; sellable quantity remains unobserved"},
        {"metric": "paper_fees_rmb", "value": f"{fees:.2f}", "limit": "", "status": "informational",
         "basis": "shared Ledger fee policy; no broker fee observation"},
        {"metric": "cash_after_buy_orders_only_rmb", "value": f"{cash_after_buys_only:.2f}",
         "limit": "0.00", "status": "pass" if cash_after_buys_only >= 0 else "review",
         "basis": "initial cash minus accepted buy costs; does not rely on unobserved sell proceeds"},
        {"metric": "projected_cash_after_paper_orders_rmb", "value": f"{projected_cash:.2f}",
         "limit": "", "status": "assumption",
         "basis": "synthetic cash after assumed sell proceeds; sellable quantity remains unobserved"},
        {"metric": "buy_lot_validity", "value": f"{buy_lot_valid}/{len(buy_rows)}", "limit": "all",
         "status": "pass" if buy_lot_valid == len(buy_rows) else "review",
         "basis": "paper A-share 100-share buy-lot rule"},
        {"metric": "paper_account_observed", "value": "false", "limit": "true before release", "status": "blocked",
         "basis": "no same-session account, holdings or sellable quantity was read"},
    ]
    csv_path = out / "paper_account_sizing_projection.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value", "limit", "status", "basis"])
        writer.writeheader()
        writer.writerows(checks)
    summary = {
        "schema": "paper_account_sizing_projection_v1",
        "status": "paper_sizing_only",
        "initial_cash_rmb": f"{initial_cash:.2f}",
        "accepted_order_count": len(accepted),
        "accepted_buy_gross_rmb": f"{buy_gross:.2f}",
        "accepted_sell_gross_rmb": f"{sell_gross:.2f}",
        "fees_rmb": f"{fees:.2f}",
        "cash_after_buys_only_rmb": f"{cash_after_buys_only:.2f}",
        "projected_cash_after_paper_orders_rmb": f"{projected_cash:.2f}",
        "gross_limit": str(gross_limit),
        "single_limit": str(single_limit),
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "assumption": "initial_cash comes from strategy configuration; accepted orders are synthetic reference-price fills",
        "not_attempted": [
            "No broker cash, position or sellable quantity was inferred.",
            "No current quote or execution capacity was inferred.",
            "This artifact cannot authorize prefill, submission or release.",
        ],
        "source_artifacts": {"paper_fill_projection": str(paper_fill_path), "checks": str(csv_path)},
    }
    json_path = out / "paper_account_sizing_projection.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path, summary


def _write_strategy_account_sizing_matrix(
    out: Path,
    strategies: list[dict],
    orders: list[dict[str, str]],
    preflight: list[dict[str, str]],
    execution_day: str,
) -> tuple[Path, Path]:
    """Compare paper account-sized limits per candidate, never across candidates."""
    from .trader_desk_snapshot import _write_paper_fill_projection

    rows = []
    with tempfile.TemporaryDirectory(prefix="paper_strategy_sizing_") as temporary:
        temporary_root = Path(temporary)
        for strategy in strategies:
            strategy_id = strategy.get("strategy_id", "")
            strategy_orders = [row for row in orders if row.get("strategy_id") == strategy_id]
            strategy_preflight = [row for row in preflight if row.get("strategy_id") == strategy_id]
            config = strategy.get("execution_config") or {}
            if not strategy_orders or not strategy_preflight:
                rows.append({
                    "strategy_id": strategy_id,
                    "candidate_order_count": len(strategy_orders),
                    "preflight_accepted": "",
                    "accepted_buy_gross_rmb": "",
                    "gross_cap_rmb": "",
                    "gross_headroom_rmb": "",
                    "max_single_buy_rmb": "",
                    "single_cap_rmb": "",
                    "single_headroom_rmb": "",
                    "projected_cash_after_orders_rmb": "",
                    "cash_after_buys_only_rmb": "",
                    "risk_status": "missing_paper_inputs",
                    "account_observed": "false",
                    "execution_admitted": "false",
                })
                continue
            strategy_out = temporary_root / strategy_id
            strategy_out.mkdir(parents=True, exist_ok=True)
            fill_path, *_ = _write_paper_fill_projection(
                strategy_out, strategy_orders, strategy_preflight, config, execution_day
            )
            sizing_csv, _, sizing = _write_paper_account_sizing_projection(
                strategy_out, strategy_orders, config, fill_path
            )
            sizing_checks = {
                row.get("metric"): row
                for row in _read_csv(sizing_csv)
            }
            buy_check = sizing_checks.get("accepted_buy_gross_rmb", {})
            single_check = sizing_checks.get("max_single_buy_gross_rmb", {})
            cash_check = sizing_checks.get("cash_after_buy_orders_only_rmb", {})
            risk_status = "review" if "review" in {
                buy_check.get("status"), single_check.get("status"), cash_check.get("status")
            } else "pass"
            buy_value = Decimal(buy_check.get("value") or "0")
            gross_cap = Decimal(buy_check.get("limit") or "0")
            single_value = Decimal(single_check.get("value") or "0")
            single_cap = Decimal(single_check.get("limit") or "0")
            rows.append({
                "strategy_id": strategy_id,
                "candidate_order_count": len(strategy_orders),
                "preflight_accepted": sum(row.get("preflight_status") == "accepted" for row in strategy_preflight),
                "accepted_buy_gross_rmb": sizing.get("accepted_buy_gross_rmb", ""),
                "gross_cap_rmb": f"{gross_cap:.2f}",
                "gross_headroom_rmb": f"{(gross_cap - buy_value):.2f}",
                "max_single_buy_rmb": f"{single_value:.2f}",
                "single_cap_rmb": f"{single_cap:.2f}",
                "single_headroom_rmb": f"{(single_cap - single_value):.2f}",
                "projected_cash_after_orders_rmb": sizing.get("projected_cash_after_paper_orders_rmb", ""),
                "cash_after_buys_only_rmb": sizing.get("cash_after_buys_only_rmb", ""),
                "risk_status": risk_status,
                "account_observed": "false",
                "execution_admitted": "false",
            })
    fields = ["strategy_id", "candidate_order_count", "preflight_accepted", "accepted_buy_gross_rmb",
              "gross_cap_rmb", "gross_headroom_rmb", "max_single_buy_rmb", "single_cap_rmb",
              "single_headroom_rmb", "cash_after_buys_only_rmb", "projected_cash_after_orders_rmb", "risk_status",
              "account_observed", "execution_admitted"]
    csv_path = out / "strategy_account_sizing_matrix.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    json_path = out / "strategy_account_sizing_matrix.json"
    json_path.write_text(json.dumps({
        "schema": "strategy_account_sizing_matrix_v1",
        "status": "paper_strategy_comparison",
        "selection_policy": "comparison_only; no automatic winner selection",
        "execution_day": execution_day,
        "account_observed": False,
        "quotes_observed": False,
        "execution_admitted": False,
        "strategies": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def build(opening_pack: str | Path, strategy_id: str, destination: str | Path) -> tuple[Path, dict]:
    pack_path = Path(opening_pack).resolve()
    pack = json.loads(pack_path.read_text(encoding="utf-8-sig"))
    if pack.get("schema") != "a_share_opening_pack_v1" or pack.get("execution_admitted") is not False:
        raise ValueError("An explicitly non-admitted a_share_opening_pack_v1 is required")
    strategies = [row for row in pack.get("strategies") or [] if row.get("strategy_id") == strategy_id]
    if len(strategies) != 1:
        available = [row.get("strategy_id") for row in pack.get("strategies") or []]
        raise ValueError(f"Strategy is not unique: {strategy_id}; available={available}")
    selected = strategies[0]
    artifacts = pack.get("artifacts") or {}
    required = ("candidate_orders", "candidate_targets", "ledger_preflight_by_strategy", "strategy_summary")
    missing = [name for name in required if not artifacts.get(name)]
    if missing:
        raise ValueError(f"Opening pack is missing artifacts: {missing}")
    paths = {name: Path(artifacts[name]).resolve() for name in required}
    if any(not path.exists() for path in paths.values()):
        raise FileNotFoundError(f"Opening pack artifact missing: {paths}")

    orders = [row for row in _read_csv(paths["candidate_orders"]) if row.get("strategy_id") == strategy_id]
    targets = [row for row in _read_csv(paths["candidate_targets"]) if row.get("strategy_id") == strategy_id]
    preflight = [row for row in _read_csv(paths["ledger_preflight_by_strategy"]) if row.get("strategy_id") == strategy_id]
    summary_rows = [row for row in _read_csv(paths["strategy_summary"]) if row.get("strategy_id") == strategy_id]
    if not orders or not targets or not preflight or len(summary_rows) != 1:
        raise ValueError("Selected strategy does not have a complete frozen paper set")
    summary = summary_rows[0]
    accepted = [row for row in preflight if row.get("preflight_status") == "accepted"]
    rejected = [row for row in preflight if row.get("preflight_status") != "accepted"]
    out = Path(destination).resolve()
    out.mkdir(parents=True, exist_ok=False)
    orders_path = out / "selected_orders.csv"
    targets_path = out / "selected_targets.csv"
    preflight_path = out / "selected_preflight.csv"
    _write_csv(orders_path, orders)
    _write_csv(targets_path, targets)
    _write_csv(preflight_path, preflight)
    from .trader_desk_snapshot import (
        _write_allocation_projection,
        _write_paper_fill_projection,
        _write_paper_next_session_carry,
        _write_paper_release_decision,
        _write_target_order_reconciliation,
    )
    fill_path, ledger_path, lifecycle_path, position_path, reconciliation_path, fill_summary = (
        _write_paper_fill_projection(
            out, orders, preflight, selected.get("execution_config") or {}, pack.get("execution_day")
        )
    )
    allocation_path = _write_allocation_projection(out, orders, preflight)
    carry_path = _write_paper_next_session_carry(
        out, fill_path, position_path, ledger_path, pack.get("execution_day")
    )
    account_sizing_path, account_sizing_json_path, account_sizing_summary = (
        _write_paper_account_sizing_projection(
            out, orders, selected.get("execution_config") or {}, fill_path
        )
    )
    target_reconciliation_path = _write_target_order_reconciliation(out, targets_path, orders)
    prefill_payload_path, prefill_payload_json_path, prefill_payload_summary = (
        _write_offline_prefill_payloads(out, orders, preflight, pack.get("execution_day"))
    )
    release_path = _write_paper_release_decision(
        out, orders, preflight, target_reconciliation_path, False, False
    )
    release_rows = _read_csv(release_path)
    release_counts = {}
    for row in release_rows:
        decision = row.get("paper_release_decision")
        if decision and decision != "summary":
            release_counts[decision] = release_counts.get(decision, 0) + 1
    plan = {
        "schema": "offline_selected_strategy_plan_v1",
        "status": "paper_only_manual_strategy_selection",
        "created_at": datetime.now().astimezone().isoformat(),
        "strategy_id": strategy_id,
        "selection_policy": "explicit_operator_argument; no automatic winner selection",
        "source_cutoff": pack.get("source_cutoff"),
        "execution_day": pack.get("execution_day"),
        "execution_scope": selected.get("execution_scope"),
        "reference_price_basis": "frozen opening-pack research price; not a current RMB quote",
        "target_holdings_observed": False,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "execution_admitted": False,
        "automatic_strategy_selection": False,
        "summary": {
            "candidate_order_count": len(orders),
            "buy_count": sum(row.get("side") == "buy" for row in orders),
            "sell_count": sum(row.get("side") == "sell" for row in orders),
            "estimated_notional_rmb": _money(orders),
            "ledger_preflight_count": len(preflight),
            "ledger_preflight_accepted": len(accepted),
            "ledger_preflight_rejected": len(rejected),
            "comparison_return": summary.get("comparison_return"),
            "comparison_max_drawdown": summary.get("comparison_max_drawdown"),
            "comparison_sharpe": summary.get("comparison_sharpe"),
        },
        "strategy_metadata": selected,
        "source_artifacts": {name: str(path) for name, path in paths.items()},
        "selected_artifacts": {
            "orders": str(orders_path),
            "targets": str(targets_path),
            "preflight": str(preflight_path),
            "paper_fill_projection": str(fill_path),
            "paper_ledger_projection": str(ledger_path),
            "paper_order_lifecycle": str(lifecycle_path),
            "paper_position_projection": str(position_path),
            "paper_reconciliation": str(reconciliation_path),
            "allocation_settlement_projection": str(allocation_path),
            "paper_next_session_carry": str(carry_path),
            "target_order_reconciliation": str(target_reconciliation_path),
            "paper_release_decision": str(release_path),
            "ths_prefill_payloads": str(prefill_payload_path),
            "ths_prefill_payloads_summary": str(prefill_payload_json_path),
            "ths_prefill_readback_template": str(out / "ths_prefill_readback_template.json"),
            "paper_account_sizing_projection": str(account_sizing_path),
            "paper_account_sizing_summary": str(account_sizing_json_path),
        },
        "paper_rehearsal": fill_summary,
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "ledger_mutated": False,
        "submission_attempted": False,
        "paper_release_summary": release_counts,
        "ths_prefill_payloads": prefill_payload_summary,
        "paper_account_sizing": account_sizing_summary,
        "next_required_evidence": [
            "same-session normalized account observation",
            "same-session raw RMB quote observation",
            "manual review before any THS prefill",
        ],
        "not_attempted": [
            "No account balance, sellable quantity or current quote was inferred.",
            "No journal plan was saved.",
            "No THS client operation, prefill, submission, cancellation or transfer was attempted.",
        ],
    }
    json_path = out / "offline_strategy_plan.json"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# Offline Selected Strategy Plan",
        "",
        f"Strategy: {strategy_id}; cutoff: {plan['source_cutoff']}; execution day: {plan['execution_day']}.",
        f"Orders: {len(orders)}; buys: {plan['summary']['buy_count']}; sells: {plan['summary']['sell_count']}; "
        f"frozen reference notional: {plan['summary']['estimated_notional_rmb']} RMB.",
        f"Ledger preflight: {len(accepted)} accepted / {len(rejected)} rejected; no account or quote observation.",
        "This is an explicit paper selection, not an automatic performance winner or live order plan.",
        f"JSON: {json_path}",
        f"Orders: {orders_path}",
        f"Targets: {targets_path}",
        f"Preflight: {preflight_path}",
        f"Paper fills: {fill_path}",
        f"Paper positions: {position_path}",
        f"Paper reconciliation: {reconciliation_path}",
        f"Allocation projection: {allocation_path}",
        f"Next-session carry: {carry_path}",
        f"Target/order reconciliation: {target_reconciliation_path}",
        f"Offline THS prefill payloads: {prefill_payload_path}",
        f"Offline THS prefill payload summary: {prefill_payload_json_path}",
        f"Offline THS readback template: {out / 'ths_prefill_readback_template.json'}",
        f"Paper release decision: {release_path}",
        f"Paper account sizing projection: {account_sizing_path}",
        f"Paper account sizing summary: {account_sizing_json_path}",
        f"Paper release summary: {json.dumps(release_counts, ensure_ascii=False, sort_keys=True)}.",
        f"Internal paper reconciliation: {fill_summary['internal_reconciliation_status']}; live broker reconciliation: {fill_summary['live_reconciliation_status']}.",
    ]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return json_path, plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opening-pack", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    json_path, plan = build(args.opening_pack, args.strategy, args.out)
    print(json.dumps({
        "output": str(json_path.parent),
        "strategy_id": plan["strategy_id"],
        "orders": plan["summary"]["candidate_order_count"],
        "execution_admitted": plan["execution_admitted"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
