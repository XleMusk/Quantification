"""Pure application views for read-only account and order risk projections.

The HTTP adapter supplies validated packages and reconciliation snapshots.  The
views only join those values with the existing THS risk arithmetic; they never
open a client, mutate a Ledger, or authorize an order.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _native_observation(package: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": package.get("status", "missing"),
        "source_scope": package.get("source_scope", "missing"),
        "source_file": package.get("source_file"),
        "account_observed": bool(package.get("account_observed")),
        "orders_observed": bool(package.get("orders_observed")),
        "trades_observed": bool(package.get("trades_observed")),
        "cashflows_observed": bool(package.get("cashflows_observed")),
        "ready_for_ledger_reconciliation": bool(
            package.get("ready_for_ledger_reconciliation")
        ),
        "unknowns": package.get("unknowns") or [],
    }


def build_account_risk_view(
    *,
    package: Mapping[str, Any],
    complete_package: Mapping[str, Any],
    native_package: Mapping[str, Any],
    quote_preview: Mapping[str, Any],
    quote_observation: Mapping[str, Any] | None,
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    same_ledger_compat_fields: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    account_risk_builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Join one read-only account package with its persisted observations."""

    result = account_risk_builder(
        package,
        quote_observation=quote_observation,
        reconciliation=same_ledger_bundle_reconciliation,
    )
    if complete_package.get("source_scope") == "isolated_rehearsal":
        result["isolated_rehearsal_view"] = account_risk_builder(
            complete_package,
            quote_observation=quote_observation,
            reconciliation=same_ledger_bundle_reconciliation,
        )
    result["native_observation"] = _native_observation(native_package)
    result["primary_evidence"] = package.get("source_scope") or "missing"
    result.update(
        {
            "source_file": package.get("source_file"),
            "source_paths": package.get("source_paths") or {},
            "quote_observation": {
                "status": quote_preview.get("status"),
                "source": quote_preview.get("source"),
                "observed_at": quote_preview.get("observed_at"),
                "trading_date": quote_preview.get("trading_date"),
                "price_count": quote_preview.get("price_count", 0),
                "missing_symbols": quote_preview.get("missing_symbols") or [],
                "canonical_inputs_ready": bool(quote_preview.get("canonical_inputs_ready")),
            },
            "basis": "validated THS account export plus optional raw RMB quote observation; read-only",
            "ths_journal_reconciliation": journal_reconciliation,
            "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
            **same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation),
            "ledger_mutated": bool(journal_reconciliation["ledger_mutated"]),
            "next_action": (
                "use the existing explicit same-Ledger preview/import for this persistent export"
                if result.get("source_scope") == "persistent_user_export"
                and result.get("ledger_binding", {}).get("ready_for_ledger_reconciliation")
                and not result.get("ledger_binding", {}).get("ledger_reconciled")
                else "capture a complete persistent THS account/orders/trades/cashflows export"
                if result.get("status")
                in {"blocked_missing_or_invalid_account_evidence", "isolated_rehearsal_only"}
                else "review risk flags; no order submission is enabled"
            ),
        }
    )
    return result


def build_order_risk_view(
    *,
    package: Mapping[str, Any],
    native_package: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    quote_observation: Mapping[str, Any] | None,
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    same_ledger_compat_fields: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    order_risk_builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Project one explicit order batch against the same read-only package."""

    result = order_risk_builder(
        package,
        orders,
        quote_observation=quote_observation,
        reconciliation=same_ledger_bundle_reconciliation,
    )
    result["primary_evidence"] = package.get("source_scope") or "missing"
    result["native_observation"] = _native_observation(native_package)
    result["ths_journal_reconciliation"] = journal_reconciliation
    result["same_ledger_bundle_reconciliation"] = same_ledger_bundle_reconciliation
    result.update(
        same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation)
    )
    result["ledger_mutated"] = bool(journal_reconciliation["ledger_mutated"])
    return result


__all__ = ["build_account_risk_view", "build_order_risk_view"]
