"""Materialize F2 publication-sidecar rows into typed financial facts.

The bridge is intentionally read-only and fail-closed. It consumes one
``f01_publication_sidecar_v1`` receipt, verifies the publication contract,
normalizes explicit unit metadata, and emits a machine-readable receipt. A
row whose historical as-of evidence is missing, whose scope/unit metadata is
ambiguous, or whose availability precedes the publication window remains
``BLOCKED_DATA``. No row is promoted to a factor, target, order, or account
replay by this module.

The typed materialization path closes the gap between the existing
sidecar/coverage receipts and :class:`FinancialFact`. It is suitable for a
later F01 consumer once a prior comparable fact is supplied; it does not infer
that prior value from a provider CSV or from the current row.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .f01_financial_factor import FinancialFact
from .financial_factor_contracts import TimeWindow, publication_interval
from .financial_units import normalize_amount
from .f2_historical_evidence import BLOCKED as HISTORICAL_BLOCKED, READY as HISTORICAL_READY, SCHEMA as HISTORICAL_SCHEMA
from .publication_event_contract import evaluate_publication_evidence


SCHEMA = "f2_sidecar_fact_bridge_v1"
SIDE_CAR_SCHEMA = "f01_publication_sidecar_v1"
READY = "FACTS_READY_NO_FACTOR"
BLOCKED = "BLOCKED_DATA"
_REQUIRED_FIELDS = ("parent_net_profit", "total_assets")


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return parsed


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must be offset-aware")
    return parsed


def _period_bounds(period: Any, stat_date: Any) -> tuple[date, date]:
    """Resolve a quarterly identity without guessing a missing report date."""

    end = _date(stat_date, "stat_date")
    if not isinstance(period, str) or len(period) != 6 or period[4] != "Q":
        raise ValueError("period must be YYYYQn")
    try:
        year, quarter = int(period[:4]), int(period[5])
    except ValueError as exc:
        raise ValueError("period must be YYYYQn") from exc
    if quarter not in (1, 2, 3, 4):
        raise ValueError("period quarter must be 1..4")
    expected_month = quarter * 3
    # A report's stat_date is authoritative for the end of the fact period;
    # reject a mismatched quarter instead of silently relabelling it.
    if end.year != year or end.month != expected_month:
        raise ValueError("period and stat_date are inconsistent")
    next_month = date(year + (expected_month == 12), (expected_month % 12) + 1, 1)
    if end != next_month - timedelta(days=1):
        raise ValueError("stat_date must be the quarter end")
    start = date(year, expected_month - 2, 1)
    return start, end


def _scope_metadata(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None, list[str]]:
    """Read explicit scope/currency/unit metadata; never infer an unverified scope."""

    reasons: list[str] = []
    scope = row.get("scope_group") or row.get("scope")
    currency = row.get("currency")
    unit = row.get("unit")
    if not isinstance(scope, str) or not scope.strip():
        reasons.append("scope_missing")
        scope = None
    if not isinstance(currency, str) or not currency.strip():
        reasons.append("currency_missing")
        currency = None
    if not isinstance(unit, str) or not unit.strip():
        reasons.append("unit_missing")
        unit = None
    return scope, currency, unit, reasons


def _row_facts(row: Mapping[str, Any], *, source_row: Mapping[str, Any] | None = None,
               decision_day: str | None = None,
               historical_row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    symbol = row.get("symbol")
    period = row.get("period")
    reasons: list[str] = []
    if not isinstance(symbol, str) or not symbol.strip():
        reasons.append("symbol_missing")
    if not isinstance(period, str) or not period.strip():
        reasons.append("period_missing")

    if historical_row is None:
        reasons.append("historical_evidence_missing")
    elif historical_row.get("status") != HISTORICAL_READY or historical_row.get("pit_asof_safe") is not True:
        historical_reasons = historical_row.get("reason_codes")
        if isinstance(historical_reasons, list) and historical_reasons:
            reasons.extend(f"historical:{value}" for value in historical_reasons)
        else:
            reasons.append("historical:pit_asof_unproven")
    try:
        period_start, period_end = _period_bounds(period, row.get("stat_date"))
    except (TypeError, ValueError) as exc:
        period_start = period_end = None
        reasons.append(f"period_invalid:{exc}")

    # Re-evaluate the publication contract from row-level evidence instead of
    # trusting a cached boolean.  A caller must prove the decision cutoff,
    # retrieval/as-of timestamps, revision identity and source hash at the
    # bridge boundary; a forged ``pit_asof_safe`` flag cannot promote a fact.
    evaluation = row.get("contract_evaluation")
    if isinstance(decision_day, str) and decision_day.strip():
        try:
            evaluation = evaluate_publication_evidence(row, decision_day)
        except (TypeError, ValueError) as exc:
            evaluation = {"pit_asof_safe": False,
                          "publication_evidence_reasons": [f"contract_invalid:{exc}"]}
    if not isinstance(evaluation, Mapping):
        reasons.append("publication_contract_missing")
        evaluation = {}
    if evaluation.get("pit_asof_safe") is not True:
        contract_reasons = evaluation.get("publication_evidence_reasons")
        if isinstance(contract_reasons, list) and contract_reasons:
            reasons.extend(f"publication:{value}" for value in contract_reasons)
        else:
            reasons.append("publication:pit_asof_unproven")

    publication_date = row.get("publication_date")
    try:
        event_window = publication_interval(publication_date)
    except (TypeError, ValueError) as exc:
        event_window = None
        reasons.append(f"publication_date_invalid:{exc}")

    availability: TimeWindow | None = None
    if evaluation.get("pit_asof_safe") is True:
        try:
            asof = _timestamp(row.get("asof_at"), "asof_at")
            if event_window is not None and asof < event_window.end:
                reasons.append("asof_before_publication_window_end")
            availability = TimeWindow.exact(asof)
        except (TypeError, ValueError) as exc:
            reasons.append(f"available_at_invalid:{exc}")

    scope, currency, unit, metadata_reasons = _scope_metadata(row)
    reasons.extend(metadata_reasons)
    # A scoped source receipt can prove that values are expressed in RMB yuan.
    # It cannot prove issuer scope, so it only fills the unit/currency pair.
    checks = source_row.get("source_checks") if isinstance(source_row, Mapping) else None
    if unit is None and isinstance(checks, Mapping) and checks.get("unit_explicit_as_rmb_yuan") is True:
        unit = "yuan"
        reasons = [value for value in reasons if value != "unit_missing"]
        if currency is None:
            currency = "CNY"
            reasons = [value for value in reasons if value != "currency_missing"]

    facts: list[dict[str, Any]] = []
    if not reasons:
        assert period_start is not None and period_end is not None and availability is not None
        for metric in _REQUIRED_FIELDS:
            raw = row.get(metric)
            try:
                normalized = normalize_amount(raw, unit, target_unit="yuan")
            except (TypeError, ValueError) as exc:
                reasons.append(f"{metric}_invalid:{exc}")
                continue
            fact = FinancialFact(
                fact_id=f"{symbol}:{period}:{metric}:v1",
                issuer_id=symbol,
                metric_id=metric,
                period_start=period_start,
                period_end=period_end,
                period_kind="single_quarter",
                scope_group=scope,
                currency=currency,
                unit="yuan",
                value=normalized["value"],
                available=availability,
            )
            facts.append({
                **fact_to_dict(fact),
                "raw_value": str(normalized["raw_value"]),
                "raw_unit": normalized["raw_unit"],
                "conversion": normalized["conversion"],
            })

    status = READY if not reasons and len(facts) == len(_REQUIRED_FIELDS) else BLOCKED
    if status == BLOCKED and not reasons:
        reasons.append("required_fact_materialization_incomplete")
    return {
        "symbol": symbol,
        "period": period,
        "status": status,
        "reason_codes": reasons,
        "event_window": event_window.to_dict() if event_window else None,
        "available_at": availability.to_dict() if availability else None,
        "facts": facts,
        "factor_status": "NOT_COMPUTED",
        "execution_admitted": False,
    }


def fact_to_dict(fact: FinancialFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "issuer_id": fact.issuer_id,
        "metric_id": fact.metric_id,
        "period_start": fact.period_start.isoformat(),
        "period_end": fact.period_end.isoformat(),
        "period_kind": fact.period_kind,
        "scope_group": fact.scope_group,
        "currency": fact.currency,
        "unit": fact.unit,
        "value": str(fact.value) if fact.value is not None else None,
        "available_at": fact.available.to_dict(),
        "status": fact.status,
    }


def build_fact_bridge(sidecar: str | Path, *, scoped_receipt: str | Path | None = None,
                      historical_evidence: str | Path | None = None) -> dict[str, Any]:
    """Build a deterministic typed-fact receipt from one publication sidecar."""

    sidecar_path = Path(sidecar).expanduser().resolve()
    payload = _read_json(sidecar_path)
    if not isinstance(payload, Mapping) or payload.get("schema") != SIDE_CAR_SCHEMA:
        raise ValueError(f"expected {SIDE_CAR_SCHEMA}")
    if payload.get("execution_admitted") is not False:
        raise ValueError("sidecar execution_admitted must remain false")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("sidecar rows must be a list")

    source_rows: dict[str, Mapping[str, Any]] = {}
    scoped_path: Path | None = None
    if scoped_receipt is not None:
        scoped_path = Path(scoped_receipt).expanduser().resolve()
        scoped = _read_json(scoped_path)
        for item in scoped.get("rows_detail", []) if isinstance(scoped, Mapping) else []:
            if isinstance(item, Mapping):
                source_rows[f"{item.get('symbol')}:{item.get('period')}"] = item

    historical_rows: dict[str, Mapping[str, Any]] = {}
    historical_path: Path | None = None
    if historical_evidence is not None:
        historical_path = Path(historical_evidence).expanduser().resolve()
        historical = _read_json(historical_path)
        if not isinstance(historical, Mapping) or historical.get("schema") != HISTORICAL_SCHEMA:
            raise ValueError(f"expected {HISTORICAL_SCHEMA}")
        for item in historical.get("rows", []) if isinstance(historical.get("rows"), list) else []:
            if isinstance(item, Mapping):
                historical_rows[f"{item.get('symbol')}:{item.get('period')}"] = item

    materialized: list[dict[str, Any]] = []
    decision_day = payload.get("decision_day")
    for row in rows:
        if not isinstance(row, Mapping):
            materialized.append({"status": BLOCKED, "reason_codes": ["row_not_mapping"], "facts": [], "execution_admitted": False})
            continue
        key = f"{row.get('symbol')}:{row.get('period')}"
        materialized.append(_row_facts(
            row,
            source_row=source_rows.get(key),
            decision_day=decision_day,
            historical_row=historical_rows.get(key),
        ))

    reason_counts = Counter(reason for item in materialized for reason in item.get("reason_codes", []))
    ready = sum(item.get("status") == READY for item in materialized)
    blocked = len(materialized) - ready
    status = READY if ready and blocked == 0 else BLOCKED
    return {
        "schema": SCHEMA,
        "status": status,
        "sidecar": str(sidecar_path),
        "sidecar_sha256": _sha256(sidecar_path),
        "scoped_receipt": str(scoped_path) if scoped_path else None,
        "scoped_receipt_sha256": _sha256(scoped_path) if scoped_path and scoped_path.is_file() else None,
        "historical_evidence": str(historical_path) if historical_path else None,
        "historical_evidence_sha256": _sha256(historical_path) if historical_path and historical_path.is_file() else None,
        "decision_day": payload.get("decision_day"),
        "rows": materialized,
        "summary": {
            "rows": len(materialized),
            "ready_rows": ready,
            "blocked_rows": blocked,
            "fact_rows": sum(len(item.get("facts", [])) for item in materialized),
            "reason_counts": dict(sorted(reason_counts.items())),
        },
        "execution_admitted": False,
        "account_replay": False,
        "factor_computed": False,
        "limitations": [
            "No prior comparable fact is inferred; F01 remains NOT_COMPUTED.",
            "Rows without revision-safe historical as-of evidence remain BLOCKED_DATA.",
            "This receipt does not create targets, orders, fills, or broker actions.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--scoped-receipt", type=Path)
    parser.add_argument("--historical-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_fact_bridge(
        args.sidecar,
        scoped_receipt=args.scoped_receipt,
        historical_evidence=args.historical_evidence,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BLOCKED", "READY", "SCHEMA", "build_fact_bridge", "fact_to_dict"]
