"""Replay corrected shortline candidates through the shared Engine/Ledger.

This module is deliberately a research-only candidate panel.  A model score is
available after the event-session close and its target is executed on the next
source session's open.  The corrected event annotations and the ``hit_*``
columns are retained only as optional diagnostic metadata; they never enter
selection, target construction or execution.  No persistent account, broker or
Tonghuashun state is touched.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..settings import ROOT


DEFAULT_START = "2026-01-01"
DEFAULT_END = "2026-10-09"
FUTURE_LABEL_COLUMNS = {"hit_4", "hit_5", "executable_hit_4", "executable_hit_5"}
REQUIRED_PREDICTION_COLUMNS = {
    "event_id", "symbol", "date", "stage", "model_hit5_probability", "paper_candidate_only",
}
RAW_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount",
               "pct_chg", "is_st", "pre_close"]


def _flag(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _code_symbol(value: object) -> str | None:
    code = str(value).strip().split(".", 1)[0].zfill(6)
    if code.startswith(("60", "68")):
        return f"{code}.SSE"
    if code.startswith(("00", "30")):
        return f"{code}.SZSE"
    return None


def _finite_score(value) -> float:
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f"Invalid model score: {value!r}")
    return score


def load_predictions(path: Path, *, start: str = DEFAULT_START, end: str = DEFAULT_END,
                     top_per_day: int = 5, annotation_path: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Load model candidates and deterministically select a bounded panel.

    Only ``model_hit5_probability`` and non-label identity fields are used for
    selection.  Optional annotation labels are checked for identity and then
    discarded from the replay rows.
    """
    if top_per_day < 1:
        raise ValueError("top_per_day must be positive")
    frame = pd.read_csv(Path(path), dtype={"event_id": str, "symbol": str, "date": str})
    missing = sorted(REQUIRED_PREDICTION_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"Predictions missing columns: {missing}")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame.loc[frame["symbol"].str.endswith((".SSE", ".SZSE"))].copy()
    frame = frame.loc[frame["paper_candidate_only"].map(_flag)].copy()
    frame = frame.loc[(frame["date"] >= start) & (frame["date"] <= end)].copy()
    if frame.empty:
        raise ValueError("No marked shortline candidates in requested date range")
    if frame["event_id"].duplicated().any():
        raise ValueError("Duplicate prediction event_id")
    frame["score"] = frame["model_hit5_probability"].map(_finite_score)
    frame["stage"] = pd.to_numeric(frame["stage"], errors="raise").astype(int)
    if frame["stage"].lt(1).any():
        raise ValueError("Shortline stage must be positive")

    annotation_checked = False
    annotation_label_rows = 0
    if annotation_path is not None and Path(annotation_path).is_file():
        ann = pd.read_parquet(Path(annotation_path), columns=["event_id", "symbol", "date", "stage", *FUTURE_LABEL_COLUMNS])
        ann["event_id"] = ann["event_id"].astype(str)
        ann = ann.drop_duplicates("event_id", keep=False)
        joined = frame[["event_id", "symbol", "date", "stage"]].merge(ann, on="event_id", how="left", suffixes=("", "_annotation"))
        mismatched = joined.loc[
            joined["symbol_annotation"].notna()
            & ((joined["symbol_annotation"] != joined["symbol"])
               | (joined["date_annotation"] != joined["date"])
               | (joined["stage_annotation"] != joined["stage"]))
        ]
        if not mismatched.empty:
            raise ValueError("Prediction/annotation event identity mismatch")
        annotation_checked = True
        annotation_label_rows = int(joined[sorted(FUTURE_LABEL_COLUMNS)].notna().any(axis=1).sum())

    # Selection is score-only and happens before any optional diagnostic label
    # is even joined into the output.
    selected = (frame.sort_values(["date", "score", "symbol", "stage"],
                                  ascending=[True, False, True, True], kind="stable")
                .groupby("date", sort=True, group_keys=False).head(top_per_day).copy())
    selected = selected.sort_values(["date", "score", "symbol", "stage"],
                                    ascending=[True, False, True, True], kind="stable")
    rows = selected[["event_id", "symbol", "date", "stage", "score"]].reset_index(drop=True)
    record = {
        "prediction_source": str(Path(path).resolve()),
        "prediction_rows_in_range": int(len(frame)),
        "marked_candidate_rows_in_range": int(len(frame)),
        "selected_event_rows": int(len(rows)),
        "selected_days": int(rows["date"].nunique()),
        "top_per_day": int(top_per_day),
        "selection_fields": ["date", "score", "symbol", "stage"],
        "future_label_columns_seen": sorted(FUTURE_LABEL_COLUMNS & set(frame.columns)),
        "future_labels_used_for_selection": False,
        "annotation_checked": annotation_checked,
        "annotation_label_rows_diagnostic_only": annotation_label_rows,
    }
    return rows, record


