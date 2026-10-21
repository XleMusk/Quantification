"""Replay frozen StockDB target baskets through the shared paper Ledger.

This is deliberately a research-only bridge.  It converts a frozen target
CSV and raw-close snapshot into explicit orders, fills, fees, T+1 availability
and daily marked equity.  It does not claim intraday execution, corporate
action completeness, broker state or live readiness.
"""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..report import metrics
from .stockdb_forward_compare import _load_coverage_manifest


SCHEMA = "stockdb_shared_ledger_replay_v1"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_targets(path):
    frame = pd.read_csv(path, dtype={"strategy_id": str, "symbol": str})
    required = {"symbol", "target_qty", "active_target"}
    if not required <= set(frame.columns):
        raise ValueError(f"{path} is missing target fields")
    active = frame.loc[frame["active_target"].astype(str).str.lower().eq("true")].copy()
    active["target_qty"] = pd.to_numeric(active["target_qty"], errors="coerce")
    if active["symbol"].duplicated().any() or active.empty:
        raise ValueError(f"{path} has no unique active targets")
    if not active["target_qty"].map(lambda value: pd.notna(value) and float(value).is_integer() and value > 0).all():
        raise ValueError(f"{path} has invalid active target quantities")
    active["target_qty"] = active["target_qty"].astype(int)
    return active[["symbol", "target_qty"]]


def _quote_cents(value):
    return int((Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(value):
    return round(value / 100, 2)


def _load_prices(snapshot_root):
    metadata = json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))
    if metadata.get("schema") != "stockdb_research_snapshot_v1":
        raise ValueError("Unsupported StockDB snapshot schema")
    if metadata.get("research_only") is not True or metadata.get("execution_admitted") is not False:
        raise ValueError("Shared-ledger replay requires an unadmitted research-only snapshot")
    path = snapshot_root / metadata["files"]["daily"]["path"]
    prices = pd.read_csv(path, dtype={"symbol": str, "date": str})
    required = {"symbol", "date", "close"}
    if not required <= set(prices.columns):
        raise ValueError("Snapshot daily file is missing symbol/date/close")
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    if prices[["symbol", "date"]].duplicated().any():
        raise ValueError("Snapshot contains duplicate symbol/date rows")
    return metadata, prices


def _complete_dates(prices, symbols, start_date, end_date):
    window = prices.loc[prices["date"].between(start_date, end_date)]
    pivot = window.pivot(index="symbol", columns="date", values="close")
    dates = sorted(window["date"].unique())
    complete = [day for day in dates if all(symbol in pivot.index and pd.notna(pivot.at[symbol, day]) for symbol in symbols)]
    return pivot, complete


class _StockDBEngineFeed:
    """Minimal frozen-snapshot feed consumed by the production engine."""

    def __init__(self, prices, symbols, path_dates):
        available = sorted(prices["date"].dropna().unique())
        prior = [day for day in available if day < path_dates[0]]
        if not prior:
            raise ValueError("Engine replay needs one prior snapshot session for volume participation")
        self.calendar = [prior[-1], *path_dates]
        self.rows = {}
        self.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in symbols}
        self.actions = []
        self.manifest = {"replay_only": True, "scope": "stockdb_raw_close_research_only"}
        self.instrument_profiles = {}
        indexed = prices.set_index(["date", "symbol"])
        volume_rows = {}
        for day in self.calendar:
            day_rows = {}
            for symbol in symbols:
                if (day, symbol) not in indexed.index:
                    raise ValueError(f"Engine feed missing {symbol} on {day}")
                source = indexed.loc[(day, symbol)]
                close = float(source["close"])
                opening = float(source["open"]) if pd.notna(source.get("open")) else close
                preclose = float(source["pre_close"]) if pd.notna(source.get("pre_close")) else close
                volume = float(source["volume"]) if pd.notna(source.get("volume")) else 0.0
                amount = float(source["amount"]) if pd.notna(source.get("amount")) else 0.0
                day_rows[symbol] = {
                    "open": opening, "high": float(source.get("high", opening)),
                    "low": float(source.get("low", opening)), "close": close,
                    "preclose": preclose, "volume": volume, "amount": amount,
                    "isST": 0, "tradestatus": 1, "adjustflag": "3",
                }
                volume_rows.setdefault(day, {})[symbol] = volume
            self.rows[day] = day_rows
        self.volumes = pd.DataFrame.from_dict(volume_rows, orient="index").reindex(index=self.calendar, columns=symbols).fillna(0.0)

    def next_day(self, day):
        index = self.calendar.index(day)
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else day

    def signal_frame(self, day, config, context):
        return SignalUnavailable("frozen_target_basket_replay_no_new_signal")


