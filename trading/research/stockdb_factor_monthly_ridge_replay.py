"""Replay same-lineage monthly Ridge scores through the shared Engine/Ledger.

This is a bounded research adapter.  It turns the score artifact produced by
``stockdb-factor-monthly-ridge-diagnostic`` into score-only signals, target
holdings, paper orders and simulated fills on one frozen StockDB raw-bar
snapshot.  Publication-time PIT eligibility, corporate actions and broker/THS
state are deliberately not inferred or promoted.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..settings import ROOT
from .shortline_panel_replay import load_raw_bars
from .stockdb_eligibility_view import load_eligibility_view
from .stockdb_alpha158_lgbm_panel_replay import _config


REQUIRED_SAFE_FLAGS = (
    "execution_admitted", "strategy_validated", "ranking_generated",
    "target_holdings_generated", "paper_orders_generated", "eligibility_applied",
    "shared_ledger_replay",
)


def _read_json(path: Path) -> dict:
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_ridge_run(value: Path) -> tuple[Path, dict, Path]:
    path = Path(value).resolve()
    if path.is_dir():
        path = path / "run.json"
    record = _read_json(path)
    if record.get("status") != "completed_stockdb_factor_monthly_ridge_diagnostic":
        raise ValueError("Completed monthly Ridge diagnostic required")
    unsafe = [flag for flag in REQUIRED_SAFE_FLAGS if record.get(flag) is not False]
    if unsafe:
        raise ValueError(f"Ridge diagnostic has non-research flags: {unsafe}")
    result = record.get("result") or {}
    score_path = Path(result.get("score_path", "")).resolve()
    if not score_path.is_file():
        raise ValueError(f"Ridge score artifact missing: {score_path}")
    return path, record, score_path


def load_scores(run: Path, *, start: str | None = None, end: str | None = None,
                top_per_day: int = 5, eligibility_view: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Read a Ridge score artifact and select deterministic top scores only."""
    if top_per_day < 1:
        raise ValueError("top_per_day must be positive")
    run_path, record, score_path = _resolve_ridge_run(run)
    scores = pd.read_parquet(score_path)
    if scores.index.names != ["date", "symbol"] or scores.index.has_duplicates:
        raise ValueError("Ridge scores need a unique date/symbol index")
    if scores.columns.tolist() != ["score"]:
        raise ValueError("Ridge score artifact must contain exactly one score column")
    rows = scores.reset_index()
    rows["date"] = pd.to_datetime(rows["date"], errors="raise").dt.strftime("%Y-%m-%d")
    rows["symbol"] = rows["symbol"].astype(str).str.upper()
    rows = rows.loc[rows["symbol"].str.endswith((".SSE", ".SZSE"))].copy()
    if start is not None:
        rows = rows.loc[rows["date"] >= str(start)]
    if end is not None:
        rows = rows.loc[rows["date"] <= str(end)]
    rows["score"] = pd.to_numeric(rows["score"], errors="coerce")
    rows = rows.loc[rows["score"].map(math.isfinite)].copy()
    if rows.empty:
        raise ValueError("No finite Ridge scores in requested date range")
    eligibility_applied = False
    eligibility_before = int(len(rows))
    eligibility_after = int(len(rows))
    eligibility_source = None
    if eligibility_view is not None:
        eligibility_source = str(Path(eligibility_view).resolve())
        eligible = load_eligibility_view(Path(eligibility_view))
        rows = rows.merge(eligible[["date", "symbol", "eligible_for_research"]],
                          on=["date", "symbol"], how="left", validate="one_to_one")
        rows["eligible_for_research"] = rows["eligible_for_research"].fillna(False).astype(bool)
        rows = rows.loc[rows["eligible_for_research"]].drop(columns=["eligible_for_research"])
        eligibility_after = int(len(rows))
        eligibility_applied = True
        if rows.empty:
            raise ValueError("Dated eligibility view removed every Ridge score")
    selected = (rows.sort_values(["date", "score", "symbol"], ascending=[True, False, True], kind="stable")
                .groupby("date", sort=True, group_keys=False).head(top_per_day)
                .reset_index(drop=True))
    selection = {
        "ridge_run": str(run_path),
        "score_path": str(score_path),
        "score_rows_in_range": int(len(rows)),
        "selected_event_rows": int(len(selected)),
        "selected_days": int(selected["date"].nunique()),
        "top_per_day": int(top_per_day),
        "selection_fields": ["date", "score", "symbol"],
        "future_labels_used_for_selection": False,
        "model_refit": False,
        "diagnostic_status": record["status"],
        "eligibility_applied": eligibility_applied,
        "eligibility_source": eligibility_source,
        "eligibility_score_rows_before": eligibility_before,
        "eligibility_score_rows_after": eligibility_after,
    }
    return selected[["date", "symbol", "score"]], selection


