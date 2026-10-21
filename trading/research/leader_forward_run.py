"""Incremental fixed-window V3 research replay using the sole Engine/Ledger.

One atomic research checkpoint carries the same twenty case accounts forward.
It is not a paper/live account worker and never opens a provider connection.
Only an explicit completed-source bundle can register the common future window.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import RunContext, TradingMode
from ..engine import ReplayRiskExitIntent, advance
from ..ledger import Ledger, new_state
from ..market_dataset import write_json
from ..report import metrics
from ..worker import lock
from .experiment import clean
from .leader_daily_replay import (
    EXIT_POLICIES, FAMILIES, POOLS, CommonCohortDecisionFeed, CommonCohortRawFeed,
    freeze_cohorts,
)
from .leader_daily_signals import _calendar_view
from .shortline_decision_v2 import fixed_exit_policy, verified_breadth
from .shortline_decision_v3 import FiveSessionExpiryPolicy, POLICY_ID
from .shortline_panel_replay import _config


SCHEMA = "leader_forward_shared_ledger_checkpoint_v1"
SOURCE_CONTRACT = "complete_dated_provider_close_candidate_v1"
WINDOW_SESSIONS, TAIL_SESSIONS = 40, 6
IPO_FEATURE_METHOD = "leader_listing_window_features_asof_decision_v1"
RECENT_SOURCE_METHOD = "fixed_recent_provider_history_prefix_v1"
RAW_BIND_COLUMNS = (
    "date", "symbol", "open", "high", "low", "close", "preclose_for_limits",
    "volume", "amount", "pct_chg", "is_st", "is_st_unknown", "source_row_valid",
    "provider_raw_observation", "preclose", "tradestatus", "isST", "adjustflag",
)


def _digest(value):
    return sha256(json.dumps(clean(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _protocol(path):
    path = Path(path).resolve()
    requested = path
    supplement = json.loads(path.read_text(encoding="utf-8"))
    history_supplement = None
    if supplement.get("schema") == "leader_next_window_recent_source_supplement_v1":
        history_supplement = supplement
        if (supplement.get("method_id") != RECENT_SOURCE_METHOD
                or any(supplement.get(key) is not False for key in
                       ("fixed_alpha_thresholds_changed", "costs_changed", "window_length_changed",
                        "holding_policy_changed", "execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Recent-source supplement must preserve fixed methods, costs and admission boundaries")
        path = (path.parent / supplement["parent_protocol"]).resolve()
        supplement = json.loads(path.read_text(encoding="utf-8"))
        if (_digest(supplement) != history_supplement.get("parent_protocol_sha256")
                or supplement.get("schema") != "leader_next_window_listing_feature_supplement_v1"):
            raise ValueError("Recent-source supplement requires the unchanged listing-feature parent")
    feature_path = path
    feature_supplement = None
    if supplement.get("schema") == "leader_next_window_listing_feature_supplement_v1":
        feature_supplement = supplement
        if (feature_supplement.get("method_id") != IPO_FEATURE_METHOD
                or feature_supplement.get("decision_cutoff_policy") != "each_decision_own_15:05_local_time_for_current_and_historical_qualification"
                or any(feature_supplement.get(key) is not False for key in
                       ("fixed_alpha_thresholds_changed", "costs_changed", "window_length_changed",
                        "holding_policy_changed", "execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Listing-feature supplement must preserve the fixed window, costs and research boundary")
        path = path.parent / feature_supplement["parent_protocol"]
        supplement = json.loads(path.read_text(encoding="utf-8"))
        if (_digest(supplement) != feature_supplement.get("parent_protocol_sha256")
                or supplement.get("schema") != "leader_next_window_ipo_qualification_supplement_v1"):
            raise ValueError("Listing-feature supplement requires its unchanged explicit IPO parent")
    ipo_supplement = None
    ipo_path = path
    if supplement.get("schema") == "leader_next_window_ipo_qualification_supplement_v1":
        ipo_supplement = supplement
        if (ipo_supplement.get("execution_admitted") is not False
                or ipo_supplement.get("fixed_alpha_thresholds_changed") is not False
                or ipo_supplement.get("first_five_session_matching_enabled") is not False):
            raise ValueError("IPO supplement cannot change alpha thresholds or enable first-five execution")
        path = path.parent / ipo_supplement["parent_protocol"]
        supplement = json.loads(path.read_text(encoding="utf-8"))
    parent_path = path.parent / supplement["parent_protocol"]
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    config = _config(100000, 5)
    rules = parent.get("common_rules", {})
    expected = {"initial_cash_cny": config.initial_cash, "holdings": config.holdings,
                "gross_limit": config.gross_limit, "single_limit": config.single_limit,
                "commission": config.commission, "minimum_commission_cny": config.minimum_commission,
                "sell_tax": config.sell_tax, "transfer_fee": config.transfer_fee,
                "slippage_bps": config.slippage_bps, "participation": config.participation,
                "max_holding_open_sessions": 5}
    if (supplement.get("schema") != "leader_next_window_independent_expiry_supplement_v1"
            or supplement.get("fixed_policy", {}).get("policy_id") != POLICY_ID
            or supplement.get("window", {}).get("length_open_sessions") != WINDOW_SESSIONS
            or parent.get("schema") != "leader_strategy_next_independent_window_protocol_v1"
            or any(rules.get(key) != value for key, value in expected.items())
            or any(payload.get("execution_admitted") is not False for payload in (parent, supplement))):
        raise ValueError("Forward run requires the unchanged frozen forty-session V3 protocol and costs")
    calendar_path = Path(parent["evaluation_window"]["calendar"]).resolve()
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    binding = {"supplement": str(path), "supplement_sha256": _digest(supplement),
            "parent": str(parent_path.resolve()), "parent_sha256": _digest(parent),
            "calendar": str(calendar_path), "calendar_sha256": _digest(payload),
            "config": config.to_dict(), "risk_exit_policy": POLICY_ID}
    if ipo_supplement is not None:
        from .ipo_qualification import load_ipo_qualification
        evidence = (ipo_path.parent / ipo_supplement["qualification_evidence"]).resolve()
        master = (ipo_path.parent / ipo_supplement["research_master_run"]).resolve()
        proof = load_ipo_qualification(evidence, calendar_path, master_run=master)
        binding.update(ipo_supplement=str(ipo_path), ipo_supplement_sha256=_digest(ipo_supplement),
                       ipo_qualification=proof.verification)
    if feature_supplement is not None:
        binding.update(feature_supplement=str(feature_path), feature_supplement_sha256=_digest(feature_supplement),
                       feature_qualification_method_id=IPO_FEATURE_METHOD)
    if history_supplement is not None:
        from .leader_recent_source_history import read_recent_source_view
        view_path = (requested.parent / history_supplement["source_history_view"]).resolve()
        view = read_recent_source_view(view_path)
        if (sha256(view_path.read_bytes()).hexdigest() != history_supplement.get("source_history_view_sha256")
                or view.get("view_file") != str(view_path)
                or view.get("method_id") != RECENT_SOURCE_METHOD):
            raise ValueError("Recent-source view differs from its frozen source-history binding")
        specified = datetime.fromisoformat(history_supplement["created_at"])
        if specified.tzinfo is None or specified > datetime.now().astimezone():
            raise ValueError("Recent-source specification time must be aware and cannot be in the future")
        binding.update(history_source_supplement=str(requested),
                       history_source_supplement_sha256=_digest(history_supplement), history_source_view=view,
                       history_source_specified_at=specified.isoformat())
    return binding, payload, config


def _ipo_proof(binding):
    saved = binding.get("ipo_qualification")
    if saved is None:
        return None
    from .ipo_qualification import load_ipo_qualification
    return load_ipo_qualification(saved["evidence_file"], saved["calendar"],
                                  master_run=(saved.get("research_master") or {}).get("run"))


def _check_feature_binding(bundle, binding):
    """Never continue old accounts with silently changed listing features."""
    expected = binding.get("feature_qualification_method_id")
    actual = bundle.get("feature_qualification")
    if expected is None:
        if actual is not None:
            raise ValueError("Qualified listing features require an explicit feature supplement")
        return
    if (not isinstance(actual, dict) or actual.get("method_id") != expected
            or actual.get("verification") != binding.get("ipo_qualification")
            or actual.get("decision_cutoff_policy") != "each_decision_own_15:05_local_time_for_current_and_historical_qualification"):
        raise ValueError("Source listing-feature method or qualification proof differs from the frozen protocol")
    for batches in bundle["batches"].values():
        for batch in batches.values():
            view = batch.get("feature_qualification") or {}
            if (view.get("method_id") != expected
                    or not isinstance(view.get("unknown_symbols"), list)
                    or view.get("qualification_complete") is not (not view["unknown_symbols"])
                    or (batch.get("status") == "complete" and not view["qualification_complete"])):
                raise ValueError("Listing-feature batch lost unknown membership or its method binding")


def _check_source_binding(bundle, binding):
    """Bind a newly derived price-history view without changing old accounts."""
    expected, actual = binding.get("history_source_view"), bundle.get("recent_source_history")
    if expected is None:
        if actual is not None or any(batch.get("recent_source_history") is not None
                for batches in bundle["batches"].values() for batch in batches.values()):
            raise ValueError("Recent-source history requires an explicit source-history supplement")
        return
    if (not isinstance(actual, dict) or actual != expected
            or actual.get("method_id") != RECENT_SOURCE_METHOD
            or sha256(Path(actual["view_file"]).read_bytes()).hexdigest() != actual.get("view_sha256")):
        raise ValueError("Recent-source history differs from the frozen source-history view")
    keys = ("method_id", "shared_view_id", "view_file", "view_sha256", "base_history_end", "available_at",
            "original_history_manifest_sha256")
    frozen = {proof["day"]: proof for proof in actual["source_proofs"]}
    packages = {item["day"]: item for item in bundle.get("source_packages", [])}
    for batches in bundle["batches"].values():
        for day, batch in batches.items():
            view = batch.get("recent_source_history") or {}
            used, proofs = view.get("source_days_used"), view.get("source_proofs")
            if (any(view.get(key) != actual.get(key) for key in keys)
                    or batch.get("day") != day or not isinstance(used, list) or not used
                    or used != sorted(set(used)) or any(date.fromisoformat(value).isoformat() != value or value > day for value in used)
                    or not isinstance(proofs, list) or [proof.get("day") for proof in proofs] != used
                    or not {value for value in frozen if value <= day}.issubset(used)):
                raise ValueError("Recent-source batch has a mixed view or future/missing historical prefix")
            for proof in proofs:
                source_day = proof["day"]
                if (proof.get("read_only_verified") is not True or proof.get("source_inventory_complete") is not True
                        or proof.get("full_dated_roster_inventory_complete") is not True
                        or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"
                        or proof.get("calendar_sha256") != actual["calendar_sha256"]):
                    raise ValueError("Recent-source batch prefix lacks complete same-calendar source proofs")
                reference = frozen.get(source_day)
                if reference is not None and any(proof.get(key) != reference.get(key) for key in
                                                  ("source_folder", "raw_daily_sha256", "roster_raw_sha256", "calendar_sha256")):
                    raise ValueError("Recent-source batch mixes a different historical source prefix")
                package = packages.get(source_day)
                if reference is None and (package is None or proof.get("source_folder") != package.get("folder")
                        or proof.get("raw_daily_sha256") != package.get("raw_daily_sha256")):
                    raise ValueError("Recent-source batch proof differs from the exact current source package")


def _history_available_for_decision(binding, day):
    history = binding.get("history_source_view")
    if history is not None:
        available = datetime.fromisoformat(history["available_at"])
        specified = datetime.fromisoformat(binding["history_source_specified_at"])
        if specified.tzinfo is None:
            raise ValueError("Recent-source specification cutoff requires an explicit timezone")
        available = max(available, specified)
        if available.tzinfo is None or available > datetime.fromisoformat(day + "T15:05:00+08:00"):
            raise ValueError("Recent-source history was unavailable at the decision cutoff")


def _calendar(payload, start):
    end = f"{payload['year']}-12-31"
    sessions, _, status = _calendar_view(payload, start, end, [])
    if status != "pinned_static_exchange_calendar" or len(sessions) < WINDOW_SESSIONS + TAIL_SESSIONS + 1:
        raise ValueError("Pinned calendar cannot cover the common forty-session window and fixed execution tail")
    return sessions[:WINDOW_SESSIONS + TAIL_SESSIONS + 1]


def _validate_bundle(bundle):
    if (bundle.get("input_source_contract") != SOURCE_CONTRACT
            or bundle.get("candidate_panel_only") is not True
            or bundle.get("strategy_validated") is not False
            or bundle.get("selection_enabled") is not False
            or bundle.get("execution_admitted") is not False
            or bundle.get("model_refit") is not False
            or bundle.get("future_labels_used_for_score") is not False):
        raise ValueError("Expected an unadmitted source-specific complete-close candidate bundle")
    days = bundle.get("source_days_complete", [])
    if not days or days != sorted(set(days)) or any(date.fromisoformat(day).isoformat() != day for day in days):
        raise ValueError("Source complete days must be explicit unique canonical sessions")
    raw = bundle["raw_bars"]
    if not isinstance(raw, pd.DataFrame) or raw.empty or raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Forward source needs unique actual dated raw observations")
    if not {"date", "symbol", "preclose_for_limits", "is_st"}.issubset(raw.columns):
        raise ValueError("Forward raw source schema is incomplete")
    return raw, days


class ForwardRawFeed(CommonCohortRawFeed):
    """Keep legacy physical bars; overlay only explicitly identified provider rows."""
    def __init__(self, raw, calendar, symbols, *, ipo_qualification=None, corporate_action_view=None):
        super().__init__(raw, calendar, symbols)
        self.ipo_qualification = ipo_qualification
        for symbol in symbols:
            record = ipo_qualification.records.get(symbol) if ipo_qualification is not None else None
            self.instruments[symbol].update(ipoDate=record["ipo_date"] if record else None,
                ipoDate_basis=record["source_basis"] if record else "unknown_not_first_observed")
        self.manifest.update(input_source_contract=SOURCE_CONTRACT,
                             actual_provider_status_retained=True, nullable_activity_imputed=False,
                             listing_date_basis="explicit_dated_research_qualification_or_unknown",
                             ipo_qualification=deepcopy(ipo_qualification.verification) if ipo_qualification else None)
        provider = (raw.loc[raw.provider_raw_observation.eq(True)]
                    if "provider_raw_observation" in raw else raw.iloc[:0])
        self._provider_rows = {}
        for row in provider.loc[provider.symbol.isin(symbols) & provider.date.isin(calendar)].to_dict("records"):
            values = dict(row)
            for field in ("tradestatus", "isST"):
                value = values.get(field)
                values[field] = int(value) if pd.notna(value) and value in (0, 1, "0", "1") else None
            # Nullable suspension fields remain NaN/None, never inferred zeros.
            values["preclose"] = row.get("preclose")
            values["adjustflag"] = str(row.get("adjustflag"))
            self._provider_rows.setdefault(row["date"], {})[row["symbol"]] = values
        original = self.rows
        class ActualRows:
            def get(inner, day, default=None):
                values = original.get(day, {})
                if not values and day not in self._provider_rows:
                    return default
                return {**values, **self._provider_rows.get(day, {})}
        self.rows = ActualRows()
        if corporate_action_view is not None:
            from .research_corporate_action_view import ResearchCorporateActionView
            if not isinstance(corporate_action_view, ResearchCorporateActionView):
                raise TypeError("Corporate-action opt-in requires its loaded typed research view")
            self.research_corporate_action_view = corporate_action_view
            self.actions = [action for action in corporate_action_view.actions if action["symbol"] in self.symbols]
            self.manifest["research_corporate_action_view"] = corporate_action_view.binding

    def validate_session(self, state, day):
        view = getattr(self, "research_corporate_action_view", None)
        if view is not None:
            if day not in self.calendar:
                raise ValueError("Corporate-action processing requires an exact feed session")
            view.validate_session(state, day)

    def validate_preopen_targets(self, day, targets):
        view = getattr(self, "research_corporate_action_view", None)
        if view is not None:
            if day not in self.calendar:
                raise ValueError("Corporate-action processing requires an exact feed session")
            view.validate_preopen_targets(day, targets)

    def research_listing_limit_reason(self, symbol, day, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Forward IPO metadata overlay is replay-only")
        if self.ipo_qualification is None:
            return "missing_listing_date"
        return self.ipo_qualification.assess(symbol, day)["engine_block_reason"]


def _scoped(bundle, symbols, end):
    from .provider_close_batches import scope_provider_batches
    batches, coverage = scope_provider_batches(bundle, symbols, decision_end=end)
    for family in FAMILIES:
        for day, batch in batches[family].items():
            if (any(row["symbol"] not in symbols for row in batch.get("candidates", []))
                    or len(batch.get("candidates", [])) > 5):
                raise ValueError("Scoped source altered frozen cohort or fixed five-candidate cap")
    return batches, coverage


def _prefix(raw, all_scoped, through):
    frame = raw.loc[raw.date.le(through), [field for field in RAW_BIND_COLUMNS if field in raw]].copy()
    frame = frame.sort_values(["date", "symbol"], kind="stable")
    numeric = {"open", "high", "low", "close", "preclose_for_limits", "volume", "amount", "pct_chg", "preclose", "tradestatus", "isST"}
    for field in frame:
        frame[field] = (pd.to_numeric(frame[field], errors="raise").astype("float64") if field in numeric
                        else frame[field].astype("string").fillna("<NULL>"))
    raw_hash = sha256(json.dumps(list(frame.columns)).encode())
    raw_hash.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    batches = {pool: {family: {day: value for day, value in source[family].items() if day <= through}
                      for family in FAMILIES} for pool, source in all_scoped.items()}
    return {"through": through, "raw_sha256": raw_hash.hexdigest(),
            "batches_sha256": _digest(batches)}


def _case_key(pool, family, exit_policy):
    return ":".join((pool, family, exit_policy))


def _summary(head, *, newly_completed=0):
    calendar = head["calendar"]
    rows = []
    for case in head["cases"].values():
        state, daily = case["state"], case["daily"]
        in_window = daily[:WINDOW_SESSIONS]
        complete = sum(row.get("batch_status") == "complete" for row in in_window)
        no_change = sum(row.get("no_decision_reason") is not None for row in in_window)
        available = (complete > 0 and case.get("verified_breadth_seen", False)) if case["exit_policy"] == EXIT_POLICIES[1] else complete > 0
        finished = len(daily) >= WINDOW_SESSIONS + TAIL_SESSIONS
        status = ("blocked_source_or_engine" if case.get("error") else
                  "unavailable_verified_full_market_breadth" if case["exit_policy"] == EXIT_POLICIES[1] and not case.get("verified_breadth_seen") else
                  "unavailable_complete_cohort_batches" if not complete else
                  "complete_fixed_window_research_only" if finished else "accumulating_fixed_window")
        rows.append({"family": case["family"], "pool": case["pool"], "exit_policy": case["exit_policy"],
                     "status": status, "common_window_start": head["start"], "common_window_end": calendar[WINDOW_SESSIONS - 1],
                     "completed_sessions": len(daily), "complete_decision_batches": complete, "no_change_days": no_change,
                     "last_completed_session": state["day"], "orders": len(state["orders"]), "fills": len(state["fills"]),
                     "open_positions": {symbol: sum(lot["qty"] for lot in lots) for symbol, lots in state["positions"].items() if lots},
                     "metrics": metrics(in_window, 100000) if available and finished and not case.get("error") else None,
                     "error": case.get("error")})
    blocked = any(case.get("error") for case in head["cases"].values())
    status = ("advanced_research_only_with_blocked_cases" if blocked and newly_completed else
              "blocked_research_only" if blocked else "advanced_research_only" if newly_completed else "no_new_session")
    return {"schema": "leader_forward_run_result_v1", "status": status,
            "start": head["start"], "end": calendar[WINDOW_SESSIONS - 1], "case_count": len(rows),
            "cohort": {"anchor": head["cohort"]["anchor"], "selection": head["cohort"]["selection"],
                       "counts": {pool: len(symbols) for pool, symbols in head["cohort"]["pools"].items()},
                       "future_survival_used": False, "return_based_cohort_selection": False},
            "newly_completed_case_sessions": newly_completed, "account_reset": False, "cases": rows,
            "evaluation_mode": "fixed_method_forward_historical_REPLAY_not_published_paper_or_live_forecast",
            "actual_signal_publication_verified": False, "source_packages": head.get("source_packages", []),
            "partial_window_returns_reported": False, "candidate_panel_only": True,
            "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
            "model_refit": False, "future_labels_used_for_score": False, "broker_interacted": False,
            "persistent_user_ledger_mutated": False}


def run_bundle(bundle, run_dir, *, protocol_path, through=None):
    """Create once or continue the same saved twenty-case research Ledger states.

    Terminal source closes await their actual successor full-close package.
    Successful case/session trials are atomically saved; a failed trial never
    changes its saved cash, positions, orders, fills or session cursor.
    """
    raw, source_days = _validate_bundle(bundle)
    binding, calendar_payload, config = _protocol(protocol_path)
    _check_feature_binding(bundle, binding)
    _check_source_binding(bundle, binding)
    ipo_qualification = None
    if binding.get("ipo_qualification") is not None:
        from .ipo_qualification import load_ipo_qualification
        saved = binding["ipo_qualification"]
        ipo_qualification = load_ipo_qualification(saved["evidence_file"], saved["calendar"],
                                                   master_run=saved["research_master"]["run"])
    cutoff = through or source_days[-1]
    if date.fromisoformat(cutoff).isoformat() != cutoff:
        raise ValueError("Forward cutoff must be a canonical declared session")
    if bundle.get("calendar_payload") != calendar_payload:
        raise ValueError("Source and frozen protocol exchange calendars differ")
    for decision_day in source_days:
        if "2026-10-15" <= decision_day <= cutoff:
            _history_available_for_decision(binding, decision_day)
    run_dir = Path(run_dir).resolve()
    if not (run_dir / "head.json").exists():
        eligible = [day for day in source_days if day >= "2026-10-15"]
        if not eligible:
            return {"schema": "leader_forward_run_result_v1", "status": "waiting_for_first_complete_unexamined_source_close",
                    "case_count": 0, "account_reset": False, "execution_admitted": False}
        first_day = eligible[0]
        successor = _calendar(calendar_payload, first_day)[1]
        if successor not in source_days:
            # A source-close publication can exist independently. Do not create
            # twenty research books merely to wait for their first execution raw.
            return {"schema": "leader_forward_run_result_v1", "status": "waiting_for_exact_successor_complete_source_close",
                    "first_decision_day": first_day, "required_successor_day": successor,
                    "case_count": 0, "research_checkpoint_created": False, "account_created": False,
                    "orders_generated": False, "account_reset": False, "execution_admitted": False}
    run_dir.mkdir(parents=True, exist_ok=True)
    with lock(run_dir / "runner.lock"):
        path = run_dir / "head.json"
        if path.exists():
            head = json.loads(path.read_text(encoding="utf-8"))
            if head.get("schema") != SCHEMA or head.get("binding") != binding or head.get("execution_admitted") is not False:
                raise ValueError("Saved forward research protocol, costs or admission boundary changed")
            start = head["start"]
            if head["calendar"] != _calendar(calendar_payload, start):
                raise ValueError("Saved common forward calendar changed")
            cohort = head["cohort"]
            if cohort != freeze_cohorts(raw, start):
                raise ValueError("Frozen pre-window cohorts changed; no survivor-based continuation")
        else:
            eligible = [day for day in source_days if day >= "2026-10-15"]
            if not eligible:
                return {"schema": "leader_forward_run_result_v1", "status": "waiting_for_first_complete_unexamined_source_close",
                        "case_count": 0, "account_reset": False, "execution_admitted": False}
            start = eligible[0]
            calendar = _calendar(calendar_payload, start)
            # Same fixed selection recipe, newly dated pre-window anchor. This
            # is not the old Jan diagnostic's 2025-12-31 cohort or a PnL choice.
            cohort = freeze_cohorts(raw, start)
            cases = {}
            for pool in POOLS:
                for family in FAMILIES:
                    for exit_policy in EXIT_POLICIES:
                        key = _case_key(pool, family, exit_policy)
                        cases[key] = {"family": family, "pool": pool, "exit_policy": exit_policy,
                                      "state": new_state(config.initial_cash), "daily": [], "verified_breadth_seen": False}
            head = {"schema": SCHEMA, "registered_at": datetime.now().astimezone().isoformat(),
                    "binding": binding, "start": start, "calendar": calendar, "cohort": cohort,
                    "cases": cases, "prefix": None, "source_packages": deepcopy(bundle.get("source_packages", [])),
                    "actual_signal_publication_verified": False,
                    "candidate_panel_only": True, "execution_admitted": False}
        calendar = head["calendar"]
        if start not in source_days:
            raise ValueError("Previously registered complete source day disappeared")
        all_scoped = {pool: _scoped(bundle, symbols, calendar[WINDOW_SESSIONS - 1])[0]
                      for pool, symbols in cohort["pools"].items()}
        if head["prefix"] is not None and _prefix(raw, all_scoped, head["prefix"]["through"]) != head["prefix"]:
            raise ValueError("Previously consumed raw/batch prefix changed; repair separately, do not rewrite forward results")
        old_packages = {item["day"]: item for item in head.get("source_packages", [])}
        new_packages = {item["day"]: item for item in bundle.get("source_packages", [])}
        if (len(new_packages) != len(bundle.get("source_packages", []))
                or any(new_packages.get(day) != source for day, source in old_packages.items())):
            raise ValueError("Previously observed complete source provenance/timestamps changed")
        head["source_packages"] = [deepcopy(new_packages[day]) for day in sorted(new_packages)]
        expected_cases = {_case_key(pool, family, exit_policy): (pool, family, exit_policy)
                          for pool in POOLS for family in FAMILIES for exit_policy in EXIT_POLICIES}
        if set(head["cases"]) != set(expected_cases):
            raise ValueError("Saved fixed twenty-case suite changed")
        for key, case in head["cases"].items():
            state, daily = case["state"], case["daily"]
            if ((case["pool"], case["family"], case["exit_policy"]) != expected_cases[key]
                    or state.get("broker_binding") or len(daily) > WINDOW_SESSIONS + TAIL_SESSIONS
                    or [row["date"] for row in daily] != calendar[:len(daily)]
                    or state["day"] != (calendar[len(daily) - 1] if daily else None)):
                raise ValueError("Saved account/daily/common-calendar prefix mismatch")
            if daily and round(daily[-1]["equity"] * 100) != Ledger(state, config).equity():
                raise ValueError("Saved account equity differs from the sole Ledger")
            context = RunContext(f"leader-forward:{run_dir.name}:{key}", TradingMode.REPLAY)
            if daily and state.get("replay_risk_exit_policy") != {
                    "schema": "replay_risk_exit_policy_v1", "policy_id": POLICY_ID, "context": context.to_dict()}:
                raise ValueError("Saved independent expiry policy/account context changed")
            if state.get("pending_risk_exits") is not None:
                pending = ReplayRiskExitIntent.from_dict(state["pending_risk_exits"])
                if (pending.context != context or pending.decided_on != state["day"]
                        or pending.execution_on != calendar[len(daily)]):
                    raise ValueError("Saved risk-exit context or exact next session changed")
        if any(case["state"]["day"] is not None and cutoff < case["state"]["day"] for case in head["cases"].values()):
            raise ValueError("Forward cutoff cannot move behind a processed account session")
        if not path.exists():
            # The first exact successor is present. Freeze the supplied source
            # prefix before running any shared-Ledger session trials.
            head["prefix"] = _prefix(raw, all_scoped, source_days[-1])
            write_json(path, head)
        bases = {pool: ForwardRawFeed(raw, calendar, symbols, ipo_qualification=ipo_qualification)
                 for pool, symbols in cohort["pools"].items()}
        completed = 0
        for index, day in enumerate(calendar[:WINDOW_SESSIONS + TAIL_SESSIONS]):
            if day > cutoff or calendar[index + 1] not in source_days:
                break
            if not any(len(case["daily"]) == index for case in head["cases"].values()):
                continue
            _history_available_for_decision(binding, day)
            attempt_prefix = _prefix(raw, all_scoped, calendar[index + 1])
            session_prefix = (head["prefix"] if head["prefix"]["through"] > calendar[index + 1]
                              else attempt_prefix)
            changed = False
            for key, case in head["cases"].items():
                if len(case["daily"]) > index:
                    continue
                if len(case["daily"]) != index:
                    continue  # A blocked case keeps its original cursor, never a shifted first day.
                if case.get("failed_attempt") == {"day": day, "source_prefix": attempt_prefix}:
                    continue  # The same terminal input is not an invitation to resubmit it.
                trial = deepcopy(case["state"])
                feed = CommonCohortDecisionFeed(
                    bases[case["pool"]],
                    all_scoped[case["pool"]][case["family"]], trial,
                    market_defense=case["exit_policy"] == EXIT_POLICIES[1])
                target = fixed_exit_policy(feed, trial, config)
                context = RunContext(f"leader-forward:{run_dir.name}:{key}", TradingMode.REPLAY)
                try:
                    row = advance(trial, feed, day, config, index, context=context,
                                  target_policy=target, replay_risk_exit_policy=FiveSessionExpiryPolicy())
                except (ValueError, KeyError) as error:
                    case["error"] = f"{type(error).__name__}: {error}"
                    case["failed_attempt"] = {"day": day, "source_prefix": attempt_prefix}
                    changed = True
                    continue
                batch = all_scoped[case["pool"]][case["family"]].get(day, {})
                no_decision = next((event["reason"] for event in row["events"] if event["kind"] == "no_decision"), None)
                row.update(batch_status=batch.get("status", "missing"), no_decision_reason=no_decision)
                case.update(state=trial, error=None,
                            verified_breadth_seen=case["verified_breadth_seen"] or (
                                batch.get("status") == "complete" and verified_breadth(batch) is not None))
                case.pop("failed_attempt", None)
                case["daily"].append(row)
                completed += 1
                head["prefix"] = session_prefix
                changed = True
            if changed:
                # No broker side effects occur in trials. One atomic session
                # commit safely resumes all same-case states after a crash.
                write_json(path, head)
        return _summary(head, newly_completed=completed)


def _pending_source_packages(packages):
    """Recognise only explicit live/budget checkpoints; never mask failed input."""
    from .baostock_full_close import SCHEMA as source_schema, load_complete, roster_symbols
    from .baostock_all_stock_probe import load_cached_day
    from .baostock_daily_slice import digest
    pending, others = [], []
    for folder in packages:
        folder = Path(folder).resolve()
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
        waiting = (receipt.get("status") in {"running", "partial_budget_checkpoint_not_full_close"}
                   and receipt.get("full_dated_roster_inventory_complete") is False)
        if waiting:
            symbols, absent = plan.get("symbols", []), receipt.get("pending_symbols", [])
            if (plan.get("schema") != source_schema or receipt.get("schema") != source_schema
                    or plan.get("day") != receipt.get("day") or not symbols or not absent
                    or len(symbols) != len(set(symbols)) or len(absent) != len(set(absent))
                    or not set(absent).issubset(symbols)
                    or receipt.get("completed_symbols", -1) + len(absent) != len(symbols)
                    or plan.get("ordinary_symbols") != len(symbols)
                    or receipt.get("ordinary_symbols") != len(symbols)
                    or any(payload.get(key) is not False for payload in (plan, receipt)
                           for key in ("source_verified", "pit_verified", "strategy_validated", "selection_enabled",
                                       "execution_admitted", "stockdb_modified", "ledger_mutated"))):
                raise ValueError("Pending source receipt does not match an explicit incomplete full-roster job")
            _, roster, pin = load_cached_day(folder / "roster", receipt["day"])
            if (pin["verified_current_raw_sha256"] != plan.get("roster_raw_sha256")
                    or roster_symbols(roster).symbol.tolist() != symbols):
                raise ValueError("Pending source's fixed dated roster/hash changed")
            if digest(folder / "raw_daily.csv") != receipt.get("raw_daily_sha256"):
                raise ValueError("Pending source aggregate raw hash changed")
            pending.append({"folder": str(folder), "day": receipt["day"], "status": receipt["status"],
                            "completed_symbols": receipt["completed_symbols"], "ordinary_symbols": len(symbols),
                            "pending_symbols_count": len(absent), "created_at": receipt.get("created_at"),
                            "updated_at": receipt.get("updated_at"),
                            "full_dated_roster_inventory_complete": False})
        else:
            if (receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
                    or receipt.get("full_dated_roster_inventory_complete") is not True
                    or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")):
                raise ValueError("Source is neither an explicit pending job nor a complete successful roster inventory")
            others.append(folder)
    if pending:
        # A second failed/corrupted purportedly complete package cannot hide
        # behind an otherwise legitimate in-progress package.
        for folder in others:
            load_complete(folder)
    return pending


def source_status_only(packages):
    """Read source evidence without registering/advancing a research account."""
    from .baostock_full_close import SCHEMA as source_schema, load_complete
    from .baostock_bulk_close import SCHEMA as bulk_schema
    records = []
    for folder in packages:
        folder = Path(folder).resolve()
        receipt_path = folder / "receipt.json"
        try:
            plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (plan.get("schema") not in {source_schema, bulk_schema} or receipt.get("schema") != plan.get("schema")
                    or plan.get("day") != receipt.get("day")):
                raise ValueError("Source plan/receipt identity changed")
            record = {"folder": str(folder), "receipt": str(receipt_path), "day": receipt.get("day"),
                      "source_status": receipt.get("status"), "completed_symbols": receipt.get("completed_symbols"),
                      "logical_price_queries": receipt.get("logical_price_queries"),
                      "ordinary_symbols": receipt.get("ordinary_symbols"),
                      "pending_symbols_count": len(receipt.get("pending_symbols", [])),
                      "full_dated_roster_inventory_complete": receipt.get("full_dated_roster_inventory_complete"),
                      "created_at": receipt.get("created_at"), "updated_at": receipt.get("updated_at"),
                      "run_started_at": receipt.get("run_started_at")}
            if plan["schema"] == bulk_schema:
                # A typed bulk package is an immutable reconciled complete
                # source. It has no resumable running/partial interpretation.
                load_complete(folder)
                record.update(status="complete_source_close_read_only_verified", source_schema=bulk_schema)
            elif str(receipt.get("status", "")).startswith("failed"):
                record.update(status="failed_source_close", error_type=receipt.get("error_type"),
                              error=receipt.get("error"), preserved_failed_batches=receipt.get("preserved_failed_batches", []))
            else:
                pending = _pending_source_packages([folder])
                if pending:
                    record.update(status="waiting_for_complete_source_close")
                else:
                    load_complete(folder)
                    record.update(status="complete_source_close_read_only_verified")
            records.append(record)
        except (OSError, ValueError, KeyError) as error:
            records.append({"folder": str(folder), "receipt": str(receipt_path), "status": "failed_source_status_read",
                            "error_type": type(error).__name__, "error": str(error)})
    failed = any(record["status"].startswith("failed") for record in records)
    waiting = any(record["status"] == "waiting_for_complete_source_close" for record in records)
    return {"schema": "leader_forward_source_status_v1", "checked_at": datetime.now().astimezone().isoformat(),
            "status": "failed_source_close" if failed else "waiting_for_complete_source_close" if waiting else "complete_source_closes_read_only_verified",
            "source_packages": records, "case_count": 0, "research_checkpoint_created": False,
            "orders_generated": False, "provider_contacted": False, "execution_admitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path, help="Source consumer bundle JSON with raw_bars_file parquet")
    source.add_argument("--package", type=Path, action="append", help="Every complete dated full-close source package, repeat in chronological scope")
    parser.add_argument("--history-snapshot", type=Path)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--run-dir", type=Path, help="External research checkpoint directory")
    parser.add_argument("--protocol", type=Path, help="Frozen V3 future-window supplement")
    parser.add_argument("--status-only", action="store_true", help="Read source readiness/failure only; never create an account")
    parser.add_argument("--status-output", type=Path, help="Optional durable source-status receipt, only with --status-only")
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    if args.status_only:
        if args.package is None:
            parser.error("--status-only requires explicit source --package folders")
        result = source_status_only(args.package)
        if args.status_output is not None:
            write_json(args.status_output, result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 2 if result["status"].startswith("failed") else 0
    if args.status_output is not None:
        parser.error("--status-output requires --status-only")
    if args.run_dir is None or args.protocol is None:
        parser.error("Research continuation requires --run-dir and --protocol")
    if args.bundle is not None:
        if args.history_snapshot is not None or args.calendar is not None:
            parser.error("--bundle already supplies its history/calendar scope")
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        raw_path = (args.bundle.parent / bundle.pop("raw_bars_file")).resolve()
        bundle["raw_bars"] = pd.read_parquet(raw_path)
    else:
        if args.history_snapshot is None:
            parser.error("Complete packages require --history-snapshot")
        pending = _pending_source_packages(args.package)
        if pending:
            print(json.dumps({"schema": "leader_forward_run_result_v1", "status": "waiting_for_complete_source_close",
                              "pending_source_packages": pending, "case_count": 0, "orders_generated": False,
                              "research_checkpoint_created": False, "execution_admitted": False},
                             ensure_ascii=False, allow_nan=False))
            return 0
        from .provider_close_batches import build_provider_close_bundle
        source_binding = _protocol(args.protocol)[0]
        calendar_path = args.calendar or Path(source_binding["calendar"])
        options = ({"ipo_qualification": _ipo_proof(source_binding)}
                   if source_binding.get("feature_qualification_method_id") else {})
        if source_binding.get("history_source_view") is not None:
            options["recent_source_view"] = source_binding["history_source_view"]["view_file"]
        bundle = build_provider_close_bundle(args.package, args.history_snapshot, calendar_path, **options)
    result = run_bundle(bundle, args.run_dir, protocol_path=args.protocol, through=args.through)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 2 if any(case.get("error") for case in result.get("cases", [])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
