"""Read-only application view for dated THS export templates.

The workbench uses this view to show the exact offline import contract.  It
never reads an account, opens a client, imports a file or mutates the Ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project manual statement templates and saved paper reconciliation."""

    settings = namespace["settings"]
    operator_package = namespace["operator_package"]
    session_root = namespace["_operator_package_session_root"]
    latest_structured = namespace["_latest_structured_observation_package"]

    package = operator_package()
    execution_day = str(package.get("execution_day"))
    run_root = session_root(package)
    template_specs = [
        {
            "kind": "core_observation_bundle",
            "basis": "account_positions_orders_trades_cashflows",
            "columns": ["account_json", "orders_csv", "trades_csv", "cashflows_csv"],
            "field_columns": {
                "account": ["account_alias", "observed_at", "trading_date", "cash_total", "cash_frozen", "cash_available", "positions"],
                "orders": ["symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"],
                "trades": ["symbol", "market", "side", "order_id", "execution_id", "qty", "price"],
                "cashflows": ["symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"],
            },
            "templates": {
                "account": run_root / "account_observation.template.json",
                "orders": run_root / "orders_observation.template.csv",
                "trades": run_root / "trades_observation.template.csv",
                "cashflows": run_root / "cashflows_observation.template.csv",
            },
            "follow_up": "refresh the workbench and click the explicit local import action after review",
        },
        {
            "kind": "cashflows",
            "basis": "cumulative_order_cashflow",
            "columns": ["symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"],
            "template": run_root / "cashflows_observation.template.csv",
            "follow_up": None,
        },
        {
            "kind": "movements",
            "basis": "nontrade_cash_movements",
            "columns": ["movement_id", "kind", "cash_change"],
            "template": run_root / "movements_observation.template.csv",
            "follow_up": None,
        },
        {
            "kind": "restriction_releases",
            "basis": "opening_restriction_releases",
            "columns": ["release_id", "symbol", "market", "released_qty", "release_day", "reason"],
            "template": run_root / "restriction_releases_observation.template.csv",
            "follow_up": "reconcile-session --include-restriction-releases",
        },
        {
            "kind": "share_credits",
            "basis": "account_bonus_share_credits",
            "columns": ["credit_id", "symbol", "market", "credited_qty", "credit_day", "available_day", "kind"],
            "template": run_root / "share_credits_observation.template.csv",
            "follow_up": "reconcile-session --include-share-credits",
        },
    ]
    rows = []
    for spec in template_specs:
        if spec["kind"] == "core_observation_bundle":
            templates = spec["templates"]
            template = templates["account"]
            template_exists = all(path.is_file() for path in templates.values())
            bundle_root = (settings.DATA_ROOT / "ths" / "observation_bundle").resolve()
            command = (
                f"copy current THS exports into {bundle_root} as "
                "account_observation.json, orders.csv, trades.csv and cashflows.csv; "
                "refresh the workbench, review the bundle and click the explicit local import action"
            )
        else:
            template = spec["template"]
            template_exists = template.is_file()
            command = (
                "D:\\Quantification\\env\\python.exe D:\\Quantification\\run_committee.py ths "
                f"import-export --kind {spec['kind']} --profile <verified_mapping.json> "
                f"--csv {template} --trading-date {execution_day} --observed-at <actual_export_time+08:00>"
            )
        rows.append({
            "kind": spec["kind"],
            "source": "ths_user_export",
            "basis": spec["basis"],
            "columns": spec["columns"],
            "template": str(template),
            "templates": {key: str(path) for key, path in spec.get("templates", {}).items()},
            "template_exists": template_exists,
            "import_command": command,
            "follow_up": spec["follow_up"],
            "execution_admitted": False,
        })
    structured_package = latest_structured()
    structured_reconciliation = structured_package.get("order_reconciliation") or {}
    return {
        "schema": "offline_manual_statement_view_v1",
        "status": "manual_export_templates_ready" if all(row["template_exists"] for row in rows) else "manual_export_templates_missing",
        "execution_day": execution_day,
        "source_cutoff": package.get("source_cutoff"),
        "rows": rows,
        "structured_observation_package": structured_package,
        "structured_package_ready_for_ledger_reconciliation": bool(structured_package.get("ready_for_ledger_reconciliation")),
        "order_reconciliation": structured_reconciliation,
        "order_reconciliation_status": structured_reconciliation.get("status"),
        "order_count": structured_reconciliation.get("order_count"),
        "trade_count": structured_reconciliation.get("trade_count"),
        "cashflow_count": structured_reconciliation.get("cashflow_count"),
        "client_interacted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ledger_mutated": False,
        "execution_admitted": False,
        "basis": "empty operator templates only; no account values, client readback or ledger import",
    }


__all__ = ["build_view"]
