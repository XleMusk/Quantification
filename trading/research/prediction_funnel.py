"""Join frozen predictions to saved targets and ledger outcomes, without new accounts."""
import argparse
import ast
from bisect import bisect_left
from datetime import datetime
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .alpha158_linear import ID as SOURCE_ID, SEEDS, SIGNALS, saved_score
from .experiment import write_json
from .factors import LABEL
from .loss_attribution import asset_pnl, order_costs


ID = "saved_alpha158_prediction_target_funnel_v1"
LIMITS = [
    "Inspected 2026 development data, not an untouched test or execution admission; NO_TRADE",
    "Head sizes are the existing holdings5/retain8 plus fixed quintiles; no top-N search or strategy change",
    "Membership is fixed from scores BEFORE missing labels are examined; unknown outcomes remain unknown",
    "Provider-adjusted next-open five-session labels are diagnostic returns, NOT executable account returns",
    "Daily windows overlap; rebalance-anchor descriptive means are not compounded returns or significance tests",
    "Observed pool/quintile means omit unknown labels, with explicit coverage; no subset extrapolation",
    "Buy notional weights describe executed purchase flows, NOT portfolio weights or realized asset returns",
    "Saved target events and daily equity/cash marks are consumed directly; daily holdings are not reconstructed",
    "Security CNY PnL uses the existing Ledger attribution, not a second account or market/style/industry alpha",
    "End-of-sample targets with next-day fallback are unexecuted, not exchange-calendar trading opportunities",
    "Rule control has no matched saved prediction panel and is excluded from this score funnel only",
]


def ranked_day(score, labels):
    rows = score.rename("score").reset_index().sort_values(["score", "symbol"], ascending=[False, True])
    rows = rows.set_index("symbol")
    rows["rank"] = np.arange(1, len(rows) + 1)
    # Joining outcomes after ranking prevents unknown head outcomes from promoting lower ranks.
    return rows.join(labels[[LABEL, "entry_date", "exit_date"]], validate="one_to_one")


def group_stats(rows, weights=None):
    weights = pd.Series(1., index=rows.index) if weights is None else weights.reindex(rows.index)
    if weights.isna().any() or not np.isfinite(weights).all() or weights.le(0).any():
        raise ValueError("Group weights must be positive, finite and aligned")
    known = np.isfinite(rows[LABEL])
    total, observed = float(weights.sum()), float(weights.loc[known].sum())
    mean = float((rows.loc[known, LABEL] * weights.loc[known]).sum() / observed) if observed else None
    return {"selected": len(rows), "observed": int(known.sum()), "missing": int((~known).sum()),
            "weight_sum": total, "observed_weight_fraction": observed / total if total else None,
            "observed_mean_forward_return": mean,
            "complete_mean_forward_return": mean if len(rows) and known.all() else None,
            "observed_positive_fraction": float(rows.loc[known, LABEL].gt(0).mean()) if known.any() else None}


def stale_days(values):
    parsed = values.map(ast.literal_eval)
    if not parsed.map(lambda value: isinstance(value, list)).all():
        raise ValueError("Invalid saved stale-valuation symbols")
    return int(parsed.map(bool).sum())


def target_events(account, dates, config):
    events = [event for event in account["events"] if event["kind"] == "targets_generated"]
    expected = dates[::config.rebalance_sessions]
    if [event["signal_date"] for event in events] != expected:
        raise ValueError("Saved target events do not match the original rebalance calendar")
    for event in events:
        if event["day"] != event["signal_date"] or event["execution_not_before"] <= event["signal_date"]:
            raise ValueError("Saved target event violates close-decision/next-session timing")
        weights = list(event["targets"].values())
        if (len(weights) > config.holdings or any(not np.isfinite(w) or w <= 0 or w > config.single_limit for w in weights)
                or sum(weights) > config.gross_limit + 1e-10):
            raise ValueError("Saved targets exceed the existing portfolio constraints")
    return events


