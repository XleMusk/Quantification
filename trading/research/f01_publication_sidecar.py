"""Build a provenance sidecar for the scoped F01 issuer-report facts.

The sidecar joins the three issuer-report rows to the saved financial-run
receipt and verifies the PDF/TXT hashes.  A provider CSV retrieval timestamp is
kept as provider evidence only; it is never promoted to the issuer PDF's
retrieval or as-of time.  Missing revision/as-of evidence therefore remains a
hard PIT block.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .f01_scoped_field_coverage import DEFAULT_RECEIPT, validate_receipt
from .publication_event_contract import evaluate_publication_evidence


SCHEMA = "f01_publication_sidecar_v1"


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _task_key(row: Mapping[str, Any]) -> str:
    return f"{row.get('symbol', '')}:{row.get('period', '')}"


def _provider_row(run_root: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    """Read the provider CSV only for an explicit scope/value comparison."""

    relative = task.get("file")
    if not isinstance(relative, str) or not relative:
        return {}
    path = run_root / relative
    if not path.is_file():
        return {"file": str(path), "present": False}
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return {"file": str(path), "present": False, "error": str(exc)}
    if frame.empty:
        return {"file": str(path), "present": True, "rows": 0}
    raw = frame.iloc[0].to_dict()
    value = raw.get("netProfit")
    return {
        "file": str(path),
        "sha256": _hash(path),
        "present": True,
        "rows": int(len(frame)),
        "code": raw.get("code"),
        "pubDate": raw.get("pubDate"),
        "statDate": raw.get("statDate"),
        "netProfit": None if pd.isna(value) else float(value),
        "scope_label": "provider_netProfit_scope_not_verified",
        "retrieved_at": task.get("retrieved_at", ""),
    }


def _report_row(row: Mapping[str, Any], task: Mapping[str, Any], run_root: Path,
                decision_day: str, retrieval: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = row.get("report_source") if isinstance(row.get("report_source"), Mapping) else {}
    pdf = Path(str(source.get("pdf", ""))).resolve()
    text_evidence = row.get("parent_net_profit_source")
    if not isinstance(text_evidence, Mapping):
        text_evidence = row.get("total_assets_source")
    text = Path(str(text_evidence.get("text", "") if isinstance(text_evidence, Mapping) else "")).resolve()
    expected_pdf_sha = str(source.get("pdf_sha256", ""))
    expected_text_sha = str(source.get("text_sha256", ""))
    pdf_sha = _hash(pdf) if pdf.is_file() else ""
    text_sha = _hash(text) if text.is_file() else ""
    provider = _provider_row(run_root, task)
    parent_profit = float(row["parent_net_profit"])
    provider_net_profit = provider.get("netProfit")
    retrieval = retrieval if isinstance(retrieval, Mapping) else {}
    retrieval_meta = retrieval.get("retrieval") if isinstance(retrieval.get("retrieval"), Mapping) else {}
    retrieved_at = str(retrieval_meta.get("retrieved_at") or "")
    revision_id = str(retrieval.get("revision_id") or "")
    # A current re-fetch is explicit provenance, but it is not a historical
    # as-of snapshot for the decision cutoff. Keep the field visible so the
    # contract can reject it for the correct reason instead of treating it as
    # missing evidence.
    asof_at = retrieved_at
    return {
        "symbol": row.get("symbol"),
        "period": row.get("period"),
        "stat_date": row.get("stat_date"),
        "publication_date": row.get("pub_date"),
        "parent_net_profit": row.get("parent_net_profit"),
        "total_assets": row.get("total_assets"),
        "source_url": source.get("url", ""),
        "source_file": str(pdf),
        "source_sha256": expected_pdf_sha,
        "source_sha256_verified": bool(pdf_sha and pdf_sha == expected_pdf_sha),
        "source_text_file": str(text),
        "source_text_sha256": expected_text_sha,
        "source_text_sha256_verified": bool(text_sha and text_sha == expected_text_sha),
        "retrieved_at": retrieved_at,
        "asof_at": asof_at,
        "revision_id": revision_id,
        "retrieval_evidence_status": retrieval.get("retrieved_source_hash_matches_saved"),
        "provider_evidence": provider,
        "provider_net_profit_vs_parent_profit": {
            "provider_net_profit": provider_net_profit,
            "scoped_parent_net_profit": parent_profit,
            "difference": None if provider_net_profit is None else provider_net_profit - parent_profit,
            "scope_mismatch_observed": provider_net_profit is not None and provider_net_profit != parent_profit,
        },
        "contract_evaluation": evaluate_publication_evidence(
            {
                "publication_date": row.get("pub_date"),
                # Pass the mapped retrieval evidence into the same contract
                # that is used by the coverage funnel.  The previous code
                # evaluated empty placeholders here, so a current retrieval
                # was reported as both ``retrieved_asof_invalid`` and
                # ``revision_id_missing`` even when the sidecar carried those
                # fields.  The contract must still reject a late retrieval,
                # but it should report the actual reason.
                "retrieved_at": retrieved_at,
                "asof_at": asof_at,
                "revision_id": revision_id,
                "source_file": str(pdf),
                "source_sha256": expected_pdf_sha,
            },
            decision_day,
        ),
    }


def build_sidecar(scoped_receipt: str | Path = DEFAULT_RECEIPT, *,
                  financial_run_root: str | Path,
                  decision_day: str,
                  retrieval_receipt: str | Path | None = None) -> dict[str, Any]:
    receipt_path = Path(scoped_receipt).resolve()
    validation = validate_receipt(receipt_path)
    if not validation.get("ok"):
        raise ValueError("scoped F01 receipt failed validation: " + "; ".join(validation.get("errors", [])))
    run_root = Path(financial_run_root).resolve()
    receipts_path = run_root / "receipts.json"
    if not run_root.is_dir() or not receipts_path.is_file():
        raise FileNotFoundError("financial run root or receipts.json is missing")
    receipts = _load_json(receipts_path)
    tasks = receipts.get("tasks", {}) if isinstance(receipts, Mapping) else {}
    source = _load_json(receipt_path)
    retrieval_by_key: dict[str, Mapping[str, Any]] = {}
    retrieval_receipt_path: Path | None = None
    if retrieval_receipt is not None:
        retrieval_receipt_path = Path(retrieval_receipt).resolve()
        retrieval_source = _load_json(retrieval_receipt_path)
        for item in retrieval_source.get("rows", []):
            if isinstance(item, Mapping):
                retrieval_by_key[_task_key(item)] = item
    rows: list[dict[str, Any]] = []
    missing_tasks: list[str] = []
    for row in source.get("rows_detail", []):
        key = _task_key(row)
        task = tasks.get(key, {}) if isinstance(tasks, Mapping) else {}
        if not task:
            missing_tasks.append(key)
        rows.append(_report_row(row, task, run_root, decision_day, retrieval_by_key.get(key)))
    pit_safe = sum(bool(row["contract_evaluation"]["pit_asof_safe"]) for row in rows)
    return {
        "schema": SCHEMA,
        "status": "REAL_SOURCE_MAPPED_PIT_BLOCKED" if rows and not pit_safe else (
            "REAL_SOURCE_MAPPED_PARTIAL_PIT" if pit_safe else "NO_SOURCE_SIDECAR_ROWS"),
        "execution_admitted": False,
        "scoped_receipt": str(receipt_path),
        "scoped_receipt_sha256": _hash(receipt_path),
        "financial_run_root": str(run_root),
        "financial_receipts": str(receipts_path),
        "retrieval_receipt": str(retrieval_receipt_path) if retrieval_receipt_path else None,
        "retrieval_receipt_sha256": _hash(retrieval_receipt_path) if retrieval_receipt_path else None,
        "decision_day": decision_day,
        "rows": rows,
        "summary": {
            "rows_mapped": len(rows),
            "missing_financial_tasks": len(missing_tasks),
            "source_files_present": sum(row["source_sha256_verified"] for row in rows),
            "source_text_files_present": sum(row["source_text_sha256_verified"] for row in rows),
            "publication_date_rows": sum(bool(row["publication_date"]) for row in rows),
            "provider_retrieved_at_rows": sum(bool(row["provider_evidence"].get("retrieved_at")) for row in rows),
            "issuer_retrieved_at_rows": sum(bool(row["retrieved_at"]) for row in rows),
            "asof_at_rows": sum(bool(row["asof_at"]) for row in rows),
            "revision_id_rows": sum(bool(row["revision_id"]) for row in rows),
            "provider_scope_mismatch_rows": sum(
                bool(row["provider_net_profit_vs_parent_profit"]["scope_mismatch_observed"]) for row in rows),
            "pit_safe_rows": pit_safe,
            "retrieval_receipt_rows": sum(bool(row.get("retrieval_evidence_status")) for row in rows),
        },
        "missing_tasks": missing_tasks,
        "limitations": [
            "Provider CSV retrieved_at is retained as provider evidence only; it is not issuer-PDF retrieval evidence.",
            "A current network retrieval receipt is provenance evidence only; it does not prove historical as-of availability or revision history before the decision cutoff.",
            "Provider netProfit scope is not verified and can differ from parent-attributable PDF profit.",
            "This sidecar does not promote any row to F01 or run a factor/replay.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoped-receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--financial-run-root", type=Path, required=True)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--retrieval-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_sidecar(args.scoped_receipt, financial_run_root=args.financial_run_root,
                           decision_day=args.decision_day, retrieval_receipt=args.retrieval_receipt)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

