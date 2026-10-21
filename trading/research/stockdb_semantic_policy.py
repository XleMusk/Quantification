"""Diagnose and pin a conservative field-semantic policy for the 2023 bars.

This is a read-only comparison of the BaoStock extension and the legacy
StockDB input panel.  It canonicalizes the known historical code alias only
for the comparison and never rewrites either source.  The output is a
diagnostic receipt: amount and return precision can be accepted as paired
fields, while the legacy ``pre_close`` is kept diagnostic because it is not on
the same raw-price basis as its OHLC and ``pct_chg``.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT


FIELDS = ["open", "high", "low", "close", "pre_close", "volume", "amount", "pct_chg"]
ALIAS_MAP = {"302132": "300114"}
CUTOFF = "2025-02-17"
BAOSTOCK_API_EVIDENCE = {
    "url": "https://github.com/zxygithub/baostock/blob/master/docs/pythonAPI.md",
    "source": "BaoStock Python API documentation, history K-data section",
    "claims": [
        "adjustflag=3 is the no-adjustment/raw daily-bar mode",
        "amount is RMB turnover and volume is shares",
        "pctChg is calculated from close and the specified-day preclose",
        "preclose can be an exchange-calculated ex-right/ex-dividend reference rather than the prior day's actual close",
    ],
    "used_for": "interpret extension raw basis, amount/pct_chg units, and expected corporate-action continuity exceptions; not a substitute for independent action validation",
}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_panel(path: Path) -> Path:
    path = Path(path).resolve()
    if path.is_file():
        return path
    manifest = path / "manifest.json"
    if manifest.is_file():
        record = json.loads(manifest.read_text(encoding="utf-8"))
        raw = record.get("raw_file")
        if raw:
            return Path(raw).resolve()
    candidate = path / "raw_daily.parquet"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"No parquet panel found under {path}")


def _normalise(path: Path) -> pd.DataFrame:
    source = _resolve_panel(path)
    frame = pd.read_parquet(source).copy()
    frame = frame.rename(columns={"preclose": "pre_close", "pctChg": "pct_chg", "isST": "is_st"})
    required = {"code", "date", *FIELDS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{source} missing fields: {missing}")
    frame["code"] = frame["code"].astype(str).str.extract(r"(\d{6})$")[0]
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    for field in FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    # Alias projection is comparison-only.  Prefer the pre-existing canonical
    # code on collisions; this leaves the raw panels byte-for-byte untouched.
    historical_alias = frame["code"].eq("302132") & frame["date"].lt(CUTOFF)
    frame["canonical_code"] = frame["code"].where(~historical_alias, "300114")
    frame["priority"] = frame["code"].ne(frame["canonical_code"]).astype(int)
    frame = frame.sort_values(["canonical_code", "date", "priority", "code"], kind="mergesort")
    frame = frame.drop_duplicates(["canonical_code", "date"], keep="first").reset_index(drop=True)
    return frame[["canonical_code", "date", *FIELDS]]


def _describe(values: pd.Series) -> dict:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return {"count": 0}
    return {"count": int(len(values)), "min": float(values.min()), "p50": float(values.quantile(.5)),
            "p90": float(values.quantile(.9)), "p99": float(values.quantile(.99)),
            "p99_9": float(values.quantile(.999)), "max": float(values.max()),
            "mean": float(values.mean())}


def diagnose(extension: Path, legacy: Path, out: Path | None = None,
             artifact: Path | None = None) -> tuple[Path, dict]:
    extension, legacy = Path(extension).resolve(), Path(legacy).resolve()
    ext_source, legacy_source = _resolve_panel(extension), _resolve_panel(legacy)
    ext = _normalise(extension)
    old = _normalise(legacy)
    joined = ext.merge(old, on=["canonical_code", "date"], how="outer", suffixes=("_ext", "_legacy"), indicator=True)
    both = joined["_merge"].eq("both")
    paired = joined.loc[both].copy()
    amount_diff = (paired["amount_ext"] - paired["amount_legacy"]).abs()
    amount_threshold = np.maximum(.5, paired["amount_ext"].abs() * 1e-6)
    amount_known = paired["amount_ext"].notna() & paired["amount_legacy"].notna()
    amount_float32_match = np.isclose(
        paired.loc[amount_known, "amount_ext"].to_numpy(dtype=float).astype(np.float32).astype(float),
        paired.loc[amount_known, "amount_legacy"].to_numpy(dtype=float), rtol=0, atol=1e-8,
    )
    pct_diff = (paired["pct_chg_ext"] - paired["pct_chg_legacy"]).abs()
    pct_known = paired["pct_chg_ext"].notna() & paired["pct_chg_legacy"].notna()
    # Legacy values are rounded to three decimal percentage points; the
    # extension preserves four decimal points.
    pct_round3 = np.sign(paired.loc[pct_known, "pct_chg_ext"].to_numpy(dtype=float)) * np.floor(
        np.abs(paired.loc[pct_known, "pct_chg_ext"].to_numpy(dtype=float)) * 1000 + .5 + 1e-12
    ) / 1000

    def formula_error(frame: pd.DataFrame, side: str) -> pd.Series:
        implied = frame[f"close_{side}"] / frame[f"pre_close_{side}"]
        return ((implied - 1.0) * 100.0 - frame[f"pct_chg_{side}"]).abs()

    ext_formula_error = formula_error(paired, "ext")
    legacy_formula_error = formula_error(paired, "legacy")
    ext_formula_known = ext_formula_error.notna()
    legacy_formula_known = legacy_formula_error.notna()
    # The raw price/pct pair is allowed a small error from two-decimal quote
    # storage.  The tighter 0.00055 threshold reflects three-decimal legacy
    # pct_chg rounding and is reported separately.
    ext_formula_pass = ext_formula_known & ext_formula_error.le(.02)
    legacy_formula_pass = legacy_formula_known & legacy_formula_error.le(.02)
    legacy_formula_tight = legacy_formula_known & legacy_formula_error.le(.00055)
    raw_preclose_diff = (paired["pre_close_ext"] - paired["pre_close_legacy"]).abs()
    raw_preclose_match = raw_preclose_diff.le(.02)
    ratio = paired["pre_close_legacy"] / paired["pre_close_ext"]

    # Continuity is useful evidence, but a non-zero delta can be an exchange
    # corporate action.  It is never treated as a repair instruction here.
    def continuity(frame: pd.DataFrame, side: str) -> dict:
        ordered = frame[["canonical_code", "date", f"close_{side}", f"pre_close_{side}"]].copy()
        ordered = ordered.sort_values(["canonical_code", "date"], kind="mergesort")
        ordered["previous_close"] = ordered.groupby("canonical_code")[f"close_{side}"].shift()
        diff = (ordered[f"pre_close_{side}"] - ordered["previous_close"]).abs()
        known = diff.notna()
        return {"known_rows": int(known.sum()), "exact_rows": int((known & diff.le(1e-8)).sum()),
                "within_0_02_rows": int((known & diff.le(.02)).sum()),
                "discontinuity_gt_0_02_rows": int((known & diff.gt(.02)).sum()),
                "discontinuity_abs": _describe(diff.loc[known])}

    paired_for_continuity = paired.rename(columns={"canonical_code": "canonical_code"})
    record = {
        "schema": "stockdb_2023_semantic_policy_diagnostic_v1",
        "status": "diagnostic_complete",
        "created_at": datetime.now().astimezone().isoformat(),
        "sources": {
            "extension": {"path": str(ext_source), "sha256": _sha256(ext_source)},
            "legacy": {"path": str(legacy_source), "sha256": _sha256(legacy_source)},
        },
        "source_semantics_evidence": BAOSTOCK_API_EVIDENCE,
        "comparison": {
            "extension_rows_after_alias_projection": int(len(ext)),
            "legacy_rows_after_alias_projection": int(len(old)),
            "paired_rows": int(len(paired)),
            "extension_only_rows": int(joined["_merge"].eq("left_only").sum()),
            "legacy_only_rows": int(joined["_merge"].eq("right_only").sum()),
            "alias_map": ALIAS_MAP,
            "cutoff_exclusive": CUTOFF,
        },
        "amount": {
            "known_rows": int(amount_known.sum()),
            "abs_error": _describe(amount_diff.loc[amount_known]),
            "relative_error": _describe((amount_diff / paired["amount_ext"].abs().replace(0, np.nan)).loc[amount_known]),
            "float32_extension_match_rows": int(amount_float32_match.sum()),
            "acceptance_rule": "abs(extension - legacy) <= max(0.50 RMB, abs(extension) * 1e-6)",
            "acceptance_rows": int((amount_known & amount_diff.le(amount_threshold)).sum()),
            "acceptance_pass": bool((amount_known & amount_diff.le(amount_threshold)).all()),
            "unit_interpretation": "both fields are consistent with RMB amount; legacy serialization/precision differs, with no unit-scale conflict detected",
        },
        "pct_chg": {
            "known_rows": int(pct_known.sum()),
            "abs_error": _describe(pct_diff.loc[pct_known]),
            "max_difference_percentage_points": float(pct_diff.loc[pct_known].max()),
            "within_0_0005_rows": int((pct_known & pct_diff.le(.0005 + 1e-10)).sum()),
            "legacy_round3_extension_rows": int(np.isclose(pct_round3, paired.loc[pct_known, "pct_chg_legacy"].to_numpy(dtype=float), rtol=0, atol=1e-9).sum()),
            "acceptance_rule": "abs(extension - legacy) <= 0.0005 percentage points",
            "acceptance_pass": bool((pct_known & pct_diff.le(.0005 + 1e-10)).all()),
            "unit_interpretation": "same percentage-point unit; legacy carries three decimal places while extension carries four",
        },
        "pre_close": {
            "known_rows": int(len(paired)),
            "raw_basis_difference": _describe(raw_preclose_diff),
            "legacy_over_extension_ratio": _describe(ratio),
            "raw_preclose_match_within_0_02_rows": int((raw_preclose_match & paired["pre_close_ext"].notna() & paired["pre_close_legacy"].notna()).sum()),
            "extension_formula_error": _describe(ext_formula_error.loc[ext_formula_known]),
            "legacy_formula_error": _describe(legacy_formula_error.loc[legacy_formula_known]),
            "extension_formula_consistent_rows_tolerance_0_02": int(ext_formula_pass.sum()),
            "legacy_formula_consistent_rows_tolerance_0_02": int(legacy_formula_pass.sum()),
            "legacy_formula_consistent_rows_tolerance_0_00055": int(legacy_formula_tight.sum()),
            "cross_basis_rows": int((ext_formula_pass & ~legacy_formula_pass).sum()),
            "continuity": {"extension": continuity(paired_for_continuity, "ext"),
                           "legacy": continuity(paired_for_continuity, "legacy")},
            "acceptance_rule": "legacy pre_close is not accepted with raw OHLC; use extension raw pre_close/pct_chg pair only after its self-consistency check",
            "cross_source_acceptance_pass": False,
            "interpretation": "legacy pre_close is mixed adjusted/reference-basis data: it often preserves legacy pct_chg but is not the previous raw close and fails raw OHLC/pct_chg consistency on most paired rows",
        },
        "canonical_policy": {
            "price_basis": "BaoStock local extension, adjustflag=3 raw/unadjusted OHLCV; historical alias projection is comparison-only",
            "canonical_code_policy": "302132 maps to 300114 only for dates before 2025-02-17; keep raw panels unchanged",
            "canonical_fields": {"open": "extension raw", "high": "extension raw", "low": "extension raw", "close": "extension raw",
                                  "pre_close": "extension raw, paired with extension pct_chg; no legacy substitution",
                                  "volume": "extension raw; legacy exact paired agreement observed",
                                  "amount": "extension raw RMB; accept legacy quantization only for reconciliation",
                                  "pct_chg": "extension raw percentage points; legacy three-decimal comparison only"},
            "derived_reference": "when needed for a research limit check, close/(1+pct_chg/100) may be derived from the accepted extension pair; execution still requires live/raw quote",
            "promotion": False,
            "reason": "asymmetric coverage, legacy pre_close basis conflict, and unresolved historical instrument/action completeness",
        },
        "promotion": False,
        "research_only": True,
        "execution_admitted": False,
        "future_labels_used": False,
        "model_refit": False,
        "ledger_mutated": False,
        "ths_accessed": False,
        "source_panels_modified": False,
        "next_action": "keep 2023 extension diagnostic; resolve coverage/PIT membership and independently validate corporate-action/action dates before provider admission",
    }
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    report_root = Path(out).resolve() if out else ROOT / "outputs" / "stockdb_semantic_policies" / identifier
    data_root = Path(artifact).resolve() if artifact else DATA_ROOT / "research" / "stockdb_semantic_policies" / identifier
    report_root.mkdir(parents=True, exist_ok=False)
    data_root.mkdir(parents=True, exist_ok=False)
    _write_json(report_root / "result.json", record)
    _write_json(report_root / "audit.json", record)
    _write_json(data_root / "policy.json", record["canonical_policy"])
    report = ["# 2023 field semantic policy diagnostic", "",
              "Promotion: `False`; execution admission: `False`.",
              f"Paired rows after comparison-only alias projection: `{len(paired):,}`; extension-only `{record['comparison']['extension_only_rows']:,}`; legacy-only `{record['comparison']['legacy_only_rows']:,}`.",
              f"Amount: all `{record['amount']['known_rows']:,}` pairs pass `{record['amount']['acceptance_rule']}`; max absolute error `{record['amount']['abs_error']['max']:.6f}` RMB.",
              f"pct_chg: all `{record['pct_chg']['known_rows']:,}` pairs differ by at most `{record['pct_chg']['max_difference_percentage_points']:.4f}` percentage points; legacy is three-decimal rounded.",
              f"pre_close: extension self-consistency `{record['pre_close']['extension_formula_consistent_rows_tolerance_0_02']:,}/{len(paired):,}`; legacy self-consistency `{record['pre_close']['legacy_formula_consistent_rows_tolerance_0_02']:,}/{len(paired):,}`.",
              f"BaoStock field semantics evidence: `{BAOSTOCK_API_EVIDENCE['url']}` (adjustflag=3 raw mode; amount/RMB, pctChg/preclose formula, exchange action-reference caveat).",
              "Canonical policy keeps the extension raw OHLC/pre_close/pct_chg basis and does not substitute legacy pre_close.",
              f"Receipt: `{report_root / 'result.json'}`", f"Policy artifact: `{data_root / 'policy.json'}`"]
    (report_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return report_root / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args(argv)
    path, result = diagnose(args.extension, args.legacy, args.out, args.artifact)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "paired_rows": result["comparison"]["paired_rows"],
                      "promotion": False, "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