def _config(values):
    allowed = {field for field in StrategyConfig.__dataclass_fields__}
    clean = {key: value for key, value in (values or {}).items() if key in allowed}
    return StrategyConfig(**clean).validate()


def _execution_diagnostics(state, target_symbols):
    """Expose production-engine target diagnostics without treating no-trade as return."""
    events = [
        event for event in state.get("events", [])
        if event.get("kind") == "target_execution_diagnostic"
        and event.get("symbol") in set(target_symbols)
    ]
    latest = {}
    for event in events:
        latest[event["symbol"]] = event

    rows = []
    for symbol in target_symbols:
        event = latest.get(symbol, {})
        order_ids = list(event.get("order_ids") or [])
        requested = int(event.get("requested_quantity") or 0)
        filled = int(event.get("filled_quantity") or 0)
        rows.append({
            "symbol": symbol,
            "mechanical_reason": event.get("mechanical_reason"),
            "target_weight": event.get("target_weight"),
            "ideal_fractional_quantity": event.get("ideal_fractional_quantity"),
            "engine_target_quantity": event.get("engine_target_quantity"),
            "requested_quantity": requested,
            "filled_quantity": filled,
            "remaining_quantity_gap": int(event.get("remaining_quantity_gap") or 0),
            "constraint_flags": list(event.get("constraint_flags") or []),
            "order_ids": order_ids,
        })

    no_trade_rows = [row for row in rows if not row["order_ids"] and row["requested_quantity"] == 0]
    reason_counts = Counter(
        row["mechanical_reason"] or "unknown"
        for row in no_trade_rows
    )
    flag_counts = Counter(
        flag
        for row in no_trade_rows
        for flag in row["constraint_flags"]
    )
    return {
        "schema": "stockdb_target_execution_diagnostics_v1",
        "status": "diagnosed" if events else "no_diagnostic_event",
        "target_symbol_count": len(target_symbols),
        "diagnostic_event_count": len(events),
        "symbols_with_orders": [row["symbol"] for row in rows if row["order_ids"]],
        "symbols_without_orders": [row["symbol"] for row in no_trade_rows],
        "no_trade_reason_counts": dict(reason_counts),
        "no_trade_constraint_flag_counts": dict(flag_counts),
        "rows": rows,
        "interpretation": (
            "No-order rows are execution diagnostics from the production engine; "
            "they must not be interpreted as a zero-return strategy observation."
        ),
    }


