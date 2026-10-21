"""Audit disclosure and receipt timing for annual quality candidates.

This is a diagnostic of fields already present in the annual feature artifact.
It does not certify issuer publication timestamps, units, restatement history,
or point-in-time tradability.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..settings import ROOT


DEFAULT_INPUT = Path(
    r"D:\QuantificationData\trading\research\annual_quality_features\20261005_061043_b24a77a8\annual_quality_features.parquet"
)
NOTICE_COLUMNS = ("balance_notice_at", "income_notice_at", "cashflow_notice_at")
RECEIPT_COLUMNS = ("balance_retrieved_at", "income_retrieved_at", "cashflow_retrieved_at")


def audit(input_path: Path, output: Path | None = None) -> tuple[Path, dict]:
    frame = pd.read_parquet(input_path)
    required = {"report_day", "asof", *NOTICE_COLUMNS, *RECEIPT_COLUMNS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Annual quality artifact is missing timing columns: {missing}")

    report_day = pd.to_datetime(frame["report_day"], errors="coerce")
    asof = pd.to_datetime(frame["asof"], utc=True, errors="coerce")
    notices = pd.concat(
        [pd.to_datetime(frame[column], errors="coerce") for column in NOTICE_COLUMNS],
        axis=1,
    )
    receipts = pd.concat(
        [pd.to_datetime(frame[column], utc=True, errors="coerce") for column in RECEIPT_COLUMNS],
        axis=1,
    )
    notice_present = notices.notna().all(axis=1)
    notice_order_valid = notices.ge(report_day, axis=0).all(axis=1)
    notice_asof_valid = notices.le(asof.dt.tz_localize(None), axis=0).all(axis=1)
    receipt_present = receipts.notna().all(axis=1)
    receipt_asof_valid = receipts.le(asof, axis=0).all(axis=1)
    strict_timing = notice_present & notice_order_valid & notice_asof_valid & receipt_present & receipt_asof_valid

    rows = []
    for day, group in frame.groupby(frame["report_day"].astype(str), sort=True):
        mask = frame["report_day"].astype(str).eq(day)
        rows.append({
            "report_day": day,
            "rows": int(mask.sum()),
            "notice_complete": int((notice_present & mask).sum()),
            "notice_order_valid": int((notice_present & notice_order_valid & mask).sum()),
            "notice_asof_valid": int((notice_present & notice_asof_valid & mask).sum()),
            "receipt_complete": int((receipt_present & mask).sum()),
            "receipt_asof_valid": int((receipt_present & receipt_asof_valid & mask).sum()),
            "strict_timing_rows": int((strict_timing & mask).sum()),
        })
    record = {
        "schema": "a_share_annual_quality_pit_timing_audit_v1",
        "status": "completed_timing_diagnostic",
        "created_at": datetime.now().astimezone().isoformat(),
        "input": str(input_path.resolve()),
        "rows": int(len(frame)),
        "securities": int(frame["symbol"].nunique()) if "symbol" in frame.columns else None,
        "timing_columns": {"notice": list(NOTICE_COLUMNS), "receipt": list(RECEIPT_COLUMNS)},
        "strict_timing_rows": int(strict_timing.sum()),
        "strict_timing_rate": float(strict_timing.mean()),
        "notice_complete_rows": int(notice_present.sum()),
        "notice_order_valid_rows": int((notice_present & notice_order_valid).sum()),
        "notice_asof_valid_rows": int((notice_present & notice_asof_valid).sum()),
        "receipt_complete_rows": int(receipt_present.sum()),
        "receipt_asof_valid_rows": int((receipt_present & receipt_asof_valid).sum()),
        "pit_verified_source_flag": bool(frame.get("pit_verified", pd.Series(False)).astype(bool).all()),
        "units_verified_source_flag": bool(frame.get("units_verified", pd.Series(False)).astype(bool).all()),
        "execution_admitted": False,
        "research_only": True,
        "limitations": [
            "Notice fields are source metadata and are not independently verified issuer publication timestamps.",
            "Timing consistency does not establish restatement version history, units, report scope or historical tradability.",
            "The audit cannot promote annual features into a historical signal or target portfolio.",
        ],
        "by_report_day": rows,
    }
    if output is None:
        output = ROOT / "outputs" / "annual_quality_pit_audit" / datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    json_path = output / "result.json"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Annual quality timing audit",
        "",
        f"- Rows: `{record['rows']}`; securities: `{record['securities']}`",
        f"- Strict timing rows: `{record['strict_timing_rows']}` ({record['strict_timing_rate']:.2%})",
        f"- Notice complete: `{record['notice_complete_rows']}`",
        f"- Notice order valid: `{record['notice_order_valid_rows']}`",
        f"- Notice as-of valid: `{record['notice_asof_valid_rows']}`",
        f"- Receipt complete/as-of valid: `{record['receipt_complete_rows']}` / `{record['receipt_asof_valid_rows']}`",
        "- This is a timing diagnostic only; `pit_verified=false`, units and issuer publication semantics remain unverified.",
        "",
        f"JSON: `{json_path}`",
    ]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, record = audit(args.input, args.out)
    print(json.dumps({"result": str(path), "status": record["status"],
                      "strict_timing_rows": record["strict_timing_rows"],
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

