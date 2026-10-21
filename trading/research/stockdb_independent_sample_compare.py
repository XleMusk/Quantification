"""Run a disjoint StockDB/Tencent strategy comparison through one Ledger path.

The target date is fixed before the evaluation window and the evaluation ends
before the previously inspected 2026-09-24..2026-10-09 window.  The alternate
provider is used only for comparison; StockDB remains the frozen replay input.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..settings import DATA_ROOT, ROOT
from .stockdb_ledger_replay import _complete_dates, _load_prices, replay_candidate, write_candidate
from .stockdb_rolling_low_volatility import VARIANTS, RollingStockDBFeed, _read_snapshot, portfolio
from .stockdb_independent_source_compare import compare


SCHEMA = "stockdb_independent_sample_compare_v1"


def classify_sample_window(cutoff: str, start: str, end: str) -> tuple[bool, bool]:
    """Classify a comparison window without promoting the prior diagnostic."""
    prior_forward_overlap = start <= "2026-10-09" and end >= "2026-09-24"
    independent_sample = bool(start > cutoff and not prior_forward_overlap)
    return independent_sample, prior_forward_overlap


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _alternate_symbols(
    alternate_root: Path,
    start: str,
    end: str,
    expected_dates: set[str] | None = None,
) -> set[str]:
    plan = json.loads((alternate_root / "plan.json").read_text(encoding="utf-8"))
    result = json.loads((alternate_root / "result.json").read_text(encoding="utf-8"))
    if plan.get("provider") not in {"tencent", "eastmoney"}:
        raise ValueError("Independent sample requires a separate Tencent or EastMoney cache")
    if result.get("status") not in {"completed_candidate_cache", "partial_candidate_cache"}:
        raise ValueError(f"Alternate cache is not usable: {result.get('status')}")
    complete = set()
    for symbol, entry in (result.get("entries") or {}).items():
        if not entry:
            continue
        path = alternate_root / str(entry["file"])
        if not path.is_file():
            continue
        frame = pd.read_parquet(path, columns=["date", "close"])
        frame["date"] = frame["date"].astype(str)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        requested = frame.loc[frame["date"].between(start, end), ["date", "close"]]
        # Use the frozen StockDB session set when available.  Weekday counts
        # incorrectly reject valid alternate receipts across Chinese holidays.
        required = expected_dates or set(pd.date_range(start, end, freq="B").strftime("%Y-%m-%d"))
        dates = set(requested["date"])
        if dates == required and requested["close"].notna().all():
            complete.add(symbol)
    if len(complete) < 5:
        raise ValueError(f"Alternate cache has too few complete symbols: {len(complete)}")
    return complete


def _ranked_targets(frame: pd.DataFrame, cutoff: str, universe: set[str], variant: str, top: int) -> pd.DataFrame:
    feed = RollingStockDBFeed(frame, cutoff, variant=variant)
    rows = feed.rows.get(cutoff, {})
    values = feed._features
    candidates = []
    for symbol in sorted(universe):
        row = rows.get(symbol)
        if not row or row.get("volume", 0) <= 0 or bool(row.get("is_st")):
            continue
        volatility = values["volatility"].at[cutoff, symbol]
        momentum = values["momentum"].at[cutoff, symbol]
        median_amount = values["median_amount"].at[cutoff, symbol]
        if not all(pd.notna(value) for value in (volatility, momentum, median_amount)):
            continue
        if volatility <= 0 or median_amount < 20_000_000:
            continue
        candidates.append({"symbol": symbol, "volatility": float(volatility),
                           "momentum": float(momentum), "median_amount": float(median_amount)})
    ranked = feed._rank(candidates)[:top]
    if len(ranked) < top:
        raise ValueError(f"{variant} has only {len(ranked)} eligible symbols in the fixed comparison universe")
    return pd.DataFrame({
        "strategy_id": variant,
        "symbol": [row["symbol"] for row in ranked],
        "target_qty": 100,
        "active_target": True,
        "price_basis": "research_raw_close_rmb",
    })


def _freeze_snapshot(frame: pd.DataFrame, symbols: set[str], dates: list[str], out: Path) -> Path:
    selected = frame.loc[frame["symbol"].isin(symbols) & frame["date"].isin(dates)].copy()
    required = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "pre_close", "is_st"]
    selected = selected[required].sort_values(["date", "symbol"]).reset_index(drop=True)
    if selected.duplicated(["symbol", "date"]).any():
        raise ValueError("Independent sample snapshot contains duplicate symbol/date rows")
    expected = len(symbols) * len(dates)
    if len(selected) != expected:
        raise ValueError(f"Independent sample snapshot incomplete: {len(selected)}/{expected} rows")
    out.mkdir(parents=True, exist_ok=False)
    selected.to_csv(out / "daily.csv", index=False)
    pd.DataFrame([{"symbol": symbol, "date": day, "status": "observed"}
                  for day in dates for symbol in sorted(symbols)]).to_csv(out / "availability.csv", index=False)
    (out / "minute.csv").write_text("symbol,date,time,close,volume\n", encoding="utf-8")
    metadata = {
        "schema": "stockdb_research_snapshot_v1",
        "captured_at": datetime.now().astimezone().isoformat(),
        "source": "local StockDB inventory parquet, frozen independent sample",
        "requested_symbols": sorted(symbols),
        "requested_dates": dates,
        "rows": {"requested_daily": expected, "available_daily": len(selected), "missing_daily": 0,
                 "available_minute": 0},
        "files": {
            "daily": {"path": "daily.csv", "sha256": _sha256(out / "daily.csv")},
            "availability": {"path": "availability.csv", "sha256": _sha256(out / "availability.csv")},
            "minute": {"path": "minute.csv", "sha256": _sha256(out / "minute.csv")},
        },
        "research_only": True,
        "execution_admitted": False,
        "historical_feed_promoted": False,
        "selection_cutoff": dates[0],
        "limitations": [
            "Raw StockDB fields are preserved; no corporate-action or PIT promotion",
            "The alternate-provider completeness filter defines this diagnostic universe",
            "No broker/account/order/fill evidence is used",
        ],
    }
    _write_json(out / "snapshot.json", metadata)
    return out


def run(inventory: Path, alternate: Path, cutoff: str, start: str, end: str, top: int = 5):
    if not cutoff < start <= end:
        raise ValueError("Require cutoff < evaluation start <= evaluation end")
    snapshot_dir, manifest, frame, _ = _read_snapshot(inventory, end=cutoff)
    stockdb_symbols = set(frame["symbol"].unique())
    stockdb_dates = sorted(frame["date"].unique())
    stockdb_session_set = set(stockdb_dates)
    if start not in stockdb_session_set or end not in stockdb_session_set:
        raise ValueError("Independent evaluation boundaries must be actual StockDB sessions")
    evaluation_dates = {day for day in stockdb_dates if start <= day <= end}
    if not evaluation_dates:
        raise ValueError("No StockDB sessions in independent evaluation window")
    alternate_symbols = _alternate_symbols(alternate, start, end, evaluation_dates)
    universe = stockdb_symbols & alternate_symbols
    if len(universe) < top:
        raise ValueError("StockDB/alternate intersection is smaller than the target basket")
    eval_dates = [day for day in stockdb_dates if start <= day <= end]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    root = DATA_ROOT / "research" / "stockdb_independent_sample_compare" / identifier
    root.mkdir(parents=True, exist_ok=False)
    targets_root = root / "targets"
    targets_root.mkdir()
    target_paths = []
    for variant in VARIANTS:
        target_dir = targets_root / variant
        target_dir.mkdir()
        target = _ranked_targets(frame, cutoff, universe, variant, top)
        target_path = target_dir / "selected_targets.csv"
        target.to_csv(target_path, index=False)
        _write_json(target_dir / "offline_strategy_plan.json", {
            "schema": "independent_sample_fixed_target_plan_v1",
            "strategy_id": variant,
            "source_cutoff": cutoff,
            "evaluation_start": start,
            "evaluation_end": end,
            "selection_universe_count": len(universe),
            "target_count": len(target),
            "automatic_strategy_selection": False,
            "research_only": True,
            "execution_admitted": False,
        })
        target_paths.append(target_path)

    snapshot = _freeze_snapshot(frame, universe, [cutoff, *eval_dates], root / "snapshot")
    compare_root, comparison = compare(
        snapshot, alternate, targets_root, start, end,
    )
    metadata, prices = _load_prices(snapshot)
    pivot, dates = _complete_dates(prices, sorted(universe), start, end)
    ledger_root = root / "ledger_replays"
    ledger_root.mkdir()
    config = portfolio(140000.0)
    ledger_results = []
    for target_path in target_paths:
        label = target_path.parent.name
        result = replay_candidate(label, target_path, prices, pivot, dates, config, metadata)
        write_candidate(result, ledger_root / label)
        ledger_results.append({"candidate": label, "net_return": result["metrics"]["net_return"],
                               "max_drawdown": result["metrics"]["max_drawdown"],
                               "orders": result["order_count"], "fills": result["fill_count"]})
    # The current-cutoff forward window is useful for source diagnostics, but
    # it overlaps the already inspected StockDB forward receipt and must not be
    # labelled as a project-level independent sample.
    independent_sample, prior_forward_overlap = classify_sample_window(cutoff, start, end)
    record = {
        "schema": SCHEMA,
        "status": "completed_research_only",
        "artifacts": str(root),
        "snapshot": str(snapshot),
        "provider_comparison": str(compare_root),
        "selection_cutoff": cutoff,
        "evaluation_window": {"start": start, "end": end, "sessions": dates},
        "target_universe_count": len(universe),
        "target_basket_size": top,
        "strategy_count": len(VARIANTS),
        "independent_sample": independent_sample,
        "prior_forward_overlap": prior_forward_overlap,
        "independent_sample_basis": (
            "cutoff_before_window_and_window_ends_before_2026-09-24_prior_forward_diagnostic"
            if independent_sample else
            "independent_provider_and_cutoff_holdout_only_with_prior_forward_overlap"
        ),
        "alternate_provider": json.loads((alternate / "plan.json").read_text(encoding="utf-8")).get("provider"),
        "provider_complete_paired_rows": comparison["complete_paired_rows"],
        "ledger_results": ledger_results,
        "research_only": True,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": [
            "The evaluation window is a research diagnostic, not a promotion result",
            "Universe is restricted to symbols complete in the alternate provider receipt",
            "StockDB raw prices remain unadjusted and PIT membership is not established",
        ],
    }
    _write_json(root / "run.json", record)
    out = ROOT / "outputs" / "stockdb_independent_sample_compare" / identifier
    out.mkdir(parents=True, exist_ok=False)
    _write_json(out / "run.json", record)
    (out / "REPORT.md").write_text(
        "# StockDB Independent Sample Strategy Comparison\n\n"
        "Status: `completed_research_only`; execution admitted: `False`.\n\n"
        f"Selection cutoff: `{cutoff}`; evaluation: `{start}..{end}`; strategies: `{len(VARIANTS)}`; "
        f"universe: `{len(universe)}`; basket size: `{top}`.\n\n"
        "This window is disjoint from the previously inspected 2026-09-24..2026-10-09 forward diagnostic.\n"
        "The same target baskets were replayed through the shared Engine/Ledger and compared with a separate Tencent cache.\n\n"
        "|Strategy|Ledger net return|Ledger MDD|Orders|Fills|\n|---|---:|---:|---:|---:|\n"
        + "\n".join(f"|{row['candidate']}|{row['net_return']:.4%}|{row['max_drawdown']:.4%}|{row['orders']}|{row['fills']}|"
                  for row in ledger_results)
        + "\n\nArtifacts: `" + str(root) + "`\n", encoding="utf-8"
    )
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--alternate", type=Path, required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args(argv)
    out, record = run(args.inventory, args.alternate, args.cutoff, args.start, args.end, args.top)
    print(json.dumps({"artifacts": str(out), "ledger_results": record["ledger_results"],
                      "paired_rows": record["provider_complete_paired_rows"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
