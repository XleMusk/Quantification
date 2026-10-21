"""Reconcile the 2023 BaoStock extension against the legacy raw source view."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT


FIELDS = ["open", "high", "low", "close", "pre_close", "volume", "amount", "pct_chg"]
TOLERANCES = {"open": 0.02, "high": 0.02, "low": 0.02, "close": 0.02,
              "pre_close": 0.02, "volume": 0.0, "amount": 0.001, "pct_chg": 0.02}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _normalise(frame: pd.DataFrame, *, legacy: bool) -> pd.DataFrame:
    frame = frame.copy()
    if legacy:
        frame = frame.rename(columns={"preclose": "pre_close", "pctChg": "pct_chg", "isST": "is_st"})
    frame["code"] = frame["code"].astype(str).str.extract(r"(\d{6})$")[0]
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    for field in FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame["is_st"] = frame["is_st"].astype(bool)
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("Duplicate canonical code/date rows")
    return frame[["code", "date", *FIELDS, "is_st"]]


def reconcile(extension: Path, legacy_source_root: Path, out: Path | None = None) -> tuple[Path, dict]:
    extension, legacy_source_root = Path(extension).resolve(), Path(legacy_source_root).resolve()
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    ext = _normalise(pd.read_parquet(manifest["raw_file"]), legacy=False)
    paths = sorted(legacy_source_root.glob("2023-*.parquet"))
    if not paths:
        raise ValueError(f"No legacy source panels: {legacy_source_root}")
    legacy = _normalise(pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True), legacy=True)
    joined = ext.merge(legacy, on=["code", "date"], how="outer", suffixes=("_ext", "_legacy"), indicator=True)
    both = joined["_merge"].eq("both")
    metrics = {"extension_rows": len(ext), "legacy_rows": len(legacy),
               "paired_rows": int(both.sum()), "extension_only_rows": int(joined["_merge"].eq("left_only").sum()),
               "legacy_only_rows": int(joined["_merge"].eq("right_only").sum()),
               "duplicate_extension_rows": int(ext.duplicated(["code", "date"]).sum()),
               "duplicate_legacy_rows": int(legacy.duplicated(["code", "date"]).sum())}
    mismatch = {}
    samples = []
    for field in FIELDS:
        left, right = joined.loc[both, field + "_ext"], joined.loc[both, field + "_legacy"]
        known = left.notna() & right.notna()
        diff = (left - right).abs()
        if field == "amount":
            limit = np.maximum(1000.0, right.abs() * TOLERANCES[field])
            bad = known & diff.gt(limit)
        else:
            bad = known & diff.gt(TOLERANCES[field])
        mismatch[field] = {"paired_known": int(known.sum()), "conflict_rows": int(bad.sum()),
                           "max_abs_diff": float(diff[known].max()) if known.any() else None,
                           "tolerance": TOLERANCES[field]}
        if bad.any():
            sample = joined.loc[both].loc[bad, ["code", "date", field + "_ext", field + "_legacy"]].head(50).copy()
            sample["field"] = field
            samples.append(sample)
    st_known = joined.loc[both, "is_st_ext"].notna() & joined.loc[both, "is_st_legacy"].notna()
    mismatch["is_st"] = {"paired_known": int(st_known.sum()),
                          "conflict_rows": int((joined.loc[both, "is_st_ext"] != joined.loc[both, "is_st_legacy"]).sum())}
    semantic = {}
    for side in ("ext", "legacy"):
        implied = (joined.loc[both, "close_" + side] / joined.loc[both, "pre_close_" + side] - 1) * 100
        known = implied.notna() & joined.loc[both, "pct_chg_" + side].notna()
        semantic[side] = {
            "known_rows": int(known.sum()),
            "pct_chg_consistent_rows": int((known & (implied - joined.loc[both, "pct_chg_" + side]).abs().le(.02)).sum()),
            "tolerance": 0.02,
        }
    price_conflict = joined.loc[both & joined["open_ext"].sub(joined["open_legacy"]).abs().gt(.02), "code"]
    semantic["ohlc_conflict_codes"] = price_conflict.value_counts().to_dict()
    stop_reasons = [f"{field}_conflict_rows={item['conflict_rows']}" for field, item in mismatch.items()
                   if item.get("conflict_rows", 0)]
    stop_reasons += ["extension_only_rows>0", "legacy_only_rows>0"] if metrics["extension_only_rows"] or metrics["legacy_only_rows"] else []
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    report_root = Path(out).resolve() if out else ROOT / "outputs" / "stockdb_2023_reconciliations" / identifier
    data_root = DATA_ROOT / "research" / "stockdb_2023_reconciliations" / identifier
    report_root.mkdir(parents=True, exist_ok=False); data_root.mkdir(parents=True, exist_ok=False)
    record = {"schema": "stockdb_2023_extension_reconciliation_v1", "status": "completed_diagnostic_reconciliation",
              "created_at": datetime.now().astimezone().isoformat(), "extension": str(extension),
              "legacy_source_root": str(legacy_source_root), "metrics": metrics, "field_checks": mismatch,
              "pre_close_semantics": semantic,
              "known_identity_changes": [{
                  "old_code": "300114", "new_code": "302132", "effective_date": "2025-02-17",
                  "evidence": "D:/QuantificationData/trading/evidence/stock_bulk_20261001/SZSE_code_change_20250215_300114_302132.pdf",
                  "2023_policy": "retain 300114 as the historical code; do not merge 302132 rows into the canonical 2023 provider without an explicit alias projection"
              }],
              "stop_reasons": stop_reasons, "promotion_allowed": False,
              "future_labels_used": False, "model_refit": False, "ledger_mutated": False,
              "execution_admitted": False, "ths_payload_generated": False}
    _write_json(report_root / "result.json", record); _write_json(report_root / "audit.json", record)
    _write_json(data_root / "result.json", record)
    if samples:
        pd.concat(samples, ignore_index=True).to_csv(report_root / "mismatch_samples.csv", index=False)
    report = ["# 2023 extension reconciliation", "", "Promotion: `False`.",
              f"Paired rows: `{metrics['paired_rows']}`; extension-only: `{metrics['extension_only_rows']}`; legacy-only: `{metrics['legacy_only_rows']}`.",
              f"Stop reasons: `{', '.join(stop_reasons)}`.",
              "The comparison is diagnostic only. Conflicting preclose/OHLC semantics are retained and never substituted.",
              f"Result: `{report_root / 'result.json'}`"]
    (report_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return report_root / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--legacy-source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = reconcile(args.extension, args.legacy_source_root, args.out)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "paired_rows": result["metrics"]["paired_rows"],
                      "stop_reasons": result["stop_reasons"], "promotion_allowed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