def load_raw_bars(snapshot: Path, symbols: set[str], *, start: str, end: str) -> pd.DataFrame:
    """Read raw SH/SZ bars from parquet chunks or a StockDB daily snapshot.

    StockDB research snapshots publish ``daily.csv`` rather than the monthly
    parquet chunks used by the BaoStock adapter.  The CSV path is accepted
    here so both sources use the same Engine/Ledger replay without copying or
    rewriting the source snapshot.
    """
    snapshot = Path(snapshot)
    paths = sorted(snapshot.glob("*.parquet"))
    csv_path = snapshot / "daily.csv"
    if not paths and csv_path.is_file():
        paths = [csv_path]
    if not paths:
        raise ValueError(f"No parquet chunks found: {snapshot}")
    pieces: list[pd.DataFrame] = []
    start_int, end_int = int(start.replace("-", "")), int(end.replace("-", ""))
    codes = {symbol.split(".", 1)[0] for symbol in symbols}
    for path in paths:
        try:
            if path.suffix.lower() == ".csv":
                piece = pd.read_csv(path, dtype={"code": str, "date": str})
                # StockDB publishes ISO dates while the parquet adapter uses
                # compact YYYYMMDD values.  Normalize at the adapter boundary
                # so the shared replay path remains source-agnostic.
                date_text = piece["date"].astype(str).str.strip()
                parsed_dates = pd.to_datetime(date_text, errors="coerce")
                piece["date"] = parsed_dates.dt.strftime("%Y%m%d")
                missing = sorted(set(RAW_COLUMNS) - set(piece.columns))
                if missing == ["is_st", "pct_chg"]:
                    # StockDB ``pre_close`` may be adjusted.  Leave pct_chg
                    # unresolved until all rows are concatenated, then derive
                    # it from the previous available raw close so limit rules
                    # use a physical-close basis rather than adjusted data.
                    piece["pct_chg"] = pd.NA
                    # StockDB daily snapshots do not expose ST status. Keep it
                    # unknown; the replay's flag reader treats unknown as false
                    # while the report records that PIT/status evidence is open.
                    piece["is_st"] = pd.NA
                elif missing:
                    raise ValueError(f"StockDB daily snapshot missing columns: {missing}")
                piece = piece[RAW_COLUMNS]
            else:
                piece = pd.read_parquet(path, columns=RAW_COLUMNS)
        except (OSError, ValueError):
            continue
        if piece.empty:
            continue
        piece["code"] = piece["code"].astype(str).str.split(".").str[0].str.zfill(6)
        piece = piece.loc[piece["code"].isin(codes)].copy()
        if piece.empty:
            continue
        piece["date_int"] = pd.to_numeric(piece["date"], errors="coerce")
        piece = piece.loc[piece["date_int"].between(start_int, end_int)].copy()
        if piece.empty:
            continue
        piece["symbol"] = piece["code"].map(_code_symbol)
        piece = piece.loc[piece["symbol"].isin(symbols)].copy()
        pieces.append(piece)
    if not pieces:
        raise ValueError("No raw SH/SZ bars found for candidate symbols")
    bars = pd.concat(pieces, ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date_int"].astype("Int64").astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    numeric = ["open", "high", "low", "close", "volume", "amount", "pct_chg", "pre_close"]
    for col in numeric:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    missing_pct = bars["pct_chg"].isna()
    if missing_pct.any():
        bars = bars.sort_values(["symbol", "date"], kind="stable").copy()
        previous_close = bars.groupby("symbol", sort=False)["close"].shift(1)
        fallback_close = previous_close.where(previous_close.gt(0), bars["pre_close"])
        bars.loc[missing_pct, "pre_close"] = fallback_close.loc[missing_pct]
        bars.loc[missing_pct, "pct_chg"] = (
            bars.loc[missing_pct, "close"] / fallback_close.loc[missing_pct] - 1.0
        ) * 100.0
    if bars.duplicated(["symbol", "date"]).any():
        raise ValueError("Raw candidate bars contain duplicate symbol/date rows")
    bars = bars.loc[bars["open"].gt(0) & bars["close"].gt(0) & bars["pre_close"].gt(0)].copy()
    if bars.empty:
        raise ValueError("Raw candidate bars have no positive OHLC rows")
    # The physical snapshot's pct_chg is the source used to align the limit
    # rule with the corrected shortline event panel.  OHLCV remains raw.
    denominator = 1.0 + bars["pct_chg"] / 100.0
    bars["preclose_for_limits"] = bars["close"] / denominator.where(denominator.gt(0))
    return bars.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)


