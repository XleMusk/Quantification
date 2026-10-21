"""Compare frozen candidate targets on a later StockDB research snapshot."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


SCHEMA = "stockdb_forward_compare_v1"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_target_arg(value):
    if "=" not in value:
        raise ValueError("--targets entries must be label=selected_targets.csv")
    label, path = value.split("=", 1)
    if not label.strip() or not path.strip():
        raise ValueError("--targets entries need a nonempty label and path")
    return label.strip(), Path(path).resolve()


def _endpoint_path_metrics(pivot, symbols, quantities, dates):
    """Measure a fixed-target price path without inventing missing bars."""
    complete_dates = [
        day for day in dates
        if all(symbol in pivot.index and pd.notna(pivot.at[symbol, day]) for symbol in symbols)
    ]
    if not complete_dates:
        return {
            "path_dates": [],
            "path_session_count": 0,
            "path_equal_weight_return": None,
            "path_target_qty_value_weighted_return": None,
            "path_equal_weight_max_drawdown": None,
            "path_target_qty_value_weighted_max_drawdown": None,
        }

    start_prices = pivot.loc[symbols, complete_dates[0]].astype(float)
    weights = quantities.reindex(symbols).astype(float) * start_prices
    price_path = pivot.loc[symbols, complete_dates].astype(float)
    symbol_returns = price_path.div(start_prices, axis=0) - 1.0
    equal_path = symbol_returns.mean(axis=0)
    weighted_path = symbol_returns.mul(weights, axis=0).sum(axis=0) / weights.sum()

    def max_drawdown(path):
        equity = 1.0 + path
        return float((equity / equity.cummax() - 1.0).min())

    return {
        "path_dates": complete_dates,
        "path_session_count": len(complete_dates),
        "path_equal_weight_return": float(equal_path.iloc[-1]),
        "path_target_qty_value_weighted_return": float(weighted_path.iloc[-1]),
        "path_equal_weight_max_drawdown": max_drawdown(equal_path),
        "path_target_qty_value_weighted_max_drawdown": max_drawdown(weighted_path),
    }


def _load_coverage_manifest(path, snapshot_root, start_date, end_date, selection_cutoff):
    """Validate the frozen calendar coverage used by a forward comparison."""
    path = Path(path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "stockdb_frozen_snapshot_coverage_v1":
        raise ValueError("Unsupported StockDB coverage manifest schema")
    manifest_snapshot = Path(payload.get("snapshot", "")).resolve()
    if manifest_snapshot != snapshot_root / "snapshot.json":
        raise ValueError("Coverage manifest does not describe the selected snapshot")
    if payload.get("research_only") is not True or payload.get("execution_admitted") is not False:
        raise ValueError("Coverage manifest must remain research-only and unadmitted")
    window = payload.get("forward_window") or {}
    if window.get("start") != start_date or window.get("end") != end_date:
        raise ValueError("Coverage manifest forward window does not match comparison window")
    if payload.get("selection_cutoff") != selection_cutoff:
        raise ValueError("Coverage manifest selection cutoff does not match comparison")
    coverage = payload.get("coverage") or {}
    if coverage.get("forward_complete") is not True or int(coverage.get("forward_missing_rows", -1)) != 0:
        raise ValueError("Coverage manifest contains missing forward rows")
    symbol_count = int((payload.get("symbols") or {}).get("count", 0))
    session_count = int(window.get("calendar_session_count", 0))
    rows_by_date = coverage.get("rows_by_date") or {}
    session_dates = sorted(
        date for date in rows_by_date
        if start_date <= date <= end_date and date != selection_cutoff
    )
    if len(session_dates) != session_count:
        raise ValueError("Coverage manifest session date count is inconsistent")
    expected_rows = symbol_count * session_count
    if int(coverage.get("forward_expected_rows", -1)) != expected_rows:
        raise ValueError("Coverage manifest expected row count is inconsistent")
    if int(coverage.get("forward_available_rows", -1)) != expected_rows:
        raise ValueError("Coverage manifest available row count is inconsistent")
    if any(int(rows_by_date[date]) != symbol_count for date in session_dates):
        raise ValueError("Coverage manifest has an incomplete calendar session")
    return {
        "path": str(path),
        "schema": payload["schema"],
        "status": payload.get("status"),
        "calendar_source": payload.get("calendar_source"),
        "selection_cutoff": payload.get("selection_cutoff"),
        "forward_session_dates": session_dates,
        "calendar_session_count": session_count,
        "symbol_count": symbol_count,
        "forward_expected_rows": expected_rows,
        "forward_available_rows": expected_rows,
        "forward_missing_rows": 0,
        "forward_complete": True,
    }


def compare(snapshot_root, targets, start_date=None, end_date=None, selection_cutoff=None,
            coverage_manifest=None):
    snapshot_root = Path(snapshot_root).resolve()
    metadata = json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))
    if metadata.get("schema") != "stockdb_research_snapshot_v1":
        raise ValueError("Unsupported StockDB snapshot schema")
    if metadata.get("research_only") is not True or metadata.get("execution_admitted") is not False:
        raise ValueError("Forward comparison requires an unadmitted research-only snapshot")
    prices = pd.read_csv(snapshot_root / metadata["files"]["daily"]["path"],
                         dtype={"symbol": str, "date": str})
    required = {"symbol", "date", "close"}
    if not required <= set(prices.columns):
        raise ValueError("Snapshot daily file is missing required fields")
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    if prices[["symbol", "date"]].duplicated().any():
        raise ValueError("Snapshot contains duplicate symbol/date rows")
    available_dates = sorted(prices.date.dropna().unique())
    if start_date is None and selection_cutoff:
        start_date = next((day for day in available_dates if day > selection_cutoff), None)
    start_date = start_date or (available_dates[0] if available_dates else None)
    end_date = end_date or (available_dates[-1] if available_dates else None)
    if not start_date or not end_date or start_date >= end_date:
        raise ValueError("Forward comparison needs two ordered snapshot dates")
    if selection_cutoff:
        if selection_cutoff not in available_dates:
            raise ValueError("Selection cutoff is not present in the frozen snapshot")
        if selection_cutoff >= end_date:
            raise ValueError("Selection cutoff must precede the end of the forward window")
        if start_date < selection_cutoff:
            raise ValueError("Forward window cannot begin before the selection cutoff")
    coverage_evidence = None
    if coverage_manifest:
        coverage_evidence = _load_coverage_manifest(
            coverage_manifest, snapshot_root, start_date, end_date, selection_cutoff
        )
        path_dates = coverage_evidence["forward_session_dates"]
        if any(day not in available_dates for day in path_dates):
            raise ValueError("Coverage manifest names a session absent from the StockDB snapshot")
    else:
        path_dates = [day for day in available_dates if start_date <= day <= end_date]
    window = prices.loc[prices.date.isin(path_dates), ["symbol", "date", "close"]]
    pivot = window.pivot(index="symbol", columns="date", values="close")
    if start_date not in pivot or end_date not in pivot:
        raise ValueError("Snapshot does not contain both comparison dates")

    rows, coverage = [], []
    fingerprint_first_label = {}
    for label, target_path in targets:
        target = pd.read_csv(target_path, dtype={"strategy_id": str, "symbol": str})
        # The desk reconciliation export carries two human-readable summary
        # rows after the order rows.  They are not target-contract records and
        # must not be treated as missing boolean fields.
        if "row_type" in target.columns:
            target = target.loc[target["row_type"].astype(str).str.strip().eq("order")].copy()
        required_target = {"symbol", "target_qty", "active_target"}
        if not required_target <= set(target.columns):
            raise ValueError(f"{target_path} is missing target fields")
        active_flags = target["active_target"].astype(str).str.strip().str.lower()
        if not active_flags.isin({"true", "false"}).all():
            raise ValueError(f"{target_path} contains non-boolean active_target values")
        active = target.loc[active_flags.eq("true")].copy()
        active["target_qty"] = pd.to_numeric(active["target_qty"], errors="coerce")
        active = active.loc[active.target_qty > 0]
        if active["symbol"].duplicated().any():
            raise ValueError(f"{target_path} contains duplicate active symbols")
        if not active["target_qty"].map(lambda value: float(value).is_integer()).all():
            raise ValueError(f"{target_path} contains non-integer active target quantities")
        symbols = list(dict.fromkeys(active.symbol.tolist()))
        observed = pivot.reindex(symbols)
        valid = observed[start_date].notna() & observed[end_date].notna()
        returns = observed.loc[valid, end_date] / observed.loc[valid, start_date] - 1.0
        quantities = active.set_index("symbol")["target_qty"].reindex(observed.index)
        weighted = quantities.loc[valid] * observed.loc[valid, start_date]
        weighted_return = float((returns * weighted).sum() / weighted.sum()) if weighted.sum() > 0 else None
        path_metrics = _endpoint_path_metrics(pivot, symbols, quantities, path_dates)
        missing = [symbol for symbol, ok in zip(symbols, valid.tolist()) if not ok]
        target_sha256 = _sha256(target_path)
        duplicate_of = fingerprint_first_label.get(target_sha256)
        if duplicate_of is None:
            fingerprint_first_label[target_sha256] = label
        rows.append({
            "candidate": label,
            "target_file": str(target_path),
            "target_sha256": target_sha256,
            "independence_group": target_sha256[:16],
            "independent_candidate": duplicate_of is None,
            "duplicate_of": duplicate_of,
            "start_date": start_date,
            "end_date": end_date,
            "target_symbols": len(symbols),
            "observed_symbols": int(valid.sum()),
            "missing_symbols": len(missing),
            "coverage": float(valid.mean()) if symbols else 0.0,
            "equal_weight_forward_return": float(returns.mean()) if not returns.empty else None,
            "target_qty_value_weighted_forward_return": weighted_return,
            "target_qty_start_notional": float(weighted.sum()) if not weighted.empty else None,
            "median_symbol_return": float(returns.median()) if not returns.empty else None,
            "worst_symbol_return": float(returns.min()) if not returns.empty else None,
            "best_symbol_return": float(returns.max()) if not returns.empty else None,
            **path_metrics,
            "status": "complete" if not missing and not returns.empty else "partial_or_empty",
        })
        coverage.extend({"candidate": label, "symbol": symbol, "status": "missing" if symbol in missing else "observed"}
                        for symbol in symbols)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot": str(snapshot_root),
        "snapshot_schema": metadata["schema"],
        "source_cutoff": max(metadata.get("requested_dates") or [""]),
        "comparison": {"start_date": start_date, "end_date": end_date,
                        "selection_cutoff": selection_cutoff,
                        "post_cutoff_forward_window": bool(selection_cutoff and end_date > selection_cutoff),
                        "candidate_count": len(rows),
                        "independent_candidate_count": len(fingerprint_first_label),
                        "price_field": "raw StockDB close",
                        "weighting": ["equal weight across active target symbols",
                                      "target_qty * start close value weighting"],
                        "path_policy": "use only dates with complete raw-close coverage for every target symbol",
                        "coverage_manifest": str(Path(coverage_manifest).resolve()) if coverage_manifest else None,
                        "coverage_status": coverage_evidence["status"] if coverage_evidence else None,
                        "calendar_session_count": coverage_evidence["calendar_session_count"] if coverage_evidence else None},
        "candidates": rows,
        "coverage": coverage,
        "coverage_manifest": coverage_evidence,
        "research_only": True,
        "execution_admitted": False,
        "strategy_selection_performed": False,
        "limitations": [
            "This is a forward target diagnostic, not a full Engine/Ledger replay",
            "Raw StockDB close is used; no automatic corporate-action or unit adjustment is applied",
            "It does not infer fills, cash, T+1, account holdings or broker state",
            "Path metrics use complete raw-close dates only; they are not a fill, cash or daily ledger replay",
            "This short forward window is insufficient for a performance or promotion claim",
            "Candidates sharing target_sha256 are duplicate target artifacts, not independent strategy evidence",
        ],
    }


def write_result(result, output_root):
    output_root = Path(output_root).resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Output is non-empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(result["candidates"]).to_csv(output_root / "comparison.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(result["coverage"]).to_csv(output_root / "coverage.csv", index=False, encoding="utf-8-sig")
    (output_root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = [
        "# StockDB Forward Target Diagnostic",
        "",
        f"Status: `completed_prospective_diagnostic`; execution admitted: `{result['execution_admitted']}`.",
        f"Snapshot: `{result['snapshot']}`; window: `{result['comparison']['start_date']}..{result['comparison']['end_date']}`.",
        f"Selection cutoff: `{result['comparison']['selection_cutoff'] or 'not supplied'}`; "
        f"post-cutoff forward window: `{result['comparison']['post_cutoff_forward_window']}`.",
        f"Calendar coverage: `{result['comparison'].get('coverage_status') or 'not supplied'}`; "
        f"sessions: `{result['comparison'].get('calendar_session_count') or 'unknown'}`.",
        "",
        f"Independent target fingerprints: `{result['comparison']['independent_candidate_count']}` "
        f"of `{result['comparison']['candidate_count']}` supplied candidates.",
        "",
        "| Candidate | Independent | Duplicate of | Target symbols | Observed | Coverage | Endpoint equal | Endpoint qty-weighted | Path sessions | Path MDD equal | Path MDD qty-weighted | Status |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["candidates"]:
        value = "unknown" if row["equal_weight_forward_return"] is None else f"{row['equal_weight_forward_return']:.4%}"
        weighted = ("unknown" if row["target_qty_value_weighted_forward_return"] is None else
                    f"{row['target_qty_value_weighted_forward_return']:.4%}")
        path_equal_mdd = ("unknown" if row["path_equal_weight_max_drawdown"] is None else
                          f"{row['path_equal_weight_max_drawdown']:.4%}")
        path_weighted_mdd = ("unknown" if row["path_target_qty_value_weighted_max_drawdown"] is None else
                             f"{row['path_target_qty_value_weighted_max_drawdown']:.4%}")
        report.append(f"| {row['candidate']} | {'yes' if row['independent_candidate'] else 'no'} | "
                     f"{row['duplicate_of'] or '--'} | {row['target_symbols']} | {row['observed_symbols']} | "
                     f"{row['coverage']:.2%} | {value} | {weighted} | {row['path_session_count']} | "
                     f"{path_equal_mdd} | {path_weighted_mdd} | {row['status']} |")
    report += ["", "This artifact is a research-only endpoint comparison over the frozen forward window, not a full replay, fill result, or promotion decision."]
    (output_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--targets", required=True, action="append",
                        help="label=selected_targets.csv; repeat for each candidate")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--selection-cutoff",
                        help="Selection decision cutoff; requires the forward window to end after this date")
    parser.add_argument("--coverage-manifest", type=Path,
                        help="Frozen StockDB calendar coverage_manifest_v1 for the forward window")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = compare(args.snapshot, [_parse_target_arg(item) for item in args.targets],
                     args.start_date, args.end_date, args.selection_cutoff,
                     args.coverage_manifest)
    output_root = write_result(result, args.out)
    print(json.dumps({"out": str(output_root), "candidates": result["candidates"],
                      "execution_admitted": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
