"""Bridge a gated StockDB 190-factor rank into the shared replay contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, TradingMode
from ..engine import choose_targets


def _json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def read_rank(path: Path, *, top: int = 30) -> tuple[SignalFrame, dict]:
    path = Path(path).resolve()
    record = _json(path)
    if record.get("status") != "completed_stockdb_factor_rank_diagnostic":
        raise ValueError("Completed StockDB factor rank diagnostic required")
    if any(record.get(flag) is not False for flag in
           ("execution_admitted", "ranking_generated", "target_holdings_generated",
            "paper_orders_generated", "ths_payload_generated")):
        raise ValueError("Factor rank must remain research-only")
    if top < 1 or int(record.get("ranked_count", 0)) < top:
        raise ValueError("top must fit the saved factor rank")
    ranking_path = Path(record.get("ranking_path", "")).resolve()
    ranking = pd.read_csv(ranking_path, dtype={"symbol": str})
    required = {"rank", "symbol", "score"}
    if not required.issubset(ranking.columns):
        raise ValueError("Factor rank lacks rank, symbol and score columns")
    ranking = ranking.sort_values(["rank", "symbol"], kind="stable").head(top)
    if ranking.symbol.duplicated().any() or not pd.to_numeric(ranking.score, errors="coerce").notna().all():
        raise ValueError("Invalid or duplicate factor rank rows")
    day = str(record.get("date"))
    rows = [{"symbol": row.symbol, "score": float(row.score), "rank": int(row.rank)}
            for row in ranking.itertuples()]
    signal = SignalFrame.from_legacy(
        rows,
        day,
        "research:stockdb_factor_190_v1",
        RunContext("stockdb_factor_signal_bridge", TradingMode.REPLAY),
    )
    return signal, {"status": "completed_stockdb_factor_signal_frame",
                    "rank_run": str(path), "date": day,
                    "ranking_path": str(ranking_path), "signal_count": len(rows),
                    "research_only": True}


def build_target(signal: SignalFrame, *, holdings: int = 20) -> dict:
    if signal.context.mode != TradingMode.REPLAY:
        raise ValueError("Factor target preview requires a replay SignalFrame")
    if holdings < 1 or holdings > len(signal.signals):
        raise ValueError("holdings must fit the signal batch")
    config = StrategyConfig(signal="equal_weight", holdings=holdings,
                            retain_rank=len(signal.signals), gross_limit=0.8,
                            single_limit=0.2, initial_cash=100000).validate()
    weights = choose_targets(signal.ranked_candidates(), [], config)
    return {
        "status": "completed_research_target_weight_preview",
        "strategy_id": signal.source, "decision_day": signal.day,
        "signal_frame": signal.to_dict(), "weights": weights,
        "weight_sum": sum(weights.values()),
        "selector": "trading.engine.choose_targets", "config": config.to_dict(),
        "research_only": True, "target_holdings_generated": False,
        "paper_orders_generated": False, "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "The rank is a cross-lineage diagnostic with PIT and action semantics unresolved.",
            "No prices, account cash, held positions, T+1 state, Ledger or THS client state was supplied.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank-run", required=True, type=Path)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--holdings", type=int, default=20)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-out", type=Path)
    args = parser.parse_args(argv)
    signal, record = read_rank(args.rank_run, top=args.top)
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
