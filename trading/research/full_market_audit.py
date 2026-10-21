"""Audit broad StockDB history against the existing BaoStock source cache."""

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..market_dataset import DEFAULT_ROOT as REFERENCE_ROOT
from ..settings import ROOT
from .experiment import write_json
from .full_market import verify_chunks


def reference_code(symbol):
    return symbol[:6]


def compare_paired(source, reference):
    joined = reference.merge(source, on=["code", "date"], how="left", validate="one_to_one", indicator=True)
    paired = joined.loc[joined._merge.eq("both")]
    if paired.empty:
        return {"paired_rows": 0, "missing_source_rows": len(reference)}
    differences = {}
    for field, reference_field, tolerance in (("open", "open_ref", .02), ("high", "high_ref", .02),
                                              ("low", "low_ref", .02), ("close", "close_ref", .02),
                                              ("volume", "volume_ref", .01), ("amount", "amount_ref", 1000),
                                              ("pct_chg", "pct_chg_ref", .02),
                                              ("pre_close", "pre_close_ref", .02)):
        a = pd.to_numeric(paired[field], errors="coerce")
        b = pd.to_numeric(paired[reference_field], errors="coerce")
        delta = (a - b).abs()
        differences[field] = {"missing_stockdb": int(a.isna().sum()),
                              "mismatch_rows": int(delta.gt(tolerance).sum()),
                              "max_absolute_difference": float(delta.max()) if delta.notna().any() else None}
    examples = paired.loc[(pd.to_numeric(paired.pct_chg, errors="coerce") -
                           pd.to_numeric(paired.pct_chg_ref, errors="coerce")).abs().gt(.02),
                          ["code", "date", "pct_chg", "pct_chg_ref"]].head(10).to_dict("records")
    return {"paired_rows": len(paired), "missing_source_rows": int(joined._merge.eq("left_only").sum()),
            "differences": differences, "pct_chg_mismatch_examples": examples}


def audit(snapshot, reference_root=REFERENCE_ROOT):
    import json
    snapshot, reference_root = Path(snapshot), Path(reference_root)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    verify_chunks(snapshot, manifest, raw=True)
    prefixes = {prefix: set() for prefix in manifest["prefixes"]}
    coverage = {prefix: {"rows": 0, "missing_pct_chg": 0, "missing_pre_close": 0,
                         "invalid_ohlc": 0} for prefix in manifest["prefixes"]}
    source_parts = []
    reference_files = sorted((reference_root / "raw").glob("*.parquet"))
    reference_codes = {reference_code(path.stem) for path in reference_files}
    for chunk in manifest["chunks"]:
        path = snapshot / chunk["parquet_file"]
        frame = pd.read_parquet(path)
        prefix = chunk["prefix"]
        prefixes[prefix].update(frame.code.unique())
        for field in coverage[prefix]:
            coverage[prefix][field] += int(chunk[field])
        if prefix in {"0", "3", "6"} and reference_codes:
            subset = frame.loc[frame.code.isin(reference_codes),
                               ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "pre_close"]]
            if len(subset):
                source_parts.append(subset)
    for prefix, codes in prefixes.items():
        coverage[prefix]["distinct_codes"] = len(codes)
    source = pd.concat(source_parts, ignore_index=True) if source_parts else pd.DataFrame()
    references = []
    for path in reference_files:
        frame = pd.read_parquet(path, columns=["date", "open", "high", "low", "close", "volume", "amount", "pctChg", "preclose"])
        frame = frame.loc[frame.date.between(manifest["start"], manifest["end"])].copy()
        if frame.empty:
            continue
        frame["code"] = reference_code(path.stem)
        frame["date"] = frame.date.str.replace("-", "", regex=False).astype("int64")
        frame = frame.rename(columns={"open": "open_ref", "high": "high_ref", "low": "low_ref",
                                      "close": "close_ref", "volume": "volume_ref", "amount": "amount_ref",
                                      "pctChg": "pct_chg_ref", "preclose": "pre_close_ref"})
        references.append(frame)
    reference = pd.concat(references, ignore_index=True) if references else pd.DataFrame()
    comparison = compare_paired(source, reference) if len(reference) else {"paired_rows": 0}
    universe = pd.read_csv(reference_root / "universe.csv", dtype=str, keep_default_na=False)
    expected = universe.symbol.map(reference_code)
    stock_codes = prefixes.get("0", set()) | prefixes.get("3", set()) | prefixes.get("6", set())
    missing = universe.loc[~expected.isin(stock_codes), ["symbol", "board", "status"]]
    # StockDB emits zero-OHLC, zero-volume rows for some suspensions. They are
    # untradeable observations, not evidence of a malformed active trade.
    nontrading_by_prefix = {prefix: 0 for prefix in manifest["prefixes"]}
    for chunk in manifest["chunks"]:
        if not chunk["invalid_ohlc"]:
            continue
        frame = pd.read_parquet(snapshot / chunk["parquet_file"],
                                columns=["open", "high", "low", "close", "volume"])
        suspended = frame[["open", "high", "low", "volume"]].eq(0).all(axis=1) & frame.close.gt(0)
        nontrading_by_prefix[chunk["prefix"]] += int(suspended.sum())
    for prefix, count in nontrading_by_prefix.items():
        coverage[prefix]["zero_ohlc_nontrading_rows"] = count
        coverage[prefix]["remaining_structural_issues"] = coverage[prefix]["invalid_ohlc"] - count
    result = {"status": "source_audit_only", "created_at": datetime.now().astimezone().isoformat(),
              "snapshot": str(snapshot), "reference_root": str(reference_root),
              "snapshot_status": manifest["status"], "coverage_by_prefix": coverage,
              "reference_universe_symbols": len(universe),
              "reference_universe_missing_in_stockdb": len(missing),
              "missing_by_board": missing.board.value_counts().to_dict(),
              "missing_delisted_at_retrieval": int(missing.status.eq("0").sum()),
              "missing_examples": missing.head(30).to_dict("records"),
              "paired_baostock_sample_symbols": len(reference_files),
              "paired_comparison": comparison, "execution_admitted": False,
              "conclusion": "Coverage and field agreement only; not a corporate-action or ETF return validation"}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, default=REFERENCE_ROOT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = audit(args.snapshot, args.reference_root)
    out = args.out or ROOT / "outputs" / "full_market" / args.snapshot.name / "audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, report)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
