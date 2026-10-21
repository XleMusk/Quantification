"""Explain saved stale marks and observed reopening prices, without replay."""
import argparse
from bisect import bisect_left
from collections import Counter
from datetime import date, datetime
import json
from pathlib import Path
import uuid

import pandas as pd
import pyarrow.parquet as pq

from .. import replay_reports, settings
from ..rules import execution_rule, notional_cents, quote_value
from ..symbols import normalize_symbol
from .bulk_feed import numeric
from .experiment import write_json
from .holding_paths import held_sessions
from .turnover_attribution import action_map, project_orders


LIMITS = [
    "Provider-reported suspension is not an independently verified exchange notice",
    "Raw close notional follows saved engine rounding; missing/invalid marks stay UNKNOWN, not forward-filled",
    "Last tradable price age is distinct from the date of a provider's carried suspension quote",
    "Reopening quotes are retrospective observations inside the saved account window, never signal inputs",
    "Raw reopening ratios can include company actions; they are not holding returns, fills or attributed PnL",
    "A suspended price is not an executable liquidation value; no hypothetical NAV correction or risk guarantee",
    "Account quantities come only from saved unique fills and recorded share credits; no new ledger or replay",
    "Different saved event/reference views remain identified; this consumer does not certify independent/PIT inputs",
    "Inspected development only; defaults and execution admission unchanged. NO_TRADE",
]
OBS_COLUMNS = ["date", "symbol", "quantity", "reason", "raw_quote_date", "raw_close",
    "raw_trade_status", "raw_volume", "raw_amount", "last_tradable_date", "last_tradable_close",
    "calendar_days_since_trade", "account_sessions_since_trade", "close_equals_last_tradable",
    "raw_close_mark_cny", "saved_equity_cny", "marked_exposure_fraction", "liquidation_value_cny"]
EPISODE_COLUMNS = ["symbol", "start", "end", "held_stale_sessions", "end_quantity",
    "last_tradable_date", "last_tradable_close", "max_marked_exposure_fraction",
    "next_tradable_date", "next_raw_open", "next_raw_close", "raw_reopen_open_ratio",
    "raw_reopen_close_ratio", "next_saved_account_return", "company_actions_in_interval",
    "reopening_status", "liquidation_value_cny"]


def load_quotes(raw_view, symbols, end):
    columns = ["date", "code", "open", "close", "tradestatus", "volume", "amount", "adjustflag"]
    if not symbols:
        return pd.DataFrame(columns=[*columns, "symbol"])
    codes = [normalize_symbol(s).baostock_code for s in sorted(symbols)]
    frame = pq.read_table(Path(raw_view) / "raw_daily.parquet", columns=columns,
                          filters=[("code", "in", codes), ("date", "<=", end)]).to_pandas()
    frame["symbol"] = frame.code.map(lambda s: normalize_symbol(s).vt_symbol)
    if frame.duplicated(["date", "symbol"]).any() or not frame.adjustflag.eq("3").all():
        raise ValueError("Distinct dated raw-basis quotes required")
    for column in ("open", "close", "tradestatus", "volume", "amount"):
        frame[column] = numeric(frame[column])
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def stale_observations(marks, shares, quotes):
    days = [row["date"] for row in marks]
    day_index = {day: i for i, day in enumerate(days)}
    if days != sorted(set(days)) or quotes.duplicated(["date", "symbol"]).any():
        raise ValueError("Distinct ordered marks and raw quotes required")
    marked = {row["date"]: row for row in marks}
    lookup = {(row["date"], row["symbol"]): row for row in quotes.to_dict("records")}
    tradable = {s: batch.loc[batch.tradestatus.eq(1) & batch.close.gt(0)].to_dict("records")
                for s, batch in quotes.groupby("symbol")}
    observed = shares.loc[shares.stale_valuation]
    expected = {(mark["date"], s) for mark in marks for s in mark["stale_symbols"]}
    if set(zip(observed.date, observed.symbol)) != expected:
        raise ValueError("Every stale symbol needs its observed held quantity")
    rows = []
    for held in observed.to_dict("records"):
        day, symbol = held["date"], held["symbol"]
        source = lookup.get((day, symbol))
        close_valid = source is not None and pd.notna(source["close"]) and source["close"] > 0
        reason = ("missing_raw_row" if source is None else
                  "unpriceable_raw_close" if not close_valid else
                  "provider_suspended" if source["tradestatus"] == 0 else
                  "saved_flag_disagrees_with_tradable_quote" if source["tradestatus"] == 1 else
                  "unknown_raw_trade_status")
        history = tradable.get(symbol, [])
        i = bisect_left([r["date"] for r in history], day) - 1
        prior = history[i] if i >= 0 else None
        last_day = prior["date"] if prior else None
        rule = execution_rule(symbol, day)
        if rule.is_fund:
            raise ValueError("This raw-stock consumer does not substitute ETF profiles")
        value = notional_cents(quote_value(source["close"], rule.price_unit),
                               int(held["quantity"]), rule.price_unit) / 100 if close_valid else None
        equity = float(marked[day]["equity"])
        if equity <= 0 or value is not None and value > equity:
            raise ValueError("Invalid saved equity or stale position notional")
        rows.append({"date": day, "symbol": symbol, "quantity": int(held["quantity"]), "reason": reason,
            "raw_quote_date": day if source is not None else None,
            "raw_close": float(source["close"]) if close_valid else None,
            "raw_trade_status": float(source["tradestatus"]) if source is not None else None,
            "raw_volume": source["volume"] if source is not None else None,
            "raw_amount": source["amount"] if source is not None else None,
            "last_tradable_date": last_day, "last_tradable_close": prior["close"] if prior else None,
            "calendar_days_since_trade": (date.fromisoformat(day) - date.fromisoformat(last_day)).days
                if last_day else None,
            "account_sessions_since_trade": day_index[day] - day_index[last_day] if last_day in day_index else None,
            "close_equals_last_tradable": source["close"] == prior["close"] if close_valid and prior else None,
            "raw_close_mark_cny": value, "saved_equity_cny": equity,
            "marked_exposure_fraction": value / equity if value is not None else None,
            "liquidation_value_cny": None})
    return pd.DataFrame(rows, columns=OBS_COLUMNS)


