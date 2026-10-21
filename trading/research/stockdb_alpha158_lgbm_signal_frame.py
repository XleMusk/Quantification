"""Bridge a frozen Alpha158/LightGBM diagnostic into the shared replay contract.

This consumer deliberately stops at a typed SignalFrame and a weight-only
target preview.  It does not create prices, orders, Ledger state or a THS
payload.  The input diagnostic is cross-lineage research evidence, not a
current trading signal.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, TradingMode
from ..engine import choose_targets


def _json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def read_diagnostic(path: Path, *, top: int = 30) -> tuple[SignalFrame, dict]:
    path = Path(path).resolve()
    record = _json(path)
    if record.get("status") != "completed_stockdb_alpha158_lgbm_diagnostic":
        raise ValueError("Completed StockDB Alpha158/LightGBM diagnostic required")
    if any(record.get(flag) is not False for flag in
           ("execution_admitted", "strategy_validated", "model_trained",
            "ranking_generated", "target_holdings_generated", "paper_orders_generated",
            "ths_payload_generated")):
        raise ValueError("Diagnostic must remain research-only")
    if top < 1:
        raise ValueError("top must be positive")
    frame_path = Path(record.get("score_path", "")).resolve()
    frame = pd.read_parquet(frame_path)
    if frame.index.names != ["date", "symbol"]:
        raise ValueError("Diagnostic scores must use date/symbol index")
    required = {"score_seed17", "score_seed29", "score_seed43", "rank_mean"}
    if not required.issubset(frame.columns):
        raise ValueError("Diagnostic scores lack the three seed scores and rank_mean")
    date = str(record.get("date"))
    day = frame.xs(pd.Timestamp(date), level="date").copy()
    if day.empty:
        raise ValueError(f"Diagnostic score date is absent: {date}")
    if not np.isfinite(day[list(required)].to_numpy(dtype=float)).all():
        raise ValueError("Diagnostic score batch contains nonfinite values")
    day = day.sort_values(["rank_mean"], ascending=False, kind="stable").head(top)
    rows = []
    for symbol, row in day.iterrows():
        rows.append({
            "symbol": str(symbol),
            "score": float(row["rank_mean"]),
            "score_seed17": float(row["score_seed17"]),
            "score_seed29": float(row["score_seed29"]),
            "score_seed43": float(row["score_seed43"]),
        })
    signal = SignalFrame.from_legacy(
        rows,
        date,
        "research:stockdb_alpha158_lgbm_seed_ensemble_v1",
        RunContext("stockdb_alpha158_lgbm_signal_bridge", TradingMode.REPLAY),
    )
    return signal, {
        "status": "completed_stockdb_alpha158_lgbm_signal_frame",
        "diagnostic_run": str(path),
        "date": date,
        "diagnostic_score_path": str(frame_path),
        "signal_count": len(rows),
        "research_only": True,
    }


def build_target(signal: SignalFrame, *, holdings: int = 20) -> dict:
    if signal.context.mode != TradingMode.REPLAY:
        raise ValueError("Alpha158 target preview requires a replay SignalFrame")
    if holdings < 1 or holdings > len(signal.signals):
        raise ValueError("holdings must fit the signal batch")
    config = StrategyConfig(
        signal="equal_weight", holdings=holdings, retain_rank=len(signal.signals),
        gross_limit=0.8, single_limit=0.2, initial_cash=100000,
    ).validate()
    weights = choose_targets(signal.ranked_candidates(), [], config)
    return {
        "status": "completed_research_target_weight_preview",
        "strategy_id": signal.source,
        "decision_day": signal.day,
        "signal_frame": signal.to_dict(),
        "weights": weights,
        "weight_sum": sum(weights.values()),
        "selector": "trading.engine.choose_targets",
        "config": config.to_dict(),
        "research_only": True,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "Scores come from a frozen older LightGBM lineage and current StockDB proxy features.",
            "No prices, account cash, held positions, T+1 state, Portfolio/Ledger or THS client state was supplied.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic-run", required=True, type=Path)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--holdings", type=int, default=20)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-out", type=Path)
    args = parser.parse_args(argv)
    signal, record = read_diagnostic(args.diagnostic_run, top=args.top)
    if args.holdings > args.top:
        raise ValueError("holdings cannot exceed top")
    record["signal_frame"] = signal.to_dict()
    record["target_holdings_generated"] = False
    record["paper_orders_generated"] = False
    record["ths_payload_generated"] = False
    if args.target_out:
        target = build_target(signal, holdings=args.holdings)
        args.target_out.parent.mkdir(parents=True, exist_ok=True)
        args.target_out.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record["research_target"] = target
        record["research_target_generated"] = True
    else:
        record["research_target_generated"] = False
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(args.out.resolve()), "status": record["status"],
                      "signal_count": record["signal_count"],
                      "research_target_generated": record["research_target_generated"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