def replay_candidate(label, target_path, prices, pivot, dates, config, snapshot_metadata):
    targets = _read_targets(target_path)
    symbols = targets["symbol"].tolist()
    if not dates:
        raise ValueError(f"No complete raw-close dates for {label}")
    missing = [symbol for symbol in symbols if symbol not in pivot.index or pivot.loc[symbol, dates].isna().any()]
    if missing:
        raise ValueError(f"Target basket is incomplete on the replay path: {missing[:5]}")
    start_day = dates[0]
    settlement_day = dates[1] if len(dates) > 1 else dates[0]
    context = RunContext(f"stockdb-ledger:{label}:{_sha256(target_path)[:16]}", TradingMode.REPLAY)
    state = new_state(config.initial_cash)
    feed = _StockDBEngineFeed(prices, symbols, dates)
    initial_cash_cents = state["cash"]
    state["pending_targets"] = {
        symbol: float(int(qty) * _quote_cents(pivot.at[symbol, start_day]) / initial_cash_cents)
        for symbol, qty in zip(targets["symbol"], targets["target_qty"])
    }
    state["targets"] = dict(state["pending_targets"])
    book = Ledger(state, config, context)
    daily = []
    for index, day in enumerate(dates):
        row = advance(state, feed, day, config, index, context=context)
        next_path_day = dates[index + 1] if index + 1 < len(dates) else day
        row["sellable_next_session"] = sum(book.available(symbol, next_path_day) for symbol in symbols)
        daily.append(row)
    execution_diagnostics = _execution_diagnostics(state, symbols)
    return {
        "candidate": label,
        "schema": SCHEMA,
        "status": "completed_research_only",
        "research_only": True,
        "execution_admitted": False,
        "signal_source": "frozen_target_basket",
        "broker_state_observed": False,
        "context": context.to_dict(),
        "target_file": str(target_path),
        "target_sha256": _sha256(target_path),
        "snapshot": snapshot_metadata,
        "replay": {
            "start_date": start_day,
            "end_date": dates[-1],
            "complete_path_dates": dates,
            "price_field": "raw StockDB close",
            "fill_assumption": "production engine opening fill from frozen raw OHLC with configured slippage and volume participation",
            "settlement_assumption": f"ordinary-stock buy availability begins {settlement_day}",
            "execution_admitted": False,
            "account_observed": False,
            "broker_state_observed": False,
        },
        "metrics": metrics(daily, config.initial_cash),
        "target_count": len(symbols),
        "order_count": len(state["orders"]),
        "fill_count": len(state["fills"]),
        "execution_diagnostics": execution_diagnostics,
        "ledger": {
            "initial_cash_rmb": config.initial_cash,
            "final_cash_rmb": state["cash"] / 100,
            "final_marked_equity_rmb": book.equity() / 100,
            "fees_rmb": sum(order["fees"] for order in state["orders"].values()) / 100,
            "positions": {symbol: book.quantity(symbol) for symbol in symbols},
            "t1_sellable_on_start": sum(book.available(symbol, start_day) for symbol in symbols),
            "t1_sellable_on_next_complete_date": sum(book.available(symbol, settlement_day) for symbol in symbols),
        },
        "limitations": [
            "This is a paper-only shared-Ledger replay, not a broker or client observation",
            "StockDB raw closes are used without corporate-action or PIT promotion",
            "The first close is a deterministic fill assumption, not an executable quote",
            "No sell/rebalance is implied in this one-basket forward replay",
        ],
        "state": state,
        "daily": daily,
    }


