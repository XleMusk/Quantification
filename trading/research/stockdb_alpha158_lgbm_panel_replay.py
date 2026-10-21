"""Replay a dated Alpha158/LightGBM score batch through shared Engine/Ledger.

The score batch is consumed as a close-time research signal.  Targets execute
on the following raw-bar session.  Selection uses only ``rank_mean`` and the
score-date identity; no future labels, broker state, THS payload, or account
state is read or changed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from .shortline_panel_replay import load_raw_bars
from .stockdb_alpha158_lgbm_diagnostic import SCORE_NORMALIZATION_METHOD


LEGACY_SCORE_NORMALIZATION = "whole_batch_cross_date_seed_percentile_rank_mean_v1"


def _json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def load_scores(batch_run: Path, *, top_per_day: int = 5,
                start: str | None = None, end: str | None = None) -> tuple[pd.DataFrame, dict]:
    path = Path(batch_run).resolve()
    if path.is_dir():
        path /= "result.json"
    record = _json(path)
    if record.get("status") != "completed_research_only_dated_score_batch":
        raise ValueError("Completed dated Alpha158/LightGBM score batch required")
    for flag in ("model_refit", "future_labels_used_for_score", "execution_admitted",
                 "strategy_validated", "ledger_mutated", "target_holdings_generated",
                 "paper_orders_generated", "ths_payload_generated"):
        if record.get(flag) is not False:
            raise ValueError(f"Score batch has unsafe or unverified flag: {flag}")
    if top_per_day < 1:
        raise ValueError("top_per_day must be positive")
    score_path = Path(record.get("score_path", "")).resolve()
    scores = pd.read_parquet(score_path)
    required = {"date", "symbol", "rank_mean", "score_seed17", "score_seed29", "score_seed43"}
    if not required.issubset(scores.columns):
        raise ValueError(f"Dated scores missing columns: {sorted(required - set(scores.columns))}")
    scores["date"] = pd.to_datetime(scores["date"], errors="raise").dt.strftime("%Y-%m-%d")
    scores["symbol"] = scores["symbol"].astype(str).str.upper()
    seed_columns = ["score_seed17", "score_seed29", "score_seed43"]
    scores[seed_columns] = scores[seed_columns].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(scores[seed_columns].to_numpy()).all():
        raise ValueError("Dated scores require finite saved seed predictions")
    # Old saved rank_mean values normalized across the whole multi-date batch.
    # Reuse frozen seed predictions, never that aggregate or a new model fit.
    # Normalize before slicing so a requested date is identical in every range.
    scores["rank_mean"] = scores.groupby("date", sort=False)[seed_columns].rank(pct=True).mean(axis=1)
    scores = scores.loc[scores["symbol"].str.endswith((".SSE", ".SZSE"))].copy()
    if start:
        scores = scores.loc[scores["date"] >= str(start)]
    if end:
        scores = scores.loc[scores["date"] <= str(end)]
    if scores.empty:
        raise ValueError("No dated scores in requested window")
    selected = (scores.sort_values(["date", "rank_mean", "symbol"], ascending=[True, False, True], kind="stable")
                .groupby("date", sort=True, group_keys=False).head(top_per_day).copy())
    selected = selected[["date", "symbol", "rank_mean", "score_seed17", "score_seed29", "score_seed43"]]
    selected = selected.rename(columns={"rank_mean": "score"}).reset_index(drop=True)
    return selected, {"batch_run": str(path), "score_path": str(score_path),
                      "score_rows_in_range": int(len(scores)), "selected_event_rows": int(len(selected)),
                      "selected_days": int(selected["date"].nunique()), "top_per_day": int(top_per_day),
                      "saved_score_normalization_method": record.get("score_normalization_method", LEGACY_SCORE_NORMALIZATION),
                      "selection_score_normalization_method": SCORE_NORMALIZATION_METHOD,
                      "saved_rank_mean_used_for_selection": False,
                      "normalization_recomputed_from_saved_seed_scores": True,
                      "model_refit": False, "source_payload_modified": False,
                      "future_labels_used_for_selection": False}


def _first_seen_cap(rows: pd.DataFrame, max_symbols: int) -> tuple[pd.DataFrame, int]:
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive")
    chosen: list[str] = []
    ordered = rows.sort_values(["date", "score", "symbol"], ascending=[True, False, True], kind="stable")
    for symbol in ordered["symbol"]:
        if symbol not in chosen and len(chosen) < max_symbols:
            chosen.append(symbol)
    result = rows.loc[rows["symbol"].isin(chosen)].copy()
    return result, int(len(rows) - len(result))


class ScorePanelFeed:
    def __init__(self, bars: pd.DataFrame, candidates: pd.DataFrame):
        self.symbols = sorted(candidates["symbol"].unique())
        self.calendar = sorted(bars["date"].unique().tolist())
        indexed = bars.set_index(["date", "symbol"])
        self.rows: dict[str, dict[str, dict]] = {}
        for day in self.calendar:
            day_rows = {}
            for symbol in bars.loc[bars["date"].eq(day), "symbol"].unique():
                row = indexed.loc[(day, symbol)]
                day_rows[symbol] = {
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "preclose": float(row["preclose_for_limits"]), "volume": float(row["volume"]),
                    "amount": float(row["amount"]),
                    "isST": 1 if str(row["is_st"]).strip().lower() in {"1", "true", "yes"} else 0,
                    "tradestatus": 1 if float(row["volume"]) > 0 and float(row["open"]) > 0 else 0,
                    "adjustflag": "3",
                }
            self.rows[day] = day_rows
        self.instruments = {symbol: {"ipoDate": str(group["date"].min()), "outDate": ""}
                            for symbol, group in bars.groupby("symbol", sort=False)}
        self.volumes = pd.DataFrame({symbol: bars.loc[bars["symbol"].eq(symbol)].set_index("date")["volume"]
                                     for symbol in self.symbols}).reindex(self.calendar).fillna(0.0)
        grouped = candidates.groupby("date", sort=False)
        self.signals = {
            day: [{"symbol": row.symbol, "score": float(row.score),
                   "seed17": float(row.score_seed17), "seed29": float(row.score_seed29),
                   "seed43": float(row.score_seed43)} for row in group.itertuples()]
            for day, group in grouped
        }
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {"replay_only": True, "scope": "stockdb_alpha158_lgbm_candidate_panel",
                         "raw_ohlcv": True, "score_source": "dated_frozen_lightgbm_batch"}

    def next_day(self, day: str) -> str:
        index = self.calendar.index(day)
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Score panel replay is replay-only")
        rows = self.signals.get(day)
        if not rows or day == self.calendar[-1]:
            return SignalUnavailable("no_score_batch_or_next_session")
        return SignalFrame.from_legacy(rows, day, "research:stockdb_alpha158_lgbm_panel_v1", context)


def _config(initial_cash: float, holdings: int) -> StrategyConfig:
    return StrategyConfig(signal="equal_weight", lookback=21, skip=1, volatility_window=21,
                          rebalance_sessions=1, holdings=holdings, retain_rank=max(holdings, 20),
                          gross_limit=0.8, single_limit=0.2, initial_cash=initial_cash,
                          min_amount=0, min_listing_days=1, commission=0.0003,
                          minimum_commission=5, sell_tax=0.0005, transfer_fee=0.00001,
                          slippage_bps=10, participation=0.01, dividend_tax_reserve=0.2).validate()


def replay(batch_run: Path, snapshot: Path, *, start: str | None = None, end: str | None = None,
           top_per_day: int = 5, max_symbols: int = 20, holdings: int = 5,
           initial_cash: float = 100000) -> dict:
    if holdings < 1 or holdings > top_per_day:
        raise ValueError("holdings must be between one and top_per_day")
    candidates, selection = load_scores(batch_run, top_per_day=top_per_day, start=start, end=end)
    candidates, cap_dropped = _first_seen_cap(candidates, max_symbols)
    score_end = pd.Timestamp(candidates["date"].max())
    # Keep the next raw session after the final close-time score in the feed;
    # otherwise the final target would be silently treated as no-decision.
    bar_end = (score_end + pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    bars = load_raw_bars(Path(snapshot), set(candidates["symbol"]),
                         start=str(candidates["date"].min()), end=bar_end)
    calendar = sorted(bars["date"].unique().tolist())
    first = candidates.groupby("symbol")["date"].min().to_dict()
    available = {symbol: set(group["date"]) for symbol, group in bars.groupby("symbol", sort=False)}
    complete = {symbol for symbol, first_day in first.items()
                if symbol in available and first_day in calendar and set(calendar[calendar.index(first_day):]).issubset(available[symbol])}
    path_dropped = int((~candidates["symbol"].isin(complete)).sum())
    candidates = candidates.loc[candidates["symbol"].isin(complete)].copy()
    if candidates.empty:
        raise ValueError("No score candidates have a complete raw-bar path")
    bars = bars.loc[bars["symbol"].isin(candidates["symbol"].unique())].copy()
    feed = ScorePanelFeed(bars, candidates)
    config = _config(initial_cash, holdings)
    context = RunContext("stockdb-alpha158-lgbm-panel-replay", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    daily = [advance(state, feed, day, config, index, context=context)
             for index, day in enumerate(feed.calendar)]
    blocked = [event for event in state["events"] if event.get("kind") == "order_blocked"]
    return {"schema": "a_share_stockdb_alpha158_lgbm_candidate_panel_replay_v1",
            "status": "completed_research_only_candidate_panel_replay",
            "strategy_id": "stockdb_alpha158_lgbm_candidate_panel_v1",
            "batch_run": selection["batch_run"], "score_path": selection["score_path"],
            "raw_snapshot": str(Path(snapshot).resolve()), "raw_bar_end": bar_end,
            "requested_start": start,
            "requested_end": end, "selection": selection, "max_symbols": max_symbols,
            "causal_cap_dropped_rows": cap_dropped, "path_incomplete_dropped_rows": path_dropped,
            "start_date": feed.calendar[0], "end_date": feed.calendar[-1],
            "replay_sessions": len(feed.calendar), "symbols": len(feed.symbols),
            "selected_event_rows": int(len(candidates)),
            "selected_events": candidates[["date", "symbol", "score"]].to_dict("records"),
            "config": config.to_dict(), "metrics": metrics(daily, config.initial_cash),
            "daily": daily, "orders": list(state["orders"].values()), "fills": list(state["fills"].values()),
            "state": state, "blocked_order_event_count": len(blocked),
            "engine_ledger_replay_completed": True, "target_holdings_generated": True,
            "paper_orders_generated": bool(state["orders"]), "selection_enabled": False,
            "future_labels_used_for_score": False, "model_refit": False,
            "ledger_mutated": False, "strategy_validated": False,
            "ths_payload_generated": False, "execution_admitted": False,
            "limitations": [
                "Candidate panel is bounded and not full-market or untouched validation.",
                "Raw status is positive-volume proxy; PIT membership and company actions remain unresolved.",
                "No intraday queue/order-book evidence or account/broker/THS state was used.",
            ]}


def write_result(result: dict, out: Path) -> dict:
    out = Path(out).resolve(); out.mkdir(parents=True, exist_ok=False)
    result_path = out / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pd.DataFrame(result["daily"]).to_csv(out / "daily.csv", index=False)
    pd.DataFrame(result["orders"]).to_csv(out / "orders.csv", index=False)
    pd.DataFrame(result["fills"]).to_csv(out / "fills.csv", index=False)
    (out / "account.json").write_text(json.dumps(result["state"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {"schema": "a_share_stockdb_alpha158_lgbm_candidate_panel_replay_audit_v1",
             "status": "passed_research_only_gates", "result": str(result_path),
             "checks": {"shared_engine_ledger": True, "future_labels_used_for_score": False,
                        "model_refit": False, "execution_admitted": False, "strategy_validated": False,
                        "ledger_mutated": False, "ths_payload_generated": False,
                        "selection_enabled": False, "t1_and_limit_rules_in_engine": True},
             "orders": len(result["orders"]), "fills": len(result["fills"]),
             "blocked_order_event_count": result["blocked_order_event_count"]}
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB Alpha158/LightGBM candidate-panel Engine/Ledger replay", "",
        f"Sessions: `{result['replay_sessions']}`; symbols: `{result['symbols']}`; selected events: `{result['selected_event_rows']}`.",
        f"Orders/fills: `{len(result['orders'])}/{len(result['fills'])}`; blocked order events: `{result['blocked_order_event_count']}`.",
        f"Net return: `{result['metrics']['net_return']:.4%}`; maximum drawdown: `{result['metrics']['max_drawdown']:.4%}`.",
        "Replay-only: `execution_admitted=false`, `strategy_validated=false`, `ledger_mutated=false`.", "",
        *["- " + item for item in result["limitations"]], "",
    ]) + "\n", encoding="utf-8")
    return {"result": str(result_path), "audit": str(out / "audit.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-run", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--top-per-day", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = replay(args.batch_run, args.snapshot, start=args.start, end=args.end,
                    top_per_day=args.top_per_day, max_symbols=args.max_symbols,
                    holdings=args.holdings, initial_cash=args.initial_cash)
    paths = write_result(result, args.out)
    print(json.dumps({**paths, "status": result["status"], "orders": len(result["orders"]),
                      "fills": len(result["fills"]), "net_return": result["metrics"]["net_return"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
