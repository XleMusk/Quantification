"""Compare fixed StockDB targets against a separate raw daily provider.

This is deliberately a read-only data and target-path diagnostic.  It does not
refit a signal, replace StockDB prices, mutate the Ledger, or admit a strategy
for paper/live execution.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT


SCHEMA = "stockdb_independent_source_compare_v1"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _selection_cutoff(targets_root: Path) -> str | None:
    """Read the fixed source cutoff from the selected paper plans."""
    for plan_path in sorted(targets_root.glob("*/offline_strategy_plan.json")):
        try:
            value = _json(plan_path).get("source_cutoff")
        except (OSError, json.JSONDecodeError):
            value = None
        if value:
            try:
                return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
            except ValueError:
                pass
    for value in (targets_root.name, targets_root.parent.name):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
    return None


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _active_targets(targets_root: Path) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for path in sorted(targets_root.glob("*/selected_targets.csv")):
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        required = {"strategy_id", "symbol", "target_qty", "active_target"}
        if not required <= set(frame.columns):
            continue
        active = frame.loc[
            frame["active_target"].astype(str).str.lower().eq("true")
            & pd.to_numeric(frame["target_qty"], errors="coerce").gt(0),
            ["strategy_id", "symbol", "target_qty"],
        ].copy()
        if active.empty:
            continue
        active["target_qty"] = pd.to_numeric(active["target_qty"], errors="raise")
        active["symbol"] = active["symbol"].astype(str)
        result[str(active.iloc[0]["strategy_id"])] = active.drop_duplicates("symbol")
    if not result:
        raise ValueError(f"No active selected targets found under {targets_root}")
    return result


def _prices_from_alternate(root: Path, result: dict, symbol: str, start: str, end: str) -> pd.DataFrame:
    entry = result.get("entries", {}).get(symbol)
    if not entry:
        return pd.DataFrame(columns=["date", "close"])
    path = root / str(entry["file"])
    if not path.is_file():
        raise ValueError(f"Alternate source file missing for {symbol}: {path}")
    frame = pd.read_parquet(path, columns=["date", "close"])
    frame["date"] = frame["date"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.loc[frame["date"].between(start, end)]
    if frame["date"].duplicated().any():
        raise ValueError(f"Duplicate alternate dates for {symbol}")
    return frame.sort_values("date").reset_index(drop=True)


def _returns(rows: pd.DataFrame, price_col: str, qty_col: str = "target_qty") -> dict:
    usable = rows.dropna(subset=[f"{price_col}_start", f"{price_col}_end"]).copy()
    usable = usable.loc[usable[f"{price_col}_start"].gt(0) & usable[f"{price_col}_end"].gt(0)]
    usable["return"] = usable[f"{price_col}_end"] / usable[f"{price_col}_start"] - 1.0
    if usable.empty:
        return {"symbols": 0, "equal_weight_return": None, "qty_value_weighted_return": None}
    weights = usable[qty_col] * usable[f"{price_col}_start"]
    weighted = float((usable["return"] * weights).sum() / weights.sum()) if weights.sum() else None
    return {
        "symbols": int(len(usable)),
        "equal_weight_return": float(usable["return"].mean()),
        "qty_value_weighted_return": weighted,
        "median_symbol_return": float(usable["return"].median()),
        "worst_symbol_return": float(usable["return"].min()),
        "best_symbol_return": float(usable["return"].max()),
    }


def compare(
    snapshot_root: Path,
    alternate_root: Path,
    targets_root: Path,
    start: str,
    end: str,
) -> tuple[Path, dict]:
    snapshot = _json(snapshot_root / "snapshot.json")
    alternate_plan = _json(alternate_root / "plan.json")
    alternate_result = _json(alternate_root / "result.json")
    if alternate_plan.get("provider") not in {"tencent", "eastmoney"}:
        raise ValueError("Unsupported alternate provider receipt")
    if alternate_result.get("execution_admitted") is not False:
        raise ValueError("Alternate source must remain unadmitted")
    if alternate_result.get("status") not in {"completed_candidate_cache", "partial_candidate_cache"}:
        raise ValueError(f"Alternate cache is not usable: {alternate_result.get('status')}")

    stockdb = pd.read_csv(snapshot_root / "daily.csv", dtype=str, keep_default_na=False)
    stockdb["date"] = stockdb["date"].astype(str)
    stockdb["symbol"] = stockdb["symbol"].astype(str)
    stockdb["close"] = pd.to_numeric(stockdb["close"], errors="coerce")
    stockdb = stockdb.loc[stockdb["date"].between(start, end), ["symbol", "date", "close"]]
    if stockdb.duplicated(["symbol", "date"]).any():
        raise ValueError("Duplicate StockDB symbol/date rows")
    stockdb_pivot = stockdb.pivot(index="symbol", columns="date", values="close")
    targets = _active_targets(targets_root)
    selection_cutoff = _selection_cutoff(targets_root)
    strict_independent_holdout = bool(selection_cutoff and start > selection_cutoff)
    prior_forward_overlap = start <= "2026-10-09" and end >= "2026-09-24"
    independent_sample = bool(strict_independent_holdout and not prior_forward_overlap)

    all_rows: list[dict] = []
    summaries: list[dict] = []
    for strategy, target_frame in sorted(targets.items()):
        for target in target_frame.itertuples(index=False):
            symbol = str(target.symbol)
            alternate = _prices_from_alternate(alternate_root, alternate_result, symbol, start, end)
            alternate_pivot = alternate.set_index("date")["close"] if not alternate.empty else pd.Series(dtype=float)
            stockdb_row = stockdb_pivot.loc[symbol] if symbol in stockdb_pivot.index else pd.Series(dtype=float)
            alt_start = alternate_pivot.get(start, np.nan)
            alt_end = alternate_pivot.get(end, np.nan)
            db_start = stockdb_row.get(start, np.nan)
            db_end = stockdb_row.get(end, np.nan)
            all_rows.append({
                "strategy": strategy,
                "symbol": symbol,
                "target_qty": float(target.target_qty),
                "alternate_start": alt_start,
                "alternate_end": alt_end,
                "stockdb_start": db_start,
                "stockdb_end": db_end,
                "alternate_return": (alt_end / alt_start - 1.0) if pd.notna(alt_start) and pd.notna(alt_end) and alt_start > 0 else np.nan,
                "stockdb_return": (db_end / db_start - 1.0) if pd.notna(db_start) and pd.notna(db_end) and db_start > 0 else np.nan,
                "close_abs_gap_start": abs(float(alt_start) - float(db_start)) if pd.notna(alt_start) and pd.notna(db_start) else np.nan,
                "close_abs_gap_end": abs(float(alt_end) - float(db_end)) if pd.notna(alt_end) and pd.notna(db_end) else np.nan,
            })

        current = pd.DataFrame([row for row in all_rows if row["strategy"] == strategy])
        alternate_metrics = _returns(current.rename(columns={"alternate_start": "close_start", "alternate_end": "close_end"}), "close")
        stockdb_metrics = _returns(current.rename(columns={"stockdb_start": "close_start", "stockdb_end": "close_end"}), "close")
        paired = current.dropna(subset=["alternate_start", "alternate_end", "stockdb_start", "stockdb_end"])
        summaries.append({
            "strategy": strategy,
            "target_symbols": int(len(current)),
            "alternate_complete_symbols": alternate_metrics["symbols"],
            "stockdb_complete_symbols": stockdb_metrics["symbols"],
            "paired_complete_symbols": int(len(paired)),
            "alternate_source": alternate_metrics,
            "stockdb_source": stockdb_metrics,
            "alternate_minus_stockdb_equal_return": (
                alternate_metrics["equal_weight_return"] - stockdb_metrics["equal_weight_return"]
                if alternate_metrics["equal_weight_return"] is not None and stockdb_metrics["equal_weight_return"] is not None else None
            ),
            "median_close_abs_gap_start": float(paired["close_abs_gap_start"].median()) if not paired.empty else None,
            "median_close_abs_gap_end": float(paired["close_abs_gap_end"].median()) if not paired.empty else None,
            "max_close_abs_gap": float(pd.concat([paired["close_abs_gap_start"], paired["close_abs_gap_end"]]).max()) if not paired.empty else None,
        })

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research" / "stockdb_independent_source_compare" / identifier
    root.mkdir(parents=True, exist_ok=False)
    frame = pd.DataFrame(all_rows)
    frame.to_csv(root / "comparison.csv", index=False)
    comparison = {
        "schema": SCHEMA,
        "id": identifier,
        "created_at": datetime.now().astimezone().isoformat(),
        "snapshot": str(snapshot_root.resolve()),
        "snapshot_source": snapshot.get("source"),
        "alternate_source": alternate_plan.get("source"),
        "alternate_provider": alternate_plan.get("provider"),
        "alternate_cache": str(alternate_root.resolve()),
        "targets_root": str(targets_root.resolve()),
        "selection_cutoff": selection_cutoff,
        "start": start,
        "end": end,
        "target_strategy_count": len(summaries),
        "target_count": int(len(frame)),
        "complete_paired_rows": int(frame.dropna(subset=["alternate_start", "alternate_end", "stockdb_start", "stockdb_end"]).shape[0]),
        "summaries": summaries,
        "independent_provider_identity": True,
        "independent_sample": independent_sample,
        "independent_sample_basis": (
            "strict_cutoff_holdout_without_prior_forward_overlap"
            if independent_sample else
            "independent_provider_and_cutoff_holdout_only"
        ),
        "strict_independent_holdout": strict_independent_holdout,
        "prior_forward_overlap": prior_forward_overlap,
        "selection_performed": False,
        "research_only": True,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": [
            "The Tencent and local StockDB feeds are separate provider identities.",
            ("The evaluation window is strictly after the target-generation cutoff, but it overlaps an earlier forward diagnostic window; it is a cutoff holdout, not a completely unused project-wide test window."
             if strict_independent_holdout and prior_forward_overlap else
             "This is a fixed-target source-consistency and sample-path check, not a strict fresh holdout or a strategy promotion result."),
            "Raw close is compared without adjustment, PIT membership, corporate actions or broker/account constraints.",
            "Missing source rows remain missing; no suspension, delisting or zero return is inferred.",
        ],
    }
    _write_json(root / "comparison.json", comparison)
    out = ROOT / "outputs" / "stockdb_independent_source_compare" / identifier
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "run.json", comparison | {"artifacts": str(root)})
    lines = [
        "# StockDB Independent-Provider Fixed-Target Comparison",
        "",
        "Status: completed read-only diagnostic; NO_TRADE.",
        f"Window: {start} to {end}; selection cutoff: {selection_cutoff or 'unknown'}; strategies: {len(summaries)}; target rows: {len(frame)}.",
        f"Complete paired target rows: {comparison['complete_paired_rows']}; alternate provider: {alternate_plan.get('provider')}; StockDB source: {snapshot.get('source')}.",
        "",
        "|Strategy|Targets|Alt complete|StockDB complete|Paired|Alt equal return|StockDB equal return|Alt minus StockDB|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        alt = item["alternate_source"]["equal_weight_return"]
        db = item["stockdb_source"]["equal_weight_return"]
        delta = item["alternate_minus_stockdb_equal_return"]
        fmt = lambda value: "unknown" if value is None else f"{value:.4%}"
        lines.append("|{}|{}|{}|{}|{}|{}|{}|{}|".format(
            item["strategy"], item["target_symbols"], item["alternate_complete_symbols"],
            item["stockdb_complete_symbols"], item["paired_complete_symbols"], fmt(alt), fmt(db), fmt(delta)))
    lines += [
        "",
        ("The window is strictly after the target-generation cutoff, but it overlaps an earlier forward diagnostic; treat it as a cutoff holdout only."
         if strict_independent_holdout and prior_forward_overlap else
         "The window is strictly after the target-generation cutoff and has no recorded overlap with the prior forward diagnostic." if independent_sample else
         "This does not claim a strict independent holdout: the target cutoff or evaluation window is not sufficiently separated."),
        "The provider comparison is independent in source identity only; no prices were substituted into the original replay.",
        "All missing rows remain unknown. No Ledger, target, order or THS state was changed.",
        "",
        "Artifacts: " + str(root),
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root, comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--alternate", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args(argv)
    root, comparison = compare(args.snapshot, args.alternate, args.targets, args.start, args.end)
    print(json.dumps({"artifacts": str(root), "summaries": comparison["summaries"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