def write_candidate(result, output_root):
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(result["daily"]).to_csv(output_root / "daily.csv", index=False)
    pd.DataFrame(result["state"]["orders"].values()).to_csv(output_root / "orders.csv", index=False)
    pd.DataFrame(result["state"]["fills"].values()).to_csv(output_root / "fills.csv", index=False)
    (output_root / "account.json").write_text(json.dumps(result["state"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metrics_value = result["metrics"]
    report = [
        f"# Shared Ledger Replay: {result['candidate']}", "",
        f"Status: `completed_research_only`; execution admitted: `{result['replay']['execution_admitted']}`.",
        f"Window: `{result['replay']['start_date']}..{result['replay']['end_date']}`; complete sessions: `{len(result['replay']['complete_path_dates'])}`.",
        f"Targets/orders/fills: `{result['target_count']}/{result['order_count']}/{result['fill_count']}`.",
        f"Execution diagnostics: `{len(result['execution_diagnostics']['symbols_with_orders'])}` symbols with orders; "
        f"`{len(result['execution_diagnostics']['symbols_without_orders'])}` without orders; "
        f"reasons: `{result['execution_diagnostics']['no_trade_reason_counts']}`.",
        f"Final marked equity: `{result['ledger']['final_marked_equity_rmb']:.2f}` RMB; fees: `{result['ledger']['fees_rmb']:.2f}` RMB.",
        f"Net return after modeled fees: `{metrics_value['net_return']:.4%}`; max drawdown: `{metrics_value['max_drawdown']:.4%}`.",
        f"T+1 sellable at start/next complete session: `{result['ledger']['t1_sellable_on_start']}/{result['ledger']['t1_sellable_on_next_complete_date']}` shares.",
        "",
        "This result uses the production Ledger order/fill/cash/position/mark path in replay mode. It is not a live quote, broker fill, or promotion decision.",
    ]
    (output_root / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--targets", required=True, action="append", help="label=selected_targets.csv")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--selection-cutoff",
                        help="Selection decision cutoff used by the frozen coverage manifest")
    parser.add_argument("--coverage-manifest", type=Path,
                        help="Frozen StockDB calendar coverage_manifest_v1 for the replay window")
    parser.add_argument("--config-json", type=Path, help="Optional execution_config JSON object")
    parser.add_argument("--initial-cash", type=float, default=140000.0)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    metadata, prices = _load_prices(args.snapshot.resolve())
    values = json.loads(args.config_json.read_text(encoding="utf-8")) if args.config_json else {}
    values.setdefault("initial_cash", args.initial_cash)
    values.setdefault("signal", "equal_weight")
    config = _config(values)
    parsed = []
    all_symbols = set()
    for item in args.targets:
        if "=" not in item:
            raise ValueError("--targets entries must be label=selected_targets.csv")
        label, path = item.split("=", 1)
        path = Path(path).resolve()
        parsed.append((label.strip(), path))
        all_symbols.update(_read_targets(path)["symbol"])
    pivot, complete_dates = _complete_dates(prices, sorted(all_symbols), args.start_date, args.end_date)
    coverage_evidence = None
    if args.coverage_manifest:
        coverage_evidence = _load_coverage_manifest(
            args.coverage_manifest, args.snapshot.resolve(), args.start_date, args.end_date,
            args.selection_cutoff
        )
        dates = coverage_evidence["forward_session_dates"]
        if any(day not in complete_dates for day in dates):
            raise ValueError("Coverage manifest names a session incomplete for the replay symbol union")
    else:
        dates = complete_dates
    if args.out.exists() and any(args.out.iterdir()):
        raise FileExistsError(f"Output is non-empty: {args.out}")
    args.out.mkdir(parents=True, exist_ok=False)
    results = []
    for label, target_path in parsed:
        target_symbols = _read_targets(target_path)["symbol"].tolist()
        candidate_dates = [day for day in dates if all(symbol in pivot.index and pd.notna(pivot.at[symbol, day]) for symbol in target_symbols)]
        result = replay_candidate(label, target_path, prices, pivot, candidate_dates, config, metadata)
        folder = args.out / label
        write_candidate(result, folder)
        results.append({"candidate": label, **result["metrics"],
                        "order_count": result["order_count"], "fill_count": result["fill_count"],
                        "target_count": result["target_count"],
                        "symbols_with_orders": len(result["execution_diagnostics"]["symbols_with_orders"]),
                        "symbols_without_orders": len(result["execution_diagnostics"]["symbols_without_orders"]),
                        "no_trade_reason_counts": result["execution_diagnostics"]["no_trade_reason_counts"],
                        "output": str(folder), "execution_admitted": False})
    summary = {
        "schema": SCHEMA,
        "status": "completed_research_only",
        "generated_at": datetime.now().astimezone().isoformat(),
        "snapshot": str(args.snapshot.resolve()),
        "coverage_manifest": coverage_evidence,
        "config": config.to_dict(),
        "execution_admitted": False,
        "research_only": True,
        "broker_state_observed": False,
        "candidates": results,
        "common_path_dates": dates,
    }
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(results).to_csv(args.out / "comparison.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# StockDB Shared Ledger Replay",
        "",
        "Status: `completed_research_only`; execution admitted: `False`.",
        f"Snapshot: `{args.snapshot.resolve()}`; common complete dates: `{', '.join(dates)}`.",
        f"Calendar coverage: `{coverage_evidence['status'] if coverage_evidence else 'not supplied'}`.",
        "",
        "| Candidate | Sessions | Targets | Symbols with orders | Symbols without orders | Orders | Fills | Net return after modeled fees | Max drawdown | Final equity RMB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        lines.append(
            f"| {row['candidate']} | {row['sessions']} | {row['target_count']} | {row['symbols_with_orders']} | "
            f"{row['symbols_without_orders']} | {row['order_count'] if 'order_count' in row else '--'} | "
            f"{row['fill_count'] if 'fill_count' in row else '--'} | {row['net_return']:.4%} | "
            f"{row['max_drawdown']:.4%} | {row['final_equity']:.2f} |"
        )
    lines += [
        "",
        "The individual result folders contain the actual shared-Ledger account, orders, fills and daily marks.",
        "This is not a broker observation, live quote, or strategy promotion decision.",
    ]
    (args.out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