def _first_seen_cap(rows: pd.DataFrame, max_symbols: int) -> tuple[pd.DataFrame, int]:
    if max_symbols < 1:
        raise ValueError("max_symbols must be positive")
    chosen: list[str] = []
    ordered = rows.sort_values(["date", "score", "symbol"], ascending=[True, False, True], kind="stable")
    for symbol in ordered["symbol"]:
        if symbol not in chosen and len(chosen) < max_symbols:
            chosen.append(symbol)
    kept = rows.loc[rows["symbol"].isin(chosen)].copy()
    return kept, int(len(rows) - len(kept))


class MatureFactorFeed:
    """Raw bars plus close-available monthly Ridge score batches."""

    def __init__(self, bars: pd.DataFrame, candidates: pd.DataFrame):
        self.symbols = sorted(candidates["symbol"].unique())
        self.calendar = sorted(bars["date"].unique().tolist())
        indexed = bars.set_index(["date", "symbol"])
        self.rows: dict[str, dict[str, dict]] = {}
        for day in self.calendar:
            day_rows: dict[str, dict] = {}
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
        self.signals = {
            day: [{"symbol": row.symbol, "score": float(row.score)} for row in group.itertuples()]
            for day, group in candidates.groupby("date", sort=False)
        }
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {"replay_only": True, "scope": "stockdb_factor_monthly_ridge_candidate_panel",
                         "raw_ohlcv": True, "score_source": "same_lineage_monthly_ridge"}

    def next_day(self, day: str) -> str:
        index = self.calendar.index(day)
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Mature-factor replay is replay-only")
        rows = self.signals.get(day)
        if not rows or day == self.calendar[-1]:
            return SignalUnavailable("no_monthly_ridge_score_or_next_session")
        return SignalFrame.from_legacy(rows, day, "research:stockdb_factor_monthly_ridge_v1", context)


