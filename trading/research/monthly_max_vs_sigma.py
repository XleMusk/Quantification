"""Fixed same-month low-MAX versus low-sigma research through the stock Engine.

Adjusted prices form scores; the original BulkResearchFeed supplies raw orders,
actions and account guards. This never collects sources or resumes old accounts.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..ledger import Ledger
from ..rules import stock_board
from ..report import metrics
from .baostock_daily_slice import digest
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result
from .low_risk import portfolio
from .monthly_timing import month_ends
from .weekly_liquidity_inputs import inspect_saved_weekly_inputs


METHOD = "same_calendar_month_low_max_vs_low_sigma_mainboard_v1"
ARMS = ("low_max", "low_sigma")
START, END, SESSIONS = "2024-01-02", "2026-09-30", 666
SOURCE = "https://pages.stern.nyu.edu/~rwhitela/papers/max%20jfe11.pdf"
FLAGS = dict(research_only=True, candidate_panel_only=True, strategy_validated=False,
    selection_enabled=False, execution_admitted=False, returns_reportable=False,
    pit_verified=False, historical_publication_verified=False, independent_holdout_claimed=False,
    model_refit=False, parameters_searched=False, network_accessed=False,
    original_accounts_modified=False, source_snapshots_modified=False)
SPEC = dict(id=METHOD, start=START, end=END, sessions=SESSIONS, arms=list(ARMS), source=SOURCE,
    question="does_avoiding_lottery_like_monthly_extreme_positive_returns_add_value_beyond_same_month_low_volatility",
    universe="source_covered_60xxxx_SSE_and_00xxxx_SZSE_ordinary_main_board_only_not_all_A_or_historical_roster",
    formation="each_confirmed_calendar_month_end_D_uses_the_just_completed_D_month_for_next_month_execution",
    returns="adjusted_close_t_over_exact_previous_session_adjusted_close_minus1_including_first_month_day",
    month_completeness="every_exact_month_session_plus_previous_month_last_session_active_nonST_positive_finite_close_and_volume",
    max="single_maximum_daily_return_in_that_calendar_month_no_top2_to5_or_fixed21day_substitution",
    sigma="sample_standard_deviation_ddof1_of_the_identical_calendar_month_daily_returns_no_annualization",
    eligibility="same_two_score_complete_pool_D_listing_age_ge365_no_reported_delisting_median20_amount_ge20M",
    no_extra_gate="no252_warmup_or_momentum_or_score_sign_filter_zero_sigma_and_negative_MAX_retained",
    ranking="negative_MAX_or_negative_sigma_descending_then_symbol_ascending_top30_no_future_filter",
    selection="original_choose_targets20_holdings_retain30_fixed4percent_each_gross80percent_single_cap10percent_cash_for_shortage",
    schedule="32_confirmed_D_Jan31_2024_through_Aug31_2026_first_E_Feb1_2024_January2024_cash",
    execution="original_Engine_Ledger_next_raw_open_T1_100share_lots_limits_participation_and_original_costs",
    config_windows="legacy_portfolio252_21_fields_are_not_used_for_scoring_or_pool_eligibility",
    terminal="Sep30_2026_marked_equity_and_real_residual_positions_no_forced_sale_or_uncovered_future_target",
    unknown="single_stock_missing_excluded_from_both_arms_global_batch_unknown_NO_CHANGE_known_empty_pool_explicit_empty_FULL",
    account_failure="original_action_or_reference_unknown_stops_only_affected_arm_no_zero_terms_notice_chasing_or_source_replacement",
    retry="fresh_independent_accounts_only_existing_output_rejected_no_resume_or_automatic_retry",
    evidence="previously_inspected_2024_2026_history_A_share_long_only_adaptation_not_paper_long_short_replication",
    failure_criteria={
        "incomplete_any_of32_formation_batches": "paired_economic_comparison_unknown_even_if_NO_CHANGE_ledgers_finish666_days",
        "incomplete_either_arm": "paired_economic_comparison_unknown_not_zero_or_strategy_rejection",
        "full_window_low_MAX_net_return_le0": "reject_this_fixed_expression_no_window_or_parameter_change",
        "full_window_incremental_net_return_le0": "reject_low_MAX_incremental_advantage",
        "positive_same_account_yearly_increment_fewer_than2_of3": "reject_cross_period_stability_claim",
        "year_boundaries": "2024_2025_2026_through_Sep30_from_same_continuous_Ledger_prior_year_closing_equity_not_cash_reset",
        "all_three_economic_conditions_pass": "future_observation_candidate_only_all_selection_validation_execution_flags_still_false",
        "industry_size_independence": "not_established_no_neutral_or_matched_exposure_claim",
        "source_or_implementation_change": "reject_before_Engine_never_reinterpret_old_account"})


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def config():
    return replace(portfolio(140000), single_limit=.10).validate()


def separate(out, sources):
    out = Path(out).resolve()
    if any(out == path or path in out.parents or out in path.parents
           for path in (Path(item).resolve() for item in sources)):
        raise ValueError("New research outputs must be separate from all original sources and accounts")


def month_window(calendar, day):
    """Return the exact preceding close plus every session of completed D month."""
    if (calendar != sorted(set(calendar)) or day not in month_ends(calendar, day)
            or any(date.fromisoformat(item).isoformat() != item for item in calendar)):
        raise ValueError("One confirmed month-end in the exact original calendar is required")
    days = [item for item in calendar if item[:7] == day[:7] and item <= day]
    first = calendar.index(days[0])
    if first == 0 or calendar[first - 1][:7] == day[:7] or len(days) < 2:
        raise ValueError("The completed calendar month needs its exact preceding-month close")
    return [calendar[first - 1], *days]


def score_month(closes, active, calendar, day):
    """Pure common-pool MAX/sigma calculation; future rows never enter scores."""
    window = month_window(calendar, day)
    if (closes.index.has_duplicates or active.index.has_duplicates
            or closes.columns.has_duplicates or active.columns.has_duplicates
            or list(closes.columns) != list(active.columns)
            or any(item not in closes.index or item not in active.index for item in window)):
        raise ValueError("Exact month/anchor price and activity rows are required; never compress dates")
    prices = closes.loc[window].apply(pd.to_numeric, errors="coerce")
    activity = active.loc[window].eq(True).fillna(False)
    good_price = np.isfinite(prices).all(axis=0) & prices.gt(0).all(axis=0)
    good_activity = activity.all(axis=0)
    returns = prices.div(prices.shift(1)).sub(1).iloc[1:]
    maximum, sigma = returns.max(axis=0, skipna=False), returns.std(axis=0, ddof=1, skipna=False)
    rows, unknown = [], {}
    for symbol in closes.columns:
        if not good_price[symbol]:
            unknown[symbol] = "incomplete_positive_adjusted_month_or_anchor"
        elif not good_activity[symbol]:
            unknown[symbol] = "month_or_anchor_not_observed_active_nonST"
        elif not (np.isfinite(maximum[symbol]) and np.isfinite(sigma[symbol])):
            unknown[symbol] = "nonfinite_common_month_scores"
        else:
            rows.append(dict(symbol=symbol, max_daily_return=float(maximum[symbol]),
                             sigma=float(sigma[symbol]), month_observations=len(window) - 1))
    return dict(day=day, month=day[:7], window=window, rows=rows, unknown=unknown)


def qualification_reason(symbol, info, day, own_config):
    try:
        if stock_board(symbol) != "main":
            return "outside_10percent_mainboard"
    except ValueError:
        return "outside_10percent_mainboard"
    try:
        ipo = date.fromisoformat(info.get("ipoDate", ""))
        out = date.fromisoformat(info["outDate"]) if info.get("outDate") else None
    except (TypeError, ValueError):
        return "unknown_listing_dates"
    current = date.fromisoformat(day)
    if (current - ipo).days < own_config.min_listing_days:
        return "listing_age_below365"
    if out is not None and current >= out:
        return "reported_outside_listing_period"
    return None


def formation(base, days, own_config):
    """Reuse the original adjusted matrices and exact dated raw activity reader."""
    batches, scored = {}, []
    symbols = [symbol for symbol in base.symbols
               if qualification_reason(symbol, base.instruments[symbol], START, own_config)
               != "outside_10percent_mainboard"]
    median = base.amounts[symbols].rolling(20, min_periods=20).median()
    for day in days:
        window = month_window(base.calendar, day)
        activity = pd.DataFrame(False, index=window, columns=symbols)
        activity_unknown = False
        for source_day in window:
            rows = base.rows.get(source_day)
            if rows is None:
                break
            for symbol in symbols:
                row = rows.get(symbol)
                activity_unknown |= bool(row is None or row.get("tradestatus") not in (0, 1)
                    or row.get("isST") not in (0, 1) or not np.isfinite(row.get("volume", np.nan)))
                activity.at[source_day, symbol] = bool(row is not None
                    and row.get("tradestatus") == 1 and row.get("isST") == 0
                    and np.isfinite(row.get("volume", np.nan)) and row["volume"] > 0)
        else:
            values = score_month(base.closes[symbols], activity, base.calendar, day)
            if not values["rows"] and (activity_unknown or any(reason != "month_or_anchor_not_observed_active_nonST"
                                                               for reason in values["unknown"].values())):
                batches[day] = dict(status="incomplete", reason="whole_mainboard_common_month_scores_unknown", targets=None)
                continue
            exclusions = Counter(values["unknown"].values())
            common = []
            for row in values["rows"]:
                symbol = row["symbol"]
                reason = qualification_reason(symbol, base.instruments[symbol], day, own_config)
                amount = float(median.at[day, symbol])
                if reason is None and (not np.isfinite(amount) or amount < own_config.min_amount):
                    reason = "median20_amount_unknown_or_below20M"
                if reason:
                    exclusions[reason] += 1
                else:
                    common.append({**row, "median_amount": amount})
            targets = {}
            for arm, key in zip(ARMS, ("max_daily_return", "sigma")):
                ranked = sorted(common, key=lambda row: (row[key], row["symbol"]))
                targets[arm] = [{**row, "score": -row[key]} for row in ranked[:own_config.retain_rank]]
            batches[day] = dict(status="complete", month=day[:7], window=window,
                execution_day=base.next_day(day), common_pool=len(common),
                source_mainboard_identities=len(symbols), exclusions=dict(exclusions), targets=targets)
            scored.extend(dict(date=day, **row) for row in common)
            continue
        batches[day] = dict(status="incomplete", reason="whole_exact_raw_activity_day_missing",
                            missing_day=source_day, targets=None)
    return batches, scored


class MonthlyMaxSigmaFeed:
    def __init__(self, base, batches, arm, own_config):
        self.base, self.batches, self.arm, self.config = base, batches, arm, own_config
        self.audit = []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, own_config, context):
        if context.mode != TradingMode.REPLAY or own_config != self.config or self.arm not in ARMS:
            raise ValueError("Only the fixed independent two-arm REPLAY config is supported")
        batch = self.batches.get(day)
        if batch is None or batch["status"] != "complete":
            reason = "not_confirmed_month_end" if batch is None else batch["reason"]
            self.audit.append(dict(date=day, intent="NO_CHANGE", reason=reason))
            return SignalUnavailable(reason)
        rows = batch["targets"][self.arm]
        self.audit.append(dict(date=day, intent="FULL_TARGET", candidates=len(rows),
                               reason="same_month_ranking" if rows else "known_empty_common_pool"))
        return SignalFrame.from_legacy(rows, day, "research:" + METHOD + ":" + self.arm, context)


def _implementations():
    root = Path(__file__).resolve().parent
    return {str(path): digest(path) for path in (Path(__file__).resolve(), root / "bulk_feed.py",
        root / "weekly_liquidity_inputs.py", root / "monthly_timing.py", root / "low_risk.py",
        root / "experiment.py", root.parent / "engine.py", root.parent / "ledger.py",
        root.parent / "rules.py", root.parent / "config.py", root.parent / "contracts.py")}


def _source_binding(source, inputs):
    refs, clocks = dict(source["metadata_refs"]), dict(source["source_clock"])
    for name in ("event_view", "reference_view"):
        path = Path(inputs[name]) / "result.json"
        refs[name] = dict(path=str(path), sha256=digest(path))
    plan = Path(inputs["listing_master"]).parent / "collection_plan.json"
    if plan.is_file():
        refs["listing_collection_plan"] = dict(path=str(plan), sha256=digest(plan))
        metadata = read(plan)
        clocks["listing_metadata_plan_created_at"] = metadata.get("created_at")
        clocks["listing_metadata_scope"] = metadata.get("scope")
    clocks["listing_actual_receipt_clock_independently_verified"] = False
    return refs, clocks


def register(source_run, out):
    """Metadata-only declaration; original manifests retain the large payload SHAs."""
    source_run, out = Path(source_run).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Two-arm registration requires a new directory")
    source = inspect_saved_weekly_inputs(source_run)
    original = read(source_run)
    inputs = {key: original["inputs"][key] for key in
              ("price_view", "event_view", "reference_view", "raw_view", "listing_master")}
    calendar = source["calendar"]
    dates = [day for day in calendar if START <= day <= END]
    days = [day for day in month_ends(calendar, END) if START <= day <= END]
    if (calendar[0] != "2023-01-03" or calendar[-1] != END or len(calendar) != 908
            or len(dates) != SESSIONS or len(days) != 32
            or days[0] != "2024-01-31" or days[-1] != "2026-08-31"):
        raise ValueError("Original 2023 warmup, 666-day account window and 32 confirmed month-ends required")
    refs, clocks = _source_binding(source, inputs)
    separate(out, [source_run, *inputs.values()])
    out.mkdir(parents=True, exist_ok=False)
    record = dict(schema=METHOD + "_registration", registered_at=now(), spec=SPEC,
        config=config().to_dict(), source_run=str(source_run), inputs=inputs, refs=refs,
        source_calendar=calendar, decision_days=days, first_execution_day="2024-02-01",
        canonical_panel=source["canonical_panel"], source_clock=clocks,
        source_inventory=dict(observed_source_codes=original["inputs"]["source_codes"], canonical_codes=len(source["codes"]),
            mainboard_identities=sum(symbol.startswith(("60", "00")) for symbol in source["symbols"]),
            candidate_pool="dynamic_complete_month_mainboard_eligibility_not_original_weekly_selected_names",
            original_weekly_entry_notice_filter_reused=False, original_weekly_rankings_or_accounts_reused=False),
        large_source_payload_hashes_recomputed=False, source_limitations=source["limitations"],
        implementations=_implementations(), **FLAGS)
    save(out / "registration.json", record)
    return record


def _registration(path):
    value = read(path)
    if (value.get("schema") != METHOD + "_registration" or value.get("spec") != SPEC
            or value.get("config") != config().to_dict()
            or value.get("implementations") != _implementations()
            or any(value.get(key) is not flag for key, flag in FLAGS.items())):
        raise ValueError("Exact fixed two-arm rules, costs, implementation and source registration required")
    source = inspect_saved_weekly_inputs(value["source_run"])
    original = read(value["source_run"])
    inputs = {key: original["inputs"][key] for key in
              ("price_view", "event_view", "reference_view", "raw_view", "listing_master")}
    expected_refs, clocks = _source_binding(source, inputs)
    days = [day for day in month_ends(source["calendar"], END) if START <= day <= END]
    if (inputs != value["inputs"] or source["calendar"] != value["source_calendar"]
            or days != value["decision_days"] or source["canonical_panel"] != value["canonical_panel"]
            or value["refs"] != expected_refs or value.get("source_clock") != clocks):
        raise ValueError("The original matched source/calendar/confirmed monthly schedule changed")
    for ref in value["refs"].values():
        if digest(ref["path"]) != ref["sha256"]:
            raise ValueError("Registered source metadata changed: " + ref["path"])
    return value


def _case_summary(case, folder, own_config, dates):
    state_path = folder / "account.json"
    state = read(state_path) if state_path.is_file() else None
    complete = bool(state is not None and case["status"] == "completed"
        and case.get("completed_sessions") == SESSIONS and state.get("day") == END and state["fills"])
    case.update(full_period_metrics_available=complete, account=str(state_path) if state is not None else None)
    if state is not None:
        book = Ledger(state, own_config)
        case.update(cumulative_orders=len(state["orders"]), cumulative_fills=len(state["fills"]),
            account_day=state["day"], residual_positions={symbol: book.quantity(symbol)
                for symbol in state["positions"] if book.quantity(symbol) > 0},
            receivables_cents=state["receivables"], pending_targets=state["pending_targets"],
            marked_equity_rmb=book.equity() / 100, cash_rmb=state["cash"] / 100,
            pending_orders={key: order for key, order in state["orders"].items()
                            if order["status"] in {"submitted", "partial", "cancel_pending"}})
    daily_path = folder / "daily.csv"
    if complete and not daily_path.is_file():
        raise ValueError("A complete account requires its original complete daily ledger")
    if daily_path.is_file() and case.get("completed_sessions", 0):
        daily = pd.read_csv(daily_path, converters={"stale_symbols": ast.literal_eval})
        if (daily.date.tolist() != dates[:case["completed_sessions"]] or daily.iloc[-1].date != state["day"]
                or round(float(daily.iloc[-1].equity) * 100) != book.equity()
                or not daily.stale_symbols.map(lambda value: isinstance(value, list)).all()):
            raise ValueError("Saved account/daily prefix and stale valuation fields disagree")
        case.update(saved_prefix_stale_valuation_days=int(daily.stale_symbols.map(bool).sum()),
                    terminal_stale_symbols=daily.iloc[-1].stale_symbols)
        if complete:
            years = {}
            for year in ("2024", "2025", "2026"):
                subset = daily.loc[daily.date.str.startswith(year)]
                first = subset.index[0]
                opening = own_config.initial_cash if first == 0 else float(daily.iloc[first - 1].equity)
                years[year] = dict(opening_equity_rmb=opening, same_continuous_account=True,
                                  **metrics(subset.to_dict("records"), opening))
            case["same_account_year_metrics"] = years
    if not complete:
        for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "annualized_volatility", "worst_day"):
            case[key] = None
    return case


def _economic_assessment(cases, *, formation_complete):
    if not formation_complete:
        return dict(status="unknown_incomplete_formation_source", future_observation_candidate=None)
    if not all(case["full_period_metrics_available"] for case in cases):
        return dict(status="unknown_incomplete_account_evidence", future_observation_candidate=None)
    low_max, low_sigma = cases
    increments = {year: low_max["same_account_year_metrics"][year]["net_return"]
        - low_sigma["same_account_year_metrics"][year]["net_return"] for year in ("2024", "2025", "2026")}
    delta = low_max["net_return"] - low_sigma["net_return"]
    keep = low_max["net_return"] > 0 and delta > 0 and sum(value > 0 for value in increments.values()) >= 2
    return dict(status="future_observation_candidate_only" if keep else "reject_fixed_expression_without_retuning",
        low_MAX_positive_net_return=low_max["net_return"] > 0, full_window_incremental_net_return=delta,
        same_account_yearly_increment=increments, positive_year_count=sum(value > 0 for value in increments.values()),
        future_observation_candidate=keep, industry_size_independence_established=False)


def run(registration, out):
    registration, out = Path(registration).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Fresh two-arm output required; original failures/attempts are never retried")
    saved = _registration(registration)  # No scoring, large source reader or account before this check.
    separate(out, [registration.parent, saved["source_run"], *saved["inputs"].values()])
    out.mkdir(parents=True, exist_ok=False)
    own_config = config()
    record = dict(schema=METHOD + "_result", status="running", started_at=now(),
        registration=dict(path=str(registration), sha256=digest(registration)),
        spec=SPEC, config=own_config.to_dict(), cases=[], **FLAGS)
    save(out / "started.json", record)
    base = None
    try:
        base = BulkResearchFeed(**{key: saved["inputs"][key] for key in
                                   ("price_view", "event_view", "reference_view", "listing_master")})
        if base.calendar != saved["source_calendar"]:
            raise ValueError("The execution reader no longer matches the declared complete source calendar")
        dates = [day for day in base.calendar if START <= day <= END]
        batches, scored = formation(base, saved["decision_days"], own_config)
        save(out / "batches.json", batches)
        pd.DataFrame(scored, columns=["date", "symbol", "max_daily_return", "sigma",
            "month_observations", "median_amount"]).to_parquet(out / "scores.parquet", index=False)
        save(out / "formation_receipt.json", dict(computed_at=now(), historical_publication=False,
            formation_rows=len(scored), common_batch_count=sum(row["status"] == "complete" for row in batches.values()),
            files={name: dict(path=str(out / name), sha256=digest(out / name))
                   for name in ("batches.json", "scores.parquet")}, **FLAGS))
        print(json.dumps(dict(stage="formation_saved", dates=len(dates), batches=len(batches), rows=len(scored))), flush=True)
        for arm in ARMS:
            feed = MonthlyMaxSigmaFeed(base, batches, arm, own_config)
            try:
                case = baseline_ledger_result(feed, own_config, dates, out / arm)
                case = _case_summary(case, out / arm, own_config, dates)
            except Exception as error:
                case = dict(status="failed_command", error=f"{type(error).__name__}: {error}",
                    full_period_metrics_available=False, original_account_files_preserved=True,
                    net_return=None, max_drawdown=None)
            case["arm"] = arm
            save(out / (arm + "_result.json"), case)
            save(out / (arm + "_decision_audit.json"), feed.audit)
            record["cases"].append(case)
            print(json.dumps(dict(stage="account_terminal", arm=arm, status=case["status"],
                sessions=case.get("completed_sessions"), metrics_available=case["full_period_metrics_available"])), flush=True)
        formations_complete = len(batches) == 32 and all(batch["status"] == "complete" for batch in batches.values())
        accounts_complete = all(case["full_period_metrics_available"] for case in record["cases"])
        paired = formations_complete and accounts_complete
        record.update(status="completed_two_arm_attempt", completed_at=now(),
            full_paired_account_comparison=paired, overall_outcome="complete" if paired else "partial_or_unavailable",
            all_32_formation_batches_complete=formations_complete, both_666_session_accounts_complete=accounts_complete,
            formation_rows=len(scored), formation_days=len(batches), marked_returns_not_liquidated=True,
            source_limitations=base.manifest["limitations"], automatic_retry=False,
            economic_assessment=_economic_assessment(record["cases"], formation_complete=formations_complete))
        save(out / "result.json", record)
        return record
    except BaseException as error:
        save(out / "failure.json", dict(**record, failed_at=now(), error=f"{type(error).__name__}: {error}"))
        raise
    finally:
        if base is not None:
            base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    declare = commands.add_parser("register")
    declare.add_argument("--source-run", required=True)
    declare.add_argument("--out", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--registration", required=True)
    execute.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = register(args.source_run, args.out) if args.operation == "register" else run(args.registration, args.out)
    print(json.dumps({key: result[key] for key in ("schema",) + (("status", "full_paired_account_comparison")
        if args.operation == "run" else ("registered_at", "first_execution_day"))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
