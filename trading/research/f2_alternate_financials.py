"""Adapt the alternate public financial cache into an F2 sidecar.

The adapter preserves provider field timing and semantic uncertainty.  It is
an input bridge only: current retrieval is never backdated into PIT evidence.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


SCHEMA = "f01_publication_sidecar_v1"
STATUS = "ALTERNATE_PROVIDER_MAPPED_PIT_BLOCKED"
REQUIRED = {"parent_net_profit": ("lrb", "PARENETP"), "total_assets": ("fzb", "TOTASSET")}


def _period(day: str) -> str | None:
    try:
        value = date.fromisoformat(str(day))
    except ValueError:
        return None
    if value.month not in (3, 6, 9, 12):
        return None
    return f"{value.year}Q{value.month // 3}"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _ref(path: Path) -> dict[str, Any]:
    digest = sha256(path.read_bytes()).hexdigest()
    return {"path": str(path.resolve()), "sha256": digest, "bytes": path.stat().st_size}


def build_sidecar(root: str | Path, *, decision_day: str) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    result = _json(root / "result.json")
    if result.get("execution_admitted") is not False:
        raise ValueError("alternate financial cache crossed execution boundary")
    items_path = root / "statement_items.parquet"
    items = pd.read_parquet(items_path)
    rows: list[dict[str, Any]] = []
    for (symbol, report_day), group in items.groupby(["symbol", "report_day"], sort=True):
        period = _period(report_day)
        if period is None:
            continue
        row: dict[str, Any] = {"symbol": str(symbol), "period": period,
                               "stat_date": str(report_day),
                               "decision_day": decision_day,
                               "source_kind": "alternate_financials_sina",
                               "source_root": str(root)}
        field_publication_dates: dict[str, str | None] = {}
        field_update_times: dict[str, Any] = {}
        missing: list[str] = []
        for field, (statement, item_field) in REQUIRED.items():
            found = group[(group["statement"] == statement) & (group["item_field"] == item_field)]
            if len(found) != 1:
                missing.append(field)
                continue
            item = found.iloc[0]
            row[field] = item.get("value_numeric_unscaled")
            field_publication_dates[field] = str(item.get("publish_day")) if pd.notna(item.get("publish_day")) else None
            field_update_times[field] = item.get("provider_update_time")
        dates = [value for value in field_publication_dates.values() if value]
        row.update({
            "publication_date": max(dates) if dates else None,
            "field_publication_dates": field_publication_dates,
            "field_provider_update_times": field_update_times,
            "currency": None,
            "unit": None,
            "scope_group": None,
            "source_checks": {
                "required_fields_present": not missing,
                "field_publication_dates_present": len(dates) == len(REQUIRED),
                "unit_explicit_as_rmb_yuan": False,
                "issuer_scope_verified": False,
                "revision_identity_present": False,
            },
            "contract_evaluation": {
                "publication_evidence_present": bool(dates),
                "publication_event_proven": len(set(dates)) == 1 if dates else False,
                "retrieved_asof_proven": False,
                "revision_identity_proven": False,
                "source_provenance_proven": True,
                "pit_asof_safe": False,
                "publication_evidence_reasons": [
                    "alternate_provider_retrieval_is_current_not_historical_asof",
                    *(["required_field_publication_dates_inconsistent"] if len(set(dates)) > 1 else []),
                    *(["required_fields_missing"] if missing else []),
                ],
            },
            "retrieved_at": result.get("finished_at") or result.get("started_at"),
            "asof_at": result.get("finished_at") or result.get("started_at"),
            "revision_id": None,
        })
        rows.append(row)
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "execution_admitted": False,
        "decision_day": decision_day,
        "source": {"root": str(root), "result": result, "retrieval_kind": "provider_current_history",
                   "artifacts": {
                       "result": _ref(root / "result.json"),
                       "statement_items": _ref(items_path),
                       "implementation": _ref(root / "implementation.py") if (root / "implementation.py").is_file() else None,
                   }},
        "rows": rows,
        "summary": {"rows": len(rows), "required_fields": sorted(REQUIRED),
                    "rows_with_both_fields": sum(all(field in row for field in REQUIRED) for row in rows),
                    "pit_safe_rows": 0},
        "limitations": [
            "Alternate provider publish/update fields are preserved but do not prove historical retrieval or revision-safe PIT.",
            "Provider unit and issuer scope are intentionally left unknown until reconciled against an authoritative source.",
            "This sidecar does not create a factor, target, order, fill or account replay.",
        ],
    }


def build_single_source_decision(
    sidecar: str | Path,
    bridge: str | Path,
    *,
    symbol: str,
    period: str,
) -> dict[str, Any]:
    """Bind one source row to its typed bridge decision.

    This is a small evidence projection, not another financial-data adapter.
    It keeps the provider's field timing, unit/scope uncertainty and bridge
    reason codes together so a source decision can be reviewed once without
    implying PIT eligibility or computing a factor.
    """

    sidecar_path = Path(sidecar).expanduser().resolve()
    bridge_path = Path(bridge).expanduser().resolve()
    sidecar_payload = _json(sidecar_path)
    bridge_payload = _json(bridge_path)
    if sidecar_payload.get("schema") != SCHEMA:
        raise ValueError(f"expected {SCHEMA}")
    if bridge_payload.get("schema") != "f2_sidecar_fact_bridge_v1":
        raise ValueError("expected f2_sidecar_fact_bridge_v1")
    if sidecar_payload.get("execution_admitted") is not False:
        raise ValueError("sidecar execution_admitted must remain false")
    if bridge_payload.get("execution_admitted") is not False:
        raise ValueError("bridge execution_admitted must remain false")

    source_row = next(
        (
            row
            for row in sidecar_payload.get("rows", [])
            if isinstance(row, Mapping)
            and row.get("symbol") == symbol
            and row.get("period") == period
        ),
        None,
    )
    bridge_row = next(
        (
            row
            for row in bridge_payload.get("rows", [])
            if isinstance(row, Mapping)
            and row.get("symbol") == symbol
            and row.get("period") == period
        ),
        None,
    )
    if source_row is None or bridge_row is None:
        raise ValueError(f"source row is missing: {symbol}:{period}")

    bridge_status = str(bridge_row.get("status") or "BLOCKED_DATA")
    action = (
        "READY_FOR_F01_FACT_ASSEMBLY"
        if bridge_status == "FACTS_READY_NO_FACTOR"
        else "BLOCKED_DATA"
    )
    reason_codes = list(bridge_row.get("reason_codes") or [])
    return {
        "schema": "f2_single_source_decision_v1",
        "status": action,
        "execution_admitted": False,
        "decision_day": sidecar_payload.get("decision_day"),
        "identity": {"symbol": symbol, "period": period},
        "source": {
            "kind": source_row.get("source_kind"),
            "root": source_row.get("source_root"),
            "sidecar": _ref(sidecar_path),
            "bridge": _ref(bridge_path),
            "retrieved_at": source_row.get("retrieved_at"),
            "asof_at": source_row.get("asof_at"),
            "revision_id": source_row.get("revision_id"),
        },
        "fact_contract": {
            "required_fields": sorted(REQUIRED),
            "field_values_present": all(field in source_row for field in REQUIRED),
            "field_publication_dates": source_row.get("field_publication_dates"),
            "field_provider_update_times": source_row.get("field_provider_update_times"),
            "publication_date": source_row.get("publication_date"),
            "unit": source_row.get("unit"),
            "currency": source_row.get("currency"),
            "scope_group": source_row.get("scope_group"),
            "contract_evaluation": source_row.get("contract_evaluation"),
        },
        "bridge_decision": {
            "status": bridge_status,
            "reason_codes": reason_codes,
            "fact_count": len(bridge_row.get("facts") or []),
            "factor_status": bridge_row.get("factor_status"),
        },
        "next_action": (
            "Obtain authoritative issuer evidence for historical retrieval/as-of, "
            "units, scope and revision identity before F01 assembly."
            if action == "BLOCKED_DATA"
            else "Bind the verified fact to the existing F01 consumer; do not compute a factor until the fixed specification is selected."
        ),
        "limitations": [
            "This receipt binds one source row to its existing bridge decision; it does not upgrade PIT evidence.",
            "No factor, target, order, fill or account replay is created.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_sidecar(args.root, decision_day=args.decision_day)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "rows": result["summary"]["rows"],
                      "pit_safe_rows": 0, "execution_admitted": False,
                      "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCHEMA", "STATUS", "build_sidecar", "build_single_source_decision", "main"]
