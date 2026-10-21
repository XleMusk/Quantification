"""All-code StockDB factor comparison with an explicitly non-executable return proxy."""

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..settings import ROOT
from .experiment import write_json
from .full_market import verify_chunks
from .full_market_screen import evaluate
from .institutional_mix import WEIGHTS


START, SPLIT, END = 20260105, 20260701, 20260924
GROSS, HOLDINGS = .8, 5
BUY_COST, SELL_COST = .0013, .0018
INITIAL_CASH = 140000
MARKET_GROUPS = (("sh_sz_all_equities", {"0", "3", "6"}, 20_000_000),
                 ("bse_equities", {"9"}, 5_000_000))
COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st"]


def load_rows(snapshot, manifest, prefixes, *, first_month=None):
    files = [snapshot / item["parquet_file"] for item in manifest["chunks"]
             if item["prefix"] in prefixes and (first_month is None or item["month"] >= first_month)]
    if not files:
        raise ValueError(f"No matching snapshot chunks: {prefixes}")
    frame = pd.concat((pd.read_parquet(path, columns=COLUMNS) for path in files), ignore_index=True)
    if frame.duplicated(["date", "code"]).any():
        raise ValueError("Duplicate code/date observations across chunks")
    frame["date"] = pd.to_numeric(frame.date, errors="raise").astype("int32")
    for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def market_panel(frame, min_amount, *, bse=False):
    frame = frame.copy()
    frame.loc[frame.close.le(0), ["open", "high", "low", "close"]] = np.nan
    values = {column: frame.pivot(index="date", columns="code", values=column).sort_index()
              for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st")}
    close = values["close"]
    for column in values:
        values[column] = values[column].reindex(index=close.index, columns=close.columns)
    pct = values["pct_chg"] / 100
    if (pct.isna() & close.notna()).any().any():
        raise ValueError("An observed equity close lacks provider pct_chg")
    if pct.where(close.notna()).lt(-1).any().any():
        raise ValueError("Invalid provider daily return below -100%")
    # This cumulative index is only a provider-return research proxy. It is not
    # an executable share price or a reconciled cash-dividend account.
    index = (1 + pct.fillna(0)).cumprod().where(close.notna())
    adjusted_high = index * values["high"] / close
    adjusted_low = index * values["low"] / close
    min_low = adjusted_low.rolling(20, min_periods=20).min()
    max_high = adjusted_high.rolling(20, min_periods=20).max()
    features = {
        "qlib_roc60": index / index.shift(60) - 1,
        "qlib_rsv20": (index - min_low) / (max_high - min_low + 1e-12),
        "worldquant": (close - values["open"]) / (values["high"] - values["low"] + .001),
        "aqr": index.shift(21) / index.shift(252) - 1,
        "low_vol": -pct.rolling(60, min_periods=60).std(),
    }
    complete_history = close.notna().rolling(253, min_periods=253).sum().eq(253)
    eligible = (complete_history & values["volume"].gt(0) & values["is_st"].eq(False)
                & values["amount"].rolling(20, min_periods=20).median().ge(min_amount))
    tradable = close.notna() & values["volume"].gt(0) & values["amount"].gt(0)
    limits = pd.Series({code: (.295 if bse else .195 if code.startswith(("3", "688", "689")) else .095)
                        for code in close.columns})
    buyable = tradable & pct.lt(limits, axis="columns") & values["is_st"].eq(False)
    sellable = tradable & pct.gt(-limits, axis="columns")
    return features, eligible, pct, tradable, buyable, sellable, {
        "source_rows": len(frame), "codes": close.shape[1], "calendar_sessions": close.shape[0]}


def rank_decisions(features, eligible, dates):
    signals, rows, coverage = {}, [], []
    for day in dates[::5]:
        raw = pd.DataFrame({name: value.loc[day] for name, value in features.items()})
        raw = raw.replace([np.inf, -np.inf], np.nan)
        common = raw.loc[eligible.loc[day]].dropna()
        if len(common) < 50:
            raise ValueError(f"Fewer than 50 common eligible codes on {day}")
        rank = common.rank(pct=True)
        parts = {"qlib": (rank.qlib_roc60 + rank.qlib_rsv20) / 2,
                 "worldquant": rank.worldquant, "aqr": rank.aqr, "low_vol": rank.low_vol}
        coverage.append({"date": day, "eligible_common": len(common)})
        signals[day] = {}
        for variant, weights in WEIGHTS.items():
            score = sum(weight * parts[name] for name, weight in weights.items())
            top = score.sort_values(ascending=False, kind="stable").head(HOLDINGS)
            signals[day][variant] = top.index.tolist()
            rows.extend({"decision_date": day, "variant": variant, "rank": position,
                         "code": code, "score": float(value), "eligible_common": len(common)}
                        for position, (code, value) in enumerate(top.items(), 1))
    return signals, pd.DataFrame(rows), pd.DataFrame(coverage)


def proxy_replay(dates, selections, returns, tradable, variant, buy_cost=BUY_COST, sell_cost=SELL_COST,
                 buyable=None, sellable=None):
    """Close signal -> next-session close rebalance; no order/fill or broker claim."""
    if (buyable is None) != (sellable is None):
        raise ValueError("Both sides of the approximate queue veto must be provided")
    equity, weights, pending = 1., {}, None
    costs_cny = turnover_cny = 0.
    blocked, queue_blocked, stale_days, stale_symbol_days, marks = 0, 0, 0, 0, []
    for day in dates:
        missing = []
        if weights:
            earned = returns.loc[day, list(weights)]
            missing = earned[earned.isna()].index.tolist()
            if missing:
                stale_days += 1
                stale_symbol_days += len(missing)
                earned = earned.fillna(0)
            growth = 1 + sum(weight * float(earned[code]) for code, weight in weights.items())
            if growth <= 0:
                raise ValueError(f"Nonpositive proxy account value on {day}")
            equity *= growth
            weights = {code: weight * (1 + float(earned[code])) / growth for code, weight in weights.items()}
        if pending is not None:
            if len(pending) != HOLDINGS or len(set(pending)) != HOLDINGS:
                raise ValueError("Proxy target must contain five distinct codes")
            touched = set(weights) | set(pending)
            target = {code: GROSS / HOLDINGS for code in pending}
            changes = {code: target.get(code, 0) - weights.get(code, 0) for code in touched}
            traded = [code for code, delta in changes.items() if abs(delta) > 1e-10]
            if not all(bool(tradable.at[day, code]) for code in traded):
                blocked += 1
            elif buyable is not None and any(
                    (delta > 1e-10 and not bool(buyable.at[day, code]))
                    or (delta < -1e-10 and not bool(sellable.at[day, code]))
                    for code, delta in changes.items()):
                queue_blocked += 1
            else:
                buys = sum(max(target.get(code, 0) - weights.get(code, 0), 0) for code in touched)
                sells = sum(max(weights.get(code, 0) - target.get(code, 0), 0) for code in touched)
                cost = buys * buy_cost + sells * sell_cost
                costs_cny += INITIAL_CASH * equity * cost
                turnover_cny += INITIAL_CASH * equity * (buys + sells)
                equity *= 1 - cost
                weights = target
            pending = None
        marks.append({"date": day, "equity": INITIAL_CASH * equity, "holdings": len(weights),
                      "cash_weight": 1 - sum(weights.values()), "blocked_rebalances_to_date": blocked,
                      "queue_vetoes_to_date": queue_blocked, "stale_symbols": ",".join(missing)})
        if day in selections:
            pending = selections[day][variant]
    series = pd.Series([row["equity"] for row in marks])
    peak = series.cummax().clip(lower=INITIAL_CASH)
    return {"sessions": len(dates), "proxy_return_after_cost": float(equity - 1),
            "proxy_max_drawdown": float((series / peak - 1).min()),
            "final_proxy_equity": float(INITIAL_CASH * equity), "cost_haircut_cny": costs_cny,
            "turnover_proxy_cny": turnover_cny, "blocked_rebalances": blocked,
            "approximate_queue_vetoes": queue_blocked, "queue_veto_applied": buyable is not None,
            "stale_mark_days": stale_days, "stale_symbol_days": stale_symbol_days,
            "unpriced_end_holdings": [code for code in weights if not bool(tradable.at[dates[-1], code])],
            "non_executable": True}, pd.DataFrame(marks)


def fund_diagnostics(snapshot, manifest, etf_codes, out):
    frame = load_rows(snapshot, manifest, {"1", "5"}, first_month="2026-07")
    frame = frame.loc[frame.date.between(SPLIT, END)].copy()
    if frame.empty or frame.pct_chg.isna().any():
        raise ValueError("July-September fund-code pct_chg coverage is incomplete")
    panel = {column: frame.pivot(index="date", columns="code", values=column).sort_index()
             for column in ("open", "high", "low", "close", "amount", "volume", "pct_chg")}
    close = panel["close"].where(panel["close"].gt(0))
    index = (1 + panel["pct_chg"] / 100).fillna(1).cumprod().where(close.notna())
    label = index.shift(-5) / index - 1
    wq101 = (close - panel["open"]) / (panel["high"] - panel["low"] + .001)
    days = close.index.tolist()
    rows = []
    for day in days[::5]:
        if days.index(day) + 5 >= len(days):
            continue
        for group, codes in (("current_etf_name_marker", etf_codes),
                             ("other_1_5_fund_codes", set(close.columns) - etf_codes)):
            present = close.columns.intersection(list(codes))
            allowed = (close.loc[day, present].notna() & panel["volume"].loc[day, present].gt(0)
                       & panel["amount"].loc[day, present].ge(5_000_000))
            result = evaluate(wq101.loc[day, present], label.loc[day, present], allowed, minimum=50)
            rows.append({"date": day, "group": group, "eligible": int(allowed.sum()), **result})
    result = pd.DataFrame(rows)
    result.to_csv(out / "fund_price_proxy_diagnostics.csv", index=False)
    summary = result.groupby("group").agg(decision_dates=("date", "size"),
                                          evaluated_dates=("rank_ic", "count"),
                                          mean_rank_ic=("rank_ic", "mean"),
                                          mean_top_minus_universe=("top_minus_universe", "mean"),
                                          min_eligible=("eligible", "min")).reset_index()
    summary.to_csv(out / "fund_price_proxy_summary.csv", index=False)
    return {"source_rows_july_september": len(frame), "codes": frame.code.nunique(),
            "current_etf_name_codes": len(etf_codes), "diagnostic_dates": result.date.nunique(),
            "summary": summary.to_dict("records"),
            "fund_return_basis": "provider_pct_chg_price_proxy_unreconciled_distributions",
            "point_in_time_etf_membership": False, "account_backtest": False}


def report(out, protocol):
    lines = ["# Full-market public-factor research proxy", "",
             f"Status: `{protocol['status']}`", f"Frozen StockDB snapshot: `{protocol['snapshot']}`", "",
             "This is NOT an engine/ledger account backtest. The 0/3/6 and 9 code groups are",
             "separate provider-pct_chg return proxies. The existing 250-stock admitted account",
             "replay is not directly comparable to these broad-universe figures.", "",
             "The same seven predeclared Qlib / WorldQuant / AQR-style / low-volatility",
             "factor weights were used for each equity group. Signal: close of day T; hypothetical",
             "rebalance: close of T+1; exposure: five stocks, 16% each and 20% cash.",
             "Buy/sell cost haircuts: 13/18 bps of changed notional; minimum brokerage fees,",
             "100-share lots and T+1 order mechanics are NOT modeled. The primary proxy",
             "conservatively skips an entire rebalance if a changed name is near its",
             "approximate board-specific price limit; this is not an order-book fill test.", "",
             "| Prefix group | Codes read | Historical rows | Minimum common eligible per decision |",
             "|---|---:|---:|---:|"]
    for group, info in protocol["groups"].items():
        lines.append(f"| {group} | {info['codes']:,} | {info['source_rows']:,} | {info['min_eligible_common']:,} |")
    lines += ["", "The pinned 2024-2026 snapshot includes every local code prefix below.",
              "Missing pct_chg prevents a common return-history backtest for the fund prefixes:", "",
              "| Code prefix | Rows | Missing provider pct_chg |", "|---|---:|---:|"]
    for prefix, coverage in protocol["asset_coverage"].items():
        lines.append(f"| {prefix} | {coverage['rows_all_dates']:,} | {coverage['missing_pct_chg_all_dates']:,} |")
    lines += ["", "| Group | Period | Variant | Proxy return after cost | Proxy max drawdown | Stale days | Queue vetoes |",
              "|---|---|---|---:|---:|---:|---:|"]
    for row in protocol["results"]:
        lines.append(f"| {row['group']} | {row['period']} | {row['variant']} | "
                     f"{row['proxy_return_after_cost']:.2%} | {row['proxy_max_drawdown']:.2%} | "
                     f"{row['stale_mark_days']} | {row['approximate_queue_vetoes']} |")
    lines += ["", "## Selection boundary", ""]
    for group, chosen in protocol["selection_frozen"].items():
        check = next((row for row in protocol["results"] if row["group"] == group
                      and row["period"] == "later_check" and row["variant"] == chosen), None)
        passed = bool(check and check["proxy_return_after_cost"] > 0 and check["proxy_max_drawdown"] >= -.15
                      and check["stale_mark_days"] == 0 and not check["unpriced_end_holdings"])
        lines.append(f"- {group}: first-period choice `{chosen or 'none'}`; later research check passed: `{passed}`.")
    lines += ["- First-period choice required positive return, max drawdown no worse than -20%,",
              "  zero stale marks and priced end holdings. Later check required positive return,",
              "  max drawdown no worse than -15% and zero stale marks. No choice is eligible",
              "  for simulated or live orders from this proxy.", "", "## Funds and unused fields", ""]
    funds = protocol["fund_diagnostics"]
    lines.append(f"July-September fund-code rows: {funds['source_rows_july_september']:,} across "
                 f"{funds['codes']:,} codes; {funds['current_etf_name_codes']:,} currently ETF-name-marked codes.")
    lines.append("Only 12 five-session WorldQuant#101 price-proxy diagnostics were run per fund group.")
    lines.append("The current ETF name marker is not historical membership; 2026 January-May fund pct_chg is largely absent.")
    lines.append("ETF distributions/splits are not comprehensively reconciled, so no ETF total-return or account ranking is claimed.")
    lines += ["", "| Fund group | Decision dates | Evaluated dates | Mean rank IC | Mean top-minus-universe five-session return |",
              "|---|---:|---:|---:|---:|"]
    for item in funds["summary"]:
        lines.append(f"| {item['group']} | {item['decision_dates']} | {item['evaluated_dates']} | "
                     f"{item['mean_rank_ic']:.4f} | "
                     f"{item['mean_top_minus_universe']:.2%} |")
    lines += ["", "Minute data is incomplete for the last snapshot date and cannot provide",
              "a comparable full-market intraday replay. Dated point-in-time sector,",
              "fundamental and seat histories were not established by this snapshot.", "",
              "## Limits", ""]
    lines.extend(f"- {item}" for item in protocol["limitations"])
    lines += ["", "Source definitions: [Microsoft Qlib Alpha158](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py),",
              "[WorldQuant Alpha#101](https://arxiv.org/pdf/1601.00991),",
              "[AQR momentum](https://www.aqr.com/Insights/Datasets/Momentum-Indices-Monthly),",
              "[MSCI minimum-volatility concept](https://www.msci.com/indexes/index/719841/msci-china-a-minimum-volatility-index-usd).",
              "These are public formula/style inspirations, not institutional production portfolios."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(snapshot, etf_master, out=None):
    snapshot, etf_master = Path(snapshot), Path(etf_master)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(out) if out else ROOT / "outputs" / "full_market_mix" / identifier
    out.mkdir(parents=True, exist_ok=False)
    manifest_bytes = (snapshot / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    master_bytes = etf_master.read_bytes()
    master = json.loads(master_bytes)
    protocol = {"status": "running", "snapshot": str(snapshot), "snapshot_manifest_sha256": sha256(manifest_bytes).hexdigest(),
                "etf_master": str(etf_master), "etf_master_sha256": sha256(master_bytes).hexdigest(),
                "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "weights": WEIGHTS,
                "periods": {"selection": [START, 20260630], "later_check": [SPLIT, END], "full_period": [START, END]},
                "signal": "daily close; rebalance at next exchange-session close; five-stock equal weight, 80% gross",
                "cost_proxy": {"buy_notional": BUY_COST, "sell_notional": SELL_COST,
                               "minimum_commission_and_actual_order_queue": "not modeled"},
                "approximate_limit_veto": "primary results skip whole next-close rebalance when a changed stock is >=9.5% main, >=19.5% ChiNext/STAR, >=29.5% BSE on buy, or <= corresponding negative threshold on sell; sensitivity omits veto",
                "data_use": "2024-2025 warmup and 2026 proxy-return test; all equity prefixes 0/3/6/9; separate July-Sep fund diagnostics",
                "account_backtest": False, "broker_fills_claimed": False, "strategy_validated": False,
                "paper_or_live_orders_authorized": False, "fresh_holdout_claimed": False,
                "limitations": ["Provider pct_chg is a research proxy, not a reconciled account cash/share action path",
                                "StockDB pre_close disagrees with independent source; no executable price-limit checks",
                                "Delisted codes absent at retrieval and historical membership not independently validated",
                                "Approximate limit veto is not an order book, queue, 100-share lot, T+1, minimum commission or board-specific fill model",
                                "Missing held-stock sessions carry the last proxy valuation and are counted as stale; no delisting settlement is inferred",
                                "Fund January-May returns missing; July-September fund pct_chg and distribution completeness not independently reconciled",
                                "Previously inspected 2026; this is retrospective exploration, not a fresh holdout"]}
    write_json(out / "protocol.json", protocol)
    try:
        verify_chunks(snapshot, manifest)
        if manifest.get("start") != "2024-01-01" or manifest.get("end") != "2026-09-24":
            raise ValueError("Unexpected full-market snapshot period")
        if not {"0", "1", "3", "5", "6", "9"} <= set(manifest.get("prefixes", [])):
            raise ValueError("Snapshot lacks an asset code group")
        if master.get("date") != "20260924" or master.get("count") != len(master.get("candidate_codes", [])):
            raise ValueError("ETF name-marker master does not match this research date")
        protocol["asset_coverage"] = {prefix: {"rows_all_dates": sum(item["rows"] for item in manifest["chunks"] if item["prefix"] == prefix),
                                               "missing_pct_chg_all_dates": sum(item["missing_pct_chg"] for item in manifest["chunks"] if item["prefix"] == prefix)}
                                      for prefix in manifest["prefixes"]}
        all_results = []
        for group, prefixes, threshold in MARKET_GROUPS:
            print(f"Building {group} full-code panel", flush=True)
            rows = load_rows(snapshot, manifest, prefixes)
            features, eligible, returns, tradable, buyable, sellable, info = market_panel(
                rows, threshold, bse=group == "bse_equities")
            del rows
            dates = [day for day in returns.index if START <= day <= END]
            selection_days = [day for day in dates if day < SPLIT]
            later_days = [day for day in dates if day >= SPLIT]
            if dates[0] != START or dates[-1] != END or later_days[0] != SPLIT:
                raise ValueError(f"Incomplete 2026 calendar for {group}")
            signals_full, decisions, coverage = rank_decisions(features, eligible, dates)
            signals_later, later_decisions, later_coverage = rank_decisions(features, eligible, later_days)
            decisions.assign(group=group).to_csv(out / f"{group}_decisions_full.csv", index=False)
            later_decisions.assign(group=group).to_csv(out / f"{group}_decisions_later.csv", index=False)
            coverage.assign(group=group).to_csv(out / f"{group}_coverage_full.csv", index=False)
            protocol.setdefault("groups", {})[group] = {**info, "selection_sessions": len(selection_days),
                                                         "later_sessions": len(later_days),
                                                         "decision_dates_full": len(coverage),
                                                         "min_eligible_common": int(coverage.eligible_common.min()),
                                                         "later_decision_dates": len(later_coverage)}
            results = []
            for variant in WEIGHTS:
                result, daily = proxy_replay(selection_days, signals_full, returns, tradable, variant,
                                             buyable=buyable, sellable=sellable)
                results.append({"group": group, "period": "selection", "variant": variant, **result})
                daily.to_csv(out / f"{group}_{variant}_selection_daily.csv", index=False)
            eligible_first = [row for row in results if row["proxy_return_after_cost"] > 0
                              and row["proxy_max_drawdown"] >= -.20 and row["stale_mark_days"] == 0
                              and not row["unpriced_end_holdings"]]
            eligible_first.sort(key=lambda row: (-row["proxy_return_after_cost"], -row["proxy_max_drawdown"], row["variant"]))
            protocol.setdefault("selection_frozen", {})[group] = eligible_first[0]["variant"] if eligible_first else None
            write_json(out / "selection_frozen.json", protocol["selection_frozen"])
            print(f"{group}: {info['codes']} codes, first-period choice={protocol['selection_frozen'][group]}", flush=True)
            for variant in WEIGHTS:
                for period, sessions, signals in (("later_check", later_days, signals_later),
                                                  ("full_period_descriptive", dates, signals_full)):
                    result, daily = proxy_replay(sessions, signals, returns, tradable, variant,
                                                 buyable=buyable, sellable=sellable)
                    results.append({"group": group, "period": period, "variant": variant, **result})
                    daily.to_csv(out / f"{group}_{variant}_{period}_daily.csv", index=False)
                optimistic, _ = proxy_replay(dates, signals_full, returns, tradable, variant)
                results.append({"group": group, "period": "full_period_no_queue_veto_sensitivity",
                                "variant": variant, **optimistic})
            all_results.extend(results)
            pd.DataFrame(all_results).to_csv(out / "proxy_metrics.csv", index=False)
            protocol["results"] = all_results
            write_json(out / "protocol.json", protocol)
            del features, eligible, returns, tradable, buyable, sellable
        protocol["fund_diagnostics"] = fund_diagnostics(snapshot, manifest, set(master["candidate_codes"]), out)
        protocol["research_gate"] = {}
        for group, chosen in protocol["selection_frozen"].items():
            check = next((row for row in all_results if row["group"] == group
                          and row["period"] == "later_check" and row["variant"] == chosen), None)
            protocol["research_gate"][group] = bool(check and check["proxy_return_after_cost"] > 0
                                                    and check["proxy_max_drawdown"] >= -.15
                                                    and check["stale_mark_days"] == 0
                                                    and not check["unpriced_end_holdings"])
        protocol["status"] = "completed_non_executable_research_proxy"
    except Exception as error:
        protocol["status"] = "failed"
        protocol["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        write_json(out / "protocol.json", protocol)
        if protocol["status"] == "completed_non_executable_research_proxy":
            report(out, protocol)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--etf-master", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    print(run(args.snapshot, args.etf_master, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