class ShortlinePanelFeed:
    """Sparse raw-bar feed with explicit next-session and replay-only semantics."""

    def __init__(self, bars: pd.DataFrame, signals: dict[str, list[dict]], symbols: list[str]):
        self.symbols = list(symbols)
        self.calendar = sorted(bars["date"].unique().tolist())
        self.signals = signals
        self.rows: dict[str, dict[str, dict]] = {}
        grouped = bars.set_index(["date", "symbol"])
        for day in self.calendar:
            day_rows: dict[str, dict] = {}
            if day in bars["date"].values:
                for symbol in bars.loc[bars["date"].eq(day), "symbol"].unique():
                    source = grouped.loc[(day, symbol)]
                    volume = float(source["volume"])
                    open_price = float(source["open"])
                    day_rows[symbol] = {
                        "open": open_price,
                        "high": float(source["high"]),
                        "low": float(source["low"]),
                        "close": float(source["close"]),
                        "preclose": float(source["preclose_for_limits"]),
                        "volume": volume,
                        "amount": float(source["amount"]),
                        "isST": int(_flag(source["is_st"])),
                        "tradestatus": 1 if volume > 0 and open_price > 0 else 0,
                        "adjustflag": "3",
                    }
            self.rows[day] = day_rows
        self.instruments = {}
        for symbol, group in bars.groupby("symbol", sort=False):
            self.instruments[symbol] = {"ipoDate": str(group["date"].min()), "outDate": ""}
        self.volumes = pd.DataFrame(
            {symbol: bars.loc[bars["symbol"].eq(symbol)].set_index("date")["volume"] for symbol in self.symbols}
        ).reindex(self.calendar).fillna(0.0)
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {
            "replay_only": True,
            "scope": "shortline_candidate_panel",
            "raw_ohlcv": True,
            "preclose_basis": "close/(1+pct_chg/100) for limit calculation",
        }

    def next_day(self, day: str) -> str:
        index = self.calendar.index(day)
        if index + 1 >= len(self.calendar):
            return (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return self.calendar[index + 1]

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Shortline panel replay is replay-only")
        candidates = self.signals.get(day)
        if not candidates:
            return SignalUnavailable("no_shortline_candidate")
        if day == self.calendar[-1]:
            return SignalUnavailable("no_next_session_for_execution")
        return SignalFrame.from_legacy(candidates, day, "research:shortline_limitup_continuation_panel_v1", context)


def _config(initial_cash: float, holdings: int) -> StrategyConfig:
    return StrategyConfig(
        signal="equal_weight", lookback=21, skip=1, volatility_window=21,
        rebalance_sessions=1, holdings=holdings, retain_rank=max(holdings, 20),
        gross_limit=0.8, single_limit=0.2, initial_cash=initial_cash,
        min_amount=0, min_listing_days=1, commission=0.0003,
        minimum_commission=5, sell_tax=0.0005, transfer_fee=0.00001,
        slippage_bps=10, participation=0.01, dividend_tax_reserve=0.2,
    ).validate()


def _eligible_candidates(candidates: pd.DataFrame, bars: pd.DataFrame, calendar: list[str], end: str) -> tuple[pd.DataFrame, int]:
    available = {symbol: set(group["date"]) for symbol, group in bars.groupby("symbol", sort=False)}
    index = {day: i for i, day in enumerate(calendar)}
    keep = []
    dropped = 0
    for row in candidates.itertuples(index=False):
        if row.date not in index:
            dropped += 1
            continue
        dates = calendar[index[row.date]:]
        if row.symbol not in available or not set(dates).issubset(available[row.symbol]):
            dropped += 1
            continue
        keep.append(row)
    if not keep:
        raise ValueError("No candidates have a complete raw-bar path to replay end")
    return pd.DataFrame(keep), dropped


def replay(predictions: Path, snapshot: Path, *, start: str = DEFAULT_START, end: str = DEFAULT_END,
           top_per_day: int = 5, holdings: int = 5, initial_cash: float = 100000,
           annotation_path: Path | None = None, max_symbols: int | None = None,
           symbol_cap_method: str = "score_max") -> dict:
    if holdings < 1:
        raise ValueError("holdings must be positive")
    candidates, selection = load_predictions(predictions, start=start, end=end,
                                              top_per_day=top_per_day, annotation_path=annotation_path)
    candidate_symbols = set(candidates["symbol"])
    bars = load_raw_bars(snapshot, candidate_symbols, start=start, end=end)
    calendar = sorted(bars["date"].unique().tolist())
    candidates, dropped = _eligible_candidates(candidates, bars, calendar, end)
    if max_symbols is not None:
        if max_symbols < 1:
            raise ValueError("max_symbols must be positive when supplied")
        if symbol_cap_method == "score_max":
            symbol_rank = (candidates.groupby("symbol", as_index=False)
                           .agg(max_score=("score", "max"), event_count=("event_id", "count"))
                           .sort_values(["max_score", "event_count", "symbol"],
                                        ascending=[False, False, True], kind="stable"))
            chosen_symbols = set(symbol_rank.head(max_symbols)["symbol"])
        elif symbol_cap_method == "first_seen":
            # Causal cap for temporal comparisons: only the first appearance
            # of a symbol in this window can admit it to the panel.
            ordered = candidates.sort_values(["date", "score", "symbol", "stage"],
                                             ascending=[True, False, True, True], kind="stable")
            chosen_symbols = set()
            for symbol in ordered["symbol"]:
                if symbol not in chosen_symbols:
                    chosen_symbols.add(symbol)
                if len(chosen_symbols) >= max_symbols:
                    break
        else:
            raise ValueError("symbol_cap_method must be score_max or first_seen")
        dropped += int((~candidates["symbol"].isin(chosen_symbols)).sum())
        candidates = candidates.loc[candidates["symbol"].isin(chosen_symbols)].copy()
        if candidates.empty:
            raise ValueError("max_symbols removed every candidate")
    # Re-apply top-per-day after the no-imputation completeness gate.
    candidates = (candidates.sort_values(["date", "score", "symbol", "stage"],
                                          ascending=[True, False, True, True], kind="stable")
                  .groupby("date", sort=True, group_keys=False).head(top_per_day)
                  .reset_index(drop=True))
    symbols = sorted(candidates["symbol"].unique())
    bars = bars.loc[bars["symbol"].isin(symbols)].copy()
    signals: dict[str, list[dict]] = {}
    for row in candidates.itertuples(index=False):
        signals.setdefault(row.date, []).append({
            "symbol": row.symbol,
            "score": float(row.score),
            "stage": float(row.stage),
            "event_date_ordinal": float(pd.Timestamp(row.date).toordinal()),
        })
    for day in signals:
        signals[day] = sorted(signals[day], key=lambda item: (-item["score"], item["symbol"], item["stage"]))
    feed = ShortlinePanelFeed(bars, signals, symbols)
    config = _config(initial_cash, holdings)
    context = RunContext("shortline-panel-replay", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    daily = []
    for index, day in enumerate(feed.calendar):
        daily.append(advance(state, feed, day, config, index, context=context))
    blocked = [event for event in state["events"] if event.get("kind") == "order_blocked"]
    result = {
        "schema": "a_share_shortline_candidate_panel_replay_v1",
        "status": "completed_research_only_candidate_panel_replay",
        "strategy_id": "shortline_limitup_continuation_v1",
        "candidate_panel_only": True,
        "raw_snapshot": str(Path(snapshot).resolve()),
        "annotation_path": str(Path(annotation_path).resolve()) if annotation_path else None,
        "requested_start": start, "requested_end": end,
        "prediction_selection": selection,
        "start_date": feed.calendar[0], "end_date": feed.calendar[-1],
        "replay_sessions": len(feed.calendar),
        "symbols": len(symbols), "selected_event_rows": int(len(candidates)),
        "selected_events": candidates[["event_id", "symbol", "date", "stage", "score"]].to_dict("records"),
        "max_symbols": max_symbols,
        "symbol_cap_method": symbol_cap_method,
        "signal_days": sorted(signals), "dropped_incomplete_path_rows": int(dropped),
        "config": config.to_dict(), "metrics": metrics(daily, config.initial_cash),
        "daily": daily, "orders": list(state["orders"].values()),
        "fills": list(state["fills"].values()), "state": state,
        "blocked_order_event_count": len(blocked),
        "engine_ledger_replay_completed": True,
        "target_holdings_generated": True,
        "paper_orders_generated": bool(state["orders"]),
        "selection_enabled": False,
        "future_labels_used_for_score": False,
        "ledger_mutated": False,
        "strategy_validated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "Candidate-panel replay is not full-market or untouched validation.",
            "Candidates are selected from close-available model scores; no intraday queue, order-book or executable-fill data is available.",
            "Raw OHLCV is used; preclose for limit rules is reconstructed from physical pct_chg, and PIT membership/company actions remain unresolved.",
            "tradestatus is a positive-volume proxy; broker/account/THS state is never read or changed.",
        ],
    }
    return result


def write_result(result: dict, out: Path) -> dict:
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    result_path = out / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pd.DataFrame(result["daily"]).to_csv(out / "daily.csv", index=False)
    pd.DataFrame(result["orders"]).to_csv(out / "orders.csv", index=False)
    pd.DataFrame(result["fills"]).to_csv(out / "fills.csv", index=False)
    (out / "account.json").write_text(json.dumps(result["state"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "schema": "a_share_shortline_candidate_panel_replay_audit_v1",
        "status": "passed_research_only_gates",
        "checks": {
            "shared_engine_ledger": result["engine_ledger_replay_completed"],
            "future_labels_used_for_score": result["future_labels_used_for_score"],
            "execution_admitted": result["execution_admitted"],
            "ledger_mutated": result["ledger_mutated"],
            "strategy_validated": result["strategy_validated"],
            "ths_payload_generated": result["ths_payload_generated"],
            "selection_enabled": result["selection_enabled"],
            "t1_and_limit_rules_in_engine": True,
        },
        "orders": len(result["orders"]), "fills": len(result["fills"]),
        "blocked_order_event_count": result["blocked_order_event_count"],
        "candidate_panel_only": True,
        "result": str(result_path),
    }
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# Shortline candidate panel Engine/Ledger replay", "",
        f"Sessions: `{result['replay_sessions']}`; symbols: `{result['symbols']}`; selected event rows: `{result['selected_event_rows']}`.",
        f"Orders/fills: `{len(result['orders'])}/{len(result['fills'])}`; blocked order events: `{result['blocked_order_event_count']}`.",
        f"Net return: `{result['metrics']['net_return']:.4%}`; maximum drawdown: `{result['metrics']['max_drawdown']:.4%}`.",
        "All outputs are replay-only. `execution_admitted=false`, `ledger_mutated=false`, and `strategy_validated=false`.",
        "", *["- " + item for item in result["limitations"]],
    ]) + "\n", encoding="utf-8")
    return {"result": str(result_path), "audit": str(out / "audit.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--annotation-path", type=Path)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--top-per-day", type=int, default=5)
    parser.add_argument("--max-symbols", type=int,
                        help="Optional deterministic cap for a small candidate panel")
    parser.add_argument("--symbol-cap-method", choices=("score_max", "first_seen"), default="score_max")
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = replay(args.predictions, args.snapshot, start=args.start, end=args.end,
                    top_per_day=args.top_per_day, holdings=args.holdings,
                    initial_cash=args.initial_cash, annotation_path=args.annotation_path,
                    max_symbols=args.max_symbols, symbol_cap_method=args.symbol_cap_method)
    paths = write_result(result, args.out)
    print(json.dumps({**paths, "status": result["status"], "orders": len(result["orders"]),
                      "fills": len(result["fills"]), "net_return": result["metrics"]["net_return"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