def buy_matches(account, events, dates):
    decisions = [event["signal_date"] for event in events]
    fills = {}
    for fill in account["fills"].values():
        fills.setdefault(fill["order_id"], []).append(fill)
    rows = []
    for order in account["orders"].values():
        if order["side"] != "buy" or order["filled"] <= 0:
            continue
        index = bisect_left(decisions, order["day"]) - 1
        event = events[index] if index >= 0 else None
        matched = bool(event and event["execution_not_before"] <= order["day"]
                       and order["symbol"] in event["targets"] and order["day"] in dates)
        executions = fills.get(order["id"], [])
        if (not executions or sum(fill["qty"] for fill in executions) != order["filled"]
                or any(fill["day"] < order["day"] or fill["day"] not in dates for fill in executions)):
            raise ValueError("Executed buy needs its saved causal fills on the pinned calendar")
        rows.append({"order_id": order["id"], "order_day": order["day"], "symbol": order["symbol"],
                     "signal_date": event["signal_date"] if matched else None,
                     "target_matched": matched, "notional_cny": order["notional"] / 100,
                     "filled_quantity": order["filled"], "first_fill_day": min(fill["day"] for fill in executions),
                     "last_fill_day": max(fill["day"] for fill in executions)})
    return pd.DataFrame(rows)


def summarize_groups(groups):
    rows = []
    for (group, cadence), subset in groups.groupby(["group", "cadence"], sort=False):
        complete = subset.complete_mean_forward_return.dropna()
        observed = subset.observed_mean_forward_return.dropna()
        rows.append({"group": group, "cadence": cadence, "dates": len(subset),
                     "dates_with_observed_labels": len(observed), "complete_dates": len(complete),
                     "selected_rows": int(subset.selected.sum()), "observed_rows": int(subset.observed.sum()),
                     "missing_rows": int(subset.missing.sum()),
                     "mean_observed_forward_return": float(observed.mean()) if len(observed) else None,
                     "mean_complete_forward_return": float(complete.mean()) if len(complete) else None,
                     "mean_coverage": float(subset.observed_weight_fraction.mean()) if len(subset) else None,
                     "mean_complete_head_minus_observed_pool": float(subset.head_minus_pool.mean())
                     if subset.head_minus_pool.notna().any() else None})
    return rows


def common_anchors(panels):
    complete, anchors = {}, None
    for signal, groups in panels.items():
        rebalance = groups.loc[groups.cadence.eq("rebalance")]
        values = rebalance.loc[rebalance.group.isin(["head5", "actual_targets"])].pivot(
            index="date", columns="group", values="complete_mean_forward_return").dropna()
        complete[signal] = values
        anchors = set(values.index) if anchors is None else anchors & set(values.index)
    dates = sorted(anchors or [])
    if not dates:
        raise ValueError("No common label-complete target/head rebalance dates")
    comparisons, rows = [], []
    for signal, groups in panels.items():
        values = complete[signal].loc[dates]
        rebalance = groups.loc[groups.cadence.eq("rebalance") & groups.date.isin(dates)]
        pool = rebalance.loc[rebalance.group.eq("all_pool")].set_index("date").loc[dates]
        if pool.observed_mean_forward_return.isna().any():
            raise ValueError("Common anchors need observed pool outcomes")
        rows.extend({"signal": signal, "date": day, "head5": values.loc[day, "head5"],
                     "actual_targets": values.loc[day, "actual_targets"],
                     "observed_pool": pool.loc[day, "observed_mean_forward_return"]} for day in dates)
        comparisons.append({"signal": signal, "common_anchors": len(dates),
            "head5_mean_forward_return": float(values.head5.mean()),
            "target_mean_forward_return": float(values.actual_targets.mean()),
            "observed_pool_mean_forward_return": float(pool.observed_mean_forward_return.mean()),
            "head5_minus_observed_pool": float((values.head5 - pool.observed_mean_forward_return).mean()),
            "targets_minus_observed_pool": float((values.actual_targets - pool.observed_mean_forward_return).mean()),
            "targets_minus_head5": float((values.actual_targets - values.head5).mean()),
            "quintiles": [{"group": group, "mean_observed_forward_return": float(subset.observed_mean_forward_return.mean()),
                           "selected_rows": int(subset.selected.sum()), "observed_rows": int(subset.observed.sum())}
                          for group, subset in rebalance.loc[rebalance.group.str.startswith("quintile")].groupby("group")]})
    all_dates = set(next(iter(panels.values())).loc[lambda f: f.cadence.eq("rebalance"), "date"])
    return {"dates": dates, "excluded_anchor_dates": sorted(all_dates - set(dates)),
            "comparisons": comparisons}, pd.DataFrame(rows)


