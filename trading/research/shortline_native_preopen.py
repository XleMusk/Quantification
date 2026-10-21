"""Publish an independent native-score research policy before the next open.

This converts saved probabilities into an explicitly new paper hypothesis. It
does not alter score files, fit models, read labels, or create account state.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import SecurityId
from ..engine import choose_targets
from .exchange_calendar import load_calendar
from .leader_daily_signals import _calendar_view
from .ipo_qualification import load_ipo_qualification
from .shortline_frozen_model import CN, FLAGS as RAW_FLAGS
from .shortline_panel_replay import _config


SCHEMA = "native_shortline_preopen_research_plan_v1"
REGISTRATION_SCHEMA = "native_shortline_preopen_policy_registration_v1"
DECISIONS_SCHEMA = "native_shortline_preopen_research_decisions_v1"
POLICY_ID = "native_stagewise_top10pct_daily_top5_preopen_paper_v1"
FEATURE_CONTRACT_ID = "native_active_known_nonST_observed_nine_features_v1"
MODEL_KINDS = ("lr", "lgbm")
CSV_COLUMNS = ["event_id", "symbol", "date", "stage", "model_hit5_probability", "paper_candidate_only"]
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "current_signal_admitted": False,
    "model_refit": False, "future_labels_used_for_score": False, "orders_generated": False,
    "paper_orders_generated": False, "account_accessed": False, "account_created": False,
    "ledger_mutated": False, "engine_advanced": False, "network_accessed": False,
    "ths_payload_generated": False, "broker_interacted": False, "pit_verified": False,
    "ipo_qualification_verified": False, "corporate_actions_verified": False}
SPEC = {"policy_id": POLICY_ID, "feature_contract_id": FEATURE_CONTRACT_ID,
    "models": list(MODEL_KINDS), "model_selection_performed": False,
    "scope": "uncapped_own_source_known_stage123_event_universe",
    "stage_selection": "symbol_ascending_stable_then_probability_rank_first_percentile_le_0.10_within_each_stage",
    "minimum_one_per_stage": False, "daily_selection": "probability_desc_symbol_asc_stage_asc_top5",
    "top_per_day": 5, "holdings": 5, "initial_cash": 100000,
    "target_selector": "trading.engine.choose_targets", "gross_limit": .8, "single_limit": .2,
    "candidate_shortage": "cash_no_refill_or_upsizing", "empty_candidates": "NO_CHANGE_not_exit_all",
    "incomplete_stage_scope": "whole_day_both_models_NO_CHANGE",
    "holding_policy": "daily_rebalance_holdings_count_5_no_fixed_session_expiry",
    "original_raw_candidate_and_target_flags_changed": False,
    "source_session": "exact_previous_exchange_session_not_publication_date",
    "publication": "actual_local_now_after_closed_payloads_before_exact_next_open",
    "config": _config(100000, 5).to_dict()}
QUALIFIED_POLICY_ID = "native_stagewise_top10pct_daily_top5_known_no_limit_exclusion_preopen_paper_v1"
QUALIFIED_SPEC = {**SPEC, "policy_id": QUALIFIED_POLICY_ID,
    "scope": "uncapped_own_source_known_stage123_with_explicit_known_after_no_limit_IPO_exclusions",
    "membership_projection": "only_positive_known_first_five_no_limit_evidence_excludes_raw_unknowns",
    "scored_no_limit_conflict": "whole_day_both_models_NO_CHANGE_without_score_row_removal",
    "qualification_clock": "actual_new_policy_registration_not_historical_source_close",
    "original_features_and_score_rows_preserved": True,
    "full_IPO_qualified_universe_claimed": False}
QUALIFICATION_SCHEMA = "native_shortline_stage_membership_qualification_v1"


def policy_spec(policy_id):
    if policy_id == POLICY_ID:
        return SPEC
    if policy_id == QUALIFIED_POLICY_ID:
        return QUALIFIED_SPEC
    raise ValueError("Unknown native preopen research policy")


def _now():
    return datetime.now(CN)


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("An actual offset-aware clock is required")
    return result.astimezone(CN)


def _sha(data):
    return sha256(data).hexdigest()


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"file": str(path), "sha256": _sha(data), "byte_count": len(data)}


def _write(path, record):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _flags(record, expected):
    if any(record.get(key) is not value for key, value in expected.items()):
        raise ValueError("Saved research permission flags do not match the fixed method")


def _calendar(path, day):
    path = Path(path).resolve()
    payload = load_calendar(path)
    if payload is None or int(payload["year"]) != int(day[:4]):
        raise ValueError("Source close requires its pinned full-year exchange calendar")
    year = payload["year"]
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or day not in sessions or sessions.index(day) + 1 >= len(sessions):
        raise ValueError("Exact next exchange session is unresolved; no cross-year or weekday fallback")
    execution = sessions[sessions.index(day) + 1]
    _, proof = _read(path)
    return execution, proof


def _window(day, execution, published, now, *, require_preopen):
    close = _stamp(day + "T15:00:00+08:00")
    opening = _stamp(execution + "T09:30:00+08:00")
    if not close < published <= now or published >= opening:
        raise ValueError("Actual source publication must follow its close and precede the original next open")
    if require_preopen and now >= opening:
        raise ValueError("Original preopen publication window elapsed; no date roll or backfill")
    return opening


def _saved_scores(publication, observed_at):
    pub, pub_ref = _read(publication)
    _flags(pub, RAW_FLAGS)
    if (pub.get("schema") != "native_shortline_raw_score_publication_v1"
            or pub.get("status") != "published_frozen_probabilities_only"
            or pub.get("model_refit") is not False
            or pub.get("target_generation_allowed") is not False
            or pub.get("publication_requires_saved_receipt") is not True
            or pub.get("receipt_written_after_all_bound_payloads") is not True
            or pub.get("old_payloads_modified") is not False):
        raise ValueError("A saved raw-score publication with unchanged payloads is required")
    published = _stamp(pub["published_at"])
    if published > observed_at:
        raise ValueError("Future source publication cannot be consumed")
    root = Path(pub["score_batch_directory"]).resolve()
    payloads = {}
    for binding in pub.get("files", []):
        relative = Path(binding["path"])
        file = (root / relative).resolve()
        if relative.is_absolute() or not file.is_relative_to(root) or file in payloads:
            raise ValueError("Saved score payload path escaped its batch or repeated")
        data = file.read_bytes()
        if _sha(data) != binding["sha256"] or len(data) != binding["byte_count"]:
            raise ValueError("Saved score payload bytes changed after publication")
        payloads[file] = data
    result_file = Path(pub["batch_result_file"]).resolve()
    if len(payloads) != 5 or result_file not in payloads:
        raise ValueError("Exactly the result, two score CSVs and two score proofs must be bound")
    result = json.loads(payloads[result_file].decode("utf-8-sig"))
    _flags(result, RAW_FLAGS)
    if (result.get("schema") != "native_shortline_frozen_score_batch_v1"
            or result.get("status") != "completed_frozen_probabilities_only"
            or result.get("model_refit") is not False
            or result.get("target_generation_allowed") is not False
            or _stamp(result["completed_at"]) > published):
        raise ValueError("Completed frozen score payload must precede its publication")
    summaries = result.get("score_batches", [])
    if len(summaries) != 2 or {r.get("model_kind") for r in summaries} != set(MODEL_KINDS):
        raise ValueError("Exactly both fixed model kinds are required")
    scope = result["source_scope"]
    if scope != pub["source_scope"] or not isinstance(scope.get("unknown_stage_symbols"), list):
        raise ValueError("Published stage scope must preserve explicit unknown membership")
    frames, proofs, model_refs, identities, days, used = {}, {}, {}, None, set(), {result_file}
    for kind in MODEL_KINDS:
        item = next(r for r in summaries if r["model_kind"] == kind)
        if item.get("model_refit") is not False or item.get("target_generation_allowed") is not False or item.get("paper_candidate_flags_true") != 0:
            raise ValueError("Original score candidate and target generation must remain disabled")
        csv_file, proof_file = Path(item["score_file"]).resolve(), Path(item["provenance_file"]).resolve()
        if csv_file not in payloads or proof_file not in payloads or csv_file in used or proof_file in used:
            raise ValueError("Each model must use its own bound CSV and provenance")
        used.update((csv_file, proof_file))
        proof = json.loads(payloads[proof_file].decode("utf-8-sig"))
        _flags(proof, RAW_FLAGS)
        source = proof.get("feature_source", {})
        if (proof.get("schema") != "shortline_frozen_single_date_score_v1"
                or proof.get("model_kind") != kind or proof.get("day") != item["day"]
                or proof.get("status") not in {"completed_frozen_single_date_probabilities_only", "partial_known_stage_frozen_single_date_probabilities_only"}
                or proof.get("feature_contract_id") != FEATURE_CONTRACT_ID
                or proof.get("model_refit") is not False
                or proof.get("target_generation_allowed") is not False
                or proof.get("score_only_not_candidate_batch") is not True
                or source.get("source_scope") != scope
                or source.get("stage_membership_complete") is not scope.get("stage_membership_complete")
                or source.get("unknown_stage_symbols") != scope["unknown_stage_symbols"]
                or proof.get("unknown_stage_symbols") != scope["unknown_stage_symbols"]
                or proof.get("stage_membership_complete") is not scope.get("stage_membership_complete")):
            raise ValueError("Model proof must retain the exact native method, day and complete/partial scope")
        if any(_stamp(value) > published for value in
                (proof["scored_at"], proof["score_available_at"], proof["model_available_at"], source["available_at"])):
            raise ValueError("Publication precedes a required saved score input or computation")
        frame = pd.read_csv(io.BytesIO(payloads[csv_file]), usecols=CSV_COLUMNS,
            dtype={"event_id": str, "symbol": str, "date": str, "paper_candidate_only": str})
        stage = pd.to_numeric(frame.stage, errors="raise")
        probability = pd.to_numeric(frame.model_hit5_probability, errors="raise")
        if (frame.empty or len(frame) != item["rows"] or len(frame) != proof["score_rows"]
                or not frame.date.eq(item["day"]).all() or not stage.isin((1, 2, 3)).all()
                or not np.isfinite(probability).all() or not probability.between(0, 1).all()
                or not frame.paper_candidate_only.str.lower().isin(("false", "0")).all()
                or frame.symbol.duplicated().any() or frame.event_id.duplicated().any()):
            raise ValueError("Saved raw score identities, probabilities or false candidate flags are invalid")
        frame["stage"], frame["model_hit5_probability"] = stage.astype(int), probability
        for row in frame.itertuples(index=False):
            SecurityId(row.symbol)
            if row.event_id != f"{row.symbol}|{row.date}|s{row.stage}":
                raise ValueError("Score event identity does not match its symbol/day/stage")
        current = set(zip(frame.event_id, frame.symbol, frame.date, frame.stage))
        if identities is not None and current != identities:
            raise ValueError("Both model outputs must score the exact same event identities")
        identities = current
        days.add(item["day"])
        frame["paper_candidate_only"] = False
        frames[kind], proofs[kind] = frame, proof
        model_refs[kind] = {key: proof[key] for key in
            ("manifest_file", "manifest_sha256", "generation_id", "model_available_at", "feature_contract_id")}
    if len(days) != 1 or used != set(payloads):
        raise ValueError("One exact source day and the complete five-payload binding are required")
    if any(model_refs["lr"][key] != model_refs["lgbm"][key] for key in model_refs["lr"]):
        raise ValueError("Both models must retain the same frozen generation and feature contract")
    complete = scope.get("stage_membership_complete") is True and not scope["unknown_stage_symbols"]
    return {"publication": pub, "publication_ref": pub_ref, "frames": frames, "proofs": proofs,
        "model_refs": model_refs, "source_day": next(iter(days)), "scope": scope, "scope_complete": complete}


def qualify_saved_scope(saved, qualification, *, decision_as_of):
    """Project explicit no-limit facts; do not assign stages or change score rows.

    The source's feature scope and membership proof remain untouched. Unknown
    listing facts do not resolve a raw unknown. A positively known no-limit
    issuer already present in the scores blocks the entire new variant rather
    than silently removing a scored row and changing its percentile ranks.
    """
    clock = _stamp(decision_as_of)
    unknown = saved["scope"]["unknown_stage_symbols"]
    scored = sorted(set(saved["frames"]["lr"].symbol))
    if (len(set(unknown)) != len(unknown) or set(unknown) & set(scored)
            or saved["scope"].get("stage_membership_complete") not in (True, False)):
        raise ValueError("Qualification requires distinct unresolved and already scored identities")
    for symbol in unknown:
        SecurityId(symbol)
    assessments = [qualification.qualify(saved["source_day"], symbol, decision_as_of=clock.isoformat())
        for symbol in sorted(set(unknown) | set(scored))]
    excluded, conflicts = [], []
    for item in assessments:
        known_no_limit = (item.get("knowledge_status") == "known_after_source_availability"
            and item.get("no_price_limit_first_five") is True
            and item.get("engine_block_reason") == "unsupported_first_five_listing_sessions"
            and item.get("ipo_feature_exclusion_reason") == "no_price_limit_first_five_not_limit_up_streak"
            and type(item.get("listing_session_ordinal")) is int
            and 1 <= item["listing_session_ordinal"] <= 5)
        if not known_no_limit:
            continue
        if _stamp(item["available_at"]) > clock:
            raise ValueError("No-limit exclusion cannot precede its saved knowledge availability")
        (excluded if item["symbol"] in unknown else conflicts).append(item["symbol"])
    remaining = sorted(set(unknown) - set(excluded))
    complete = bool(not remaining and not conflicts and (saved["scope_complete"] or bool(unknown)))
    return {"schema": QUALIFICATION_SCHEMA, "policy_id": QUALIFIED_POLICY_ID,
        "source_day": saved["source_day"], "decision_as_of": clock.isoformat(),
        "raw_stage_membership_complete": saved["scope"]["stage_membership_complete"],
        "raw_unknown_stage_symbols": sorted(unknown), "excluded_no_limit_symbols": sorted(excluded),
        "remaining_unknown_stage_symbols": remaining, "scored_no_limit_conflicts": sorted(conflicts),
        "qualified_stage_membership_complete": complete,
        "raw_score_event_count_per_model": {kind: len(saved["frames"][kind]) for kind in MODEL_KINDS},
        "same_scored_unique_symbol_count": len(scored), "assessments": assessments,
        "qualification_verification": qualification.verification,
        "source_scope_preserved": True, "original_feature_contract_id": FEATURE_CONTRACT_ID,
        "feature_values_recomputed": False, "score_rows_removed": False, "stage_values_imputed": False,
        "full_IPO_qualified_universe_claimed": False, **FLAGS}


def _qualified_saved(saved, calendar, ipo_evidence, research_master_run, registered):
    _, evidence_ref = _read(ipo_evidence)
    master_ref = _read(research_master_run)[1] if research_master_run is not None else None
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    projection = qualify_saved_scope(saved, qualification, decision_as_of=registered)
    inputs = {"ipo_evidence": evidence_ref, "research_master_run": master_ref}
    projection["qualification_inputs"] = inputs
    qualified = {**saved, "scope_complete": projection["qualified_stage_membership_complete"],
        "policy_id": QUALIFIED_POLICY_ID, "qualification": projection}
    return qualified, projection, inputs


def _qualification_summary(projection):
    return {key: projection[key] for key in ("decision_as_of", "raw_stage_membership_complete",
        "raw_unknown_stage_symbols", "excluded_no_limit_symbols", "remaining_unknown_stage_symbols",
        "scored_no_limit_conflicts", "qualified_stage_membership_complete",
        "source_scope_preserved", "feature_values_recomputed", "score_rows_removed",
        "stage_values_imputed", "full_IPO_qualified_universe_claimed")}


def _prepare(saved, execution):
    previews, models = {}, []
    for kind in MODEL_KINDS:
        frame = saved["frames"][kind].sort_values("symbol", kind="stable").copy()
        frame["stage_rank_pct"] = frame.groupby("stage")["model_hit5_probability"].rank(ascending=False, method="first", pct=True)
        frame["rank_pass"] = frame.stage_rank_pct.le(.10) & saved["scope_complete"]
        frame = frame.sort_values(["model_hit5_probability", "symbol", "stage"], ascending=[False, True, True], kind="stable").reset_index(drop=True)
        selected = frame.loc[frame.rank_pass].head(5)
        frame["candidate_selected"] = frame.event_id.isin(selected.event_id)
        reason = ("stage_membership_incomplete_whole_day_NO_CHANGE" if not saved["scope_complete"]
            else "no_candidates_after_fixed_stage_rank_NO_CHANGE" if selected.empty else "fixed_native_stagewise_top10pct_daily_top5")
        if "qualification" in saved and not saved["scope_complete"]:
            reason = ("scored_no_limit_conflict_whole_day_NO_CHANGE" if saved["qualification"]["scored_no_limit_conflicts"]
                else "qualified_stage_membership_incomplete_whole_day_NO_CHANGE")
        weights = None
        if not selected.empty:
            weights = choose_targets([{"symbol": r.symbol, "score": float(r.model_hit5_probability)}
                for r in selected.itertuples(index=False)], [], _config(100000, 5))
        models.append({"model_kind": kind, "decision": "FULL_TARGET" if weights else "NO_CHANGE",
            "reason": reason, "weights": weights, "target_cash_weight": 1 - sum(weights.values()) if weights else None,
            "source_day": saved["source_day"], "execution_day": execution, "scope_complete": saved["scope_complete"],
            "input_rows": len(frame), "selected_events": selected[CSV_COLUMNS].to_dict("records"),
            "selected_count": len(selected), "input_scope": saved["scope"],
            "raw_target_generation_allowed": False, "raw_candidate_flags_changed": False,
            "isolated_research_paper_only": True, **FLAGS})
        if "qualification" in saved:
            models[-1]["qualified_input_scope"] = _qualification_summary(saved["qualification"])
        previews[kind] = frame
    return previews, {"schema": DECISIONS_SCHEMA, "policy_id": saved.get("policy_id", POLICY_ID),
        "source_day": saved["source_day"], "execution_day": execution, "models": models, **FLAGS}


def build_preopen_plan(publication, calendar, out, *, ipo_evidence=None, research_master_run=None):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Preopen plan requires an explicitly new output directory")
    if research_master_run is not None and ipo_evidence is None:
        raise ValueError("An explicit research master requires the explicit IPO scope variant")
    actual = _now()
    saved = _saved_scores(publication, actual)
    day = saved["source_day"]
    execution, calendar_ref = _calendar(calendar, day)
    opening = _window(day, execution, _stamp(saved["publication"]["published_at"]), actual, require_preopen=True)
    projection, inputs = None, None
    policy_id, spec = POLICY_ID, SPEC
    if ipo_evidence is None:
        previews, decisions = _prepare(saved, execution)
        registered = _now()
    else:
        registered = _now()
        saved, projection, inputs = _qualified_saved(saved, calendar, ipo_evidence, research_master_run, registered)
        policy_id, spec = QUALIFIED_POLICY_ID, QUALIFIED_SPEC
        previews, decisions = _prepare(saved, execution)
    _window(day, execution, _stamp(saved["publication"]["published_at"]), registered, require_preopen=True)
    registration = {"schema": REGISTRATION_SCHEMA, "policy_id": policy_id, "registered_at": registered.isoformat(),
        "source_day": day, "execution_day": execution, "spec": spec,
        "source_publication": saved["publication_ref"], "calendar": calendar_ref,
        "frozen_model_refs": saved["model_refs"], "new_native_policy_not_old_strategy_validation": True, **FLAGS}
    if projection is not None:
        registration["qualification_inputs"] = inputs
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "registration.json", registration)
    for kind in MODEL_KINDS:
        previews[kind].to_csv(out / f"{kind}_ranked_preview.csv", mode="x", index=False, encoding="utf-8-sig")
    _write(out / "decisions.json", decisions)
    if projection is not None:
        _write(out / "qualification_scope.json", projection)
    files = []
    names = ["registration.json", "lr_ranked_preview.csv", "lgbm_ranked_preview.csv", "decisions.json"]
    if projection is not None:
        names.append("qualification_scope.json")
    for name in names:
        data = (out / name).read_bytes()
        files.append({"path": name, "sha256": _sha(data), "byte_count": len(data)})
    published = _now()
    _window(day, execution, _stamp(saved["publication"]["published_at"]), published, require_preopen=True)
    if published < registered:
        raise ValueError("Actual plan publication precedes its policy registration")
    plan = {"schema": SCHEMA, "status": "published_preopen_research_plan", "policy_id": policy_id,
        "published_at": published.isoformat(), "publication_clock_basis": "actual_local_now_after_closed_payloads_before_exclusive_plan_write",
        "publication_requires_saved_plan": True, "source_day": day, "execution_day": execution,
        "next_open_deadline": opening.isoformat(), "source_publication": saved["publication_ref"],
        "calendar": calendar_ref, "spec": spec, "files": files,
        "decisions_file": "decisions.json", "registration_file": "registration.json",
        "research_target_weights_generated": any(m["weights"] is not None for m in decisions["models"]),
        "raw_score_target_generation_allowed": False, "raw_score_payloads_modified": False,
        "model_selection_performed": False, "fixed_session_expiry_enabled": False, **FLAGS}
    if projection is not None:
        plan["qualification_scope_file"] = "qualification_scope.json"
        plan["qualified_input_scope"] = _qualification_summary(projection)
    _write(out / "plan.json", plan)
    return plan


def load_preopen_plan(path):
    path = Path(path).resolve()
    path = path / "plan.json" if path.is_dir() else path
    plan, plan_ref = _read(path)
    _flags(plan, FLAGS)
    spec = policy_spec(plan.get("policy_id"))
    qualified_variant = plan["policy_id"] == QUALIFIED_POLICY_ID
    if (plan.get("schema") != SCHEMA or plan.get("status") != "published_preopen_research_plan"
            or plan.get("spec") != spec
            or plan.get("publication_requires_saved_plan") is not True
            or plan.get("raw_score_target_generation_allowed") is not False
            or plan.get("raw_score_payloads_modified") is not False):
        raise ValueError("Saved plan must retain the independent fixed native research policy")
    payloads = {}
    for binding in plan.get("files", []):
        file = (path.parent / binding["path"]).resolve()
        if file.parent != path.parent or file in payloads:
            raise ValueError("Plan payload must remain a distinct file inside the plan directory")
        data = file.read_bytes()
        if _sha(data) != binding["sha256"] or len(data) != binding["byte_count"]:
            raise ValueError("Plan payload changed after actual publication")
        payloads[file] = data
    names = {p.name for p in payloads}
    expected_names = {"registration.json", "lr_ranked_preview.csv", "lgbm_ranked_preview.csv", "decisions.json"}
    if qualified_variant:
        expected_names.add("qualification_scope.json")
    if names != expected_names:
        raise ValueError("A saved plan must bind its exact closed policy and qualification payloads")
    actual, published = _now(), _stamp(plan["published_at"])
    source_ref = plan["source_publication"]
    saved = _saved_scores(source_ref["file"], actual)
    if saved["publication_ref"] != source_ref:
        raise ValueError("Original score publication changed")
    execution, calendar_ref = _calendar(plan["calendar"]["file"], saved["source_day"])
    opening = _window(saved["source_day"], execution, published, actual, require_preopen=False)
    if (calendar_ref != plan["calendar"] or saved["source_day"] != plan["source_day"]
            or execution != plan["execution_day"] or opening.isoformat() != plan["next_open_deadline"]
            or _stamp(saved["publication"]["published_at"]) > published):
        raise ValueError("Plan cannot change the source session, calendar or original execution opening")
    registration = json.loads(payloads[path.parent / "registration.json"].decode("utf-8"))
    _flags(registration, FLAGS)
    if (registration.get("schema") != REGISTRATION_SCHEMA or registration.get("spec") != spec
            or registration.get("policy_id") != plan["policy_id"] or registration.get("source_day") != plan["source_day"]
            or registration.get("execution_day") != execution or registration.get("calendar") != calendar_ref
            or registration.get("source_publication") != source_ref or registration.get("frozen_model_refs") != saved["model_refs"]
            or not _stamp(saved["publication"]["published_at"]) <= _stamp(registration["registered_at"]) <= published):
        raise ValueError("Plan registration must retain its actual clocks, inputs and fixed model generation")
    if qualified_variant:
        if plan.get("qualification_scope_file") != "qualification_scope.json":
            raise ValueError("Qualified policy must bind its distinct saved membership projection")
        inputs = registration.get("qualification_inputs")
        if not isinstance(inputs, dict) or set(inputs) != {"ipo_evidence", "research_master_run"}:
            raise ValueError("Qualified policy requires its exact IPO and optional master references")
        evidence = inputs["ipo_evidence"]
        master = inputs["research_master_run"]
        if (not isinstance(evidence, dict) or _read(evidence["file"])[1] != evidence
                or (master is not None and (not isinstance(master, dict) or _read(master["file"])[1] != master))):
            raise ValueError("Qualification inputs changed after policy registration")
        saved, projection, expected_inputs = _qualified_saved(saved, plan["calendar"]["file"], evidence["file"],
            master["file"] if master is not None else None, _stamp(registration["registered_at"]))
        stored = json.loads(payloads[path.parent / "qualification_scope.json"].decode("utf-8"))
        if (expected_inputs != inputs or stored != projection
                or plan.get("qualified_input_scope") != _qualification_summary(projection)):
            raise ValueError("Saved no-limit scope differs from its pinned facts, actual clock or unchanged scores")
    elif any(key in plan for key in ("qualification_scope_file", "qualified_input_scope")) or "qualification_inputs" in registration:
        raise ValueError("Original policy cannot silently acquire the distinct qualification variant")
    decisions = json.loads(payloads[path.parent / "decisions.json"].decode("utf-8"))
    _, expected = _prepare(saved, execution)
    if decisions != expected:
        raise ValueError("Saved model decisions differ from the registered fixed score-only policy")
    return {"record": plan, "decisions": {r["model_kind"]: r for r in decisions["models"]},
        "publication": saved["publication"], "calendar": calendar_ref, "source_day": plan["source_day"],
        "execution_day": execution, "registration": registration, "plan_ref": plan_ref}


def bind_preopen_plan(path, model_kind, context):
    from ..contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext
    if model_kind not in MODEL_KINDS or not isinstance(context, RunContext):
        raise ValueError("An exact model kind and explicit simulated account context are required")
    loaded = load_preopen_plan(path)
    item = loaded["decisions"][model_kind]
    metadata = {"reason": item["reason"], "context": context,
        "source": f"research:shortline_native_preopen:{loaded['plan_ref']['sha256']}:{model_kind}"}
    intent = (PortfolioIntent.no_change(**metadata) if item["decision"] == "NO_CHANGE"
        else PortfolioIntent.full_target(item["weights"], **metadata))
    return PreopenPortfolioIntent(loaded["source_day"], loaded["execution_day"],
        loaded["record"]["published_at"], intent, explicit_exit_all=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path)
    parser.add_argument("--research-master-run", type=Path)
    args = parser.parse_args(argv)
    plan = build_preopen_plan(args.publication, args.calendar, args.out,
        ipo_evidence=args.ipo_evidence, research_master_run=args.research_master_run)
    print(json.dumps({k: plan[k] for k in ("status", "source_day", "execution_day", "published_at", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
