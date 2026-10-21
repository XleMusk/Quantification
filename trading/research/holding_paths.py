"""Trace saved ranks/retention/quantities, without fitting or replaying a strategy."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .. import replay_reports, settings
from ..config import StrategyConfig
from ..engine import choose_targets
from .experiment import write_json
from .loss_attribution import asset_pnl
from .turnover_attribution import action_map, project_orders


FAMILY = "economic_profitability_reporting_calendar_v1"
STOCKDB_FAMILY = "stockdb_fixed_factors_2023_v1"
LIMITS = [
    "Saved selection/holding mechanics, not a causal attribution or counterfactual no-buffer account",
    "Ranks outside the saved top30 are unknown; absence means outside the saved buffer, not a fabricated exact rank",
    "Positive share quantities come only from saved unique fills and actual/explicit saved corporate-action credits",
    "Selected zero budgets and missing attempts remain distinct; selection does not imply a purchase or holding",
    "Held session counts include suspended/stale valuation sessions; they are not executable holding returns",
    "Security PnL includes saved costs/distributions/terminal marks, not market/industry-neutral alpha",
    "Accounts bind saved source ranks/config; action/reference revisions are recorded, not silently treated as an isolated implementation cause",
    "Observed historical periods are inspected development; no loser deletion, refit, threshold search or execution admission. NO_TRADE",
    "Fixed20-session adjusted-open cohort labels are not the saved account's holding returns, cash/share PnL or an alternate portfolio",
    "Cohorts/ranks are fixed without outcome filtering; label means use observed endpoints only and missing-label coverage remains explicit",
    "A few month-end observations do not establish significance or independent information; no compounding of overlapping cohort means",
    "Oriented feature percentiles describe unweighted contemporaneous cohort proxies, not account-weighted risk exposures or independent PIT/industry-neutral alpha",
]


def selection_rows(targets, ranks, decision_holdings, traces, config, *, financial_ranks=True):
    if ranks.duplicated(["date", "symbol"]).any() or ranks.duplicated(["date", "rank"]).any():
        raise ValueError("Distinct dated saved ranks required")
    lookup = {(r["day"], r["symbol"]): r for r in traces}
    if len(lookup) != len(traces):
        raise ValueError("Duplicate execution attempts")
    rows = []
    for event in targets:
        day, execution_day = event["signal_date"], event["execution_not_before"]
        if event["day"] != day or execution_day <= day or day not in decision_holdings:
            raise ValueError("Saved decision timing or holdings unavailable")
        batch = ranks.loc[ranks.date.eq(day)].sort_values("rank")
        if (batch["rank"].tolist() != list(range(1, config.retain_rank + 1))
                or not np.isfinite(batch.score).all() or not batch.score.is_monotonic_decreasing):
            raise ValueError("Exact fixed rank buffer required on every observed decision date")
        if financial_ranks and not {"source_pub_date", "source_stat_date"} <= set(batch):
            raise ValueError("Financial ranks need their saved report provenance")
        if financial_ranks and (not batch.source_pub_date.lt(day).all()
                or not batch.source_stat_date.le(batch.source_pub_date).all()):
            raise ValueError("Financial ranks need strictly lagged report provenance")
        held = decision_holdings[day]
        if any(type(q) is not int or q <= 0 for q in held.values()):
            raise ValueError("Positive observed integer holdings required")
        expected = choose_targets(batch.to_dict("records"), list(held), config)
        actual = event["targets"]
        if (set(actual) != set(expected) or any(not np.isfinite(w) or w < 0 or w > config.single_limit
                                               for w in actual.values())
                or sum(actual.values()) > config.gross_limit + 1e-9):
            raise ValueError("Saved selected membership/config disagrees with original selector")
        ranked = {r["symbol"]: r for r in batch.to_dict("records")}
        for symbol in sorted(set(ranked) | set(held)):
            rank = ranked.get(symbol, {}).get("rank")
            selected, prior = symbol in actual, held.get(symbol, 0)
            reason = ("retained_top_head" if selected and prior and rank <= config.holdings else
                      "retained_rank_buffer" if selected and prior else
                      "new_selection" if selected else
                      "removed_outside_saved_buffer" if prior and rank is None else
                      "unselected_head_displaced_by_retention" if rank is not None and rank <= config.holdings else
                      "not_selected_saved_buffer")
            weight = actual.get(symbol, 0.)
            trace = lookup.get((execution_day, symbol))
            match = trace is not None and abs(trace["target_weight"] - weight) <= 1e-10
            record = ranked.get(symbol, {})
            rows.append({"signal_date": day, "execution_date": execution_day, "symbol": symbol,
                "saved_rank": rank, "rank_status": "inside_saved_buffer" if rank else "outside_saved_buffer",
                "selection_reason": reason, "selected_slot": selected, "target_weight": weight,
                "explicit_zero_budget": selected and weight == 0, "decision_held_quantity": prior,
                "trace_status": "saved_matching_attempt" if match else "missing_or_overridden_attempt",
                "engine_target_quantity": trace["engine_target_quantity"] if match else None,
                "held_after": trace["held_after"] if match else None,
                "has_execution_open": trace["has_execution_open"] if match else None,
                **{key: record.get(key) for key in ("source_pub_date", "source_stat_date", "momentum",
                    "volatility", "roeAvg_provider_value", "gpMargin_provider_value", "score")}})
    if len(targets) != len(decision_holdings):
        raise ValueError("Decision observations and targets disagree")
    return pd.DataFrame(rows)


def selected_momentum_count(selected):
    known = pd.to_numeric(selected.momentum, errors="raise").notna()
    return int(selected.momentum.lt(0).sum()) if known.all() else None


def cohort_labels(signals, labels, decisions, ranks, signal, config):
    from .stockdb_factor_diagnostics import BLEND
    from .stockdb_factor_replay import SIZE
    score_column = BLEND if signal == BLEND else SIZE
    if (signals.index.names != ["date", "symbol"] or signals.index.has_duplicates
            or labels.index.names != ["date", "symbol"] or labels.index.has_duplicates):
        raise ValueError("Unique saved score and label identities required")
    rows = []
    for day, decision in decisions.groupby("signal_date", sort=True):
        frame = signals.xs(pd.Timestamp(day), level="date")
        frame = frame.loc[np.isfinite(frame).all(axis=1)]
        scores = frame[score_column]
        feature_ranks = frame.drop(columns=BLEND).rank(pct=True)
        batch = ranks.loc[ranks.date.eq(day)].sort_values("rank")
        identity = batch.set_index("symbol").security_identity
        expected = scores.rename("score").reset_index().sort_values(
            ["score", "symbol"], ascending=[False, True], kind="stable").head(config.retain_rank)
        if (expected.symbol.tolist() != batch.security_identity.tolist()
                or not np.array_equal(expected.score.to_numpy(), batch.score.to_numpy())):
            raise ValueError("Saved rank buffer differs from its fixed source scores")
        observed = labels.xs(pd.Timestamp(day), level="date").reindex(scores.index)
        observed = observed.where(np.isfinite(observed))
        pool_mean = observed.mean()
        percentile = scores.rank(pct=True)
        def members(mask):
            return identity.reindex(decision.loc[mask, "symbol"]).tolist()
        selected = decision.selected_slot & decision.target_weight.gt(0)
        cohorts = {
            "common_pool": scores.index.tolist(),
            "q5": scores.index[percentile.gt(.8)].tolist(),
            "top20_head": batch.security_identity.head(config.holdings).tolist(),
            "positive_selected_targets": members(selected),
            "positive_entry_holdings": members(selected & decision.trace_status.eq("saved_matching_attempt")
                                                & decision.held_after.gt(0)),
            "retained_rank_buffer": members(selected & decision.selection_reason.eq("retained_rank_buffer")),
            "displaced_head": members(decision.selection_reason.eq("unselected_head_displaced_by_retention")),
        }
        for cohort, symbols in cohorts.items():
            if len(set(symbols)) != len(symbols) or not set(symbols) <= set(scores.index):
                raise ValueError("Selected cohort has unknown or duplicated source identities")
            values = observed.reindex(symbols)
            rows.append({"signal_date": day, "execution_date": decision.execution_date.iloc[0],
                "signal": signal, "cohort": cohort, "members": len(symbols),
                "known_labels": int(values.notna().sum()), "missing_labels": int(values.isna().sum()),
                "label_coverage_complete": bool(len(values) and values.notna().all()),
                "mean_adjusted_open_label_h20": values.mean(),
                "common_pool_observed_mean_label_h20": pool_mean,
                "excess_adjusted_open_label_h20": values.mean() - pool_mean,
                "common_pool_members": len(scores), "common_pool_missing_labels": int(observed.isna().sum()),
                **{"oriented_rank_" + column: feature_ranks[column].reindex(symbols).mean()
                   for column in feature_ranks}})
    return pd.DataFrame(rows)


def saved_monthly_labels(source_run, days, end):
    root = Path(source_run)
    pieces = [pq.read_table(root / "labeled_inputs" / (month + ".parquet"),
        columns=["date", "symbol", "decision_eligible", "label_h20", "exit_h20"],
        filters=[("date", "in", [pd.Timestamp(day) for day in days if day.startswith(month)])]).to_pandas()
        for month in sorted({day[:7] for day in days})]
    frame = pd.concat(pieces, ignore_index=True)
    frame = frame.loc[frame.decision_eligible].copy()
    frame["label_h20"] = frame.label_h20.where(frame.exit_h20.notna() & frame.exit_h20.le(pd.Timestamp(end)))
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("Saved labeled inputs duplicate an eligible source identity")
    return frame.set_index(["date", "symbol"]).label_h20.sort_index()


def held_sessions(marks, observations):
    days = [r["date"] for r in marks]
    if not days or days != sorted(set(days)) or not set(observations) <= set(days):
        raise ValueError("Saved holding changes must lie in the successful marked sessions")
    current, rows = {}, []
    for mark in marks:
        current = observations.get(mark["date"], current)
        count = Decimal(str(mark["holdings"]))
        if not count.is_finite() or count < 0 or count != int(count):
            raise ValueError("Saved daily holding count must be a nonnegative integer")
        if len(current) != int(count):
            raise ValueError(f"Saved fill/action quantity path disagrees with daily holding count: "
                             f"{mark['date']} projected={len(current)} marked={mark['holdings']}")
        rows.extend({"date": mark["date"], "symbol": symbol, "quantity": quantity,
                     "stale_valuation": symbol in mark["stale_symbols"]}
                    for symbol, quantity in sorted(current.items()))
    return pd.DataFrame(rows, columns=["date", "symbol", "quantity", "stale_valuation"])


def summarize_symbols(decisions, sessions, pnl):
    symbols = sorted(set(decisions.symbol) | set(pnl.symbol))
    lookup = pnl.set_index("symbol")
    rows = []
    for symbol in symbols:
        anchors = decisions.loc[decisions.symbol.eq(symbol)]
        selected = anchors.loc[anchors.selected_slot]
        held = sessions.loc[sessions.symbol.eq(symbol)]
        counts = Counter(anchors.selection_reason)
        last = anchors.iloc[-1] if len(anchors) else None
        rows.append({"symbol": symbol, "held_sessions": len(held),
            "first_held_date": held.date.iloc[0] if len(held) else None,
            "last_held_date": held.date.iloc[-1] if len(held) else None,
            "stale_held_sessions": int(held.stale_valuation.sum()),
            "selected_anchors": len(selected),
            "zero_budget_anchors": int(selected.explicit_zero_budget.sum()),
            "retained_head_anchors": counts["retained_top_head"],
            "retained_buffer_anchors": counts["retained_rank_buffer"],
            "new_selection_anchors": counts["new_selection"],
            "head_displaced_anchors": counts["unselected_head_displaced_by_retention"],
            "removed_outside_buffer_anchors": counts["removed_outside_saved_buffer"],
            "selected_negative_momentum_anchors": selected_momentum_count(selected),
            "selected_momentum_known_anchors": int(selected.momentum.notna().sum()),
            "last_observed_decision": last.signal_date if last is not None else None,
            "last_observed_rank": last.saved_rank if last is not None and pd.notna(last.saved_rank) else None,
            "last_selection_reason": last.selection_reason if last is not None else None,
            "observed_pnl_cny": float(lookup.at[symbol, "observed_pnl_cny"]) if symbol in lookup.index else None})
    return pd.DataFrame(rows)


def run(source_id, source_signal, cases):
    source, _ = replay_reports._read(source_id)
    fixed_factors = source.get("research_family") == STOCKDB_FAMILY
    expected_status = "completed_factor_ledger_diagnostics" if fixed_factors else "completed_profitability_diagnostics"
    if (source.get("research_family") not in {FAMILY, STOCKDB_FAMILY} or source.get("status") != expected_status
            or source.get("execution_admitted") is not False
            or source_signal not in source["protocol"]["signals"]):
        raise ValueError("Completed fixed reporting-calendar source and known signal required")
    ranks = None if fixed_factors else pd.read_csv(Path(source["rankings"]) / source_signal / "rankings.csv",
        dtype={key: str for key in ("date", "symbol", "source_pub_date", "source_stat_date")})
    scores = pd.read_parquet(Path(source["source_run"]) / "fixed_signals.parquet") if fixed_factors else None
    config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][source_signal]).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/holding_paths" / identifier
    artifacts = settings.DATA_ROOT / "research/holding_paths" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    record = {"id": identifier, "source_run": source_id, "source_signal": source_signal,
              "cases": [], "artifacts": str(artifacts), "status": "running",
              "accounts_replayed": False, "execution_admitted": False, "limits": LIMITS}
    write_json(out / "run.json", record)
    try:
        for case in cases:
            run_id, signal = case.split(":", 1)
            saved, root = replay_reports._read(run_id)
            if fixed_factors:
                if (saved.get("research_family") != STOCKDB_FAMILY
                        or saved.get("source_run") != source["source_run"]
                        or saved["protocol"]["stock_inputs"] != source["protocol"]["stock_inputs"]
                        or saved["stock_manifest"] != source["stock_manifest"]
                        or saved["pool_counts"] != source["pool_counts"]):
                    raise ValueError("Fixed-factor accounts need identical saved source/pool and market inputs")
                ranks = pd.read_csv(root / "rankings.csv", dtype={key: str for key in
                    ("date", "symbol", "security_identity", "case")}, float_precision="round_trip")
                ranks = ranks.loc[ranks.case.eq(signal)]
            elif run_id != source_id and (saved.get("research_family") not in {
                    "capital_lot_fee_implementation", "fixed_positive_momentum_exit_v1"}
                    or saved.get("source_run") != str(settings.ROOT / "outputs/market_replay" / source_id / "run.json")
                    or saved["protocol"].get("source_rankings") != source["rankings"]
                    or signal.split("__")[0] != source_signal):
                raise ValueError("Saved implementation account must bind the same signal/ranks/source")
            if not fixed_factors and run_id == source_id and signal != source_signal:
                raise ValueError("Source account signal changed")
            detail = replay_reports.detail(run_id, signal, include_marks=True)
            if (detail["config"] != config.to_dict() or any(saved.get(k) != source.get(k)
                                                          for k in ("start", "end", "dataset", "scope"))):
                raise ValueError("Saved account selector/cost config changed")
            folder = root / saved.get("ledger_folders", {}).get(signal, signal)
            account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
            decision_qty, daily_qty = {}, {}
            _, traces, _ = project_orders(account, action_map(saved, run_id),
                                         decision_holdings=decision_qty, daily_holdings=daily_qty)
            targets = [e for e in account["events"] if e["kind"] == "targets_generated"]
            decisions = selection_rows(targets, ranks, decision_qty, traces, config, financial_ranks=not fixed_factors)
            sessions = held_sessions(detail["daily_marks"], daily_qty)
            _, pnl = asset_pnl(account, config)
            summary = summarize_symbols(decisions, sessions, pnl)
            dest = artifacts / (run_id + "_" + signal)
            dest.mkdir()
            decisions.to_csv(dest / "selection_path.csv", index=False)
            sessions.to_csv(dest / "daily_share_path.csv", index=False)
            summary.to_csv(dest / "symbol_path_summary.csv", index=False)
            traded = summary.loc[summary.observed_pnl_cny.notna()].sort_values("observed_pnl_cny")
            selected = decisions.loc[decisions.selected_slot]
            inputs = saved.get("inputs", saved["protocol"].get("stock_inputs", {}))
            item = {"case": case, "status": detail["status"], "scope": "full_saved_account"
                    if detail["status"] == "completed" else "successful_prefix_only",
                    "sessions": len(detail["daily_marks"]), "anchors": len(targets),
                    "source_account": str(folder / "account.json"), "allocation": account.get("lot_allocation"),
                    "source_event_view": inputs.get("event_view"), "source_reference_view": inputs.get("reference_view"),
                    "selection_counts": dict(Counter(decisions.selection_reason)),
                    "selected_slots": len(selected), "selected_negative_momentum_slots": selected_momentum_count(selected),
                    "selected_momentum_known_slots": int(selected.momentum.notna().sum()),
                    "explicit_zero_budget_slots": int(selected.explicit_zero_budget.sum()),
                    "observed_held_sessions": len(sessions), "traded_or_held_symbols": len(traded),
                    "negative_contributions": int(traded.observed_pnl_cny.lt(0).sum()),
                    "net_observed_pnl_cny": round(float(traded.observed_pnl_cny.sum()), 2),
                    "worst5": traded.head(5).astype(object).where(pd.notna(traded.head(5)), None).to_dict("records")}
            if fixed_factors:
                labels = saved_monthly_labels(source["source_run"], sorted(decision_qty), source["end"])
                cohorts = cohort_labels(scores, labels, decisions, ranks, signal, config)
                cohorts.to_csv(dest / "month_end_cohort_labels.csv", index=False)
                item["cohort_labels"] = cohorts.groupby("cohort", sort=True).agg(
                    anchors=("signal_date", "count"), known_mean_anchors=("mean_adjusted_open_label_h20", "count"),
                    members=("members", "sum"), known_labels=("known_labels", "sum"),
                    missing_labels=("missing_labels", "sum"),
                    mean_label_h20=("mean_adjusted_open_label_h20", "mean"),
                    mean_excess_h20=("excess_adjusted_open_label_h20", "mean"),
                    **{column: (column, "mean") for column in cohorts if column.startswith("oriented_rank_")}
                    ).reset_index().to_dict("records")
            record["cases"].append(item)
            print(f"{case}: {item['sessions']} sessions, {len(targets)} anchors, no replay", flush=True)
    except Exception as error:
        record.update(status="failed_saved_holding_paths", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    record["status"] = "completed_saved_holding_paths"
    write_json(out / "run.json", record)
    lines = ["# Saved Selection and Holding Paths", "", "No account replay or rule changes. NO_TRADE.", "",
             "| Case | Scope / sessions | Selected / negative-momentum slots | Zero budgets | Retained buffer | Displaced head |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for item in record["cases"]:
        counts = item["selection_counts"]
        lines.append(f"|{item['case']}|{item['scope']} / {item['sessions']}|"
                     f"{item['selected_slots']} / {item['selected_negative_momentum_slots'] if item['selected_negative_momentum_slots'] is not None else 'UNKNOWN'}|"
                     f"{item['explicit_zero_budget_slots']}|{counts.get('retained_rank_buffer', 0)}|"
                     f"{counts.get('unselected_head_displaced_by_retention', 0)}|")
    for item in record["cases"]:
        lines += ["", f"## {item['case']}", "",
                  f"Observed security contribution {item['net_observed_pnl_cny']:,.2f}CNY; "
                  f"{item['negative_contributions']}/{item['traded_or_held_symbols']} negative. "
                  "A prefix is not a full-period result.", "",
                  "| Worst observed contribution | PnL CNY | Held sessions | Head / buffer retention anchors | Negative-momentum selected anchors | Last observed decision / rank |",
                  "| --- | ---: | ---: | ---: | ---: | --- |"]
        for row in item["worst5"]:
            rank = int(row["last_observed_rank"]) if row["last_observed_rank"] is not None else "outside saved buffer"
            lines.append(f"|{row['symbol']}|{row['observed_pnl_cny']:,.2f}|{row['held_sessions']}|"
                         f"{row['retained_head_anchors']} / {row['retained_buffer_anchors']}|"
                         f"{row['selected_negative_momentum_anchors'] if row['selected_negative_momentum_anchors'] is not None else 'UNKNOWN'}|"
                         f"{row['last_observed_decision']} / {rank}|")
        if item.get("cohort_labels"):
            lines += ["", "### Fixed20-Session Labels On The Saved Decision Dates", "",
                      "Observed-endpoint means, not account returns. No missing-label candidate deletion or compounded wealth path.",
                      "| Cohort | Anchors with mean | Members / known / missing labels | Mean label | Mean pool excess |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for row in item["cohort_labels"]:
                fmt = lambda value: f"{value:.6%}" if pd.notna(value) else "UNKNOWN"
                lines.append(f"|{row['cohort']}|{row['known_mean_anchors']}/{row['anchors']}|"
                             f"{row['members']} / {row['known_labels']} / {row['missing_labels']}|"
                             f"{fmt(row['mean_label_h20'])}|{fmt(row['mean_excess_h20'])}|")
            lines += ["", "Unweighted monthly mean oriented feature percentiles on the exact common pool.",
                      "High ranks mean high B/P, high E/P, SMALL float MV and LOW turnover respectively; not actual portfolio-weighted exposures.",
                      "| Cohort | B/P | E/P | Small float MV | Low turnover |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for row in item["cohort_labels"]:
                if row["cohort"] in {"top20_head", "positive_selected_targets", "positive_entry_holdings"}:
                    values = [fmt(row.get("oriented_rank_" + name)) for name in (
                        "book_to_price_candidate", "earnings_to_price_candidate",
                        "log_float_mv_candidate", "turnover_pct_candidate")]
                    lines.append("|" + row["cohort"] + "|" + "|".join(values) + "|")
    lines += ["", "## Limits", "", *["- " + value for value in LIMITS], "", f"Detailed saved paths: `{artifacts}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--source-signal", required=True)
    parser.add_argument("--account", action="append", required=True, help="Saved run_id:signal; repeat for implementations")
    args = parser.parse_args(argv)
    if len(set(args.account)) != len(args.account):
        raise ValueError("Distinct saved accounts required")
    run(args.source_run, args.source_signal, args.account)