def diagnose_case(signal, score, labels, folder, metrics, config):
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    daily = pd.read_csv(folder / "daily.csv")
    dates = daily.date.tolist()
    score_dates = score.index.get_level_values("date").unique().strftime("%Y-%m-%d").tolist()
    if (account.get("broker_binding") or dates != score_dates or account["day"] != dates[-1]
            or metrics["status"] != "completed" or len(dates) != metrics["completed_sessions"]):
        raise ValueError("Matched complete unbound historical score/account calendars required")
    events = target_events(account, dates, config)
    event_by_day = {event["signal_date"]: event for event in events}
    purchases = buy_matches(account, events, dates)
    groups, members, target_rows = [], [], []
    for timestamp, values in score.groupby(level="date", sort=True):
        day = timestamp.strftime("%Y-%m-%d")
        ranked = ranked_day(values.droplevel("date"), labels.xs(timestamp, level="date"))
        pool = group_stats(ranked)
        selections = {"all_pool": ranked, "head5": ranked.iloc[:config.holdings],
                      "head8": ranked.iloc[:config.retain_rank]}
        quintiles = np.array_split(np.arange(len(ranked)), 5)
        for number, positions in enumerate(quintiles):
            selections[f"quintile{5 - number}"] = ranked.iloc[positions]
        event = event_by_day.get(day)
        weights = None
        if event is not None:
            weights = pd.Series(event["targets"], dtype=float)
            if not weights.index.isin(ranked.index).all():
                raise ValueError("Actual targets are absent from the original prediction pool")
            selected = ranked.loc[weights.index]
            if selected["rank"].gt(config.retain_rank).any():
                raise ValueError("Actual target exceeds the saved rank-retention rule")
            selections["actual_targets"] = selected
            for symbol, row in selected.iterrows():
                target_rows.append({"date": day, "symbol": symbol, "rank": int(row["rank"]),
                    "weight": float(weights[symbol]), "outside_head5": int(row["rank"]) > config.holdings,
                    "execution_not_before": event["execution_not_before"],
                    "has_execution_session_in_sample": event["execution_not_before"] in dates,
                    LABEL: row[LABEL]})
        for group, selected in selections.items():
            stat = group_stats(selected, weights if group == "actual_targets" else None)
            spread = (stat["complete_mean_forward_return"] - pool["observed_mean_forward_return"]
                      if stat["complete_mean_forward_return"] is not None and pool["observed_mean_forward_return"] is not None
                      else None)
            cadences = ["daily"] if group != "actual_targets" else []
            if event is not None:
                cadences.append("rebalance")
            for cadence in cadences:
                groups.append({"date": day, "group": group, "cadence": cadence, **stat, "head_minus_pool": spread})
        for symbol, row in ranked.iloc[:config.retain_rank].iterrows():
            members.append({"date": day, "symbol": symbol, "score": row.score, "rank": int(row["rank"]),
                            LABEL: row[LABEL], "entry_date": row.entry_date, "exit_date": row.exit_date})
        matching = purchases.index[purchases.signal_date.eq(day)]
        for index in matching:
            symbol = purchases.loc[index, "symbol"]
            row = ranked.loc[symbol]
            purchases.loc[index, "rank"] = int(row["rank"])
            purchases.loc[index, LABEL] = row[LABEL]
            purchases.loc[index, "label_entry_date"] = row.entry_date
            purchases.loc[index, "label_exit_date"] = row.exit_date
            purchases.loc[index, "filled_on_label_entry"] = purchases.loc[index, "first_fill_day"] == row.entry_date.strftime("%Y-%m-%d") if pd.notna(row.entry_date) else False
            purchases.loc[index, "all_fills_on_label_entry"] = purchases.loc[index, "first_fill_day"] == purchases.loc[index, "last_fill_day"] == row.entry_date.strftime("%Y-%m-%d") if pd.notna(row.entry_date) else False
        if len(matching):
            selected = purchases.loc[matching].set_index("order_id")
            stat = group_stats(selected, selected.notional_cny)
            groups.append({"date": day, "group": "executed_buy_flows", "cadence": "rebalance", **stat,
                           "head_minus_pool": None})
    groups = pd.DataFrame(groups)
    targets = pd.DataFrame(target_rows)
    pnl_summary, pnl = asset_pnl(account, config)
    costs = order_costs(list(account["orders"].values()), config)
    if round(daily.equity.iloc[-1] - config.initial_cash, 2) != pnl_summary["observed_account_pnl_cny"]:
        raise ValueError("Daily terminal equity differs from the existing Ledger attribution")
    exposure = (daily.equity - daily.cash - daily.receivables) / daily.equity
    negative = pnl.loc[pnl.observed_pnl_cny.lt(0)].sort_values("observed_pnl_cny")
    buys = purchases.loc[purchases.target_matched].copy()
    unknown_buy_labels = purchases[LABEL].isna() if LABEL in purchases else pd.Series(True, index=purchases.index)
    summary = {"signal": signal, "source_account": str(folder), "sessions": len(dates),
        "account_metrics": metrics, "groups": summarize_groups(groups),
        "target_rows": len(targets), "target_rows_outside_head5": int(targets.outside_head5.sum()),
        "unexecuted_terminal_target_rows": int((~targets.has_execution_session_in_sample).sum()),
        "buy_orders": len(purchases), "unmatched_buy_orders": int((~purchases.target_matched).sum()),
        "unknown_buy_label_orders": int(unknown_buy_labels.sum()),
        "buy_orders_outside_head5": int(buys["rank"].gt(config.holdings).sum()),
        "buys_with_fills_not_all_on_original_label_entry": int((~buys.all_fills_on_label_entry.astype(bool)).sum()),
        "buy_notional_cny": float(purchases.notional_cny.sum()),
        "buy_notional_with_unknown_labels_cny": float(purchases.loc[unknown_buy_labels, "notional_cny"].sum()),
        "median_saved_exposure": float(exposure.median()), "max_saved_holdings": int(daily.holdings.max()),
        "saved_days_with_more_than_target_holdings": int(daily.holdings.gt(config.holdings).sum()),
        "stale_valuation_days": stale_days(daily.stale_symbols),
        "fees_cny": float(costs.fees_cny.sum()), "turnover_value_cny": float(costs.notional_cny.sum()),
        "negative_symbol_pnl_cny": float(negative.observed_pnl_cny.sum()),
        "worst5_symbol_pnl_cny": float(negative.head(5).observed_pnl_cny.sum()),
        "worst5_share_of_gross_symbol_losses": float(negative.head(5).observed_pnl_cny.sum() / negative.observed_pnl_cny.sum()) if len(negative) else None,
        "worst5_symbols": negative.head(5).to_dict("records"), **pnl_summary}
    return summary, {"daily_groups": groups, "head_members": pd.DataFrame(members),
                     "actual_targets": targets, "executed_buys": purchases, "symbol_pnl": pnl}


