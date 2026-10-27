"""Pure application view for broker/THS observation alerts.

The adapter supplies the saved alert, reconciliation and readiness snapshots.
This module only joins those observations and never contacts a broker, retries
an unknown result, or mutates the Ledger.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def build_view(
    *,
    alerts: Mapping[str, Any],
    journal_reconciliation: Mapping[str, Any],
    same_ledger_bundle_reconciliation: Mapping[str, Any],
    persistent_readiness: Mapping[str, Any],
    same_ledger_compat_fields: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Join saved observation alerts without creating account evidence."""

    profile = persistent_readiness.get("profile") or {}
    mapping = profile.get("mapping_validation") or {}
    source_scope = same_ledger_bundle_reconciliation.get("source_scope")
    manifest_status = persistent_readiness.get("source_manifest_status")
    bundle_is_account_evidence = bool(
        persistent_readiness.get("status") == "bundle_ready_for_reconciliation"
        and manifest_status == "verified"
    )
    alert_rows = alerts.get("alerts") or []
    live_alerts = bool(alerts.get("live_alerts"))
    if alert_rows and not live_alerts:
        next_action = (
            "treat the listed differences as synthetic rehearsal evidence; provide a verified persistent THS export before account-level recovery"
        )
    elif alert_rows:
        next_action = "resolve the listed broker/Ledger differences before any recovery"
    else:
        next_action = (
            "retain the explicit wait state; provide a verified persistent THS export before treating alerts as broker evidence"
        )
    return {
        **dict(alerts),
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        "persistent_bundle_readiness": {
            "status": persistent_readiness.get("status"),
            "preview_status": (persistent_readiness.get("preview") or {}).get("status"),
            "source_manifest_status": manifest_status,
            "source_manifest_issues": persistent_readiness.get("source_manifest_issues") or [],
            "profile_mapping_status": mapping.get("status"),
            "profile_mapping_issues": mapping.get("issues") or [],
            "bundle_is_account_evidence": bundle_is_account_evidence,
            "execution_admitted": False,
            "ledger_mutated": False,
        },
        "alert_source_selection": {
            "current_persistent_bundle_status": persistent_readiness.get("status"),
            "reconciliation_source_scope": source_scope,
            "reconciliation_source_manifest_status": same_ledger_bundle_reconciliation.get(
                "source_manifest_status"
            ),
            "reconciliation_is_account_evidence": bool(
                source_scope == "persistent_user_export"
                and same_ledger_bundle_reconciliation.get("source_manifest_status") == "verified"
            ),
            "synthetic_source_is_not_account_evidence": source_scope != "persistent_user_export",
            "basis": "current persistent bundle readiness is authoritative for account evidence; isolated rehearsal remains diagnostic only",
        },
        **same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation),
        "ledger_mutated": bool(journal_reconciliation["ledger_mutated"]),
        "client_interacted": False,
        "next_action": next_action,
    }


__all__ = ["build_view"]
