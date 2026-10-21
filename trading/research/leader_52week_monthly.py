"""Registered paired monthly rankings, saved adjusted signals and shared raw execution.

This is an A-share long-only adaptation within a common positive-momentum pool,
not a reproduction of George/Hwang's overlapping long/short portfolios.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE
from . import leader_momentum_capacity as capacity
from . import leader_momentum_year as annual
from .baostock_daily_slice import digest
from .leader_historical_execution_policy import HistoricalExecutionPolicy
from .low_risk import WINDOW, SKIP
from .order_capacity_replay import PriorVolumeReplayPolicy
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting

METHOD = "adjusted_52week_high_vs_matched_12_1_monthly_positive_pool_v1"
REGISTRATION_SCHEMA = METHOD + "_registration"
SCORE_SCHEMA = METHOD + "_scores"
ARMS = ("high52", "momentum12_1")
YEARS = (2024, 2025)
FIXED_CODE_EXCLUSIONS = ("300114.SZSE", "302132.SZSE")
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "returns_reportable": False,
    "model_refit": False, "future_labels_used_for_score": False, "pit_verified": False,
    "historical_publication_verified": False, "corporate_actions_verified": False,
    "provider_contacted": False, "network_accessed": False}
SPEC = {"id": METHOD, "years": list(YEARS), "arms": list(ARMS),
    "research_question": "within_same_adjusted_positive_momentum_eligible_pool_does_52week_ranking_improve_monthly_selection",
    "paper": "https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf",
    "adaptation": "A_share_long_only_monthly_top5_not_original_30pct_six_month_overlapping_long_short_replication",
    "signal_source": "existing_derived_adjusted_daily_original_codes_not_canonical_alias_projection",
    "adjusted_source_is_independent_vendor_qfq": False,
    "high52_formula": "adjusted_close[D]/max(adjusted_high[D-251:D_inclusive])",
    "matched_momentum_formula": "adjusted_close[D-21]/adjusted_close[D-252]-1",
    "lookback_sessions": 252, "skip_sessions": 21,
    "common_required_history": "253_exact_calendar_active_positive_finite_adjusted_closes_and_252_exact_adjusted_highs_no_shrinking_or_imputation",
    "common_pool": "original_each_year_cohort_recomputed_adjusted_momentum_gt0_finite_positive_adjusted252_volatility_and_raw20_median_amount_ge20M_own_D_active_nonST_listing_supported",
    "common_pool_not_old_raw_momentum_candidates": True,
    "fixed_code_exclusions": list(FIXED_CODE_EXCLUSIONS),
    "identity_exclusion": "both_ambiguous_provider_codes_excluded_for_both_arms_without_cohort_edit_or_alias_merge",
    "volatility": "std_of252_adjusted_daily_returns_ddof1_times_sqrt252_requires253_active_closes",
    "minimum_raw_median_amount_cny": 20000000,
    "ranking": "descending_own_score_then_original_symbol_ascending_no_secondary_factor_or_threshold",
    "high52_ties": "retain_score1_ties_count_and_exact_symbol_order_no_tie_break_factor",
    "selection": "top5_only_uncapped_original_no_funding_filter_or_refill_fewer_names_leave_cash",
    "initial_cash": 100000, "holdings": 5, "gross_limit": .8, "single_limit": .2,
    "monthly_clock": "original_saved_annual_month_ends_exact_next_open",
    "unknown_batch": "NO_CHANGE_not_empty_FULL_TARGET",
    "study_tail": "original_end_D_and_six_tail_sessions_exit_only_last_NO_CHANGE_persistent_full_exits",
    "execution": "existing_advance_Ledger_raw_annual_prices_PriorVolumeReplayPolicy_T1_lot_limit_fee_rules",
    "capacity": "uncapped", "capital_policy": "original",
    "annual_accounts": "four_independent_fresh_cash_books_no_cross_year_equity_join_no_old_account_resume",
    "independent_holdout_claimed": False, "parameters_selected_by_return": False,
    "source_clock": "actual_registration_and_computation_clocks_not_backdated_historical_publications",
    "only_difference_between_paired_arms": "score_and_resulting_rank_other_pool_prices_costs_dates_targets_and_exit_rules_identical"}


def now():
    return datetime.now().astimezone().isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def pin(path):
    return {"path": str(Path(path).resolve()), "sha256": digest(path)}


def _symbol(code):
    if not isinstance(code, str) or len(code) != 9 or code[:3] not in {"sh.", "sz."} or not code[3:].isdigit():
        raise ValueError("Exact original SH/SZ provider codes required")
    return code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE")


def _separate(out, inputs):
    out = Path(out).resolve()
    for source in inputs:
        source = Path(source).resolve()
        if out == source or out in source.parents or source in out.parents:
            raise ValueError("New research outputs must not overlap original source/account directories")
    if out.exists():
        raise FileExistsError("Use a new independent method output directory")
    return out


def _adjusted(view, *, verify_panel):
    view = Path(view).resolve()
    value = read(view / "result.json")
    if (value.get("status") != "research_reconstruction_completed"
            or value.get("scope") != "every_pinned_provider_raw_row_no_strategy_universe_selection"
            or value.get("panel_file") != "adjusted_daily.parquet"
            or value.get("derived_research_levels") is not True
            or any(value.get(k) is not False for k in
                ("execution_admitted", "independent_prices_verified", "cash_share_actions_verified"))):
        raise ValueError("Explicit unchanged derived adjusted source required, never canonical aliases")
    panel = view / value["panel_file"]
    if verify_panel and digest(panel) != value["panel_sha256"]:
        raise ValueError("Adjusted source panel changed")
    source_plan = Path(value["raw_view"]) / "plan.json"
    plan = read(source_plan)
    calendar = plan["days"]
    if (calendar != sorted(set(calendar)) or len(calendar) != value["dates"]
            or calendar[-1] != value["cutoff"] or plan.get("execution_admitted") is not False):
        raise ValueError("Adjusted source exact session calendar changed")
    calendar_file = Path(plan["calendar_source"])
    observed_calendar_sha = digest(calendar_file) if calendar_file.is_file() else None
    pointer_observation = {"path":str(calendar_file.resolve()),
        "declared_sha256":plan["calendar_sha256"],"observed_sha256":observed_calendar_sha,
        "status": "original_external_pointer_matches_but_not_used" if observed_calendar_sha == plan["calendar_sha256"]
            else "changed_external_pointer_not_used" if observed_calendar_sha is not None else "missing_external_pointer_not_used",
        "used_for_session_generation":False}
    parquet = pq.ParquetFile(panel)
    required = {"date", "code", "close", "high", "volume", "amount", "tradestatus", "isST"}
    if not required <= set(parquet.schema_arrow.names) or parquet.metadata.num_row_groups != len(calendar):
        raise ValueError("Adjusted source must retain one exact original-code day per rowgroup")
    date_index = parquet.schema_arrow.names.index("date")
    for index, day in enumerate(calendar):
        stats = parquet.metadata.row_group(index).column(date_index).statistics
        if stats is None or not stats.has_min_max or stats.min != day or stats.max != day or stats.null_count != 0:
            raise ValueError("Adjusted footer and frozen dated rowgroup calendar differ")
    finished = datetime.fromisoformat(value["finished_at"])
    if finished.utcoffset() is None or finished > datetime.now().astimezone():
        raise ValueError("Adjusted source actual construction clock is invalid")
    return {"directory": str(view), "result": pin(view/"result.json"),
        "panel": {"path": str(panel), "sha256": value["panel_sha256"]},
        "raw_plan": pin(source_plan), "calendar_pointer_observation":pointer_observation,
        "calendar_basis":"pinned_raw_plan_inline_exact_days_each_bound_to_original_adjusted_rowgroup_footer",
        "calendar": calendar,
        "finished_at": value["finished_at"], "source_hashes": value["source_hashes"],
        "derived_research_levels": True, "independent_vendor_qfq": False,
        "rename_evidence_retained_but_not_applied": value.get("rename")}


def register(annual_inputs, adjusted_view, out, *, action_event_view=None, action_reference_view=None):
    """Freeze both years, both scores and common qualification before scoring."""
    if (action_event_view is None) != (action_reference_view is None):
        raise ValueError("A replacement action pair must be explicit together")
    if (WINDOW, SKIP) != (252, 21):
        raise ValueError("The matched control must retain original252/21 constants")
    folder = _separate(out, [*annual_inputs, adjusted_view])
    adjusted = _adjusted(adjusted_view, verify_panel=True)
    studies = {}
    for item in annual_inputs:
        inputs, manifest, plan, refs = capacity._checked_inputs(item)
        year = int(manifest["start"][:4])
        if year not in YEARS or str(year) in studies:
            raise ValueError("Exactly the two fixed independent2024/2025 annual sources required")
        config = StrategyConfig(**plan["config"]).validate()
        if config != annual._config(100000, 5):
            raise ValueError("Original100k five-slot config and costs cannot change")
        if manifest["source_days"] != [day for day in adjusted["calendar"] if manifest["start"] <= day <= manifest["through"]]:
            raise ValueError("Annual source days must equal the exact inline adjusted-calendar study interval")
        members = read(inputs/"cohort.json")["scope_members"]
        if len(members) != len(set(members)) or len(members) != manifest["cohort_count"]:
            raise ValueError("Original annual frozen cohort changed")
        pair = {"event": pin(Path(action_event_view)/"result.json") if action_event_view else plan["action_event_view"],
                "reference": pin(Path(action_reference_view)/"result.json") if action_reference_view else plan["action_reference_view"]}
        studies[str(year)] = {"annual_inputs": str(inputs), "source_pins": refs,
            "manifest": pin(inputs/"manifest.json"), "start": manifest["start"], "end": manifest["end"],
            "through": manifest["through"], "decision_days": manifest["decision_days"],
            "source_days": manifest["source_days"], "config": config.to_dict(),
            "original_cohort_count": len(members), "fixed_excluded_original_members":
                [s for s in members if s in FIXED_CODE_EXCLUSIONS], "action_pair": pair}
    if sorted(map(int, studies)) != list(YEARS):
        raise ValueError("Both2024 and2025 must be registered together, never selected by performance")
    record = {"schema": REGISTRATION_SCHEMA, "status": "registered_before_new_scores_and_replays",
        "registered_at": now(), "spec": deepcopy(SPEC), "implementation": pin(__file__),
        "studies": studies, "adjusted_source": adjusted, "execution_implementations":
            capacity._runtime_pins() + [pin(Path(__file__).parent.parent/name) for name in
                ("engine.py", "ledger.py", "contracts.py", "rules.py", "report.py")],
        "new_scores_computed": False, "new_accounts_created": False, "new_PnL_computed": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    save(folder/"registration.json", record)
    return record


def _load_registration(path, year):
    path = Path(path).resolve()
    record = read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("spec") != SPEC
            or record.get("implementation") != pin(__file__) or str(year) not in record["studies"]
            or list(sorted(map(int, record["studies"]))) != list(YEARS)
            or any(record.get(k) is not v for k,v in FLAGS.items())
            or datetime.fromisoformat(record["registered_at"]) > datetime.now().astimezone()):
        raise ValueError("Exact independently frozen paired method required")
    for ref in record["execution_implementations"]:
        if pin(ref["path"]) != ref:
            raise ValueError("Shared execution implementation changed after method registration")
    study = record["studies"][str(year)]
    inputs, manifest, plan, refs = capacity._checked_inputs(study["annual_inputs"])
    if refs != study["source_pins"] or plan["config"] != study["config"]:
        raise ValueError("Original annual source or config changed")
    for ref in study["action_pair"].values():
        if pin(ref["path"]) != ref:
            raise ValueError("Explicit fixed action pair changed")
    return record, study, inputs, manifest, plan


def score_panel(frame, members, calendar, decision_days, own_rows, qualification):
    """Pure same-pool paired features; no posterior bars or candidate survivor list."""
    if calendar != sorted(set(calendar)) or decision_days != sorted(set(decision_days)):
        raise ValueError("Exact chronological source and decision sessions required")
    if frame.duplicated(["date", "code"]).any():
        raise ValueError("Duplicate original-code adjusted observation")
    frame = frame.copy()
    frame["symbol"] = frame.code.map(_symbol)
    selected = [s for s in members if s not in FIXED_CODE_EXCLUSIONS]
    if not decision_days or calendar.index(decision_days[0]) < WINDOW:
        raise ValueError("The first decision requires253 exact source sessions")
    dates = calendar[calendar.index(decision_days[0])-WINDOW:calendar.index(decision_days[-1])+1]
    fields = {}
    for key in ("close", "high", "volume", "amount"):
        numeric = pd.to_numeric(frame[key], errors="coerce")
        fields[key] = frame.assign(value=numeric).pivot(index="date", columns="symbol", values="value").reindex(index=dates, columns=selected)
    status = frame.assign(value=frame.tradestatus.eq("1")).pivot(index="date", columns="symbol", values="value").reindex(index=dates, columns=selected).fillna(False)
    active = status & fields["volume"].gt(0) & np.isfinite(fields["volume"])
    close = fields["close"].where(active & fields["close"].gt(0) & np.isfinite(fields["close"]))
    high = fields["high"].where(active & fields["high"].gt(0) & np.isfinite(fields["high"]) & fields["high"].ge(close))
    momentum = close.shift(SKIP) / close.shift(WINDOW) - 1
    volatility = close.pct_change(fill_method=None).rolling(WINDOW, min_periods=WINDOW).std(ddof=1)*np.sqrt(252)
    maximum = high.rolling(WINDOW, min_periods=WINDOW).max()
    proximity = close / maximum
    median = fields["amount"].where(np.isfinite(fields["amount"]) & fields["amount"].ge(0)).rolling(20, min_periods=20).median()
    close_count = close.rolling(WINDOW+1, min_periods=WINDOW+1).count()
    index = frame.set_index(["date", "symbol"])
    frame_dates = set(frame.date)
    batches, audit = {arm:{} for arm in ARMS}, []
    for day in decision_days:
        excluded, eligible, source_unknown, finite_history = Counter(), [], [], 0
        source_dates = calendar[calendar.index(day)-WINDOW:calendar.index(day)+1]
        global_missing = [d for d in source_dates if d not in frame_dates]
        for symbol in selected:
            values = {"momentum": momentum.at[day,symbol], "volatility": volatility.at[day,symbol],
                "median_amount": median.at[day,symbol], "high52": proximity.at[day,symbol]}
            if close_count.at[day,symbol] != WINDOW+1 or not np.isfinite(list(values.values())).all():
                excluded["common_exact_window_price_activity_or_liquidity_unknown"] += 1; continue
            finite_history += 1
            if values["momentum"] <= 0 or values["volatility"] <= 0 or values["median_amount"] < SPEC["minimum_raw_median_amount_cny"]:
                excluded["fixed_common_positive_momentum_volatility_or_liquidity_condition"] += 1; continue
            if not 0 < values["high52"] <= 1:
                source_unknown.append({"symbol":symbol,"reason":"adjusted_close_exceeds_same_scale_window_high"}); continue
            raw = own_rows.get(day, {}).get(symbol)
            adjusted = index.loc[(day,symbol)]
            if raw is None:
                source_unknown.append({"symbol":symbol,"reason":"own_D_original_raw_row_missing"}); continue
            if any(str(raw[key]) != str(adjusted[key]) for key in ("tradestatus", "isST")):
                source_unknown.append({"symbol":symbol,"reason":"own_D_raw_and_adjusted_status_conflict"}); continue
            if any(Decimal(str(raw[key])) != Decimal(str(adjusted[key])) for key in ("volume", "amount")):
                source_unknown.append({"symbol":symbol,"reason":"own_D_raw_and_adjusted_unscaled_quantity_conflict"}); continue
            if str(raw["tradestatus"]) != "1" or str(raw["isST"]) != "0":
                excluded["own_D_raw_activity_or_ST_excluded"] += 1; continue
            proof = qualification.assess(symbol, day, decision_as_of=day+"T15:05:00+08:00")
            if proof["engine_block_reason"] is not None:
                excluded["own_D_listing_qualification_unsupported"] += 1; continue
            eligible.append({"symbol":symbol, **{k:float(v) for k,v in values.items()}})
        status = "incomplete" if global_missing or source_unknown or not finite_history else "complete"
        reason = "common_source_unknown_preserves_NO_CHANGE" if status == "incomplete" else "monthly_same_pool_ranking" if eligible else "known_empty_common_eligible_pool"
        common = {"day":day,"status":status,"reason":reason,"eligible_count":len(eligible),"finite_common_history_count":finite_history,
            "excluded_count":sum(excluded.values()),"exclusions":dict(excluded),
            "source_unknown":source_unknown,"globally_missing_exact_sessions":global_missing,
            "common_eligible_symbols":sorted(row["symbol"] for row in eligible),
            "same_pool_for_both_arms":True,"fixed_excluded_codes":list(FIXED_CODE_EXCLUSIONS)}
        scores_one = sum(row["high52"] == 1 for row in eligible)
        ties = Counter(row["high52"] for row in eligible)
        audit.append({**common,"high52_exact_score1_count":scores_one,
            "high52_tie_groups":[{"score":score,"count":count} for score,count in sorted(ties.items(),reverse=True) if count>1]})
        for arm in ARMS:
            scored = [{**row,"score":row["high52"] if arm == "high52" else row["momentum"]} for row in eligible]
            scored.sort(key=lambda row:(-row["score"],row["symbol"]))
            batches[arm][day] = {**common,"candidates":scored if status == "complete" else [],
                "arm":arm,"high52_exact_score1_count":scores_one}
    return batches, audit


def scores(registration, year, out):
    record, study, inputs, manifest, plan = _load_registration(registration, year)
    folder = _separate(out, [inputs, record["adjusted_source"]["directory"], Path(registration).parent])
    adjusted = _adjusted(record["adjusted_source"]["directory"], verify_panel=True)
    def fixed_source(value):
        result=deepcopy(value)
        observation=result["calendar_pointer_observation"]
        observation.pop("observed_sha256",None);observation.pop("status",None)
        return result
    if fixed_source(adjusted) != fixed_source(record["adjusted_source"]):
        raise ValueError("Frozen adjusted source identity or calendar changed")
    members = read(inputs/"cohort.json")["scope_members"]
    calendar, decision_days = adjusted["calendar"], study["decision_days"]
    begin = calendar.index(decision_days[0])-WINDOW
    if begin < 0:
        raise ValueError("Adjusted source lacks the registered first-window prefix")
    needed = calendar[begin:calendar.index(decision_days[-1])+1]
    source = pq.ParquetFile(adjusted["panel"]["path"])
    columns = ["date","code","close","high","volume","amount","tradestatus","isST"]
    original_codes = {("sh." if s.endswith(".SSE") else "sz.")+s[:6] for s in members if s not in FIXED_CODE_EXCLUSIONS}
    chunks = []
    for day in needed:
        rows = source.read_row_group(calendar.index(day), columns=columns).to_pandas()
        if rows.date.nunique() != 1 or rows.date.iloc[0] != day:
            raise ValueError("Adjusted exact-day rowgroup changed after registration")
        chunks.append(rows.loc[rows.code.isin(original_codes)])
    frame = pd.concat(chunks, ignore_index=True)
    raw = pq.read_table(inputs/"raw_daily.parquet", columns=["date","code","volume","amount","tradestatus","isST"],
        filters=[("date","in",decision_days)]).to_pandas()
    raw["symbol"] = raw.code.map(_symbol)
    if raw.duplicated(["date","symbol"]).any():
        raise ValueError("Original annual own-D raw rows duplicate")
    own_rows = {d: rows.set_index("symbol").to_dict("index") for d,rows in raw.groupby("date")}
    qualification = capacity._qualification(inputs, manifest, plan)
    batches, audit = score_panel(frame, members, calendar, decision_days, own_rows, qualification)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder/"batches.json",batches); save(folder/"pool_audit.json",audit)
    result = {"schema":SCORE_SCHEMA,"status":"completed_paired_monthly_scores_only","created_at":now(),
        "registration":pin(registration),"year":year,"spec":deepcopy(SPEC),"study":deepcopy(study),
        "batches":pin(folder/"batches.json"),"pool_audit":pin(folder/"pool_audit.json"),
        "source_rowgroups_read":len(needed),"source_first_day":needed[0],"source_last_day":needed[-1],
        "new_price_conversion":False,"annual_raw_or_cohort_rebuilt":False,"models_trained":False,
        "research_accounts_created":0,"engine_advanced":False,"scores_have_historical_publication":False,**FLAGS}
    save(folder/"result.json",result)
    return result


class PairedMonthlyFeed(annual.AnnualFeed):
    def __init__(self, execution, batches, config, end, final_day, qualification, arm):
        if arm not in ARMS:
            raise ValueError("Unknown fixed paired score arm")
        super().__init__(execution,batches,config,end,final_day,qualification,"uncapped","original")
        self.arm = arm
        self.close_candidate_policy = {"id":METHOD+":"+arm,"capacity":"uncapped","capital_policy":"original"}

    def _own_source(self, value):
        return replace(value, source="research:"+METHOD+":"+self.arm) if isinstance(value,SignalFrame) else value

    def signal_frame(self, day, config, context):
        return self._own_source(super().signal_frame(day,config,context))

    def close_signal_frame(self, day, config, context, signal_batch, book):
        return self._own_source(super().close_signal_frame(day,config,context,signal_batch,book))


def replay(registration, score_directory, arm, out):
    scored = read(Path(score_directory)/"result.json")
    year = scored["year"]
    record, study, inputs, manifest, plan = _load_registration(registration,year)
    if (arm not in ARMS or scored.get("schema") != SCORE_SCHEMA or scored.get("spec") != SPEC
            or scored.get("registration") != pin(registration) or scored.get("study") != study
            or any(pin(scored[key]["path"]) != scored[key] for key in ("batches","pool_audit"))):
        raise ValueError("Exact registered paired score sidecar required")
    folder = _separate(out,[inputs,score_directory,Path(registration).parent,record["adjusted_source"]["directory"]])
    all_batches = read(scored["batches"]["path"])
    if list(all_batches) != list(ARMS) or any(list(all_batches[k]) != study["decision_days"] for k in ARMS):
        raise ValueError("Both registered score arms must retain the complete same month-end prefix")
    for day in study["decision_days"]:
        a,b = all_batches[ARMS[0]][day],all_batches[ARMS[1]][day]
        if any(a[k] != b[k] for k in ("status","reason","common_eligible_symbols","source_unknown")):
            raise ValueError("Paired arms cannot select different eligible source pools")
    members = read(inputs/"cohort.json")["scope_members"]
    members = [s for s in members if s not in FIXED_CODE_EXCLUSIONS]
    days = manifest["source_days"]
    raw = pd.concat([pd.read_parquet(inputs/"context.parquet"),pd.read_parquet(inputs/"raw_daily.parquet")],ignore_index=True)
    raw = raw.loc[raw.symbol.isin(members)]
    calendar = [manifest["context_day"]]+days+[manifest["next_session_after_year"]]
    base = ExactSourceRawFeed(raw,calendar,members,context_anchor={"day":manifest["context_day"],"closes":{}})
    pair = study["action_pair"]
    accounting = load_action_accounting(Path(pair["event"]["path"]).parent,Path(pair["reference"]["path"]).parent,
        symbols=members,raw_days=[manifest["context_day"]]+days,replay_days=days,price_source="independent_baostock_raw")
    context = RunContext(METHOD+":"+str(year)+":"+arm,TradingMode.REPLAY)
    prior_volume = PriorVolumeReplayPolicy(context)
    execution = HistoricalExecutionPolicy(base,accounting,days,prior_volume_policy=prior_volume)
    config = StrategyConfig(**study["config"]).validate()
    feed_adapter = PairedMonthlyFeed(execution,all_batches[arm],config,study["end"],days[-1],
        capacity._qualification(inputs,manifest,plan),arm)
    feed = annual._AnnualQualifiedFeed(feed_adapter,feed_adapter.qualification)
    state,daily,blocking = new_state(config.initial_cash),[],None
    # Opening cash has no entitlement at registrations before its inception.
    for event in base.manifest.get("historical_price_adjustments",[]):
        if (event["kind"] in {"rights_issue","restructuring_holder_distribution"}
                and event.get("registration_date") and event["registration_date"]<days[0]):
            prefix = "rights:" if event["kind"] == "rights_issue" else ""
            state["entitlements"][prefix+event["symbol"]+":"+event["date"]] = 0
    folder.mkdir(parents=True,exist_ok=False)
    for index,day in enumerate(days):
        trial = deepcopy(state)
        try:
            row = advance(trial,feed,day,config,index,context=context,
                full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
        except (ValueError,KeyError,TypeError,PermissionError) as error:
            blocking = {"day":day,"reason":"shared_engine_or_source_unresolved",
                "error":type(error).__name__+": "+str(error),"session_not_committed":True}
            save(folder/"failed_trial.json",trial)
            break
        state,daily = trial,daily+[row]
    residual = {s:sum(lot["qty"] for lot in lots) for s,lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    exits = (state.get("full_exit_lifecycle") or {}).get("remaining_full_exits",{})
    complete = not blocking and len(daily)==len(days) and bool(state["fills"]) and not residual and state.get("pending_targets") is None and not exits
    result = {"schema":METHOD,"status":"completed_conditional_replay" if complete else "incomplete_or_no_fills",
        "created_at":now(),"registration":pin(registration),"score_sidecar":pin(Path(score_directory)/"result.json"),
        "year":year,"arm":arm,"spec":deepcopy(SPEC),"config":config.to_dict(),"context":context.to_dict(),
        "start":study["start"],"end":study["end"],"through":study["through"],
        "engine_sessions_committed":len(daily),"complete_PnL_available":bool(complete),
        "metrics":metrics(daily,config.initial_cash) if complete else None,
        "blocking_evidence":blocking,"residual_positions":residual,"residual_full_exits":exits,
        "pending_targets":state.get("pending_targets"),"parent_orders":len(state["orders"]),"fill_callbacks":len(state["fills"]),
        "action_accounting":accounting.provenance,"prior_volume_policy":prior_volume.method_id,
        "full_exit_lifecycle":RISK_COMPAT_FULL_EXIT_LIFECYCLE,
        "old_accounts_resumed_or_modified":False,"new_price_conversion":False,**FLAGS}
    for name,value in (("account",state),("daily",daily),("decisions",feed_adapter.decisions),
            ("orders",list(state["orders"].values())),("fills",list(state["fills"].values())),
            ("execution_checks",execution.checks),("result",result)):
        save(folder/(name+".json"),value)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    reg=commands.add_parser("register")
    reg.add_argument("--annual-inputs",type=Path,action="append",required=True)
    reg.add_argument("--adjusted-view",type=Path,required=True)
    reg.add_argument("--action-event-view",type=Path);reg.add_argument("--action-reference-view",type=Path)
    reg.add_argument("--out",type=Path,required=True)
    score=commands.add_parser("scores")
    score.add_argument("--registration",type=Path,required=True);score.add_argument("--year",type=int,choices=YEARS,required=True)
    score.add_argument("--out",type=Path,required=True)
    run=commands.add_parser("replay")
    run.add_argument("--registration",type=Path,required=True);run.add_argument("--scores",dest="score_directory",type=Path,required=True)
    run.add_argument("--arm",choices=ARMS,required=True);run.add_argument("--out",type=Path,required=True)
    args=vars(parser.parse_args(argv));command=args.pop("command")
    result={"register":register,"scores":scores,"replay":replay}[command](**args)
    print(json.dumps({k:result.get(k) for k in ("schema","status","registered_at","year","arm",
        "engine_sessions_committed","complete_PnL_available","metrics","blocking_evidence")},ensure_ascii=False,allow_nan=False))
    return 0
