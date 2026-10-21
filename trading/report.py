import json
import math
import pandas as pd

from . import settings, store
from .contracts import Price, PriceBasis


def quote_rmb(value, unit):
    if unit not in {"cent", "mill"}:
        raise ValueError("Unknown report quote unit")
    if type(value) is int and value == 0:
        return 0.
    price = Price(value, PriceBasis.RAW) if unit == "cent" else Price.from_mills(value, PriceBasis.RAW)
    return float(price.rmb)


def metrics(daily, initial_cash):
    if not daily:
        return None
    frame = pd.DataFrame(daily)
    equity = pd.to_numeric(frame.equity, errors="raise")
    if not math.isfinite(initial_cash) or initial_cash <= 0 or not equity.map(math.isfinite).all() or equity.le(0).any():
        raise ValueError("Invalid account equity or initial capital")
    peaks = equity.cummax().clip(lower=initial_cash)
    # Include the first session's costs/return from the opening account capital.
    returns = equity.pct_change(fill_method=None).fillna(equity.iloc[0] / initial_cash - 1)
    volatility = float(returns.std(ddof=1)) if len(returns) > 1 else None
    sharpe = math.sqrt(252) * float(returns.mean()) / volatility if volatility and volatility > 0 else None
    return {"sessions": len(frame), "net_return": float(equity.iloc[-1] / initial_cash - 1),
            "max_drawdown": float((equity / peaks - 1).min()), "fees": float(frame.fees.sum()),
            "turnover_value": float(frame.turnover_value.sum()), "final_equity": float(equity.iloc[-1]),
            "stale_valuation_days": int(frame.stale_symbols.map(bool).sum()),
            "sharpe_zero_rf_252": sharpe,
            "annualized_volatility": volatility * math.sqrt(252) if volatility is not None else None,
            "worst_day": float(returns.min())}


def comparison(identifier):
    run = store.get(identifier, full=True)
    if run is None:
        raise ValueError("Unknown run")
    daily = store.daily(identifier)
    initial = run["config"]["initial_cash"]
    segments = []
    for year, label in (("2024", "development"), ("2025", "validation"), ("2026", "test_not_tuned")):
        selected = [row for row in daily if row["date"].startswith(year)]
        if selected:
            first = daily.index(selected[0])
            beginning = daily[first - 1]["equity"] if first else initial
            segments.append({"segment": label, "year": year, **metrics(selected, beginning)})
    bound = run["state"].get("broker_binding")
    return {"id": identifier, "name": run["name"], "signal": run["config"]["signal"], "status": run["status"],
            "config": run["config"], "dataset": run["dataset"], "start": run["start"], "end": run["end"], "metrics": metrics(daily, initial), "segments": segments,
            "strategy_validated": False, "scope": "ths_simulated_account_observations" if bound else "historical_10_name_pilot",
            "execution_source": "broker_observation" if bound else "simulator"}


def export(identifier, completed=False):
    run = store.get(identifier, full=True)
    rows = store.daily(identifier)
    folder = settings.DATA_ROOT / "results" / identifier
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{k: v for k, v in row.items() if k != "events"} for row in rows]).to_csv(folder / "daily.csv", index=False)
    pd.DataFrame(run["state"]["orders"].values()).to_csv(folder / "orders.csv", index=False)
    pd.DataFrame(run["state"]["fills"].values()).to_csv(folder / "fills.csv", index=False)
    result = comparison(identifier)
    if completed:
        result["status"] = "completed"
    (folder / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "account.json").write_text(json.dumps(run["state"], ensure_ascii=False, indent=2), encoding="utf-8")


def detail(identifier):
    run = store.get(identifier, full=True)
    if run is None:
        return None
    state = run.pop("state")
    from .config import StrategyConfig
    from .ledger import Ledger
    book = Ledger(state, StrategyConfig(**run["config"]))
    day = state["day"] or run["start"]
    holdings = [{"symbol": s, "quantity": book.quantity(s), "sellable": book.available(s, day),
                 "price": quote_rmb(state["prices"].get(s, 0), state.get("price_units", {}).get(s, "cent")),
                 "price_unit": state.get("price_units", {}).get(s, "cent"),
                 "value": book.position_value(s) / 100}
                for s in state["positions"] if book.quantity(s)]
    orders = [{**order, "price_unit": order.get("price_unit", "cent"),
               "limit_price_rmb": quote_rmb(order["limit_price"], order.get("price_unit", "cent"))}
              for order in state["orders"].values()]
    fills = [{**fill, "price_unit": fill.get("price_unit", "cent"),
              "price_rmb": quote_rmb(fill["price"], fill.get("price_unit", "cent"))}
             for fill in state["fills"].values()]
    signals = [event for event in state["events"] if event["kind"] == "targets_generated"]
    return {**run, "holdings": holdings, "equity": book.equity() / 100,
            "accounted_fees": (sum(o["fees"] for o in state["orders"].values()) - sum(
                m["cash_change_cents"] for m in state.get("broker_cash_movements", {}).values() if m["kind"] in {"tax", "fee"})) / 100,
            "execution_source": "broker_observation" if state.get("broker_binding") else "simulator",
            "broker_binding": state.get("broker_binding"),
            "valuation_observed_at": state.get("broker_valuation_observed_at"),
            "last_signal_date": signals[-1]["day"] if signals else None,
            "next_rebalance_in_sessions": run["config"]["rebalance_sessions"] - ((run["cursor"] - 1) % run["config"]["rebalance_sessions"]) if run["cursor"] else 0,
            "available_cash": book.cash_available() / 100, "receivables": sum(state["receivables"].values()) / 100,
            "orders": orders[-100:][::-1], "fills": fills[-100:][::-1], "events": state["events"][-40:][::-1],
            "targets": state["targets"], "candidates": state["candidates"], "daily": store.daily(identifier),
            "comparison": comparison(identifier), "live_enabled": False}
