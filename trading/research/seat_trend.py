"""Historical-only Donchian/seat comparisons through the existing account engine."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import uuid

import numpy as np
import pandas as pd
import talib

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session, normalize_symbol
from ..config import StrategyConfig
from ..feed import HistoricalFeed
from ..prepare_data import checked, prepare_data
from ..settings import ROOT
from .experiment import replay, write_json
from .factors import research_panel
from .seats import DEFAULT_HOME, is_investor_category_aggregate, period, read_raw

DEFAULT_CONFIG = Path(__file__).with_name("seat_trend.json")
MODES = ("TREND", "TREND_ANY_LHB", "TREND_H1", "TREND_H4", "TREND_H1_H4")


def refresh_daily(feed, end, destination):
    """Verify the live calendar/latest raw bars; replace only this research snapshot if needed."""
    socket.setdefaulttimeout(20)
    checks, differences = [], []
    with baostock_session() as bs:
        calendar = checked(bs.query_trade_dates(start_date=feed.calendar[-1], end_date=end))
        open_days = calendar.loc[calendar.is_trading_day.eq("1"), "calendar_date"].tolist()
        if not open_days:
            raise ValueError("No verified latest trading session")
        latest = open_days[-1]
        for symbol in feed.symbols:
            code = normalize_symbol(symbol).baostock_code
            fresh = checked(bs.query_history_k_data_plus(code, BAOSTOCK_FIELDS,
                            start_date=latest, end_date=latest, frequency="d", adjustflag="3"))
            if len(fresh) != 1 or fresh.iloc[0].date != latest:
                raise ValueError(f"Missing latest published bar: {symbol}")
            fields = ["open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus", "isST"]
            row = fresh.iloc[0]
            checks.append({"symbol": symbol, "date": latest, **{k: float(row[k]) for k in fields}})
            old = feed.rows.get(latest, {}).get(symbol)
            if old is None or any(not np.isclose(float(row[k]), float(old[k]), rtol=1e-8, atol=1e-4) for k in fields):
                differences.append(symbol)
    record = {"checked_at": datetime.now().astimezone().isoformat(), "source": "BaoStock",
              "requested_end": end, "latest_session": latest, "old_snapshot": str(feed.folder),
              "calendar": calendar.to_dict("records"), "latest_raw_bars": checks, "changed_symbols": differences}
    if differences:
        manifest = prepare_data(DEFAULT_HOME / "daily", end=end)
        feed = HistoricalFeed(manifest["folder"])
        record["action"] = "new_research_snapshot_downloaded"
    else:
        record["action"] = "already_current_no_rewrite"
    record["used_snapshot"] = str(feed.folder)
    write_json(destination / "daily_freshness.json", record)
    return feed


def donchian_matrices(high, low, close, entry_window=20, exit_window=10, atr_window=20):
    """The same prior-bar channels and Wilder ATR for one or many securities."""
    if min(entry_window, exit_window, atr_window) < 2:
        raise ValueError("Windows must be at least two sessions")
    if not high.index.equals(low.index) or not high.index.equals(close.index) or not high.columns.equals(low.columns) or not high.columns.equals(close.columns):
        raise ValueError("Donchian OHLC matrices must have identical identities")
    entry = high.shift(1).rolling(entry_window).max().to_numpy()
    exit_ = low.shift(1).rolling(exit_window).min().to_numpy()
    h, l, c = (frame.to_numpy(dtype=float) for frame in (high, low, close))
    atr = np.column_stack([talib.ATR(h[:, i], l[:, i], c[:, i], timeperiod=atr_window)
                           for i in range(c.shape[1])])
    active, regime = np.zeros_like(c, dtype=bool), np.zeros_like(c, dtype=np.int32)
    held, count = np.zeros(c.shape[1], dtype=bool), np.zeros(c.shape[1], dtype=np.int32)
    for i in range(len(c)):
        valid = np.isfinite(c[i]) & np.isfinite(entry[i]) & np.isfinite(exit_[i])
        enter = valid & ~held & (c[i] > entry[i])
        leave = valid & held & (c[i] < exit_[i])
        held = (held | enter) & ~leave
        count += enter
        active[i], regime[i] = held, count
    score = np.divide(c - exit_, atr, out=np.full_like(c, np.nan), where=atr > 0)
    return {"entry_channel": entry, "exit_channel": exit_, "atr": atr,
            "trend_active": active, "regime": regime, "score": score}


def donchian(panel, calendar, entry_window=20, exit_window=10, atr_window=20):
    """Close-confirmed, prior-bar channels. No shorts, pyramids or intraday fill claims."""
    parts = []
    for symbol, rows in panel.groupby("symbol"):
        frame = rows.set_index("date").reindex(pd.to_datetime(calendar)).copy()
        features = donchian_matrices(frame[["high"]].rename(columns={"high": symbol}),
                                    frame[["low"]].rename(columns={"low": symbol}),
                                    frame[["close"]].rename(columns={"close": symbol}),
                                    entry_window, exit_window, atr_window)
        for name, values in features.items():
            frame[name] = values[:, 0]
        frame["symbol"] = symbol
        frame.index.name = "date"
        parts.append(frame.reset_index())
    return pd.concat(parts, ignore_index=True).set_index(["date", "symbol"]).sort_index()


def select_seats(events, fit, minimum=2, maximum=10):
    subset = events.loc[events.eligible & ~events.seat_name.map(is_investor_category_aggregate)
                        & events.trade_date.between(*fit)].copy()
    subset = subset.drop_duplicates(["seat_name", "trade_date", "symbol"])
    counts = subset.groupby("seat_name").size().rename("stock_days").reset_index()
    counts = counts.sort_values(["stock_days", "seat_name"], ascending=[False, True])
    chosen = counts.loc[counts.stock_days.ge(minimum)].head(maximum)
    return chosen.seat_name.tolist(), counts


def delayed_day(day, calendar, lag):
    if type(lag) is not int or lag < 1:
        raise ValueError("Missing publication time requires at least one full session delay")
    index = calendar.index(day) + lag
    return calendar[index] if index < len(calendar) else None


def event_signals(events, summaries, seats, calendar, lag, threshold):
    """Only date/identity/amount fields, never vendor after-event outcome columns."""
    signals = {}
    def mark(day, symbol, field):
        if day not in calendar:
            raise ValueError(f"Disclosure outside exchange calendar: {day}")
        usable = delayed_day(day, calendar, lag)
        if usable:
            signals.setdefault((usable, symbol), {"buy": False, "sell": False, "lhb": False})[field] = True
    for row in summaries:
        if "window_kind" in row:
            kind, day, symbol = row["window_kind"], row["trade_date"], row["symbol"]
        else:
            kind, _ = period(row["EXPLANATION"] or "")
            day = row["TRADE_DATE"][:10]
            symbol = row["SECUCODE"].replace(".SH", ".SSE").replace(".SZ", ".SZSE")
        if kind == "single":
            mark(day, symbol, "lhb")
    for row in events.loc[events.eligible & ~events.seat_name.map(is_investor_category_aggregate)
                           & events.seat_name.isin(seats)].itertuples():
        ratio = row.net_cny / row.period_turnover_cny
        if abs(ratio) >= threshold:
            mark(row.trade_date, row.symbol, "buy" if ratio > 0 else "sell")
    return signals


def candidates_by_day(features, eligible, calendar, signals, mode):
    if mode not in MODES:
        raise ValueError("Unknown comparison")
    by_day, audit = {}, []
    confirmed, vetoed = set(), set()
    for day in calendar:
        rows = features.loc[pd.Timestamp(day)]
        candidates = []
        for symbol, row in rows.iterrows():
            key = (symbol, int(row.regime))
            event = signals.get((day, symbol), {})
            if row.trend_active:
                if event.get("buy") or (mode == "TREND_ANY_LHB" and event.get("lhb")):
                    confirmed.add(key)
                if event.get("sell"):
                    vetoed.add(key)
            allowed = bool(row.trend_active and eligible.get((pd.Timestamp(day), symbol), False) and np.isfinite(row.score))
            if mode in {"TREND_H1", "TREND_H1_H4", "TREND_ANY_LHB"}:
                allowed = allowed and key in confirmed
            if mode in {"TREND_H4", "TREND_H1_H4"}:
                allowed = allowed and key not in vetoed
            if allowed:
                candidates.append({"symbol": symbol, "score": float(row.score)})
            audit.append({"date": day, "symbol": symbol, "mode": mode, "trend_active": bool(row.trend_active),
                          "regime": int(row.regime), "entry_channel": row.entry_channel,
                          "exit_channel": row.exit_channel, "atr": row.atr, "score": row.score,
                          "seat_buy": bool(event.get("buy")), "seat_sell": bool(event.get("sell")),
                          "any_lhb": bool(event.get("lhb")), "candidate": allowed})
        by_day[day] = sorted(candidates, key=lambda x: (-x["score"], x["symbol"]))
    return by_day, pd.DataFrame(audit)


class TrendFeed:
    def __init__(self, base, scores):
        self.base, self.by_day = base, scores

    def __getattr__(self, key):
        return getattr(self.base, key)

    def scores(self, day, config):
        from ..contracts import SignalUnavailable
        return self.by_day.get(day, SignalUnavailable("trend_score_batch_missing"))


def validate_units(events, feed):
    single = events.loc[events.window_kind.eq("single")].drop_duplicates(["symbol", "trade_date", "period_turnover_cny"])
    rows = []
    for row in single.itertuples():
        bar = feed.rows.get(row.trade_date, {}).get(row.symbol)
        if bar and bar["amount"] > 0:
            error = row.period_turnover_cny / bar["amount"] - 1
            rows.append({"date": row.trade_date, "symbol": row.symbol, "eastmoney_cny": row.period_turnover_cny,
                         "baostock_cny": bar["amount"], "relative_error": error})
    result = pd.DataFrame(rows)
    if result.empty or result.relative_error.abs().gt(.01).any():
        raise ValueError("Seat turnover unit/coverage disagrees with raw daily turnover by more than 1%")
    return result


def run(config_path=DEFAULT_CONFIG, out=None, refresh=False):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(out) if out else ROOT / "outputs" / "seat_trend" / run_id
    out.mkdir(parents=True, exist_ok=False)
    artifacts = DEFAULT_HOME / "runs" / run_id
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {"run_id": run_id, "status": "running", "config": config, "output": str(out),
              "artifacts": str(artifacts), "live_ready": False, "strategy_validated": False,
              "started_at": datetime.now().astimezone().isoformat()}
    write_json(out / "protocol.json", record)
    try:
        portfolio = StrategyConfig(**config["portfolio"]).validate()
        if portfolio.rebalance_sessions != 1:
            raise ValueError("Daily exit candidate requires daily target generation")
        feed = HistoricalFeed(config["dataset"])
        if refresh:
            feed = refresh_daily(feed, config["requested_end"], out)
        if config["seat_fit"][1] >= config["evaluation"][0] or config["seat_fit"][0] > config["seat_fit"][1]:
            raise ValueError("Seat selection must precede evaluation")
        if config["evaluation"][0] <= feed.manifest["selection_date"] or config["evaluation"][1] > feed.calendar[-1]:
            raise ValueError("Evaluation outside point-in-time universe/data bounds")
        if any(not s.startswith(("000", "001", "002", "003", "600", "601", "603", "605")) for s in feed.symbols):
            raise ValueError("This ledger profile only supports established main-board shares")
        events_folder = Path(config["events"])
        event_manifest = json.loads((events_folder / "manifest.json").read_text(encoding="utf-8"))
        if event_manifest["status"] != "ready":
            raise ValueError("Seat collection is incomplete")
        query = event_manifest["query"]
        if query["start"] > config["seat_fit"][0] or query["end"] < config["evaluation"][1]:
            raise ValueError("Insufficient declared seat collection interval")
        if query["codes"] and not {s.split('.')[0] for s in feed.symbols}.issubset(query["codes"]):
            raise ValueError("Seat collection does not cover the entire comparison universe")
        events = pd.read_parquet(events_folder / "events.parquet")
        summaries = read_raw(events_folder, "summary")
        units = validate_units(events, feed)
        units.to_csv(out / "turnover_unit_checks.csv", index=False)
        seats, counts = select_seats(events, config["seat_fit"], config["min_seat_events"], config["max_seats"])
        counts.to_csv(out / "seat_training_counts.csv", index=False)
        write_json(out / "selected_seats.json", {"selected": seats, "fit_period": config["seat_fit"],
            "selection_uses_returns": False, "rule": config["selection_rule"], "frozen_at": datetime.now().astimezone().isoformat()})
        panel, _, eligible, data_check = research_panel(feed, portfolio)
        features = donchian(panel, feed.calendar, config["entry_window"], config["exit_window"], config["atr_window"])
        features.to_parquet(artifacts / "trend_features.parquet")
        events.to_csv(out / "disclosure_events.csv", index=False)
        eval_days = [d for d in feed.calendar if config["evaluation"][0] <= d <= config["evaluation"][1]]
        if not eval_days:
            raise ValueError("No evaluation sessions")
        results, audits = [], []
        # Fix the selected identities and rules before observing any portfolio results.
        for lag in (config["publication_lag_sessions"], config["publication_lag_sessions"] + 1):
            signals = event_signals(events, summaries, seats, feed.calendar, lag, config["seat_net_turnover_threshold"])
            for mode in MODES:
                by_day, audit = candidates_by_day(features, eligible, feed.calendar, signals, mode)
                audit = audit.loc[audit.date.isin(eval_days)].copy()
                audit["lag"] = lag
                audits.append(audit)
                destination = artifacts / f"lag{lag}_{mode}"
                outcome = replay(TrendFeed(feed, by_day), portfolio, eval_days, destination)
                state = json.loads((destination / "account.json").read_text(encoding="utf-8"))
                blocked = [e for e in state["events"] if e["kind"] == "order_blocked"]
                write_json(destination / "blocked_orders.json", blocked)
                outcome.update(mode=mode, lag=lag, period_start=eval_days[0], period_end=eval_days[-1],
                               blocked_attempts=len(blocked), candidate_stock_days=int(audit.candidate.sum()),
                               relevant_buy_stock_days=int((audit.seat_buy & audit.trend_active).sum()),
                               relevant_sell_stock_days=int((audit.seat_sell & audit.trend_active).sum()))
                results.append(outcome)
                print(f"lag={lag} {mode}: return={outcome['net_return']:.4%}, orders={outcome['orders']}", flush=True)
        metrics = pd.DataFrame(results)
        metrics.to_csv(out / "portfolio_metrics.csv", index=False)
        joined = pd.concat(audits, ignore_index=True)
        joined.to_csv(out / "daily_signal_audit.csv", index=False)
        coverage = {"collection": event_manifest, "raw_seat_rows": sum(event_manifest["reports"][k]["rows"] for k in ("buy", "sell")),
            "normalized_rows": len(events), "eligible_rows": int(events.eligible.sum()),
            "unique_symbol_days": len(events[["symbol", "trade_date"]].drop_duplicates()),
            "window_counts": events.window_kind.value_counts().to_dict(), "selected_seats": len(seats),
            "training_counts": counts.loc[counts.seat_name.isin(seats)].to_dict("records"),
            "test_selected_events": int((events.eligible & events.seat_name.isin(seats) & events.trade_date.between(*config["evaluation"])).sum()),
            "publication_time_verified": False, "matched_industry_size_control": "not_available",
            "vendor_exhaustiveness_independently_verified": False}
        write_json(out / "coverage.json", coverage)
        write_json(out / "execution_config.json", portfolio.to_dict())
        record.update(status="completed_exploratory", completed_at=datetime.now().astimezone().isoformat(),
                      dataset=str(feed.folder), data_checks=data_check, coverage=coverage, metrics=results)
        write_json(out / "run.json", record)
        report(out, record)
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    print(str(out))
    return out


def report(out, record):
    c = record["config"]
    lines = ["# Donchian + disclosed-seat historical pilot", "",
             "Status: completed exploratory comparison, NOT validated for deployment.", "",
             f"Evaluation: {c['evaluation'][0]} through {c['evaluation'][1]}; fresh virtual cash 100,000 CNY per variant.",
             f"Daily snapshot: `{record['dataset']}`.", f"Full account/daily/order/fill artifacts: `{record['artifacts']}`.", "",
             "| Variant | Publication lag (sessions) | Net return | Max drawdown | Fees CNY | Orders | Blocked attempts |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for row in record["metrics"]:
        lines.append(f"| {row['mode']} | {row['lag']} | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {row['fees']:.2f} | {row['orders']} | {row['blocked_attempts']} |")
    lines += ["", "## Rules", "",
        "A completed close above the previous 20 sessions' highs starts a long-only signal regime. A close below the previous 10 sessions' lows ends it. No current bar in the breakout channel. TA-Lib Wilder ATR20 ranks candidates only; no intraday ATR stop or pyramiding.",
        "Each daily close produces next-session targets through trading.engine.advance and the unchanged ledger. Up to five equal 16% target weights, gross <=80%, single <=20%, 100-share buys, T+1 availability, raw-price execution, corporate actions, existing fees, 10bp slippage and prior-volume participation limit. Existing daily target resizing can incur fees even without a new regime.",
        "H1 requires selected-seat net buying >=1% of the single-day turnover to confirm a currently active regime. H4 vetoes that regime after analogous net selling, until a new regime starts. The control confirms with any single-day LHB disclosure. These states do not estimate anybody's real holdings.",
        "Exact names selected only by earlier disclosure frequency, not returns or media celebrity. See selected_seats.json and seat_training_counts.csv.", "", "## Boundaries", "",
        c["scope"], c["known_feedback"], c["availability"],
        "The current universe was selected on 2023-12-29; the existing engine supports established main-board shares from 2024. Do not backdate these results to September 2023.",
        "Raw buy/sell lists are retained. Exact identifiable duplicate amounts are not added twice. Conflicting amounts, unknown/generic identities and multiday disclosures are excluded from the first single-day seat signal. Multiday start dates remain unknown, not guessed as three calendar days.",
        "Raw vendor future returns / rolling seat win-rate fields are not research features. Single-day turnover is cross-checked against BaoStock CNY amounts. Tushare daily amounts would require x1000 conversion; no Tushare credentials or purchase were used.",
        "The raw provider returned all pages for the requested pilot universe; this does not independently verify exchange-level completeness or historical publication times. Missing disclosures do not mean no trading. The generic LHB control is not industry/size/turnover matched, and few eligible events cannot establish seat alpha.",
        "No strategy promotion, production signal publishing, paper-account mutation, THS operations or real orders.", "", "## Sources", "",
        "- https://github.com/vnpy/vnpy_ctastrategy/blob/main/vnpy_ctastrategy/strategies/turtle_signal_strategy.py (rule reference; adapted Donchian variant, not an exact Turtle reproduction)",
        "- https://akshare.akfamily.xyz/data/stock/stock.html (installed Eastmoney LHB endpoint reference)",
        "- https://data.eastmoney.com/stock/lhb.html (raw public disclosures)",
        "- https://tushare.pro/document/2?doc_id=107 (seat units and duplicate/disclosure examples)",
        "- https://tushare.pro/document/2?doc_id=27 (daily turnover units)",
        "- https://investor.szse.cn/disclosure/notice/general/t20260917_622911.html (September holiday calendar)", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--refresh-daily", action="store_true")
    args = parser.parse_args(argv)
    run(args.config, args.out, args.refresh_daily)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
