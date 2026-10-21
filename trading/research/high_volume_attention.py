"""Fixed own-history trading-activity challenger through the existing ledger.

This is an A-share long-only adaptation, not the original NYSE long/short study.
Nothing here acquires market data, changes old accounts or authorizes trading.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..ledger import Ledger
from .baostock_daily_slice import digest
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from .experiment import baseline_ledger_result
from .high_volume_attention_signal import (
    START, END, ARMS, activity_ranks, decision_days, eligible_group, rank_group,
)
from .low_risk import LowRiskFeed, features, portfolio


METHOD = "own50_activity_conditioned_momentum_fixed20session_v1"
SOURCE = "https://rodneywhitecenter.wharton.upenn.edu/wp-content/uploads/2014/04/9901.pdf"
FLAGS = dict(candidate_panel_only=True, strategy_validated=False,
    selection_enabled=False, execution_admitted=False, returns_reportable=False,
    pit_verified=False, historical_publication_verified=False,
    model_refit=False, future_labels_used_for_score=False,
    network_accessed=False, original_accounts_modified=False)
SPEC = dict(id=METHOD, start=START, end=END, arms=list(ARMS),
    question="does_own_history_high_activity_improve_the_same_momentum_portfolio",
    source=SOURCE, source_review="author_working_paper_physical_pages8_to10_and_footnote9",
    mechanism="visibility_and_subsequent_demand_hypothesis_not_net_flow_or_hidden_intent",
    activity="reported_actual_CNY_traded_amount_not_CRSP_share_volume_times_last_price_proxy",
    reference="49_prior_exact_sessions_plus_own_formation_session",
    rank="midrank_1_plus_count_prior_less_plus_half_count_prior_equal",
    high="rank_ge46", low="rank_le5", normal="rank_gt5_and_lt46_including_half_rank_ties",
    history="all50_positive_finite_amount_and_volume_no_halt_or_missing_imputation",
    common_pool="same_low_risk252_complete_history_active_nonST_age365_median20_amount_ge20M",
    ordering="original_12minus1_momentum_descending_symbol_ascending_within_each_group",
    selection="top20_no_rank_buffer_no_refill_same80percent_gross_4percent_per_name",
    decisions="exact_range_sessions19_colon_colon20_zero_based_no_shift_for_empty_or_unknown",
    execution="same_Engine_Ledger_next_raw_open_T1_lots_limits_participation_fees",
    accounts="four_independent_cash_accounts_not_combined_or_selected_by_performance",
    terminal="mark_at_fixed_last_source_close_no_forced_liquidation_or_claim_all_positions_sold",
    supporting_observation="next_open_to_open20_sessions_later_adjusted_price_proxy_before_fees",
    supporting_comparison="same_formation_day_paired_differences_conditional_and_complete_baskets_separate",
    sample="previously_inspected2024_to2026_retrospective_new_mechanism_not_independent_holdout",
    paper_differences="NYSE1963to1996_long_short_50day_blocks_with_skipday_vs_local_long_only20day_momentum_sort",
    safeguards="missing_account_terms_stop_only_affected_arm_no_notice_chasing_or_source_replacement",
    parameters_selected_by_return=False, profit_or_black_swan_guarantee=False)


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def config():
    return replace(portfolio(), retain_rank=20).validate()


def register(source_run, out, *, saved_inputs=None):
    source_run, out = Path(source_run).resolve(), Path(out).resolve()
    source = read(source_run)
    if (source.get("status") != "completed_weekly_reversal_diagnostic"
            or source.get("scope") != BULK_SCOPE
            or (source.get("start"), source.get("end"), source.get("sessions")) != (START, END, 666)
            or source.get("strategy_validated") is not False):
        raise ValueError("Require the saved complete full-source weekly experiment as an input locator")
    inputs = source["inputs"]
    refs = {"source_run": {"path": str(source_run), "sha256": digest(source_run)}}
    for key in ("price_view", "raw_view", "event_view", "reference_view"):
        path = Path(inputs[key]) / "result.json"
        refs[key] = {"path": str(path), "sha256": digest(path)}
    path = Path(inputs["listing_master"])
    refs["listing_master"] = {"path": str(path), "sha256": digest(path)}
    for path in [source_run, *(Path(inputs[k]) for k in
            ("price_view", "raw_view", "event_view", "reference_view", "listing_master"))]:
        if out == path or path in out.parents or out in path.parents:
            raise ValueError("New registration must be separate from source and account directories")
    reused = None
    if saved_inputs is not None:
        folder = Path(saved_inputs).resolve()
        previous_result = read(folder / "result.json")
        previous = read(previous_result["registration"])
        if (previous_result.get("schema") != METHOD
                or previous_result.get("status") != "completed_fixed_four_arm_research"
                or digest(previous_result["registration"]) != previous_result["registration_sha256"]
                or previous.get("spec") != SPEC or previous.get("config") != config().to_dict()
                or previous.get("inputs") != inputs or previous.get("refs") != refs
                or [c.get("arm") for c in previous_result["cases"]] != list(ARMS)
                or any(c.get("error") != "ValueError: Invalid signal diagnostic"
                    or c.get("completed_sessions") != 19 or c.get("cumulative_orders") != 0
                    or c.get("cumulative_fills") != 0 for c in previous_result["cases"])):
            raise ValueError("Saved reuse is only the exact pre-trade diagnostic-serialization failure")
        files = ("batches.json", "formation.parquet", "forward_observations.parquet",
            "price_summaries.json", "paired_price_observations.json", "result.json")
        reused = dict(reason="numeric_signal_projection_fix_no_changed_rule_or_rescoring",
            observed_at=now(), original_payload_hashes_recorded_before_failure=False,
            files={name: {"path": str(folder / name), "sha256": digest(folder / name)} for name in files})
        if out == folder or folder in out.parents or out in folder.parents:
            raise ValueError("Corrected registration must not overwrite the failed study")
    out.mkdir(parents=True, exist_ok=False)
    value = dict(schema=METHOD + "_registration", registered_at=now(), spec=SPEC,
        config=config().to_dict(), inputs=inputs, refs=refs,
        implementations={str(path.resolve()): digest(path) for path in
            (Path(__file__), Path(__file__).with_name("high_volume_attention_signal.py"),
             Path(__file__).with_name("low_risk.py"), Path(__file__).with_name("bulk_feed.py"),
             Path(__file__).parents[1] / "engine.py", Path(__file__).parents[1] / "ledger.py")},
        saved_inputs=reused, **FLAGS)
    save(out / "registration.json", value)
    return value


class AttentionFeed:
    def __init__(self, base, batches, arm, own_config):
        self.base, self.batches, self.arm, self.config = base, batches, arm, own_config
        self.audit = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, own_config, context):
        if (context.mode != TradingMode.REPLAY or own_config != self.config
                or self.arm not in ARMS):
            raise ValueError("Registered independent REPLAY config required")
        batch = self.batches.get(day)
        if batch is None or batch["status"] != "complete":
            reason = "not_fixed_formation_day" if batch is None else batch["reason"]
            self.audit.append(dict(date=day, intent="NO_CHANGE", reason=reason))
            return SignalUnavailable(reason)
        candidates = batch["targets"][self.arm]
        self.audit.append(dict(date=day, intent="FULL_TARGET", candidates=len(candidates),
            reason="known_empty_group_exit" if not candidates else "fixed_activity_conditioned_ranking"))
        # Human-readable group labels remain in the immutable batch/audit.
        # Signal diagnostics have an explicit numeric-only contract.
        numeric = [{key: row[key] for key in ("symbol", "score", "momentum", "volatility",
            "median_amount", "activity_rank")} for row in candidates]
        return SignalFrame.from_legacy(numeric, day, "research:" + METHOD + ":" + self.arm, context)


def batches_from_common_pool(base, own_config, days):
    values = features(base, END, feature_days=days)
    ranks = activity_ranks(base.amounts.loc[:END], base.volumes.loc[:END], days)
    pool_feed = LowRiskFeed(base, values, "momentum_12_1_20", END, diagnostic_extension=True)
    batches, observations = {}, []
    for day in days:
        pool = pool_feed.pool(day, own_config)
        if isinstance(pool, SignalUnavailable):
            batches[day] = dict(status="incomplete", reason=pool.reason, targets=None)
            continue
        annotated = [{**row, "activity_rank": float(ranks.at[day, row["symbol"]])} for row in pool]
        unknown = [r["symbol"] for r in annotated if eligible_group(r["activity_rank"]) is None]
        if unknown:
            batches[day] = dict(status="incomplete", reason="common_pool_activity_rank_unknown",
                common_count=len(pool), unknown_symbols=unknown, targets=None)
            continue
        targets = {arm: rank_group(annotated, arm)[:own_config.holdings] for arm in ARMS}
        batches[day] = dict(status="complete", common_count=len(pool),
            group_counts=dict(Counter(eligible_group(r["activity_rank"]) for r in annotated)), targets=targets)
        chosen = {arm: {r["symbol"] for r in selected} for arm, selected in targets.items()}
        for row in annotated:
            observations.append(dict(date=day, **row,
                activity_group=eligible_group(row["activity_rank"]),
                **{f"selected_{arm}": row["symbol"] in chosen[arm] for arm in ARMS}))
    return batches, pd.DataFrame(observations)


def _endpoint(base, day, symbol, rows):
    row = rows.get(symbol)
    if row is None:
        return None, "missing_exact_endpoint_row"
    if row.get("tradestatus") != 1 or not np.isfinite(row.get("volume", np.nan)) or row["volume"] <= 0:
        return None, "endpoint_inactive_or_volume_unknown"
    raw_open, raw_close = row.get("open", np.nan), row.get("close", np.nan)
    adjusted_close = base.closes.at[day, symbol]
    if not np.isfinite([raw_open, raw_close, adjusted_close]).all() or min(raw_open, raw_close, adjusted_close) <= 0:
        return None, "endpoint_price_unknown"
    return float(raw_open * adjusted_close / raw_close), None


def forward_observations(base, observations):
    result = observations.copy()
    result["forward20_price_return"] = np.nan
    result["endpoint_reason"] = "after_fixed_source_cutoff"
    result["entry_day"] = None
    result["exit_day"] = None
    for day, group in result.groupby("date", sort=True):
        i = base.day_index[day]
        entry = base.calendar[i + 1] if i + 1 < len(base.calendar) else None
        exit_day = base.calendar[i + 21] if i + 21 < len(base.calendar) else None
        result.loc[group.index, "entry_day"] = entry
        result.loc[group.index, "exit_day"] = exit_day
        if entry is None or exit_day is None or exit_day > END:
            continue
        entry_rows, exit_rows = base.rows.get(entry, {}), base.rows.get(exit_day, {})
        for ix, row in group.iterrows():
            a, reason_a = _endpoint(base, entry, row.symbol, entry_rows)
            b, reason_b = _endpoint(base, exit_day, row.symbol, exit_rows)
            reason = reason_a or reason_b
            if reason is None:
                result.at[ix, "forward20_price_return"] = b / a - 1
                result.at[ix, "endpoint_reason"] = "known_price_observation_not_fill"
            else:
                result.at[ix, "endpoint_reason"] = reason
    return result


def summarize_observations(frame):
    rows = []
    periods = [("all", START, END), ("2024", "2024-01-01", "2024-12-31"),
        ("2025", "2025-01-01", "2025-12-31"), ("2026", "2026-01-01", END)]
    for period, start, end in periods:
        part = frame.loc[frame.date.between(start, end)]
        for scope in ("full_group", "selected20"):
            for arm in ARMS:
                selected = (part if arm == "all" else part.loc[part.activity_group.eq(arm)]) if scope == "full_group" else part.loc[part[f"selected_{arm}"]]
                known = selected.loc[selected.forward20_price_return.notna()]
                means = known.groupby("date").forward20_price_return.mean()
                day_counts = selected.groupby("date").size()
                known_counts = known.groupby("date").size().reindex(day_counts.index, fill_value=0)
                complete_days = day_counts.index[known_counts.eq(day_counts)]
                complete = means.reindex(complete_days).dropna()
                rows.append(dict(period=period, scope=scope, arm=arm,
                    formation_rows=len(selected), endpoint_known=len(known), endpoint_unknown=len(selected)-len(known),
                    observed_dates=len(means), complete_basket_dates=len(complete),
                    day_equal_price_return=float(means.mean()) if len(means) else None,
                    complete_basket_day_equal_price_return=float(complete.mean()) if len(complete) else None))
    return rows


def paired_price_observations(frame):
    """Match original formation dates, retaining each basket's unknown endpoints."""
    output = []
    for day, part in frame.groupby("date", sort=True):
        for scope in ("full_group", "selected20"):
            baskets = {}
            for arm in ARMS:
                selected = (part if arm == "all" else part.loc[part.activity_group.eq(arm)]) if scope == "full_group" else part.loc[part[f"selected_{arm}"]]
                known = selected.forward20_price_return.dropna()
                baskets[arm] = dict(count=len(selected), known=len(known),
                    mean=float(known.mean()) if len(known) else None)
            for arm, control in (("high", "all"), ("high", "normal"), ("low", "all")):
                left, right = baskets[arm], baskets[control]
                difference = left["mean"]-right["mean"] if left["mean"] is not None and right["mean"] is not None else None
                complete = (left["count"] > 0 and right["count"] > 0
                    and left["count"] == left["known"] and right["count"] == right["known"])
                output.append(dict(date=day, scope=scope, contrast=arm+"_minus_"+control,
                    arm_count=left["count"], arm_known=left["known"],
                    control_count=right["count"], control_known=right["known"],
                    conditional_same_date_difference=difference,
                    complete_basket_same_date_difference=difference if complete else None))
    return output