def episode_rows(observations, marks, quotes, events):
    days = [row["date"] for row in marks]
    index = {day: i for i, day in enumerate(days)}
    equity = {row["date"]: float(row["equity"]) for row in marks}
    rows = []
    for symbol, batch in observations.groupby("symbol", sort=True):
        batch = batch.sort_values("date")
        group_ids = batch.date.map(index).diff().ne(1).cumsum()
        for _, part in batch.groupby(group_ids):
            first, last = part.iloc[0], part.iloc[-1]
            reopened = quotes.loc[quotes.symbol.eq(symbol) & quotes.date.gt(last.date)
                & quotes.date.le(days[-1]) & quotes.tradestatus.eq(1) & quotes.close.gt(0)].sort_values("date")
            after = reopened.iloc[0] if len(reopened) else None
            next_day = after.date if after is not None else None
            before = first.last_tradable_close
            actions = [e for e in events if e["kind"] == "corporate_action" and e["symbol"] == symbol
                and (pd.isna(first.last_tradable_date) or first.last_tradable_date < e["day"])
                and e["day"] <= (next_day or last.date)]
            # These look-forward price ratios describe a past reopening, never an executable account.
            ratio_known = after is not None and pd.notna(before) and before > 0
            next_i = index.get(next_day)
            rows.append({"symbol": symbol, "start": first.date, "end": last.date,
                "held_stale_sessions": len(part), "end_quantity": int(last.quantity),
                "last_tradable_date": first.last_tradable_date, "last_tradable_close": before,
                "max_marked_exposure_fraction": part.marked_exposure_fraction.max(),
                "next_tradable_date": next_day, "next_raw_open": after.open if after is not None else None,
                "next_raw_close": after.close if after is not None else None,
                "raw_reopen_open_ratio": after.open / before - 1 if ratio_known and pd.notna(after.open) and after.open > 0 else None,
                "raw_reopen_close_ratio": after.close / before - 1 if ratio_known else None,
                "next_saved_account_return": equity[next_day] / equity[days[next_i - 1]] - 1
                    if next_i is not None and next_i > 0 else None,
                "company_actions_in_interval": len(actions),
                "reopening_status": "observed_within_saved_window" if after is not None else "not_observed_within_saved_window",
                "liquidation_value_cny": None})
    return pd.DataFrame(rows, columns=EPISODE_COLUMNS)


def records(frame):
    return frame.astype(object).where(pd.notna(frame), None).to_dict("records")