def _complete_symbols(candidates: pd.DataFrame, bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    calendar = sorted(bars["date"].unique().tolist())
    index = {day: i for i, day in enumerate(calendar)}
    available = {symbol: set(group["date"]) for symbol, group in bars.groupby("symbol", sort=False)}
    first = candidates.groupby("symbol")["date"].min().to_dict()
    complete = {symbol for symbol, first_day in first.items()
                if symbol in available and first_day in index and set(calendar[index[first_day]:]).issubset(available[symbol])}
    dropped = int((~candidates["symbol"].isin(complete)).sum())
    result = candidates.loc[candidates["symbol"].isin(complete)].copy()
    if result.empty:
        raise ValueError("No Ridge candidates have a complete raw-bar path")
    return result, dropped


def replay(ridge_run: Path, snapshot: Path, *, start: str | None = None, end: str | None = None,
           top_per_day: int = 5, max_symbols: int = 500, holdings: int = 5,
           initial_cash: float = 100000, eligibility_view: Path | None = None) -> dict:
    if holdings < 1 or holdings > top_per_day:
        raise ValueError("holdings must be between one and top_per_day")
    candidates, selection = load_scores(ridge_run, start=start, end=end, top_per_day=top_per_day,
                                        eligibility_view=eligibility_view)
    candidates, cap_dropped = _first_seen_cap(candidates, max_symbols)
    score_end = pd.Timestamp(candidates["date"].max())
    bar_end = (score_end + pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    bars = load_raw_bars(Path(snapshot), set(candidates["symbol"]),
                         start=str(candidates["date"].min()), end=bar_end)
    candidates, path_dropped = _complete_symbols(candidates, bars)
    candidates = (candidates.sort_values(["date", "score", "symbol"], ascending=[True, False, True], kind="stable")
                  .groupby("date", sort=True, group_keys=False).head(top_per_day).reset_index(drop=True))
    bars = bars.loc[bars["symbol"].isin(candidates["symbol"].unique())].copy()
    feed = MatureFactorFeed(bars, candidates)
    config = _config(initial_cash, holdings)
    context = RunContext("stockdb-factor-monthly-ridge-replay", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    daily = [advance(state, feed, day, config, index, context=context)
             for index, day in enumerate(feed.calendar)]
    blocked = [event for event in state["events"] if event.get("kind") == "order_blocked"]
    return {
        "schema": "a_share_stockdb_factor_monthly_ridge_replay_v1",
        "status": "completed_research_only_mature_factor_replay",
        "strategy_id": "stockdb_factor_monthly_ridge_v1",
        "ridge_run": selection["ridge_run"], "score_path": selection["score_path"],
        "raw_snapshot": str(Path(snapshot).resolve()), "raw_bar_end": bar_end,
        "requested_start": start, "requested_end": end, "selection": selection,
        "max_symbols": max_symbols, "causal_cap_dropped_rows": cap_dropped,
        "path_incomplete_dropped_rows": path_dropped,
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
        "pit_verified": False, "company_actions_applied": False,
        "eligibility_applied": bool(selection.get("eligibility_applied", False)), "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "This is a bounded monthly score candidate panel, not full-market or untouched validation.",
            "Ridge labels and raw tradestatus are StockDB research proxies; PIT publication and revisions are unresolved.",
            "Dated raw-bar eligibility was applied only when an explicit eligibility view was supplied; PIT publication and corporate actions remain unresolved.",
            "No account, broker or Tonghuashun state was read or changed; no real action is permitted.",
        ],
    }


def write_result(result: dict, out: Path, *, receipt_name: str = "20261024_mature_factor_stockdb_monthly_ridge_replay_receipt.json",
                 report_title: str = "StockDB monthly Ridge mature-factor Engine/Ledger replay") -> dict:
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    result_path = out / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pd.DataFrame(result["daily"]).to_csv(out / "daily.csv", index=False)
    cash_position = pd.DataFrame(result["daily"])[
        ["date", "equity", "cash", "receivables", "holdings", "fees", "turnover_value", "new_orders", "stale_symbols"]
    ].copy()
    cash_position["stale_symbols"] = cash_position["stale_symbols"].map(json.dumps)
    cash_position.to_csv(out / "cash_position_diagnostics.csv", index=False)
    pd.DataFrame(result["orders"]).to_csv(out / "orders.csv", index=False)
    pd.DataFrame(result["fills"]).to_csv(out / "fills.csv", index=False)
    (out / "account.json").write_text(json.dumps(result["state"], ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    audit = {
        "schema": "a_share_stockdb_factor_monthly_ridge_replay_audit_v1",
        "status": "passed_research_only_gates", "result": str(result_path),
        "checks": {
            "shared_engine_ledger": True, "future_labels_used_for_score": False,
            "model_refit": False, "execution_admitted": False,
            "strategy_validated": False, "ledger_mutated": False,
            "pit_verified": False, "company_actions_applied": False,
            "eligibility_applied": bool(result.get("eligibility_applied", False)), "ths_payload_generated": False,
            "selection_enabled": False, "t1_and_limit_rules_in_engine": True,
        },
        "orders": len(result["orders"]), "fills": len(result["fills"]),
        "blocked_order_event_count": result["blocked_order_event_count"],
    }
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema": "mature_factor_stockdb_monthly_ridge_replay_receipt_v1",
        "status": "COMPLETED_RESEARCH_ONLY_ENGINE_LEDGER_REPLAY",
        "run": str(result_path), "audit": str(out / "audit.json"),
        "ridge_run": result["ridge_run"], "score_path": result["score_path"],
        "raw_snapshot": result["raw_snapshot"],
        "replay_sessions": result["replay_sessions"], "symbols": result["symbols"],
        "selected_event_rows": result["selected_event_rows"],
        "orders": len(result["orders"]), "fills": len(result["fills"]),
        "metrics": result["metrics"],
        "shared_engine_ledger": True, "target_holdings_generated": True,
        "paper_orders_generated": result["paper_orders_generated"],
        "pit_verified": False, "company_actions_applied": False,
        "eligibility_applied": bool(result.get("eligibility_applied", False)), "execution_admitted": False,
        "strategy_validated": False, "ths_payload_generated": False,
        "blockers": [
            "PIT publication/revision evidence and company actions are not bound; dated raw-bar eligibility is applied only when explicitly supplied.",
            "This replay is a bounded research candidate panel and does not promote the model.",
        ],
    }
    if result.get("variant_id") is not None:
        receipt.update({
            "variant_id": result["variant_id"],
            "horizon_sessions": result.get("horizon_sessions"),
            "predeclared_variant": bool(result.get("predeclared_variant", False)),
        })
    receipt_dir = ROOT / "outputs" / "pipeline_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / receipt_name).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        f"# {report_title}", "",
        f"Sessions: `{result['replay_sessions']}`; symbols: `{result['symbols']}`; selected score rows: `{result['selected_event_rows']}`.",
        f"Orders/fills: `{len(result['orders'])}/{len(result['fills'])}`; blocked order events: `{result['blocked_order_event_count']}`.",
        f"Net return: `{result['metrics']['net_return']:.4%}`; maximum drawdown: `{result['metrics']['max_drawdown']:.4%}`.",
        "Research-only: `execution_admitted=false`, `strategy_validated=false`, `ledger_mutated=false`, `pit_verified=false`.", "",
        *["- " + item for item in result["limitations"]], "",
    ]) + "\n", encoding="utf-8")
    return {"result": str(result_path), "audit": str(out / "audit.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ridge-run", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--top-per-day", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=500)
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--eligibility-view", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = replay(args.ridge_run, args.snapshot, start=args.start, end=args.end,
                    top_per_day=args.top_per_day, max_symbols=args.max_symbols,
                    holdings=args.holdings, initial_cash=args.initial_cash,
                    eligibility_view=args.eligibility_view)
    paths = write_result(result, args.out)
    print(json.dumps({**paths, "status": result["status"], "orders": len(result["orders"]),
                      "fills": len(result["fills"]), "net_return": result["metrics"]["net_return"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