def run(registration_path, out):
    registration_path, out = Path(registration_path).resolve(), Path(out).resolve()
    registration = read(registration_path)
    if (registration.get("schema") != METHOD + "_registration" or registration.get("spec") != SPEC
            or registration.get("config") != config().to_dict()):
        raise ValueError("Require unchanged fixed method/config registration")
    for ref in registration["refs"].values():
        if digest(ref["path"]) != ref["sha256"]:
            raise ValueError("Registered source metadata changed")
    for path, expected in registration["implementations"].items():
        if digest(path) != expected:
            raise ValueError("Registered implementation changed")
    reused = registration.get("saved_inputs")
    if reused is not None:
        for ref in reused["files"].values():
            if digest(ref["path"]) != ref["sha256"]:
                raise ValueError("Saved original score/observation payload changed")
    for path in [registration_path, *(Path(r["path"]) for r in registration["refs"].values())]:
        if out == path or out in path.parents or path in out.parents:
            raise ValueError("Output cannot overlap registered sources")
    out.mkdir(parents=True, exist_ok=False)
    record = dict(schema=METHOD, status="running", started_at=now(), registration=str(registration_path),
        registration_sha256=digest(registration_path), config=config().to_dict(), cases=[], **FLAGS)
    save(out / "started.json", record)
    base = None
    try:
        inputs = registration["inputs"]
        base = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        dates = [d for d in base.calendar if START <= d <= END]
        if len(dates) != 666 or len(base.symbols) != inputs["canonical_codes"]:
            raise ValueError("Registered complete sample/calendar changed")
        days = decision_days(base.calendar)
        print(json.dumps(dict(stage="source_loaded", sessions=len(dates), symbols=len(base.symbols), formation_days=len(days))), flush=True)
        if reused is None:
            batches, observed = batches_from_common_pool(base, config(), days)
            save(out / "batches.json", batches)
            observed.to_parquet(out / "formation.parquet", index=False)
            print(json.dumps(dict(stage="formation_saved", rows=len(observed), complete_batches=sum(b['status']=='complete' for b in batches.values()))), flush=True)
            observed = forward_observations(base, observed)
            observed.to_parquet(out / "forward_observations.parquet", index=False)
            summaries = summarize_observations(observed)
            save(out / "price_summaries.json", summaries)
            save(out / "paired_price_observations.json", paired_price_observations(observed))
        else:
            batches = read(reused["files"]["batches.json"]["path"])
            observed = pd.read_parquet(reused["files"]["forward_observations.parquet"]["path"])
            original_result = read(reused["files"]["result.json"]["path"])
            if list(batches) != days or len(observed) != original_result["formation_rows"]:
                raise ValueError("Saved original fixed formation schedule/rows changed")
            save(out / "saved_inputs.json", reused)
            print(json.dumps(dict(stage="original_inputs_reused", rows=len(observed), scoring_rerun=False,
                forward_price_observation_rerun=False)), flush=True)
        for arm in ARMS:
            feed = AttentionFeed(base, batches, arm, config())
            result = baseline_ledger_result(feed, config(), dates, out / arm)
            save(out / arm / "decision_audit.json", feed.audit)
            state = read(out / arm / "account.json")
            book = Ledger(state, config())
            result.update(arm=arm, account=str(out / arm / "account.json"),
                residual_positions={s: book.quantity(s) for s in state.get("positions", {}) if book.quantity(s)>0},
                cumulative_orders=len(state["orders"]), cumulative_fills=len(state["fills"]),
                full_period_metrics_available=result["status"] == "completed" and bool(state["fills"]))
            if not result["full_period_metrics_available"]:
                result.update(net_return=None, max_drawdown=None, sharpe_zero_rf_252=None,
                    annualized_volatility=None, worst_day=None)
            record["cases"].append(result)
            print(json.dumps(dict(stage="arm_terminal", arm=arm, status=result["status"], sessions=result["completed_sessions"], fills=len(state["fills"]))), flush=True)
        record.update(status="completed_fixed_four_arm_research", completed_at=now(), sessions=len(dates),
            source_symbols=len(base.symbols), formation_days=len(days), formation_rows=len(observed),
            unique_symbols=int(observed.symbol.nunique()),
            endpoint_reason_counts=observed.endpoint_reason.value_counts().to_dict(),
            complete_batches=sum(b["status"]=="complete" for b in batches.values()),
            full_four_arm_comparison_available=all(c["full_period_metrics_available"] for c in record["cases"]),
            marked_returns_not_liquidated=True, price_diagnostic_not_economic_total_return=True,
            action_notices_downloaded=0, source_limitations=base.manifest["limitations"])
        record["saved_inputs"] = reused
        save(out / "result.json", record)
        return record
    except Exception as error:
        record.update(status="failed_research_command", completed_at=now(), error=f"{type(error).__name__}: {error}")
        save(out / "failure.json", record)
        raise
    finally:
        if base is not None:
            base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    reg = subs.add_parser("register")
    reg.add_argument("--source-run", required=True)
    reg.add_argument("--out", required=True)
    reg.add_argument("--saved-inputs")
    execute = subs.add_parser("run")
    execute.add_argument("--registration", required=True)
    execute.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    value = register(args.source_run, args.out, saved_inputs=args.saved_inputs) if args.operation == "register" else run(args.registration, args.out)
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