def run(run_ids):
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/valuation_gaps" / identifier
    artifacts = settings.DATA_ROOT / "research/valuation_gaps" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    result = {"id": identifier, "runs": run_ids, "cases": [], "status": "running", "artifacts": str(artifacts),
              "accounts_replayed": False, "execution_admitted": False, "limits": LIMITS}
    write_json(out / "run.json", result)
    try:
        for run_id in run_ids:
            saved, root = replay_reports._read(run_id)
            raw_view = saved["protocol"].get("stock_manifest", {}).get("raw_view")
            if raw_view is None:
                raise ValueError("Saved raw-stock manifest required; no new data acquisition")
            for case in saved["results"]:
                signal = case["signal"]
                detail = replay_reports.detail(run_id, signal, include_marks=True)
                marks = detail["daily_marks"]
                folder = root / saved.get("ledger_folders", {}).get(signal, signal)
                account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
                quantities = {}
                project_orders(account, action_map(saved, run_id), daily_holdings=quantities)
                shares = held_sessions(marks, quantities)
                symbols = set(shares.loc[shares.stale_valuation, "symbol"])
                quotes = load_quotes(raw_view, symbols, marks[-1]["date"])
                observations = stale_observations(marks, shares, quotes)
                episodes = episode_rows(observations, marks, quotes, account["events"])
                unknown = int(observations.raw_close_mark_cny.isna().sum())
                daily = observations.groupby("date").agg(value=("raw_close_mark_cny", "sum"),
                    equity=("saved_equity_cny", "first"), unknown=("raw_close_mark_cny", lambda x: x.isna().sum()))
                known = daily.loc[daily.unknown.eq(0)]
                maximum = float((known.value / known.equity).max()) if len(known) else None
                destination = artifacts / (run_id + "_" + signal)
                destination.mkdir()
                observations.to_csv(destination / "stale_held_quotes.csv", index=False)
                episodes.to_csv(destination / "reopening_episodes.csv", index=False)
                item = {"case": run_id + ":" + signal, "status": detail["status"],
                    "scope": "full_saved_account" if detail["status"] == "completed" else "successful_prefix_only",
                    "sessions": len(marks), "metrics": detail["metrics"], "source_account": str(folder / "account.json"),
                    "raw_view": raw_view, "source_event_view": saved["protocol"]["stock_manifest"].get("event_view"),
                    "stale_dates": len(daily), "stale_symbol_days": len(observations), "stale_symbols": len(symbols),
                    "source_reasons": dict(Counter(observations.reason)), "unknown_mark_rows": unknown,
                    "fully_known_exposure_dates": len(known), "max_known_stale_exposure_fraction": maximum,
                    "max_account_sessions_since_trade": int(observations.account_sessions_since_trade.max())
                        if observations.account_sessions_since_trade.notna().any() else None,
                    "episodes": records(episodes), "account_replayed": False, "execution_admitted": False}
                result["cases"].append(item)
                write_json(out / "run.json", result)
                print(f"{item['case']}: {item['stale_dates']} stale days, {len(episodes)} episodes; no replay", flush=True)
    except Exception as error:
        result.update(status="failed_saved_valuation_gaps", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", result)
        raise
    result["status"] = "completed_saved_valuation_gaps"
    write_json(out / "run.json", result)
    lines = ["# Saved Valuation Gaps And Reopening", "", "Read-only saved account diagnostics. NO_TRADE.", "",
        "| Case | Sessions | Stale dates / symbol-days | Source reasons | Maximum known stale exposure | Unknown marks |",
        "| --- | ---: | ---: | --- | ---: | ---: |"]
    for item in result["cases"]:
        weight = item["max_known_stale_exposure_fraction"]
        exposure = f"{weight:.2%}" if weight is not None else "none observed" if not item["stale_dates"] else "UNKNOWN"
        lines.append(f"|{item['case']}|{item['sessions']}|{item['stale_dates']} / {item['stale_symbol_days']}|"
                     f"{item['source_reasons']}|{exposure}|{item['unknown_mark_rows']}|")
    lines += ["", "## Held Stale Episodes", "",
        "| Case / symbol | Held stale period | Sessions | End quantity | Last tradable date | Next tradable date | Raw reopen open / close ratio |",
        "| --- | --- | ---: | ---: | --- | --- | --- |"]
    for item in result["cases"]:
        for episode in item["episodes"]:
            ratios = " / ".join(f"{episode[key]:+.2%}" if episode[key] is not None else "UNKNOWN"
                                for key in ("raw_reopen_open_ratio", "raw_reopen_close_ratio"))
            lines.append(f"|{item['case']} / {episode['symbol']}|{episode['start']}..{episode['end']}|"
                f"{episode['held_stale_sessions']}|{episode['end_quantity']}|{episode['last_tradable_date']}|"
                f"{episode['next_tradable_date']}|{ratios}|")
    lines += ["", "## Boundaries", "", *["- " + value for value in LIMITS], "",
              f"Dated quotes/quantities/exposure and reopening observations: `{artifacts}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(out), flush=True)
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="Saved research run; repeat for controls")
    args = parser.parse_args(argv)
    if len(set(args.run)) != len(args.run):
        raise ValueError("Distinct saved runs required")
    run(args.run)


def saved(identifier, signal):
    source, root = replay_reports._read(identifier)
    if not replay_reports.SIGNAL.fullmatch(signal) or not any(r["signal"] == signal for r in source["results"]):
        raise FileNotFoundError("Unknown saved research signal")
    account = (root / source.get("ledger_folders", {}).get(signal, signal) / "account.json").resolve()
    account.relative_to(root.resolve())
    case_id = identifier + ":" + signal
    for folder in sorted((settings.ROOT / "outputs/valuation_gaps").glob("*"), reverse=True):
        if not folder.is_dir() or not replay_reports.RUN_ID.fullmatch(folder.name):
            continue
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        if record.get("status") != "completed_saved_valuation_gaps":
            continue
        item = next((r for r in record["cases"] if r["case"] == case_id
                     and Path(r["source_account"]).resolve() == account), None)
        if item is not None:
            return {**item, "status": "prepared", "account_status": item["status"], "diagnostic_id": record["id"],
                    "accounts_replayed": False, "execution_admitted": False, "limits": record["limits"]}
    return {"status": "not_prepared", "case": case_id, "episodes": [],
            "accounts_replayed": False, "execution_admitted": False}