def report(out, record):
    lines = ["# Saved Prediction -> Target -> Ledger Diagnostic", "", "Status: " + record["status"] + ". NO_TRADE.", "",
             "All statistics reuse the original five fixed score panels and saved accounts. No training/replay/parameter search.",
             "Forward returns below are arithmetic five-session LABEL means, not account performance.", ""]
    if record.get("common_anchors"):
        common = record["common_anchors"]
        lines += [f"## Primary Comparison: Same {len(common['dates'])} Label-Complete Rebalance Anchors", "",
                  "Membership remains score-first. Head5 AND actual target labels must be complete for ALL five cases.",
                  "The pool is the identical observed subset on these same dates; it is not complete-market or executable PnL.", "",
                  "|Case|Head5 label|Target label|Observed pool|Target minus head5|", "|---|---:|---:|---:|---:|"]
        for row in common["comparisons"]:
            lines.append(f"|{row['signal']}|{row['head5_mean_forward_return']:.3%}|{row['target_mean_forward_return']:.3%}|"
                         f"{row['observed_pool_mean_forward_return']:.3%}|{100 * row['targets_minus_head5']:+.3f}pp|")
        lines += ["", "Excluded/unknown anchors: " + ", ".join(common["excluded_anchor_dates"]), "",
                  "## Per-Case Coverage and Full Saved Account Outcomes", ""]
    lines += [
             "|Case|Broad IC|Head5 label / anchors|Target label / anchors|Observed pool label|Account net|MDD|",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for case in record["cases"]:
        groups = {row["group"]: row for row in case["groups"] if row["cadence"] == "rebalance"}
        head, target, pool = (groups[name] for name in ("head5", "actual_targets", "all_pool"))
        def complete(row):
            value = row["mean_complete_forward_return"]
            return f"{value:.3%} / {row['complete_dates']}" if value is not None else "unknown / 0"
        metric = case["account_metrics"]
        lines.append(f"|{case['signal']}|{case['mean_rank_ic']:.4f}|{complete(head)}|{complete(target)}|"
                     f"{pool['mean_observed_forward_return']:.3%}|{metric['net_return']:.2%}|{abs(metric['max_drawdown']):.2%}|")
    lines += ["", "Pool observations may be incomplete; head/target columns use only dates with ALL selected labels.",
              "Use per-date CSV/paired spreads for aligned dates. Last anchors and any missing head labels remain excluded/unknown.", ""]
    for case in record["cases"]:
        lines += [f"## {case['signal']}",
            f"Targets outside head5: {case['target_rows_outside_head5']}/{case['target_rows']}; "
            f"terminal unexecuted target rows: {case['unexecuted_terminal_target_rows']}.",
            f"Buy orders: {case['buy_orders']}; unmatched: {case['unmatched_buy_orders']}; "
            f"unknown label orders: {case['unknown_buy_label_orders']}; fills not all on original label-entry date: "
            f"{case['buys_with_fills_not_all_on_original_label_entry']}.",
            f"Median saved exposure: {case['median_saved_exposure']:.2%}; "
            f"max saved holdings: {case['max_saved_holdings']}; stale valuation days: {case['stale_valuation_days']}.",
            f"Saved net PnL: {case['observed_account_pnl_cny']:.2f}CNY; fees: {case['fees_cny']:.2f}CNY; "
            f"worst5 symbols: {case['worst5_symbol_pnl_cny']:.2f}CNY of {case['negative_symbol_pnl_cny']:.2f}CNY gross symbol losses.", ""]
    lines += ["## Boundaries", *["- " + limit for limit in LIMITS], "", f"Source: {record['source_run']}",
              f"Artifacts: {record['artifacts']}"]
    if record.get("error"):
        lines.append("Failure: " + record["error"])
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id):
    source_path = (ROOT / "outputs/market_replay" / source_id / "run.json").resolve()
    source_path.relative_to((ROOT / "outputs/market_replay").resolve())
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("purpose") != SOURCE_ID or source.get("status") != "completed_alpha158_linear_diagnostics":
        raise ValueError("Completed matched Alpha158 fixed linear/all-seed comparison required")
    original = json.loads(Path(source["source_run"]).read_text(encoding="utf-8"))
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    if (config.to_dict() != original["portfolio_config"] or original["config"]["seeds"] != list(SEEDS)
            or config.holdings != 5 or config.retain_rank != 8 or source["execution_admitted"] is not False):
        raise ValueError("Original matched portfolio and all seeds required")
    specs = []
    for row in source["controls"]:
        if row["seed"] in SEEDS:
            model = Path(original["model_sources"][row["signal"]])
            specs.append((row["signal"], model / "historical_scores.parquet", Path(row["source_artifacts"]), row))
    for row in source["results"]:
        if row["signal"] in SIGNALS:
            root = Path(source["artifacts"])
            specs.append((row["signal"], root / "predictions" / row["signal"] / "historical_scores.parquet", root / row["signal"], row))
    if [spec[0] for spec in specs] != [f"alpha158_seed{seed}" for seed in SEEDS] + list(SIGNALS):
        raise ValueError("All five fixed cases required, without seed selection")
    label_path = Path(source["protocol"]["cached_labels"])
    labels = pd.read_parquet(label_path)
    labels = labels.loc[(slice(pd.Timestamp(source["start"]), pd.Timestamp(source["end"])), slice(None)), :].copy()
    labels[LABEL] = labels[LABEL].where(labels.exit_date.le(pd.Timestamp(source["end"])))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out, artifacts = ROOT / "outputs/prediction_funnel" / identifier, DATA_ROOT / "research/prediction_funnel" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": identifier, "purpose": ID, "status": "running", "source_run": str(source_path),
              "artifacts": str(artifacts), "cached_labels": str(label_path), "portfolio": config.to_dict(),
              "start": source["start"], "end": source["end"], "cases": [], "limits": LIMITS,
              "models_refit": False, "accounts_replayed": False, "parameters_searched": False,
              "fresh_holdout_claimed": False, "execution_admitted": False}
    write_json(out / "run.json", record)
    try:
        master_index, panels = None, {}
        for signal, predictions, folder, metrics in specs:
            score = saved_score(predictions)
            if master_index is not None and not score.index.equals(master_index):
                raise ValueError("Saved prediction pools differ")
            master_index = score.index
            summary, frames = diagnose_case(signal, score, labels, folder, metrics, config)
            panels[signal] = frames["daily_groups"]
            ic = (next(row for row in original["model_metrics"] if row["seed"] == metrics["seed"] and row["split"] == "test")
                  if signal not in SIGNALS else next(row for row in source["prediction_diagnostics"] if row["signal"] == signal))
            summary.update(mean_rank_ic=ic["mean_rank_ic"], source_predictions=str(predictions))
            destination = artifacts / signal
            destination.mkdir()
            for name, frame in frames.items():
                frame.to_csv(destination / (name + ".csv"), index=False)
            write_json(destination / "summary.json", summary)
            record["cases"].append(summary)
            write_json(out / "run.json", record)
            print(f"{signal}: targets={summary['target_rows']}, buys={summary['buy_orders']}, net={metrics['net_return']:.4%}", flush=True)
        record["common_anchors"], comparison = common_anchors(panels)
        comparison.to_csv(artifacts / "common_anchor_labels.csv", index=False)
        pd.DataFrame(record["common_anchors"]["comparisons"]).drop(columns="quintiles").to_csv(
            out / "common_anchor_comparison.csv", index=False)
        record["status"] = "completed_saved_prediction_funnel"
    except Exception as error:
        record.update(status="failed_saved_prediction_funnel", error=f"{type(error).__name__}: {error}")
    finally:
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    args = parser.parse_args(argv)
    _, record = run(args.source_run)
    return 0 if record["status"] == "completed_saved_prediction_funnel" else 1


if __name__ == "__main__":
    raise SystemExit(main())
