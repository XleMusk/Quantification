"""Local historical factor inputs with row-level source/reference boundaries."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import normalize_symbol
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from .experiment import write_json
from .full_market import FIELDS, VALUATION_FIELDS, inspect_rows


FEATURES = ("book_to_price_candidate", "earnings_to_price_candidate",
            "log_float_mv_candidate", "turnover_pct_candidate")
REFERENCE_FIELDS = ("date", "code", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "pctChg", "tradestatus", "isST")
LIMITS = [
    "Offline retrieval-time candidate inputs, not revision-safe financials or a PIT universe",
    "All source groups are retained; source code prefixes alone do not identify ETFs or ordinary A-shares",
    "A stock identity comes from the existing full master, including delisted records, not current active membership",
    "Unknown source/reference fields stay missing; missing daily bars are not invented suspensions or zero returns",
    "Source pre_close/pct_chg are diagnostics only, never execution references or total-return labels",
    "Volume and amount are retained without automatic scaling; pair agreement is not independent unit certification",
    "Value inputs are algebraic provider-ratio transforms, not evidence of original financial disclosure availability",
    "No canonical quote replacement, action/status backfill, model tuning, account creation or new backtester; NO_TRADE",
]


def stock_identities(master):
    identities = {}
    for code in master.loc[master.type.eq("1"), "code"]:
        symbol = normalize_symbol(code).vt_symbol
        try:
            stock_board(symbol)
        except ValueError:
            continue
        key = symbol[:6]
        if key in identities and identities[key] != symbol:
            raise ValueError("Ambiguous ordinary stock code in the reference master")
        identities[key] = symbol
    return identities


def factor_inputs(source, identities):
    frame = source.copy()
    frame["symbol"] = frame.code.map(identities)
    frame = frame.loc[frame.symbol.notna()].copy()
    for field in VALUATION_FIELDS:
        frame[field] = pd.to_numeric(frame[field].replace("", np.nan), errors="raise")
        frame[field] = frame[field].where(np.isfinite(frame[field]))
    frame["book_to_price_candidate"] = 1 / frame.pb.where(frame.pb.gt(0))
    frame["earnings_to_price_candidate"] = 1 / frame.pe_ttm.where(frame.pe_ttm.ne(0))
    frame["log_float_mv_candidate"] = np.log(frame.float_mv.where(frame.float_mv.gt(0)))
    frame["turnover_pct_candidate"] = frame.turnover.where(frame.turnover.ge(0))
    for field in FEATURES:
        frame[field] = frame[field].where(np.isfinite(frame[field]))
    return frame[["code", "date", "symbol", *VALUATION_FIELDS, *FEATURES]]


def pair_rows(source, reference):
    reference = reference.rename(columns={
        "preclose": "pre_close", "pctChg": "pct_chg", "isST": "is_st"})
    reference = reference.rename(columns={field: field + "_ref" for field in reference if field not in {"date", "code"}})
    joined = source.merge(reference, on=["code", "date"], how="outer", validate="one_to_one", indicator=True)
    source_rows = joined._merge.ne("right_only")
    both = joined._merge.eq("both")
    for field in ("open", "high", "low", "close", "pre_close", "volume", "amount", "pct_chg"):
        for name in (field, field + "_ref"):
            joined[name] = pd.to_numeric(joined[name].replace("", np.nan), errors="raise")
    gaps = pd.DataFrame({field: (joined[field] - joined[field + "_ref"]).abs()
                         for field in ("open", "high", "low", "close")})
    known_prices = both & gaps.notna().all(axis=1)
    joined["ohlc_pair_exact"] = gaps.max(axis=1).le(1e-8).astype("boolean").where(known_prices)
    joined["ohlc_pair_within_2c"] = gaps.max(axis=1).le(.02).astype("boolean").where(known_prices)
    for field, tolerance in (("volume", 0), ("pre_close", .02)):
        known = both & joined[field].notna() & joined[field + "_ref"].notna()
        joined[field + "_pair_agrees"] = (joined[field] - joined[field + "_ref"]).abs().le(tolerance).astype("boolean").where(known)
    amount_known = both & joined.amount.notna() & joined.amount_ref.notna()
    amount_tolerance = np.maximum(1000, joined.amount_ref.abs() * .001)
    joined["amount_pair_agrees"] = (joined.amount - joined.amount_ref).abs().le(amount_tolerance).astype("boolean").where(amount_known)
    joined["volume_source_reference_ratio"] = (joined.volume / joined.volume_ref.where(joined.volume_ref.gt(0))).where(both)
    vwap = joined.amount / joined.volume.where(joined.volume.gt(0))
    known_activity = source_rows & vwap.notna() & joined.low.gt(0) & joined.high.ge(joined.low)
    joined["source_vwap_in_ohlc"] = vwap.between(joined.low - .011, joined.high + .011).astype("boolean").where(known_activity)
    implied = (joined.close / joined.pre_close.where(joined.pre_close.gt(0)) - 1) * 100
    known_return = source_rows & implied.notna() & joined.pct_chg.notna()
    joined["pct_chg_matches_source_pre_close"] = (implied - joined.pct_chg).abs().le(.02).astype("boolean").where(known_return)
    joined["pair_state"] = joined._merge.astype(str)
    joined = joined.drop(columns="_merge")
    summary = {"source_rows": int(source_rows.sum()), "paired_rows": int(both.sum()),
               "source_only_rows": int(joined.pair_state.eq("left_only").sum()),
               "reference_only_rows": int(joined.pair_state.eq("right_only").sum())}
    for field in ("ohlc_pair_exact", "ohlc_pair_within_2c", "volume_pair_agrees", "amount_pair_agrees",
                  "pre_close_pair_agrees", "source_vwap_in_ohlc", "pct_chg_matches_source_pre_close"):
        summary[field + "_true"] = int(joined[field].eq(True).sum())
        summary[field + "_false"] = int(joined[field].eq(False).sum())
        summary[field + "_unknown"] = int(joined[field].isna().sum())
    summary["volume_ratio_near_100"] = int(np.isclose(joined.volume_source_reference_ratio, 100, atol=.001).sum())
    summary["volume_ratio_near_001"] = int(np.isclose(joined.volume_source_reference_ratio, .01, atol=.000001).sum())
    return joined, summary


def build(snapshot, reference_file, universe):
    snapshot, reference_file, universe = (Path(path).resolve() for path in (snapshot, reference_file, universe))
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    required_fields = [*FIELDS, *VALUATION_FIELDS]
    if (manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False
            or len(manifest["chunks"]) != manifest["chunks_expected"] or manifest["fields"] != required_fields):
        raise ValueError("Completed unadmitted source with valuation projection required")
    identities = stock_identities(pd.read_csv(universe, dtype=str, keep_default_na=False))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research/stockdb_history_inputs" / identifier
    root.mkdir(parents=True, exist_ok=False)
    for folder in ("source_panels", "comparisons", "feature_inputs"):
        (root / folder).mkdir()
    shutil.copy2(__file__, root / "implementation.py")
    plan = {"id": identifier, "created_at": datetime.now().astimezone().isoformat(),
            "snapshot": str(snapshot), "reference_file": str(reference_file), "universe": str(universe),
            "formulas": {"book_to_price_candidate": "1 / pb for pb > 0",
                         "earnings_to_price_candidate": "1 / pe_ttm for finite nonzero PE; negative retained",
                         "log_float_mv_candidate": "log(float_mv) for float_mv > 0",
                         "turnover_pct_candidate": "provider turnover for finite nonnegative values, no scaling"},
            "execution_admitted": False, "limits": LIMITS}
    write_json(root / "plan.json", plan)
    result = {"id": identifier, "status": "running", "months": [], "ohlc_conflicts_by_code": {}, "execution_admitted": False}
    write_json(root / "result.json", result)
    codes_by_prefix, security_parts = {}, []
    try:
        months = sorted({chunk["month"] for chunk in manifest["chunks"]})
        for month in months:
            parts = []
            for chunk in [item for item in manifest["chunks"] if item["month"] == month]:
                path = (snapshot / chunk["parquet_file"]).resolve()
                path.relative_to(snapshot)
                source = pd.read_parquet(path, columns=required_fields)
                source, _ = inspect_rows(source.values.tolist(), chunk["prefix"],
                                         chunk["lower_inclusive"], chunk["upper_inclusive"], required_fields)
                codes_by_prefix.setdefault(chunk["prefix"], set()).update(source.code)
                if not source.empty:
                    parts.append(source)
            source = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=required_fields)
            source["date"] = pd.to_datetime(source.date.astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
            if source.duplicated(["code", "date"]).any():
                raise ValueError("Duplicate source code/day across chunks")
            lower = max(manifest["start"], month + "-01")
            upper = min(manifest["end"], str(pd.Period(month).end_time.date()))
            reference = pq.read_table(reference_file, columns=list(REFERENCE_FIELDS),
                filters=[("date", ">=", lower), ("date", "<=", upper)]).to_pandas()
            reference["code"] = reference.code.map(lambda code: normalize_symbol(code).vt_symbol[:6])
            pairs, check = pair_rows(source, reference)
            conflicts = pairs.loc[pairs.ohlc_pair_within_2c.eq(False).fillna(False), "code"].value_counts()
            for code, count in conflicts.items():
                result["ohlc_conflicts_by_code"][code] = result["ohlc_conflicts_by_code"].get(code, 0) + int(count)
            features = factor_inputs(source, identities)
            flags = ["code", "date", "pair_state", "ohlc_pair_within_2c", "volume_pair_agrees", "amount_pair_agrees"]
            features = features.merge(pairs[flags], on=["code", "date"], validate="one_to_one")
            source.to_parquet(root / "source_panels" / (month + ".parquet"), index=False)
            pairs.to_parquet(root / "comparisons" / (month + ".parquet"), index=False)
            features.to_parquet(root / "feature_inputs" / (month + ".parquet"), index=False)
            coverage = source.groupby("code").agg(rows=("date", "size"), first_day=("date", "min"), last_day=("date", "max")).reset_index()
            security_parts.append(coverage)
            result["months"].append({"month": month, "feature_stock_rows": len(features),
                "feature_stocks": int(features.symbol.nunique()), **check,
                **{field + "_known": int(features[field].notna().sum()) for field in FEATURES}})
            write_json(root / "result.json", result)
            print(f"{month}: source={len(source)}, stock_inputs={len(features)}, paired={check['paired_rows']}, "
                  f"OHLC conflicts={check['ohlc_pair_within_2c_false']}, volume conflicts={check['volume_pair_agrees_false']}", flush=True)
        coverage = pd.concat(security_parts, ignore_index=True).groupby("code").agg(
            rows=("rows", "sum"), first_day=("first_day", "min"), last_day=("last_day", "max")).reset_index()
        coverage["symbol"] = coverage.code.map(identities)
        coverage.to_csv(root / "security_coverage.csv", index=False)
        result.update(status="completed_research_inputs", source_codes=int(coverage.code.nunique()),
                      feature_stock_codes=int(coverage.symbol.nunique()),
                      source_codes_by_prefix={prefix: len(codes) for prefix, codes in codes_by_prefix.items()},
                      totals={field: sum(item[field] for item in result["months"])
                              for field in result["months"][0] if field not in {"month", "feature_stocks"}})
    except Exception as error:
        result.update(status="failed_partial_inputs", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result["finished_at"] = datetime.now().astimezone().isoformat()
        write_json(root / "result.json", result)
    out = ROOT / "outputs/stockdb_history_inputs" / identifier
    out.mkdir(parents=True, exist_ok=False)
    totals = result["totals"]
    report = ["# Local Historical Factor Inputs", "", f"Status: {result['status']}; NO_TRADE.",
              f"Input snapshot: {snapshot}", f"Data: {root}", "",
              f"Source codes: {result['source_codes']}; supported SH/SZ stock identities: {result['feature_stock_codes']}.",
              f"Source rows: {totals['source_rows']}; stock feature rows: {totals['feature_stock_rows']}.",
              f"Reference pairs: {totals['paired_rows']}; source-only: {totals['source_only_rows']}; reference-only: {totals['reference_only_rows']}.",
              f"OHLC conflicts over 2c: {totals['ohlc_pair_within_2c_false']}; volume conflicts: {totals['volume_pair_agrees_false']}.",
              f"OHLC conflicts by source code: {result['ohlc_conflicts_by_code']}.",
              f"pre_close conflicts over 2c: {totals['pre_close_pair_agrees_false']}; source percent-change / pre_close inconsistencies: {totals['pct_chg_matches_source_pre_close_false']}.",
              "", "Monthly feature_inputs contain fixed value/size/turnover inputs and paired-source flags.",
              "Use the flags and original source fields; do not interpret field coverage as tested profitability.", "",
              *["- " + limit for limit in LIMITS]]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"root": str(root), "report": str(out / "REPORT.md"),
                      "source_codes": result["source_codes"], "feature_stock_codes": result["feature_stock_codes"],
                      "totals": totals, "execution_admitted": False}, ensure_ascii=False), flush=True)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--reference-file", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    args = parser.parse_args(argv)
    build(args.snapshot, args.reference_file, args.universe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
