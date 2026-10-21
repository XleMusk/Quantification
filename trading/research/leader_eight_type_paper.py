"""Publish unbound fixed-rule research weights using only the latest saved close.

No account, Ledger, Engine session, order, THS payload or successor raw bar is
created or required. The real recording clock alone establishes timeliness.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import math
from pathlib import Path

from ..contracts import MARKET_TZ, IntentKind, PortfolioIntent
from ..engine import choose_targets
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_dated_turnover import load_turnover_source
from .leader_eight_type_panel_replay import CAPS, GUARDS, PROTOCOL, TYPE_IDS, _config, _environment, _rank_path, load_inputs
from .leader_environment import ARCHETYPES, _clean
from .leader_environment_policy import TYPE_RULES
from .leader_eight_type_inputs import _industry
from .leader_type_risk_exit import POLICY_ID, RULE_SPEC
from .recent_alpha158_panel_replay import ExactSourceRawFeed, _raw_issues, _reference

SCHEMA = "leader_eight_type_unbound_close_paper_plan_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
         "selection_enabled": False, "execution_admitted": False, "current_signal_admitted": False,
         "model_refit": False, "future_labels_used_for_score": False, "provider_contacted": False,
         "account_created": False, "account_accessed": False, "ledger_mutated": False,
         "engine_advanced": False, "orders_generated": False, "ths_payload_generated": False,
         "broker_interacted": False, "historical_publication_verified": False,
         "pit_verified": False, "corporate_actions_verified": False, "case_selected": False}


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("Actual paper publication and source clocks require a timezone")
    return value.astimezone(MARKET_TZ)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _protocol_clocks(record, created):
    """The frozen protocol uses explicit clock-tool UTC text, not filenames."""
    fields = {"recorded_at_utc": record.get("recorded_at_utc"),
              "pre_run_clarification.recorded_at_utc": record.get("pre_run_clarification", {}).get("recorded_at_utc"),
              "risk_rule_fixed_before_new_actual_run.recorded_at_utc": record.get("risk_rule_fixed_before_new_actual_run", {}).get("recorded_at_utc")}
    clocks = {}
    for key, value in fields.items():
        if not isinstance(value, str):
            raise ValueError("Frozen protocol needs all three explicit actual registration clocks")
        if value.endswith(" UTC"):
            clock = datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        else:
            clock = _stamp(value)
        if clock > created:
            raise ValueError("Actual protocol/risk registration occurred after the input manifest")
        clocks[key] = clock.isoformat()
    return clocks


def _source_bindings(loaded, observed):
    """Bind the saved side-source references without recomputing candidates."""
    provenance = loaded["provenance"]
    record = provenance["input_record"]
    created = _stamp(record["created_at"])
    if not _stamp(record["scope_frozen_at"]) <= created <= observed:
        raise ValueError("Actual scope or input recording clock is unavailable")
    external = provenance.get("external_fixed_protocol")
    expected = [f"{identity}__{guard}__{cap}" for identity in TYPE_IDS for guard in GUARDS for cap, _ in CAPS]
    if (not external or external["record"].get("schema") != "leader_eight_type_fixed_comparison_protocol_v1"
            or external["record"].get("case_count") != 32
            or external["record"].get("config") != _config(100000, 5).to_dict()
            or external["record"].get("combined_risk_policy_id") != POLICY_ID
            or external["record"].get("structural_risk_rules") != RULE_SPEC
            or [case["id"] for case in external["record"].get("cases", [])] != expected
            or any(external["record"].get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score"))):
        raise ValueError("Paper must use the separately frozen original thirty-two cases and costs")
    protocol_clocks = _protocol_clocks(external["record"], created)
    for key in ("created_at", "recorded_at", "registered_at"):
        if external["record"].get(key) and _stamp(external["record"][key]) > created:
            raise ValueError("Frozen protocol was actually recorded after its saved input manifest")
    industry = record["industry_source"]
    actual_industry = _industry(industry["source_file"], observed.isoformat())
    if ({key: value for key, value in actual_industry.items() if key != "membership"} != industry
            or not _stamp(industry["available_at"]) <= created or industry.get("theme_membership") is not False):
        raise ValueError("Saved lagged broad-industry source/actual clock changed")
    identity = record["identity_report"]
    if digest(identity["file"]) != identity["sha256"] or _stamp(identity["read_at"]) > created:
        raise ValueError("Saved identity observation or its actual read clock changed")
    identity_record = json.loads(Path(identity["file"]).read_text(encoding="utf8"))
    if (identity_record.get("registry_sha256") != identity["registry_sha256"]
            or identity_record.get("registry") != identity["registry"]
            or identity_record.get("read_at") != identity["read_at"]
            or not _stamp(identity_record["registered_at"]) <= _stamp(identity["read_at"]) <= created
            or identity_record.get("execution_admitted") is not False):
        raise ValueError("Identity summary no longer binds the fixed unadmitted registry")
    turns = record["turnover_sources"]
    if len({item["day"] for item in turns}) != len(turns):
        raise ValueError("Duplicate dated turnover evidence cannot be silently selected")
    for saved in turns:
        proof = saved["sourceproof"]
        source = proof.get("manifest_file") or proof.get("extras_result")
        if not source:
            raise ValueError("Turnover source needs its exact original typed/sidecar locator")
        current = load_turnover_source(source, observed_at=observed.isoformat(), day=saved["day"])
        if ({key: value for key, value in current.items() if key != "rows"} != saved
                or _stamp(saved["available_at"]) > created):
            raise ValueError("Saved turnover source, percentage unit or actual availability changed")
    return {"input_manifest": provenance["panel"], "input_manifest_sha256": provenance["panel_sha256"],
            "input_recorded_at": record["created_at"], "scope_frozen_at": record["scope_frozen_at"],
            "shared_view_id": record.get("shared_view_id"), "external_fixed_protocol": deepcopy(external),
            "protocol_actual_registration_clocks": protocol_clocks,
            "source_proofs": deepcopy(provenance["source_proofs"]),
            "context_anchor": deepcopy(provenance["context_anchor"]),
            "IPO_qualification": deepcopy(provenance["IPO_qualification"]),
            "industry_source": deepcopy(industry), "turnover_sources": deepcopy(turns),
            "identity_report": deepcopy(identity), "all_inputs_available_by_actual_recording": True}


def _prepare(loaded, observed):
    panels, source_days = loaded["panel_by_day"], loaded["source_days"]
    if sorted(panels) != source_days or source_days != sorted(set(source_days)):
        raise ValueError("The latest saved complete source and latest dated panel must be the same D")
    day = source_days[-1]
    sessions, previous, status = _calendar_view(loaded["calendar_payload"],
        f"{loaded['calendar_payload']['year']}-01-01", f"{loaded['calendar_payload']['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar" or sorted(loaded["raw"].date.unique()) != source_days:
        raise ValueError("Only the pinned current/past source dates may enter a paper plan")
    if day not in sessions or _stamp(day + "T15:00:00+08:00") > observed:
        raise ValueError("Paper targets require an actually closed latest source date")
    index = sessions.index(day)
    execution = sessions[index + 1] if index + 1 < len(sessions) else None
    if not execution:
        raise ValueError("Pinned calendar lacks the exact original next session")
    members = set(loaded["scope_members"])
    paths, _ = _rank_path(panels, members, source_days[0], day)
    environment = _environment(panels, sessions, observed.isoformat())[day]
    base = ExactSourceRawFeed(loaded["raw"], source_days, sorted(members), context_anchor=loaded["context_anchor"])
    config, prepared = _config(100000, 5), []
    current = panels[day]
    by_type = {item["id"]: item for item in current["types"]}
    for identity in TYPE_IDS:
        item = by_type[identity]
        rule = TYPE_RULES[identity]
        for guard in GUARDS:
            for cap_name, _ in CAPS:
                path = paths[identity][cap_name][day]
                selected = deepcopy(path["candidates"])
                raw_weights = choose_targets(path["original_top5"], [], config)
                cap_weights = choose_targets(selected, [], config)
                mode = environment["per_type"][identity]["mode"] if guard != "unguarded" else "ALLOW_FIXED_CANDIDATES"
                reasons, issues, references, ipo = [], [], [], []
                # These are entry checks, not a claim about an unknown real account.
                if mode == "EXIT_ALL":
                    weights = {}
                    decision = "EXIT_ALL"
                else:
                    if mode == "NO_CHANGE": reasons.append("environment_unknown_or_unconfirmed")
                    if item.get("status") not in {"matched", "not_matched"}: reasons.append("type_observation_unknown")
                    if not isinstance(item.get("unresolved_symbols"), list): reasons.append("whole_type_membership_evidence_missing")
                    unresolved = set(item.get("unresolved_symbols", [])).intersection(members)
                    if unresolved: reasons.append("whole_type_membership_unknown_in_fixed_panel")
                    if not selected: reasons.append("type_empty_or_capacity_filtered_all_no_signal")
                    required = {row["symbol"] for row in selected}
                    issues = _raw_issues(base, day, required)
                    for symbol in sorted(required):
                        raw = base.rows.get(day, {}).get(symbol)
                        if raw is not None and (raw["tradestatus"] != 1 or raw["isST"] != 0 or raw["volume"] <= 0 or raw["amount"] <= 0):
                            issues.append({"symbol": symbol, "day": day, "reason": "selected_current_not_active_nonST"})
                    references = [_reference(base, day, symbol, previous) for symbol in sorted(required)]
                    # Actual current clock, never a fictitious historical 15:05.
                    ipo = [loaded["qualification"].qualify(day, symbol, decision_as_of=observed.isoformat()) for symbol in sorted(required)]
                    if issues: reasons.append("selected_current_raw_unknown_or_ineligible")
                    if any(proof["status"] != "consistent_proxy" for proof in references): reasons.append("selected_current_reference_unknown_or_incompatible")
                    if any(proof["engine_block_reason"] is not None or not proof["ipo_window_feature_support"] for proof in ipo): reasons.append("selected_current_IPO_unknown_or_unsupported")
                    weights = None if reasons else cap_weights
                    decision = "NO_CHANGE" if reasons else "FULL_TARGET"
                setup = []
                for candidate in selected:
                    row = base.rows.get(day, {}).get(candidate["symbol"], {})
                    features = current.get("risk_features", {}).get(candidate["symbol"]) or {}
                    setup.append({"symbol": candidate["symbol"], "source_day": day,
                        "reference_close_cny": _finite(row.get("close")), "setup_low_cny": _finite(row.get("low")),
                        "sma20_observed": _finite(features.get("sma20")), "sma60_observed": _finite(features.get("sma60")),
                        "reference_is_order_price": False, "reference_is_actual_held_entry": False,
                        "missing_structural_fields_stay_unknown": True})
                prepared.append({"case_id": f"{identity}__{guard}__{cap_name}", "type": identity,
                    "guard": guard, "capacity": cap_name, "decision": decision, "environment_mode": mode,
                    "reason": ";".join(reasons) if reasons else environment["per_type"][identity]["reason"] if mode == "EXIT_ALL" else "fixed_original_rank_and_capacity_target",
                    "required_evidence": deepcopy(rule["additional_candidate_requirements"]),
                    "risk_and_invalidation": deepcopy(rule["invalidation_signals"]),
                    "structural_invalidation_policy": RULE_SPEC[identity],
                    "type_missing_evidence": deepcopy(item.get("missing_evidence", [])),
                    "unresolved_member_count": len(set(item.get("unresolved_symbols", [])).intersection(members)),
                    "original_top5": deepcopy(path["original_top5"]), "cap_candidates": selected,
                    "capacity_members_before": path["cap_symbols_before"], "capacity_members_after": path["cap_symbols_after"],
                    "cap_dropped_rows": path["cap_dropped_rows"], "raw_weights_before_capacity_guard_and_qualification": raw_weights,
                    "cap_weights_before_guard_and_qualification": cap_weights, "prepared_weights": weights,
                    "current_raw_issues": issues, "current_reference_evidence": references, "IPO_evidence": ipo,
                    "candidate_reference_and_setup": setup, "entry_unknown_checks_bypassed_for_independent_exit": mode == "EXIT_ALL",
                    "holdings_binding_pending": True, "cash_binding_pending": True, "quote_binding_pending": True,
                    "held_risk_assessment_pending": True, "T_plus_1_and_actual_sellability_pending": True,
                    "holding_retention_and_expiry_computed": False, "account_assumed_empty": False,
                    "successor_raw_required": False, "successor_raw_used": False, "refill_performed": False})
    return day, execution, environment, prepared


def _finalize(day, execution, environment, prepared, bindings, recorded):
    close, opening = _stamp(day + "T15:00:00+08:00"), _stamp(execution + "T09:30:00+08:00")
    if recorded < close or _stamp(bindings["input_recorded_at"]) > recorded:
        raise ValueError("Actual recording cannot precede source close or source evidence")
    timely = recorded.date().isoformat() == day and recorded < opening
    cases = []
    for item in prepared:
        item = deepcopy(item)
        weights = item.pop("prepared_weights")
        metadata = {"decided_at": recorded.isoformat(), "context": None,
                    "execution_not_before": execution if timely else None,
                    "source": "research:leader_eight_type_unbound_paper:" + item["case_id"]}
        if weights is None:
            intent = PortfolioIntent.no_change(item["reason"], **metadata)
        else:
            intent = PortfolioIntent.full_target(weights, **metadata)
        item.update(portfolio_intent=intent.to_dict(), target_weights=None if intent.kind == IntentKind.NO_CHANGE else dict(intent.weights),
                    target_cash_weight=None if intent.kind == IntentKind.NO_CHANGE else 1 - sum(weight for _, weight in intent.weights),
                    publication_status="unbound_current_close_research" if timely else "late_context_only",
                    **FLAGS)
        cases.append(item)
    return {"schema": SCHEMA, "status": "unbound_current_close_paper_research" if timely else "late_context_only",
        "source_close_day": day, "proposed_execution_day": execution, "execution_day_bound": execution if timely else None,
        "next_open_deadline": opening.isoformat(), "recorded_at": recorded.isoformat(), "publication_at": recorded.isoformat(),
        "publication_clock_basis": "actual_local_now_after_input_and_candidate_validation",
        "publication_time_backfilled": False, "execution_day_rolled_forward": False,
        "same_source_day_evening_required_for_binding": True, "research_budget_cny": 100000,
        "budget_is_account_cash": False, "holdings_binding_pending": True, "cash_binding_pending": True,
        "quote_binding_pending": True, "held_risk_assessment_pending": True, "source": bindings,
        "protocol": deepcopy(PROTOCOL), "risk_policy_id": POLICY_ID, "risk_policy_spec": deepcopy(RULE_SPEC),
        "environment_policy": environment, "case_count": len(cases), "cases": cases,
        "successor_raw_required": False, "successor_raw_used": False, "tail_replay_required": False,
        "limitations": ["Thirty-two fixed research cases are retained; none is selected by performance.",
            "NO_CHANGE is not an empty portfolio, and EXIT_ALL is only an unbound zero-weight intent.",
            "Reference closes and setup lows are research evidence, not order prices or actual held-entry anchors.",
            "Actual holdings, FIFO fill anchor, T+1, cash, quotes and sellability remain unbound.",
            "Unknown PIT, actions, theme membership and circulating shares are not cleared by this publication."], **FLAGS}


def _chinese_plan(result):
    labels = dict(ARCHETYPES)
    cases = {item["case_id"]: item for item in result["cases"]}
    lines = [f"# {result['source_close_day']} 八类龙型纸面研究方案", "",
        f"实际记录：{result['recorded_at']}。原下一交易日：{result['proposed_execution_day']}；"
        + ("本收盘日内记录，日期可绑定；账户与执行仍未绑定。" if result["execution_day_bound"] else "已属较早收盘的迟到背景，不绑定执行日，也不滚到更后一天。"), "",
        "研究预算10万元，只输出未绑定账户的权重。NO_CHANGE表示保留原状，不能理解为空仓；EXIT_ALL是研究空目标，未产生卖单。", "",
        "| 固定龙型 | 原规则·cap20 | 原规则·uncapped | 结合市场环境·cap20 | 结合市场环境·uncapped |",
        "|---|---|---|---|---|"]
    for identity in TYPE_IDS:
        values = [cases[f"{identity}__{guard}__{cap}"]["decision"] for guard in GUARDS for cap, _ in CAPS]
        lines.append("| " + labels[identity] + " | " + " | ".join(values) + " |")
    lines += ["", "每个case的完整冻结候选、权重、理由、参考收盘价、结构低点及失效条件见 paper_plan.json。没有按收益挑选case，也没有未知剔除后补位。",
        "现仓、可卖数量、现金、实时报价和实际买入锚点均待绑定；T+1、五交易日到期及结构退出未对未知持仓作判断。不生成股数、委托价、订单或同花顺预填。"]
    return "\n".join(lines) + "\n"


def publish(panel, ipo_evidence, research_master_run, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Paper research publication requires a new output directory")
    loaded = load_inputs(panel, ipo_evidence, research_master_run)
    observed = _now()
    bindings = _source_bindings(loaded, observed)
    day, execution, environment, prepared = _prepare(loaded, observed)
    recorded = _now()
    if recorded < observed or digest(panel) != loaded["provenance"]["panel_sha256"]:
        raise ValueError("Actual paper clock moved backwards or saved input manifest changed")
    result = _finalize(day, execution, environment, prepared, bindings, recorded)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "paper_plan.json", _clean(result))
    (out / "PLAN.md").write_text(_chinese_plan(result), encoding="utf8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("panel", "ipo-evidence", "research-master-run", "out"):
        parser.add_argument("--" + field, required=True, type=Path)
    args = parser.parse_args(argv)
    result = publish(args.panel, args.ipo_evidence, args.research_master_run, args.out)
    print(json.dumps({key: result[key] for key in ("status", "source_close_day", "proposed_execution_day", "recorded_at", "case_count", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
