"""Explicit saved-history projection for eight-type research browsing.

Only ``build`` reads the completed source results. ``read_history`` verifies
the small projection index and the selected case; it does not reopen sources,
evaluate a policy, recalculate returns, create an account or contact a provider.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from ..contracts import MARKET_TZ
from .leader_eight_type_panel_replay import TYPE_IDS, GUARDS, CAPS, PROTOCOL


SCHEMA = "leader_eight_type_saved_history_view_v1"
MANIFEST_SCHEMA = "leader_eight_type_saved_history_projection_v1"
POINTER_SCHEMA = "leader_eight_type_saved_history_current_v1"
DEFAULT_SUBDIR = Path("research/leader_eight_type_history_views/20261017_v1")
CASE_IDS = tuple(f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS)
ABLATION_SCHEMA = "leader_eight_type_saved_history_with_ablation_v1"
ABLATION_MANIFEST_SCHEMA = "leader_eight_type_saved_history_ablation_projection_v1"
ABLATION_GUARDS = (*GUARDS, "two_close_entry_filter_only")
ABLATION_CASE_IDS = tuple(f"{kind}__{guard}__{cap}" for kind in TYPE_IDS for guard in ABLATION_GUARDS for cap, _ in CAPS)
ABLATION_FAMILY = "fixed_environment_exit_ablation48"
KNOWN_SCOPE_FAMILY = "known_positive_crossing_scope4"
KNOWN_SCOPE_CASE_IDS = tuple(f"cross_regime_candidate__{guard}__{cap}" for guard in GUARDS for cap, _ in CAPS)
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "returns_reportable": False, "pit_verified": False, "profits_recomputed": False,
    "engine_advanced": False, "ledger_mutated": False, "orders_generated": False,
    "provider_contacted": False, "broker_interacted": False, "source_files_modified": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return json.loads(data.decode("utf-8-sig")), {"path": str(path), "sha256": sha256(data).hexdigest()}


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(data)
    return {"path": str(path), "sha256": sha256(data).hexdigest()}


def _boundary(record):
    if (record.get("candidate_panel_only") is not True
            or any(record.get(k) is not False for k in
                   ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Only saved unvalidated research history can be projected")


def _records_by_day(values):
    grouped = defaultdict(list)
    for row in values:
        grouped[row["day"]].append(deepcopy(row))
    return grouped


def _money_record(row, *, order):
    result = deepcopy(row)
    # The source path is the ordinary-equity shared Ledger, whose saved order
    # and fill monetary values use integer fen. Preserve those original fields.
    field = "limit_price" if order else "price"
    for name in (field, "fees"):
        value = row.get(name)
        if value is not None and type(value) is not int:
            raise ValueError("Saved ordinary-equity order/fill money must retain integer fen")
    result.update(price_cny=row[field] / 100 if row.get(field) is not None else None,
        fees_cny=row["fees"] / 100 if row.get("fees") is not None else None,
        monetary_unit="fen", normalized_money_unit="CNY")
    if order:
        result["order_snapshot_basis"] = "final_saved_order_record_by_creation_day"
    return result


def _classifications(decision, blocks, committed):
    result = []
    labels = {"no_signal": "没有新增信号", "data_unknown": "资料未明确",
        "capacity_filtered": "累计名单容量过滤", "environment_blocked": "环境条件未通过",
        "order_blocked": "订单受阻"}
    def add(code, reason):
        row = {"code": code, "label": labels[code], "reason": reason}
        if row not in result:
            result.append(row)
    dropped = decision.get("cap_dropped_rows")
    if dropped is not None and dropped > 0:
        add("capacity_filtered", f"cap_dropped_rows={dropped}")
    for reason in decision.get("reasons", []):
        if "environment" in reason:
            add("environment_blocked", reason)
        elif "empty_or_capacity" in reason:
            add("capacity_filtered" if dropped else "no_signal", reason)
        elif "tail_no_new_entry" in reason or "empty" in reason:
            add("no_signal", reason)
        elif any(key in reason for key in ("unknown", "missing", "unavailable", "unsupported", "incomplete")):
            add("data_unknown", reason)
    if decision.get("environment_mode") == "EXIT_ALL":
        add("environment_blocked", "EXIT_ALL")
    if not committed:
        add("data_unknown", "no_saved_committed_daily_row")
    for event in blocks:
        add("order_blocked", event.get("reason") or event.get("error") or event.get("kind") or "saved_session_block")
    return result


def _detail(case, source, summary, built_at, source_refs, *, ablation=False):
    case_id = summary["case_id"]
    decisions = {r["day"]: r for r in case["decisions"]}
    daily = {r["date"]: r for r in case["daily"]}
    if len(decisions) != len(case["decisions"]) or len(daily) != len(case["daily"]):
        raise ValueError("Saved per-case decision or ledger dates are duplicated")
    orders, fills = _records_by_day(case["orders"]), _records_by_day(case["fills"])
    days = []
    for day in source["source_days"]:
        decision = deepcopy(decisions.get(day, {}))
        row = daily.get(day)
        environment_source = (source["control_environment_decisions"] if ablation and
            summary["guard"] == "two_close_entry_filter_only" else source["environment_decisions"])
        environment = deepcopy(environment_source.get(day, {}))
        type_policy = environment.pop("per_type", {}).get(summary["type"], {})
        environment["type_policy"] = type_policy
        environment["case_mode"] = decision.get("environment_mode")
        if ablation:
            original = source["environment_decisions"].get(day, {}).get("per_type", {}).get(summary["type"], {})
            environment.update(effective_mode=decision.get("environment_mode", type_policy.get("mode")),
                effective_reason=("unguarded_original_rule_environment_observation_only" if summary["guard"] == "unguarded"
                    else type_policy.get("control_reason", type_policy.get("reason"))),
                original_type_mode=original.get("mode"), original_type_reason=original.get("reason"),
                explicit_exit_suppressed=summary["guard"] == "two_close_entry_filter_only" and original.get("mode") == "EXIT_ALL")
        events = row.get("events", []) if row else []
        blocks = [deepcopy(e) for e in events if e.get("kind") in ("order_blocked", "rejected", "submit_rejected")]
        failure = case.get("blocking_evidence")
        if failure and failure.get("day") == day:
            blocks.append(deepcopy(failure))
        days.append({"day": day, "execution_day": decision.get("execution_day"),
            "in_decision_window": source["start"] <= day <= source["end"],
            "is_tail": day > source["end"], "committed_daily_row": row is not None,
            "environment": environment, "decision": decision,
            "raw_top5_count": len(decision["original_top5"]) if "original_top5" in decision else None,
            "cap_candidate_count": len(decision["cap_candidates"]) if "cap_candidates" in decision else None,
            "cap_dropped_count": decision.get("cap_dropped_rows"),
            "cash_cny": row.get("cash") if row else None,
            "equity_cny": row.get("equity") if row else None,
            "holdings_count": row.get("holdings") if row else None,
            "fees_cny": row.get("fees") if row else None,
            "position_quantities_available": False,
            "orders": [_money_record(r, order=True) for r in orders.get(day, [])],
            "fills": [_money_record(r, order=False) for r in fills.get(day, [])],
            "risk_assessments": [deepcopy(e) for e in events if e.get("kind") == "leader_type_risk_assessed"],
            "order_blocks": blocks,
            "classifications": _classifications(decision, blocks, row is not None)})
    return {"schema": "leader_eight_type_saved_history_case_v1", "case_id": case_id,
        "type": summary["type"], "guard": summary["guard"], "capacity": summary["capacity"],
        "built_at": built_at, "source_refs": deepcopy(source_refs), "summary": deepcopy(summary),
        "days": days, "position_quantities_available": False,
        "holdings_semantics": "daily_saved_holdings_count_only_not_a_per_symbol_position_snapshot",
        "risk_semantics": "original_emitted_facts_and_entry_anchor_not_complete_position_quantities",
        "orders_semantics": "terminal_saved_order_records_grouped_by_original_creation_day",
        "source_originals_rechecked_at_read": False, **FLAGS}


def _window_case_ids(window):
    if window.get("source_family") == KNOWN_SCOPE_FAMILY:
        if (window.get("case_ids") != list(KNOWN_SCOPE_CASE_IDS) or window.get("guards") != list(GUARDS)
                or window.get("types") != ["cross_regime_candidate"]
                or window.get("capacities") != [cap for cap, _ in CAPS] or window.get("case_count") != 4):
            raise ValueError("The explicit known-positive scope window requires its fixed four cases")
        return KNOWN_SCOPE_CASE_IDS
    if window.get("source_family") == ABLATION_FAMILY:
        if (window.get("case_ids") != list(ABLATION_CASE_IDS) or window.get("guards") != list(ABLATION_GUARDS)
                or window.get("case_count") != 48):
            raise ValueError("The explicit ablation window requires its fixed 48 cases and three guards")
        return ABLATION_CASE_IDS
    if window.get("source_family") is not None:
        raise ValueError("Unknown saved historical window family")
    return CASE_IDS


def build_ablation(ablation_result, reuse_projection, out):
    return _build_ablation_projection(ablation_result, reuse_projection, out)


def _reuse_projection_payloads(previous):
    """Copy already projected windows without reopening their market sources."""
    manifest, manifest_ref = _read(previous / "manifest.json")
    _boundary(manifest)
    if manifest.get("schema") not in {MANIFEST_SCHEMA, ABLATION_MANIFEST_SCHEMA}:
        raise ValueError("Reuse an existing compatible saved-history projection")
    index = _projected(previous, manifest["index_ref"])
    _boundary(index)
    schema = ABLATION_SCHEMA if manifest["schema"] == ABLATION_MANIFEST_SCHEMA else SCHEMA
    if index.get("schema") != schema or index.get("built_at") != manifest["built_at"]:
        raise ValueError("Saved projection index and manifest differ")
    windows, payloads = deepcopy(index["windows"]), []
    for window in windows:
        expected = _window_case_ids(window) if schema == ABLATION_SCHEMA else CASE_IDS
        if [row["case_id"] for row in window["case_rows"]] != list(expected):
            raise ValueError("Retained historical case mapping changed")
        old_built = window.get("projection_built_at", index["built_at"])
        window["projection_built_at"] = old_built
        for summary in window["case_rows"]:
            detail = _projected(previous, summary["detail_ref"])
            _boundary(detail)
            if (detail["built_at"] != old_built or detail["case_id"] != summary["case_id"]
                    or detail["summary"] != {key: value for key, value in summary.items() if key != "detail_ref"}):
                raise ValueError("Retained historical detail differs from its saved summary")
            payloads.append((Path(summary["detail_ref"]["path"]), detail))
    return index, manifest_ref, windows, payloads


def build_annual(annual_result, reuse_projection, out):
    """Append one saved native annual32 window using the existing reader schema.

    The explicit builder may read terminal result artifacts. GET remains a
    projection-only reader, including when the annual producer is still running.
    """
    from .leader_eight_type_panel_replay import SCHEMA as REPLAY_SCHEMA
    from .shortline_panel_replay import _config

    annual_result, previous, out = (Path(value).resolve() for value in (annual_result, reuse_projection, out))
    if out.exists():
        raise FileExistsError("Annual history projection requires a new generation directory")
    marker_path = annual_result.parent / "historical_method.json"
    if not annual_result.is_file() or not marker_path.is_file():
        return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "pending",
            "reason": "annual_terminal_result_and_historical_method_not_both_saved",
            "source_result": str(annual_result), "metrics": None, "projection_created": False, **FLAGS}
    source, source_ref = _read(annual_result)
    marker, marker_ref = _read(marker_path)
    _boundary(source)
    _boundary(marker)
    config = _config(100000, 5).to_dict()
    if (source.get("schema") != REPLAY_SCHEMA
            or source.get("status") != "completed_research_only_mechanical_comparison"
            or source.get("protocol") != PROTOCOL or source.get("config") != config
            or source.get("case_count") != 32
            or [row["case_id"] for row in source["cases"]] != list(CASE_IDS)
            or marker.get("schema") != "leader_eight_type_historical_native_result_v1"
            or marker.get("method_id") != "fixed_eight_types_observed_native_posthoc_listing_metadata_research_v1"
            or marker.get("original_32_protocol_unchanged") is not True or marker.get("cases") != 32
            or Path(marker["shared_engine_result"]).resolve() != annual_result):
        raise ValueError("One terminal unchanged native annual32 result and its matching method marker required")
    year, days = source["start"][:4], source["source_days"]
    decisions = [day for day in days if source["start"] <= day <= source["end"]]
    tails = [day for day in days if day > source["end"]]
    if (not days or days != sorted(set(days)) or days[0] != source["start"]
            or source["start"][5:7] != "01" or days[-1][5:7] != "12"
            or any(day[:4] != year for day in days) or source["end"][:4] != year or len(tails) != 6):
        raise ValueError("Independent annual source must retain its saved single-year window and six-session tail")
    old_index, old_manifest_ref, windows, payloads = _reuse_projection_payloads(previous)
    for original in (previous, annual_result.parent):
        if out == original or original in out.parents or out in original.parents:
            raise ValueError("New annual projection must remain separate from its saved sources")
    built_at = _now()
    window_id = "annual32_" + year
    if any(window["id"] == window_id for window in windows):
        raise ValueError("Independent annual history window already exists")
    summaries = []
    for case in source["cases"]:
        case_id = case["case_id"]
        kind, guard, capacity = case_id.split("__")
        if (case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, capacity) or case.get("config") != config:
            raise ValueError("Annual case identity or original costs changed")
        summary = {key: deepcopy(case[key]) for key in ("case_id", "type", "guard", "capacity",
            "status", "metrics", "residual_positions", "engine_sessions_committed", "complete_PnL_available")}
        summary.update(order_count=len(case["orders"]), fill_count=len(case["fills"]),
            source_result=source_ref["path"], origin="independent_native_annual32")
        payloads.append((Path("windows") / window_id / (case_id + ".json"),
            _detail(case, source, summary, built_at, [source_ref, marker_ref])))
        summaries.append(summary)
    windows.append({"id": window_id, "label": f"{year}年 · 独立年度（原32组，不与其他区间拼接）",
        "period_kind": "independent_annual", "independent_account": True,
        "equity_series_concatenated": False, "source_sessions": len(days),
        "source_scope_count": len(source["scope_members"]), "strict_identity_unknown_preserved": True,
        "case_ids": list(CASE_IDS), "guards": list(GUARDS), "case_count": 32,
        "projection_built_at": built_at, "start": source["start"], "end": source["end"],
        "decision_days": decisions, "tail_days": tails, "case_rows": summaries,
        "environment_counts": dict(Counter(source["environment_decisions"][day]["phase"] for day in decisions)),
        "source_mapping_preserved": True})
    refs = deepcopy(old_index["source_refs"]) + [old_manifest_ref, source_ref, marker_ref]
    out.mkdir(parents=True, exist_ok=False)
    details = {str(relative).replace("\\", "/"): _save(out / relative, detail) for relative, detail in payloads}
    for window in windows:
        for summary in window["case_rows"]:
            relative = f"windows/{window['id']}/{summary['case_id']}.json"
            summary["detail_ref"] = {"path": relative, "sha256": details[relative]["sha256"]}
    windows.sort(key=lambda window: (window["start"], window["id"]))
    # Existing running readers already accept this mixed-window schema and
    # source_family=None's original 32 cases; no new reader or frontend is needed.
    index = {"schema": ABLATION_SCHEMA, "status": "saved_history_projection", "built_at": built_at,
        "windows": windows, "source_refs": refs, "source_originals_rechecked_at_read": False,
        "semantics": "saved_independent_year_and_existing_windows_no_equity_concatenation", **FLAGS}
    index_ref = _save(out / "index.json", index)
    _save(out / "manifest.json", {"schema": ABLATION_MANIFEST_SCHEMA, "built_at": built_at,
        "index_ref": {"path": "index.json", "sha256": index_ref["sha256"]}, "source_refs": refs, **FLAGS})
    return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "saved_independent_annual_history_projection_built",
        "built_at": built_at, "directory": str(out), "windows": [window["id"] for window in windows],
        "case_count": len(payloads), "manifest_file": str(out / "manifest.json"), **FLAGS}


def build_native_supplement(supplement_result, reuse_projection, out):
    """Append the native32 supplement as its own window, preserving mixed origins."""
    from .leader_eight_type_action_supplement import METHOD, SPEC
    from .shortline_panel_replay import _config

    path, previous, out = (Path(value).resolve() for value in (supplement_result, reuse_projection, out))
    if out.exists():
        raise FileExistsError("Native supplement history requires a new projection generation")
    progress_path = path.parent / "progress.json"
    if not path.is_file() or not progress_path.is_file():
        return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "pending",
            "reason": "native_supplement_terminal_result_and_progress_not_both_saved",
            "source_result": str(path), "metrics": None, "projection_created": False, **FLAGS}
    source, source_ref = _read(path)
    progress, progress_ref = _read(progress_path)
    _boundary(source)
    if (source.get("schema") != METHOD + "_result" or source.get("method_id") != METHOD
            or source.get("spec") != SPEC or source.get("protocol") != PROTOCOL
            or source.get("config") != _config(100000, 5).to_dict()
            or source.get("case_count") != 32 or [case["case_id"] for case in source["cases"]] != list(CASE_IDS)
            or source.get("new_action_pair_scope") != "replayed_case_ids_only"
            or source.get("historical_execution_policy_scope") != "replayed_case_ids_only"
            or source.get("late_evidence_not_historical_PIT") is not True
            or source.get("original_results_modified") is not False):
        raise ValueError("An unchanged native32 source-only supplement result is required")
    if progress.get("status") != "completed":
        return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "pending",
            "reason": "native_supplement_operation_not_completed", "source_result": str(path),
            "metrics": None, "projection_created": False, **FLAGS}
    registration_ref = source["registration"]
    registration, actual_registration_ref = _read(registration_ref["path"])
    _boundary(registration)
    if (actual_registration_ref != registration_ref or progress.get("registration") != registration_ref
            or progress.get("method_id") != METHOD or progress.get("active_case") is not None
            or progress.get("result") != source_ref
            or [entry["case_id"] for entry in progress.get("completed_cases", [])] != list(CASE_IDS)
            or registration.get("schema") != METHOD + "_registration" or registration.get("method_id") != METHOD
            or registration.get("spec") != SPEC or registration.get("config") != source["config"]
            or registration.get("case_ids") != list(CASE_IDS)
            or source.get("source_action_supplement") != registration.get("binding")
            or source.get("original_result") != registration.get("original_result")):
        raise ValueError("Actual completed progress must bind the exact supplement registration and result")
    binding = registration["binding"]
    selected, reused = binding["replayed_case_ids"], binding["reused_case_ids"]
    if (not selected or len(set(selected)) != len(selected)
            or selected != [case_id for case_id in CASE_IDS if case_id in selected]
            or reused != [case_id for case_id in CASE_IDS if case_id not in selected]
            or source.get("replayed_case_count") != len(selected) or source.get("reused_case_count") != len(reused)):
        raise ValueError("Native supplement must retain its registered source-selected and reused case partition")
    days, year = source["source_days"], source["start"][:4]
    decisions = [day for day in days if source["start"] <= day <= source["end"]]
    tails = [day for day in days if day > source["end"]]
    if (not days or days != sorted(set(days)) or days[0] != source["start"]
            or any(day[:4] != year for day in days) or source["end"][:4] != year or len(tails) != 6):
        raise ValueError("Native annual supplement must preserve the saved year and six-session tail")
    for original in (previous, path.parent):
        if out == original or original in out.parents or out in original.parents:
            raise ValueError("New supplement projection must remain separate from its sources")
    old_index, old_manifest_ref, windows, payloads = _reuse_projection_payloads(previous)
    window_id, built_at = "native_action_supplement_" + year, _now()
    if any(window["id"] == window_id for window in windows):
        raise ValueError("Native source-action supplement window already exists")
    refs = [source_ref, progress_ref, registration_ref]
    summaries = []
    for case in source["cases"]:
        case_id = case["case_id"]
        kind, guard, capacity = case_id.split("__")
        origin, replayed = case["source_origin"], case_id in selected
        expected_pair = {key: registration[key]["directory"] if replayed else
                         binding["original_pair"][key]["directory"]
                         for key in ("action_event_view", "action_reference_view")}
        accounting = (case.get("historical_execution_policy") or {}).get("accounting") or {}
        if ((case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, capacity)
                or case.get("config") != source["config"] or origin.get("mode") != ("replayed" if replayed else "reused")
                or origin.get("original_case_id") != case_id or origin.get("original_result") != source["original_result"]
                or origin.get("replayed_from_original_cash") is not replayed
                or origin.get("checkpoint_recovery_performed") is not False
                or any(Path(origin["action_pair"][key]).resolve() != Path(value).resolve()
                       for key, value in expected_pair.items())
                or Path(accounting.get("event_view", "")).resolve() != Path(expected_pair["action_event_view"]).resolve()
                or Path(accounting.get("reference_view", "")).resolve() != Path(expected_pair["action_reference_view"]).resolve()
                or (replayed and origin.get("committed_prefix_check", {}).get("status") != "exact_original_committed_prefix_preserved")
                or (case.get("complete_PnL_available") is not True and case.get("metrics") is not None)):
            raise ValueError("Native supplement case must preserve its own source origin and unavailable metrics")
        summary = {key: deepcopy(case[key]) for key in ("case_id", "type", "guard", "capacity", "status",
            "metrics", "residual_positions", "engine_sessions_committed", "complete_PnL_available")}
        summary.update(order_count=len(case["orders"]), fill_count=len(case["fills"]),
            source_result=source_ref["path"] if replayed else origin["original_result"]["path"],
            container_source_result=source_ref["path"], origin="native_action_supplement" if replayed else "reused_original_native32",
            source_supplement=deepcopy(origin), own_action_accounting_pair=deepcopy(origin["action_pair"]))
        detail = _detail(case, source, summary, built_at, refs + ([] if replayed else [origin["original_result"]]))
        detail["source_supplement"] = deepcopy(origin)
        payloads.append((Path("windows") / window_id / (case_id + ".json"), detail))
        summaries.append(summary)
    windows.append({"id": window_id, "label": f"{year}年 · 同样本后见补证（重放{len(selected)}／复用{len(reused)}，非32个新独立账户）",
        "period_kind": "same_sample_posthoc_source_supplement", "independent_account": False,
        "source_supplement_counts": {"replayed": len(selected), "reused": len(reused)},
        "new_action_pair": deepcopy(source["new_action_pair"]), "new_action_pair_scope": "replayed_case_ids_only",
        "equity_series_concatenated": False, "source_sessions": len(days), "source_scope_count": len(source["scope_members"]),
        "strict_identity_unknown_preserved": True, "case_ids": list(CASE_IDS), "guards": list(GUARDS), "case_count": 32,
        "projection_built_at": built_at, "start": source["start"], "end": source["end"],
        "decision_days": decisions, "tail_days": tails, "case_rows": summaries,
        "environment_counts": dict(Counter(source["environment_decisions"][day]["phase"] for day in decisions)),
        "source_mapping_preserved": True, "late_evidence_not_historical_PIT": True})
    refs = deepcopy(old_index["source_refs"]) + [old_manifest_ref, *refs]
    out.mkdir(parents=True, exist_ok=False)
    details = {str(relative).replace("\\", "/"): _save(out / relative, detail) for relative, detail in payloads}
    for window in windows:
        for summary in window["case_rows"]:
            relative = f"windows/{window['id']}/{summary['case_id']}.json"
            summary["detail_ref"] = {"path": relative, "sha256": details[relative]["sha256"]}
    windows.sort(key=lambda window: (window["start"], window["id"]))
    index = {"schema": ABLATION_SCHEMA, "status": "saved_history_projection", "built_at": built_at,
        "windows": windows, "source_refs": refs, "source_originals_rechecked_at_read": False,
        "semantics": "saved_original_windows_plus_same_sample_native_source_supplement_no_equity_concatenation", **FLAGS}
    index_ref = _save(out / "index.json", index)
    _save(out / "manifest.json", {"schema": ABLATION_MANIFEST_SCHEMA, "built_at": built_at,
        "index_ref": {"path": "index.json", "sha256": index_ref["sha256"]}, "source_refs": refs, **FLAGS})
    return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "saved_native_supplement_history_projection_built",
        "built_at": built_at, "directory": str(out), "windows": [window["id"] for window in windows],
        "case_count": len(payloads), "manifest_file": str(out / "manifest.json"), **FLAGS}


def _pinned_source(reference):
    value, actual = _read(reference["path"])
    if actual != reference:
        raise ValueError("Registered history source pin changed")
    return value, actual


def _case_pair(case):
    accounting = (case.get("historical_execution_policy") or {}).get("accounting") or {}
    return {"action_event_view": accounting.get("event_view"),
            "action_reference_view": accounting.get("reference_view")}


def _case_summary(case, source_ref, origin):
    kind, guard, cap = case["case_id"].split("__")
    from .shortline_panel_replay import _config
    if ((case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, cap)
            or case.get("config") != _config(100000, 5).to_dict()
            or (case.get("complete_PnL_available") is not True and case.get("metrics") is not None)):
        raise ValueError("Saved case identity, original costs or unavailable metrics changed")
    row = {key: deepcopy(case[key]) for key in ("case_id", "type", "guard", "capacity", "status",
        "metrics", "residual_positions", "engine_sessions_committed", "complete_PnL_available")}
    row.update(source_result=source_ref["path"], origin=origin, order_count=len(case["orders"]),
        fill_count=len(case["fills"]), blocking_evidence=deepcopy(case.get("blocking_evidence")),
        source_case_flags={key:value for key,value in case.items() if type(value) is bool},
        own_action_accounting_pair=_case_pair(case))
    return row


def _append_saved_layer(previous, out, source_folder, window, payloads, source_refs, *, semantics, status):
    """Use the existing projection format and copy prior case payloads verbatim."""
    return _append_saved_layers(previous, out, [source_folder], [window], payloads, source_refs,
        semantics=semantics, status=status)


def _append_saved_layers(previous, out, source_folders, new_windows, payloads, source_refs, *, semantics, status):
    """Append explicit independent saved layers; retain each old detail's bytes."""
    for original in (previous, *source_folders):
        if out == original or original in out.parents or out in original.parents:
            raise ValueError("New history layer must remain separate from its saved sources")
    old_index, old_manifest_ref, windows, old_payloads = _reuse_projection_payloads(previous)
    ids = [row["id"] for row in [*windows, *new_windows]]
    if len(ids) != len(set(ids)):
        raise ValueError("Independent saved history layer already exists")
    windows.extend(new_windows)
    refs = [*deepcopy(old_index["source_refs"]), old_manifest_ref, *source_refs]
    payloads = old_payloads + payloads
    out.mkdir(parents=True, exist_ok=False)
    details = {str(relative).replace("\\", "/"):_save(out/relative,detail) for relative,detail in payloads}
    for saved_window in windows:
        for summary in saved_window["case_rows"]:
            relative = f"windows/{saved_window['id']}/{summary['case_id']}.json"
            summary["detail_ref"] = {"path":relative,"sha256":details[relative]["sha256"]}
    windows.sort(key=lambda row:(row["start"],row["id"]))
    built = new_windows[-1]["projection_built_at"]
    index = {"schema":ABLATION_SCHEMA,"status":"saved_history_projection","built_at":built,
        "windows":windows,"source_refs":refs,"source_originals_rechecked_at_read":False,
        "semantics":semantics,**FLAGS}
    index_ref = _save(out/"index.json",index)
    _save(out/"manifest.json",{"schema":ABLATION_MANIFEST_SCHEMA,"built_at":built,
        "index_ref":{"path":"index.json","sha256":index_ref["sha256"]},"source_refs":refs,**FLAGS})
    return {"schema":ABLATION_MANIFEST_SCHEMA,"status":status,"built_at":built,
        "directory":str(out),"windows":[row["id"] for row in windows],"case_count":len(payloads),
        "manifest_file":str(out/"manifest.json"),**FLAGS}


def _layer_window(source, scope, window_id, label, built, summaries, **extra):
    days = source["source_days"]
    if (not days or days != sorted(set(days)) or source["start"] not in days or source["end"] not in days):
        raise ValueError("Saved source layer must retain its exact ordered dates")
    decisions = [day for day in days if source["start"] <= day <= source["end"]]
    return {"id":window_id,"label":label,"start":source["start"],"end":source["end"],
        "case_ids":list(CASE_IDS),"guards":list(GUARDS),"case_count":32,
        "independent_account":False,"equity_series_concatenated":False,
        "projection_built_at":built,"source_sessions":len(days),"source_scope_count":len(scope),
        "decision_days":decisions,"tail_days":[day for day in days if day>source["end"]],
        "environment_counts":dict(Counter(source["environment_decisions"][day]["phase"] for day in decisions)),
        "case_rows":summaries,"source_mapping_preserved":True,**extra}


def _known_scope_layer(path):
    """Project a terminal four-case continuation without reopening raw/panels."""
    from . import leader_known_identity_scope as scope
    from . import leader_known_identity_continue as continuation
    source, source_ref = _read(path)
    _boundary(source)
    if (source.get("schema") != continuation.SCHEMA or source.get("method_id") != continuation.METHOD_ID
            or source.get("spec") != continuation.SPEC or source.get("entry_information_policy") != scope.SPEC
            or source.get("parent_fixed_protocol") != PROTOCOL or source.get("case_count") != 4
            or [case["case_id"] for case in source.get("cases", [])] != list(KNOWN_SCOPE_CASE_IDS)
            or any(source.get(key) is not False for key in ("cash_reset", "old_committed_sessions_replayed",
                "old_accounts_modified", "independent_holdout_claimed", "returns_reportable"))):
        raise ValueError("Exact terminal known-positive four-case continuation required")
    registration, registration_ref = _pinned_source({"path": source["registration"],
        "sha256": source["registration_sha256"]})
    _boundary(registration)
    if (registration.get("schema") != continuation.REGISTRATION_SCHEMA
            or registration.get("method_id") != continuation.METHOD_ID
            or registration.get("spec") != continuation.SPEC
            or registration.get("parent_result") != source["parent_result"]
            or registration.get("origin_registration") != source["origin_registration"]
            or registration.get("action_pair") != source["action_pair"]
            or registration.get("binding") != source["source_continuation"]):
        raise ValueError("Saved known-positive continuation registration changed")
    parent, parent_ref = _pinned_source(source["parent_result"])
    original, original_ref = _pinned_source(source["origin_registration"])
    _boundary(parent)
    _boundary(original)
    if (parent.get("schema") != scope.SCHEMA or parent.get("method_id") != scope.METHOD_ID
            or parent.get("entry_information_policy") != scope.SPEC or parent.get("case_count") != 4
            or [case["case_id"] for case in parent.get("cases", [])] != list(KNOWN_SCOPE_CASE_IDS)
            or parent.get("registration") != original_ref["path"]
            or parent.get("registration_sha256") != original_ref["sha256"]
            or original.get("schema") != scope.REGISTRATION_SCHEMA or original.get("method_id") != scope.METHOD_ID
            or original.get("policy") != scope.SPEC or original.get("parent_fixed_protocol") != PROTOCOL
            or original.get("case_ids") != list(KNOWN_SCOPE_CASE_IDS)
            or any(source.get(key) != parent.get(key) for key in ("source", "start", "end", "source_days",
                "scope_members", "environment_decisions", "raw_top5_paths", "capacity_paths"))):
        raise ValueError("Original known-positive policy, source dates or saved paths changed")
    inputs, inputs_ref = _pinned_source({"path": original["inputs"], "sha256": original["inputs_sha256"]})
    if (source["source"].get("manifest") != inputs_ref["path"]
            or source["source"].get("manifest_sha256") != inputs_ref["sha256"]
            or source["source"].get("input_record") != inputs):
        raise ValueError("Known-positive saved input descriptor changed")
    progress, progress_ref = _read(path.parent / "progress.json")
    selected, reused = (registration["binding"][key] for key in ("continued_case_ids", "reused_case_ids"))
    if (progress.get("status") != "completed" or progress.get("schema") != continuation.SCHEMA
            or progress.get("result") != str(path) or progress.get("continued_case_ids") != selected
            or set(selected) & set(reused) or set(selected) | set(reused) != set(KNOWN_SCOPE_CASE_IDS)
            or source.get("continued_case_count") != len(selected) or source.get("reused_case_count") != len(reused)
            or source.get("new_cases_executed") != 0
            or progress.get("completed_cases") != [{"case_id": case_id, **source["case_origins"][case_id]}
                for case_id in KNOWN_SCOPE_CASE_IDS]):
        raise ValueError("Completed continuation must retain all four exact case origins")
    refs = [source_ref, progress_ref, registration_ref, parent_ref, original_ref, inputs_ref]
    for pin in source["action_pair"].values():
        _, result_ref = _pinned_source({"path": pin["result"], "sha256": pin["sha256"]})
        refs.append(result_ref)
    parent_cases = {case["case_id"]: case for case in parent["cases"]}
    summaries, payloads = [], []
    built, window_id = _now(), "known_positive_crossing_" + source["start"] + "_" + source["end"]
    for case in source["cases"]:
        case_id = case["case_id"]
        old, layer = parent_cases[case_id], source["case_origins"][case_id]
        continued = case_id in selected
        old_n, new_n = old["engine_sessions_committed"], case["engine_sessions_committed"]
        own_pair = ({"action_event_view": source["action_pair"]["event"]["directory"],
            "action_reference_view": source["action_pair"]["reference"]["directory"]} if continued else _case_pair(old))
        if (layer.get("parent_result") != parent_ref or layer.get("continued_from_checkpoint") is not continued
                or layer.get("reused_original_case") is not (not continued)
                or layer.get("old_committed_sessions_replayed") is not False
                or layer.get("reused_sessions") != old_n or layer.get("newly_completed_sessions") != new_n - old_n
                or layer.get("prior_blocking_evidence") != old.get("blocking_evidence")
                or case["daily"][:old_n] != old["daily"] or case["decisions"][:old_n] != old["decisions"]
                or len(case["daily"]) != new_n or len(case["decisions"]) != new_n
                or _case_pair(case) != own_pair or (not continued and case != old)):
            raise ValueError("Continuation altered a reused case, committed prefix or own source origin")
        if continued and any(case.get(key) is not value for key, value in {
                "old_committed_sessions_replayed": False, "cash_reset": False,
                "original_context_preserved": True, "committed_prefix_preserved": True}.items()):
            raise ValueError("Continued case must preserve its committed account context")
        summary = _case_summary(case, source_ref, "continued_saved_checkpoint" if continued else "reused_original_case")
        summary["source_continuation"] = {**deepcopy(layer), "cash_reset": False,
            "own_action_accounting_pair": deepcopy(own_pair)}
        detail = _detail(case, source, summary, built, refs, ablation=True)
        detail["source_continuation"] = deepcopy(summary["source_continuation"])
        summaries.append(summary)
        payloads.append((Path("windows") / window_id / (case_id + ".json"), detail))
    limitations = ["本窗口只含穿越候选四个研究账户，不是原八型32组的替代结果。",
        "同一历史样本事后研究；资格、行业身份、公司行动及当时可得性仍未完整验证。",
        "各时期来源和范围不同，不能当作同源未看样本，也不拼接净值。",
        "各环境和名单规模结果全部保留；相同股票跨账户不构成独立样本，不按盈利挑选版本。",
        "完整模拟收益不代表策略已验证；未启用策略选择或真实交易。"]
    window = _layer_window(source, source["scope_members"], window_id,
        f"{source['start']} 至 {source['end']} · 已知身份穿越候选（独立4组，原32组保留）", built, summaries,
        source_family=KNOWN_SCOPE_FAMILY, case_ids=list(KNOWN_SCOPE_CASE_IDS), case_count=4,
        types=[scope.TYPE_ID], capacities=[cap for cap, _ in CAPS],
        period_kind="same_sample_known_positive_information_policy", independent_account=True,
        entry_information_policy=deepcopy(scope.SPEC), limitations=limitations,
        source_continuation_counts={"continued": len(selected), "reused": len(reused),
            "reused_sessions": sum(origin["reused_sessions"] for origin in source["case_origins"].values()),
            "newly_completed_sessions": sum(origin["newly_completed_sessions"] for origin in source["case_origins"].values())},
        original_registration=original_ref, late_evidence_not_historical_PIT=True,
        all_cases_complete_PnL=source.get("all_cases_complete_PnL"))
    return window, payloads, refs


def build_known_scope_continuations(continuation_results, reuse_projection, out):
    """Add explicit saved four-case periods to the existing history browser."""
    paths = [Path(value).resolve() for value in continuation_results]
    previous, out = Path(reuse_projection).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Known-positive history requires a new projection generation")
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Explicit unique saved known-positive continuation results required")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        return {"status": "pending", "reason": "known_positive_terminal_result_not_saved",
            "source_results": missing, "projection_created": False, "metrics": None, **FLAGS}
    windows, payloads, refs = [], [], []
    for path in paths:
        window, details, sources = _known_scope_layer(path)
        windows.append(window)
        payloads.extend(details)
        refs.extend(sources)
    return _append_saved_layers(previous, out, [path.parent for path in paths], windows, payloads, refs,
        semantics="saved_windows_plus_known_positive_four_case_periods_no_replay_or_equity_concatenation",
        status="saved_known_positive_scope_history_projection_built")


def build_case_annual(case_result, reuse_projection, out):
    """Append a terminal durable fixed32 year without reopening its market inputs."""
    return _build_case_period(case_result, reuse_projection, out)


def build_case_partial_year(case_result, partial_year_registration, reuse_projection, out):
    """Append the explicitly registered 2023 within-year study, never a full year."""
    return _build_case_period(case_result, reuse_projection, out,
        partial_year_registration=partial_year_registration)


def _partial_year_metadata(manifest, source, registration, source_registration):
    from .leader_partial_year_inputs import METHOD_ID, SPEC, _schedule
    from .leader_eight_type_historical_native import FLAGS as SOURCE_FLAGS

    partial, partial_ref = _read(source_registration)
    _boundary(partial)
    if (partial.get("schema") != METHOD_ID + "_registration" or partial.get("method_id") != METHOD_ID
            or partial.get("spec") != SPEC or partial.get("protocol") != PROTOCOL
            or partial.get("case_ids") != list(CASE_IDS) or partial.get("config") != source["config"]
            or manifest.get("partial_year_source_registration") != partial_ref
            or manifest.get("partial_year_source_method") != METHOD_ID
            or manifest.get("partial_year_semantics") != SPEC
            or any(partial.get(key) is not value for key, value in SOURCE_FLAGS.items())):
        raise ValueError("Exact registered partial-year spec, original manifest pin and fixed32 method required")
    calendar_ref = partial["source_pins"]["calendar"]
    calendar, calendar_ref = _pinned_source(calendar_ref)
    sessions, warmup, days = _schedule(calendar, partial["all_official_year_sessions"])
    if (source["start"] != SPEC["start"] or source["end"] != SPEC["end"]
            or manifest.get("through") != SPEC["through"] or source["source_days"] != days
            or partial.get("source_days") != days or partial.get("warmup_source_days") != warmup
            or manifest.get("warmup_source_days") != warmup
            or manifest.get("calendar") != calendar_ref["path"]
            or manifest.get("calendar_sha256") != calendar_ref["sha256"]
            or partial.get("all_official_year_sessions") != sessions
            or partial["source_pins"].get("action_event") != registration["action_event_view"]
            or partial["source_pins"].get("action_reference") != registration["action_reference_view"]
            or Path(partial["action_event_view"]).resolve() != Path(registration["action_event_view"]["path"]).parent
            or Path(partial["action_reference_view"]).resolve() != Path(registration["action_reference_view"]["path"]).parent):
        raise ValueError("Registered 2023 partial-year dates, warmup, exact calendar or original action pair changed")
    clocks = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in
        (partial["registered_at"], manifest["created_at"], registration["registered_at"])]
    if any(value.utcoffset() is None for value in clocks) or clocks != sorted(clocks):
        raise ValueError("Partial-year source registration must precede source generation and case registration")
    return partial_ref, calendar_ref, SPEC


def _build_case_period(case_result, reuse_projection, out, *, partial_year_registration=None):
    """Shared saved durable32 adapter; annual and explicit partial ranges stay distinct."""
    from .leader_eight_type_case_replay import METHOD
    from .leader_eight_type_historical_native import INPUT_SCHEMA, METHOD_ID, FLAGS as SOURCE_FLAGS
    from .shortline_panel_replay import _config

    path, previous, out = (Path(value).resolve() for value in (case_result,reuse_projection,out))
    if out.exists():
        raise FileExistsError("Durable annual history requires a new projection generation")
    progress_path = path.parent/"progress.json"
    if not path.is_file() or not progress_path.is_file():
        return {"status":"pending","reason":"durable_annual_result_and_progress_not_both_saved",
            "source_result":str(path),"metrics":None,"projection_created":False,**FLAGS}
    source, source_ref = _read(path)
    progress, progress_ref = _read(progress_path)
    _boundary(source)
    if progress.get("status") != "completed":
        return {"status":"pending","reason":"durable_annual_operation_not_completed",
            "source_result":str(path),"metrics":None,"projection_created":False,**FLAGS}
    registration, registration_ref = _pinned_source(source["registration"])
    _boundary(registration)
    config = _config(100000,5).to_dict()
    if (source.get("schema") != METHOD+"_result" or source.get("method_id") != METHOD
            or source.get("protocol") != PROTOCOL or source.get("config") != config
            or source.get("case_count") != 32 or [row["case_id"] for row in source["cases"]] != list(CASE_IDS)
            or source.get("single_case_session_checkpoint_supported") is not False
            or registration.get("schema") != METHOD+"_registration" or registration.get("method_id") != METHOD
            or registration.get("protocol") != PROTOCOL or registration.get("config") != config
            or registration.get("case_ids") != list(CASE_IDS)
            or type(registration.get("check_entry_readiness")) is not bool
            or registration.get("same_case_runner") != "leader_eight_type_panel_replay._case"
            or registration.get("session_checkpoint_supported") is not False
            or registration.get("case_selection_by_return") is not False
            or progress.get("schema") != METHOD+"_progress" or progress.get("method_id") != METHOD
            or progress.get("registration") != registration_ref or progress.get("result") != source_ref
            or progress.get("run_directory") != str(path.parent) or progress.get("active_case") is not None
            or progress.get("case_ids") != list(CASE_IDS)
            or [row["case_id"] for row in progress.get("completed_cases",[])] != list(CASE_IDS)
            or any(record.get(key) is not value for record in (source,registration)
                   for key,value in SOURCE_FLAGS.items())):
        raise ValueError("One exact terminal registered durable fixed32 result and progress required")
    manifest, manifest_ref = _pinned_source(registration["inputs"])
    provenance = source.get("source",{})
    payload_keys = ("raw_file","raw_sha256","panels_file","panels_sha256",
                    "context_raw_file","context_raw_sha256","calendar","calendar_sha256")
    if (manifest.get("schema") != INPUT_SCHEMA or manifest.get("method_id") != METHOD_ID
            or manifest.get("fixed_protocol") != PROTOCOL
            or provenance.get("method_id") != METHOD_ID or provenance.get("historical_input_schema") != INPUT_SCHEMA
            or provenance.get("manifest") != manifest_ref["path"]
            or provenance.get("manifest_sha256") != manifest_ref["sha256"] or provenance.get("input_record") != manifest
            or set(registration.get("input_payload_pins",{})) != set(payload_keys)
            or any(registration["input_payload_pins"][key] != manifest.get(key) for key in payload_keys)
            or any(key not in manifest or source.get(key) != manifest[key]
                   for key in ("start","end","source_days","scope_members"))
            or any(manifest.get(key) is not value for key,value in SOURCE_FLAGS.items())):
        raise ValueError("Durable annual source manifest, original sample or payload pins changed")
    year, days = source["start"][:4], source["source_days"]
    partial_metadata = None
    if partial_year_registration is None:
        if (not days or days != sorted(set(days)) or days[0] != source["start"]
                or source["start"][5:7] != "01" or days[-1][5:7] != "12"
                or any(day[:4] != year for day in days) or source["end"][:4] != year
                or manifest.get("through") != days[-1] or len([day for day in days if day>source["end"]]) != 6):
            raise ValueError("Durable annual source must retain one saved year and its original six-session tail")
    else:
        partial_metadata = _partial_year_metadata(manifest, source, registration, partial_year_registration)
    preparation, preparation_ref = _pinned_source(source["preparation"])
    if (progress.get("preparation") != preparation_ref or preparation.get("registration") != registration_ref
            or preparation.get("environment_decisions") != source.get("environment_decisions")):
        raise ValueError("Durable annual saved preparation or environment changed")
    event, event_ref = _pinned_source(registration["action_event_view"])
    reference, reference_ref = _pinned_source(registration["action_reference_view"])
    event_view, reference_view = Path(event_ref["path"]).parent, Path(reference_ref["path"]).parent
    if (Path(event_ref["path"]).name != "result.json" or Path(reference_ref["path"]).name != "result.json"
            or event.get("status") != "research_action_events_projected"
            or reference.get("status") != "research_reference_presence_diagnostic"
            or event.get("execution_admitted") is not False or reference.get("execution_admitted") is not False
            or Path(reference["event_view"]).resolve() != event_view):
        raise ValueError("Durable annual registered action pair changed")
    refs = [source_ref,progress_ref,registration_ref,manifest_ref,preparation_ref,event_ref,reference_ref]
    if partial_metadata:
        refs.extend(partial_metadata[:2])
    built = _now()
    window_id = ("partial_year32_" + source["start"] + "_" + source["end"]
        if partial_metadata else "annual32_" + year)
    summaries, payloads = [], []
    for case in source["cases"]:
        summary = _case_summary(case,source_ref,("independent_durable_native_partial_year32"
            if partial_metadata else "independent_durable_native_annual32"))
        if case.get("historical_execution_policy") is not None and _case_pair(case) != {
                "action_event_view":str(event_view),"action_reference_view":str(reference_view)}:
            raise ValueError("Durable annual case's actual action pair differs from its registration")
        payloads.append((Path("windows")/window_id/(case["case_id"]+".json"),
                         _detail(case,source,summary,built,refs)))
        summaries.append(summary)
    label = (f"{source['start']} 至 {source['end']} · 独立年内区间（非全年，原32组，保留6日尾期）"
        if partial_metadata else f"{year}年 · 独立年度（原32组，逐case保存，不与其他区间拼接）")
    window = _layer_window(source,source["scope_members"],window_id,label,built,summaries,
        period_kind="independent_partial_year" if partial_metadata else "independent_annual",
        independent_account=True,strict_identity_unknown_preserved=True,
        producer_method_id=METHOD,original_32_protocol_unchanged=True,
        registration=registration_ref,check_entry_readiness=registration["check_entry_readiness"],
        source_skipped_case_count=source.get("skipped_case_count",0),
        source_all_cases_complete_PnL=source.get("all_cases_complete_PnL"),
        source_progress_errors=deepcopy(progress.get("errors",[])),single_case_session_checkpoint_supported=False)
    if partial_metadata:
        partial_ref, calendar_ref, spec = partial_metadata
        window.update(partial_year_source_registration=partial_ref, partial_year_semantics=deepcopy(spec),
            full_year_claimed=False, warmup_sessions=spec["warmup_sessions"])
    return _append_saved_layer(previous,out,path.parent,window,payloads,refs,
        semantics=("saved_registered_partial_year_and_existing_windows_no_equity_concatenation"
            if partial_metadata else "saved_independent_durable_year_and_existing_windows_no_equity_concatenation"),
        status=("saved_durable_partial_year_history_projection_built"
            if partial_metadata else "saved_durable_annual_history_projection_built"))


def build_identity_overlay(identity_result, reuse_projection, out):
    """Expose the registered new eight and exact parent's retained twenty-four."""
    from .leader_historical_identity_inputs import METHOD_ID, REGISTRATION_SCHEMA, TYPES, REUSED_EIGHT_EXECUTION
    from .leader_historical_identity_rules import SPEC
    path, previous, out = (Path(value).resolve() for value in (identity_result,reuse_projection,out))
    if out.exists():
        raise FileExistsError("Identity overlay history requires a new projection generation")
    if not path.is_file():
        return {"status":"pending","reason":"identity_overlay_terminal_result_not_saved",
                "source_result":str(path),"metrics":None,"projection_created":False,**FLAGS}
    source, source_ref = _read(path)
    _boundary(source)
    expected = [case_id for case_id in CASE_IDS if case_id.split("__")[0] in TYPES]
    if (source.get("schema") != "leader_historical_identity_eight_case_shared_replay_v1"
            or source.get("method_id") != METHOD_ID or source.get("parent_fixed_protocol") != PROTOCOL
            or source.get("executed_type_ids") != list(TYPES) or source.get("case_count") != len(expected)
            or [case["case_id"] for case in source["cases"]] != expected
            or source.get("case_selection_by_performance") is not False
            or source.get("shared_case_function") != "leader_eight_type_panel_replay._case"):
        raise ValueError("Exact registered two-identity eight-case result required")
    provenance = source["source"]
    manifest, manifest_ref = _pinned_source({"path":provenance["manifest"],"sha256":provenance["manifest_sha256"]})
    if provenance.get("input_record") != manifest:
        raise ValueError("Identity result source manifest differs from its saved exact descriptor")
    descriptor = manifest["identity_research"]
    registration, registration_ref = _pinned_source({"path":descriptor["registration"],"sha256":descriptor["registration_sha256"]})
    _boundary(registration)
    base, base_ref = _pinned_source({"path":registration["base_manifest"],"sha256":registration["base_manifest_sha256"]})
    reused = source["unchanged_24_cases_reused_from"]
    parent_ref = {"path":reused["result"],"sha256":reused["sha256"]}
    parent, parent_ref = _pinned_source(parent_ref)
    _boundary(parent)
    if (descriptor.get("method_id") != METHOD_ID or descriptor.get("changed_types") != list(TYPES)
            or descriptor.get("historical_publication_backdated") is not False
            or descriptor.get("old_six_type_observations_modified") is not False
            or descriptor.get("parent_manifest") != base_ref["path"] or descriptor.get("parent_manifest_sha256") != base_ref["sha256"]
            or registration.get("schema") != REGISTRATION_SCHEMA or registration.get("method_id") != METHOD_ID
            or registration.get("case_execution") != REUSED_EIGHT_EXECUTION or registration.get("rules") != SPEC
            or registration.get("changed_types") != list(TYPES) or registration.get("fixed_protocol") != PROTOCOL
            or registration.get("standalone") is True or registration.get("all32") is True
            or registration.get("baseline_result") != parent_ref["path"] or registration.get("baseline_result_sha256") != parent_ref["sha256"]
            or reused.get("input_reuse_evidence") != registration.get("baseline_reuse_evidence")
            or parent.get("case_count") != 32 or parent.get("protocol") != PROTOCOL
            or [case["case_id"] for case in parent["cases"]] != list(CASE_IDS)
            or any(parent[key] != source[key] or manifest[key] != source[key] for key in ("start","end","source_days"))
            or parent.get("scope_members") != manifest.get("scope_members")
            or base.get("identity_research") is not None):
        raise ValueError("Identity registration/source or exact reused twenty-four parent binding changed")
    refs = [source_ref,manifest_ref,registration_ref,base_ref,parent_ref]
    if registration.get("baseline_input_relation") == "exact_same_parent":
        if (parent.get("source",{}).get("manifest") != base_ref["path"]
                or parent.get("source",{}).get("manifest_sha256") != base_ref["sha256"]):
            raise ValueError("Reused twenty-four cases lost their exact original input parent")
    elif registration.get("baseline_input_relation") == "explicit_prior_saved_input_reuse":
        proof, proof_ref = _pinned_source({"path":registration["baseline_reuse_evidence"],
                                          "sha256":registration["baseline_reuse_evidence_sha256"]})
        required = {"raw_sha256","context_raw_sha256","scope_members","start","end","through",
                    "source_days","calendar_sha256","fixed_protocol"}
        if (proof.get("v3_engine_run_performed") is not False or proof.get("v2_actual_result") != parent_ref["path"]
                or base_ref["path"] not in proof.get("manifests",[]) or not required<=set(proof.get("pins_equal",{}))
                or not all(proof["pins_equal"].values())):
            raise ValueError("Explicit parent-input reuse evidence changed")
        refs.append(proof_ref)
    else:
        raise ValueError("Unknown identity-overlay parent reuse contract")
    for key in ("raw_sha256","context_raw_sha256","scope_members","start","end","through","source_days","calendar_sha256","fixed_protocol"):
        if manifest.get(key) != base.get(key):
            raise ValueError("Identity overlay changed original sample or price inputs: "+key)
    identity_cases = {case["case_id"]:case for case in source["cases"]}
    parent_cases = {case["case_id"]:case for case in parent["cases"]}
    built, window_id = _now(), "identity_overlay_"+source["start"][:4]
    summaries, payloads = [], []
    for case_id in CASE_IDS:
        executed = case_id in identity_cases
        case, own_source, own_ref = ((identity_cases[case_id],source,source_ref) if executed else
                                     (parent_cases[case_id],parent,parent_ref))
        summary = _case_summary(case,own_ref,"new_identity_overlay" if executed else "reused_parent_24")
        summary["identity_overlay"] = {"executed":executed,"reused":not executed,
            "original_registration":registration_ref,"source_manifest":manifest_ref if executed else base_ref,
            "unchanged_24_cases_reused_from":deepcopy(reused),"new_independent_all32_claimed":False}
        detail = _detail(case,own_source,summary,built,refs)
        detail["identity_overlay"] = deepcopy(summary["identity_overlay"])
        summaries.append(summary)
        payloads.append((Path("windows")/window_id/(case_id+".json"),detail))
    window = _layer_window(source,manifest["scope_members"],window_id,
        f"{source['start'][:4]}年 · 官方宽行业身份观察（新执行8／复用24，非32个新独立账户）",built,summaries,
        period_kind="same_sample_registered_identity_overlay",identity_execution_counts={"executed":8,"reused":24},
        executed_type_ids=list(TYPES),late_evidence_not_historical_PIT=True)
    return _append_saved_layer(previous,out,path.parent,window,payloads,refs,
        semantics="saved_windows_plus_registered_identity8_and_exact_parent24_no_equity_concatenation",
        status="saved_identity_overlay_history_projection_built")


def build_secondary_supplement(secondary_result, reuse_projection, out):
    """Append the independent secondary layer; latest per-case origin wins."""
    from .leader_eight_type_secondary_supplement import METHOD, SPEC
    from .shortline_panel_replay import _config
    path, previous, out = (Path(value).resolve() for value in (secondary_result,reuse_projection,out))
    if out.exists():
        raise FileExistsError("Secondary history requires a new projection generation")
    progress_path = path.parent/"progress.json"
    if not path.is_file() or not progress_path.is_file():
        return {"status":"pending","reason":"secondary_result_and_progress_not_both_saved",
                "source_result":str(path),"metrics":None,"projection_created":False,**FLAGS}
    source, source_ref = _read(path)
    progress, progress_ref = _read(progress_path)
    _boundary(source)
    if progress.get("status") != "completed":
        return {"status":"pending","reason":"secondary_operation_not_completed","source_result":str(path),
                "metrics":None,"projection_created":False,**FLAGS}
    registration, registration_ref = _pinned_source(source["registration"])
    _boundary(registration)
    selected = registration["binding"]["replayed_case_ids"]
    reused = registration["binding"]["reused_case_ids"]
    if (source.get("schema") != METHOD+"_result" or source.get("method_id") != METHOD or source.get("spec") != SPEC
            or source.get("protocol") != PROTOCOL or source.get("config") != _config(100000,5).to_dict()
            or source.get("case_count") != 32 or [case["case_id"] for case in source["cases"]] != list(CASE_IDS)
            or registration.get("schema") != METHOD+"_registration" or registration.get("method_id") != METHOD
            or registration.get("spec") != SPEC or registration.get("protocol") != PROTOCOL
            or registration.get("config") != source["config"] or registration.get("case_ids") != list(CASE_IDS)
            or source.get("source_secondary_supplement") != registration["binding"]
            or source.get("parent_result") != registration["parent"]["result"]
            or len(selected) != 2 or selected != [case_id for case_id in CASE_IDS if case_id in selected]
            or reused != [case_id for case_id in CASE_IDS if case_id not in selected]
            or source.get("replayed_case_count") != 2 or source.get("reused_case_count") != 30
            or progress.get("method_id") != METHOD or progress.get("registration") != registration_ref
            or progress.get("result") != source_ref or progress.get("active_case") is not None
            or progress.get("case_ids") != selected
            or [row["case_id"] for row in progress.get("completed_cases",[])] != selected
            or source.get("new_action_pair_scope") != "replayed_case_ids_only"
            or source.get("historical_execution_policy_scope") != "replayed_case_ids_only"
            or source.get("late_evidence_not_historical_PIT") is not True or source.get("original_results_modified") is not False
            or any(record.get("issuer_confirmed") is not False or record.get("PIT_verified") is not False
                   for record in (source,registration))):
        raise ValueError("Exact completed registered secondary2/retained30 result and progress required")
    parent, parent_ref = _pinned_source(registration["parent"]["result"])
    _boundary(parent)
    parent_progress, parent_progress_ref = _pinned_source(registration["parent"]["progress"])
    parent_registration, parent_registration_ref = _pinned_source(registration["parent"]["registration"])
    _, inputs_ref = _pinned_source(registration["inputs"])
    if (parent.get("registration") != parent_registration_ref or parent_progress.get("status") != "completed"
            or parent_progress.get("registration") != parent_registration_ref or parent_progress.get("result") != parent_ref
            or parent_registration.get("inputs") != registration["inputs"]
            or parent.get("case_count") != 32 or [case["case_id"] for case in parent["cases"]] != list(CASE_IDS)
            or any(source[key] != parent[key] for key in ("start","end","source_days","scope_members","config","protocol","source"))):
        raise ValueError("Secondary parent registration/source pins or unchanged sample differ")
    refs = [source_ref,progress_ref,registration_ref,parent_ref,parent_progress_ref,parent_registration_ref,inputs_ref]
    for key in ("action_event_view","action_reference_view"):
        item = registration[key]
        _, ref = _pinned_source({"path":item["result"],"sha256":item["sha256"]})
        if Path(item["result"]).resolve() != Path(item["directory"]).resolve()/"result.json" or source["new_action_pair"][key] != item:
            raise ValueError("Secondary source pair pin differs from its registration")
        refs.append(ref)
    built, window_id = _now(), "native_secondary_supplement_"+source["start"][:4]
    parent_cases = {case["case_id"]:case for case in parent["cases"]}
    summaries, payloads = [], []
    for case in source["cases"]:
        case_id = case["case_id"]
        replayed, old = case_id in selected, parent_cases[case_id]
        layer = case["source_supplement_origin"]
        expected_pair = ({key:registration[key]["directory"] for key in ("action_event_view","action_reference_view")}
                         if replayed else _case_pair(old))
        if (layer.get("mode") != ("replayed" if replayed else "reused") or layer.get("parent_result") != parent_ref
                or layer.get("parent_case_id") != case_id or layer.get("action_pair") != expected_pair
                or _case_pair(case) != expected_pair or layer.get("replayed_from_original_cash") is not replayed
                or layer.get("checkpoint_recovery_performed") is not False
                or case.get("source_origin") != old.get("source_origin")
                or source.get("case_origins",{}).get(case_id) != layer
                or (not replayed and {key:value for key,value in case.items() if key!="source_supplement_origin"} != old)
                or (replayed and (layer.get("issuer_confirmed") is not False or layer.get("PIT_verified") is not False
                    or layer.get("committed_prefix_check",{}).get("status") != "exact_original_committed_prefix_preserved"
                    or layer.get("committed_prefix_check",{}).get("complete_account_snapshot_compared") is not True))):
            raise ValueError("Secondary latest source origin, exact retained account or prefix changed")
        own_ref = source_ref if replayed else parent_ref
        summary = _case_summary(case,own_ref,"secondary_source_supplement" if replayed else "reused_secondary_parent")
        summary.update(container_source_result=source_ref["path"],source_supplement=deepcopy(layer),
                       source_origin_history=deepcopy(case["source_origin"]))
        detail = _detail(case,source,summary,built,refs)
        detail.update(source_supplement=deepcopy(layer),source_origin_history=deepcopy(case["source_origin"]))
        summaries.append(summary)
        payloads.append((Path("windows")/window_id/(case_id+".json"),detail))
    window = _layer_window(source,source["scope_members"],window_id,
        f"{source['start'][:4]}年 · 独立secondary诊断（重放2／复用30，非32个新独立账户）",built,summaries,
        period_kind="same_sample_secondary_source_supplement",source_supplement_counts={"replayed":2,"reused":30},
        new_action_pair=deepcopy(source["new_action_pair"]),new_action_pair_scope="replayed_case_ids_only",
        issuer_confirmed=False,PIT_verified=False,late_evidence_not_historical_PIT=True)
    return _append_saved_layer(previous,out,path.parent,window,payloads,refs,
        semantics="saved_windows_plus_independent_secondary_diagnostic_no_source_or_equity_merging",
        status="saved_secondary_supplement_history_projection_built")


def _build_ablation_projection(ablation_result, reuse_projection, out, *, supplement=None):
    """Append one completed fixed-48 window to saved legacy display payloads.

    The old monthly results are not reopened: only their existing projection
    files are copied and pinned into a new separate generation.
    """
    from .leader_environment_exit_ablation import METHOD_ID, RESULT_SCHEMA, SPEC, READINESS_OPTIMIZATION
    from .shortline_panel_replay import _config

    out, previous = Path(out).resolve(), Path(reuse_projection).resolve()
    if out.exists():
        raise FileExistsError("Saved ablation projection requires a new generation directory")
    if out == previous or previous in out.parents or out in previous.parents:
        raise ValueError("New projection must be separate from the saved projection it reuses")
    source, source_ref = _read(ablation_result) if supplement is None else (supplement["source"], supplement["source_ref"])
    _boundary(source)
    allowed = {"completed_fixed_48_case_research_comparison", "checked_fixed_48_cases_with_skipped_entries",
        "checked_fixed_48_cases_with_unavailable_entries"}
    if ((supplement is None and (source.get("schema") != RESULT_SCHEMA or source.get("method_id") != METHOD_ID
            or source.get("status") not in allowed)) or source.get("spec") != SPEC
            or source.get("parent_fixed_protocol") != PROTOCOL or source.get("config") != _config(100000, 5).to_dict()
            or source.get("case_count") != 48 or [row["case_id"] for row in source["cases"]] != list(ABLATION_CASE_IDS)):
        raise ValueError("One completed unchanged fixed-48 ablation result is required")
    progress, progress_ref = _read(Path(ablation_result).resolve().parent / "progress.json")
    if (progress.get("status") != "completed" or progress.get("expected_cases") != 48
            or [row["case_id"] for row in progress.get("completed_cases", [])] != list(ABLATION_CASE_IDS)):
        raise ValueError("The actual fixed-48 operation has not completed all saved case records")
    previous_manifest, previous_ref = _read(previous / "manifest.json")
    _boundary(previous_manifest)
    if previous_manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("Reuse an explicit original monthly saved-history projection")
    old_index = _projected(previous, previous_manifest["index_ref"])
    _boundary(old_index)
    if old_index.get("schema") != SCHEMA or old_index.get("built_at") != previous_manifest["built_at"]:
        raise ValueError("The saved monthly index differs from its exact projection manifest")
    built_at, windows, payloads = _now(), deepcopy(old_index["windows"]), []
    for window in windows:
        if [row["case_id"] for row in window["case_rows"]] != list(CASE_IDS):
            raise ValueError("Retained monthly projection must preserve all original 32 cases")
        window["projection_built_at"] = old_index["built_at"]
        for summary in window["case_rows"]:
            detail = _projected(previous, summary["detail_ref"])
            _boundary(detail)
            if (detail["case_id"] != summary["case_id"] or detail["built_at"] != old_index["built_at"]
                    or detail["summary"] != {key: value for key, value in summary.items() if key != "detail_ref"}):
                raise ValueError("Retained monthly details do not match their saved summaries")
            payloads.append((Path(summary["detail_ref"]["path"]), detail))
    window_id = "ablation48_" + source["start"] + "_" + source["end"]
    if any(window["id"] == window_id for window in windows):
        raise ValueError("Duplicate explicit ablation window")
    source_refs = [source_ref, progress_ref] + (supplement["additional_refs"] if supplement else [])
    summaries, skipped = [], 0
    for case in source["cases"]:
        case_id = case["case_id"]
        kind, guard, cap = case_id.split("__")
        if (case.get("type"), case.get("guard"), case.get("capacity")) != (kind, guard, cap) or case.get("config") != source["config"]:
            raise ValueError("Saved ablation case identity or costs changed")
        is_skipped = case["status"] == "skipped_no_possible_entry_from_saved_panel"
        entry = case.get("entry_readiness")
        if is_skipped and (source.get("execution_optimization") != READINESS_OPTIMIZATION
                or not isinstance(entry, dict) or entry.get("case_id") != case_id or entry.get("potential_entry_days") != 0
                or case.get("engine_sessions_committed") != 0 or case.get("account") is not None
                or case.get("metrics") is not None or case.get("residual_positions") is not None
                or any(case.get(key) != [] for key in ("decisions", "daily", "orders", "fills"))):
            raise ValueError("Skipped cases must preserve no account, no simulated sessions and null results")
        summary = {"case_id": case_id, "type": kind, "guard": guard, "capacity": cap,
            "status": case["status"], "metrics": deepcopy(case["metrics"]),
            "order_count": len(case["orders"]), "fill_count": len(case["fills"]),
            "residual_positions": deepcopy(case["residual_positions"]),
            "source_result": source_ref["path"], "origin": ABLATION_FAMILY,
            "engine_sessions_committed": case["engine_sessions_committed"], "skipped": is_skipped,
            "entry_readiness": deepcopy(entry)}
        if supplement:
            origin = supplement["cases"][case_id]
            summary.update(source_supplement=deepcopy(origin),
                source_result=source_ref["path"] if origin["replayed"] else supplement["base_ref"]["path"])
        if is_skipped:
            skipped += 1
            detail = {"schema": "leader_eight_type_saved_history_case_v1", "case_id": case_id,
                "type": kind, "guard": guard, "capacity": cap, "built_at": built_at,
                "source_refs": source_refs, "summary": deepcopy(summary), "days": [],
                "entry_readiness": deepcopy(entry), "skipped": True,
                "skip_reason": "no_possible_entry_from_saved_panel_no_account_simulated",
                "source_originals_rechecked_at_read": False, **FLAGS}
        else:
            detail = _detail(case, source, summary, built_at, source_refs, ablation=True)
            detail.update(entry_readiness=deepcopy(entry), skipped=False)
        if supplement:
            detail["source_supplement"] = deepcopy(summary["source_supplement"])
        payloads.append((Path("windows") / window_id / (case_id + ".json"), detail))
        summaries.append(summary)
    decisions = [day for day in source["source_days"] if source["start"] <= day <= source["end"]]
    tails = [day for day in source["source_days"] if day > source["end"]]
    windows.append({"id": window_id, "label": f"{source['start']} 至 {source['end']} · 环境退出对照",
        "source_family": ABLATION_FAMILY, "case_ids": list(ABLATION_CASE_IDS), "guards": list(ABLATION_GUARDS),
        "case_count": 48, "checked_case_count": 48, "simulated_case_count": 48 - skipped,
        "skipped_case_count": skipped, "projection_built_at": built_at,
        "start": source["start"], "end": source["end"], "decision_days": decisions, "tail_days": tails,
        "environment_counts": dict(Counter(source["environment_decisions"][day]["phase"] for day in decisions)),
        "case_rows": summaries, "source_mapping_preserved": True})
    if supplement:
        windows[-1]["source_supplement_counts"] = {"replayed": 2, "reused": 46}
    refs = deepcopy(old_index["source_refs"]) + [previous_ref, *source_refs]
    source_folder = Path(source_ref["path"]).parent
    if out == source_folder or out in source_folder.parents or source_folder in out.parents:
        raise ValueError("New projection must remain separate from the original ablation evidence")
    out.mkdir(parents=True, exist_ok=False)
    detail_refs = {str(relative).replace("\\", "/"): _save(out / relative, detail) for relative, detail in payloads}
    for window in windows:
        for summary in window["case_rows"]:
            relative = f"windows/{window['id']}/{summary['case_id']}.json"
            summary["detail_ref"] = {"path": relative, "sha256": detail_refs[relative]["sha256"]}
    windows.sort(key=lambda window: (window["start"], window["id"]))
    index = {"schema": ABLATION_SCHEMA, "status": "saved_history_projection", "built_at": built_at,
        "windows": windows, "source_refs": refs, "source_originals_rechecked_at_read": False,
        "semantics": "saved_monthly_projection_plus_fixed_48_environment_exit_ablation_not_current_signals", **FLAGS}
    index_ref = _save(out / "index.json", index)
    _save(out / "manifest.json", {"schema": ABLATION_MANIFEST_SCHEMA, "built_at": built_at,
        "index_ref": {"path": "index.json", "sha256": index_ref["sha256"]}, "source_refs": refs, **FLAGS})
    return {"schema": ABLATION_MANIFEST_SCHEMA, "status": "saved_ablation_history_projection_built",
        "built_at": built_at, "directory": str(out), "windows": [window["id"] for window in windows],
        "case_count": len(payloads), "manifest_file": str(out / "manifest.json"), **FLAGS}


def build_supplement(supplement_result, reuse_projection, out):
    """Replace the same 48-case display with two supplemented and 46 retained cases."""
    from .leader_environment_exit_ablation import (
        METHOD_ID, RESULT_SCHEMA, REGISTRATION_SCHEMA, SUPPLEMENT_METHOD_ID,
        SUPPLEMENT_RESULT_SCHEMA, SUPPLEMENT_REGISTRATION_SCHEMA, SUPPLEMENT_SPEC,
        _committed_prefix_check,
    )
    source, source_ref = _read(supplement_result)
    _boundary(source)
    if (source.get("schema") != SUPPLEMENT_RESULT_SCHEMA or source.get("method_id") != SUPPLEMENT_METHOD_ID
            or source.get("ablation_method_id") != METHOD_ID or source.get("supplement_spec") != SUPPLEMENT_SPEC
            or source.get("status") != "completed_bounded_source_supplement_with_retained_cases"
            or source.get("replayed_case_count") != 2 or source.get("reused_case_count") != 46):
        raise ValueError("The explicit completed two-replay/46-retained fixed-48 supplement is required")
    base, base_ref = _read(source["original_result"])
    old_registration, old_registration_ref = _read(source["original_registration"])
    registration, registration_ref = _read(source["registration"])
    if (base_ref["sha256"] != source["original_result_sha256"]
            or old_registration_ref["sha256"] != source["original_registration_sha256"]
            or old_registration.get("schema") != REGISTRATION_SCHEMA or old_registration.get("method_id") != METHOD_ID
            or base.get("schema") != RESULT_SCHEMA or base.get("method_id") != METHOD_ID
            or base.get("registration") != old_registration_ref["path"]
            or base.get("registration_sha256") != old_registration_ref["sha256"]
            or registration_ref["sha256"] != source["registration_sha256"]
            or registration.get("schema") != SUPPLEMENT_REGISTRATION_SCHEMA
            or registration.get("method_id") != SUPPLEMENT_METHOD_ID or registration.get("spec") != SUPPLEMENT_SPEC
            or registration.get("original_result") != base_ref["path"]
            or registration.get("original_result_sha256") != base_ref["sha256"]
            or registration.get("original_registration") != old_registration_ref["path"]
            or registration.get("original_registration_sha256") != old_registration_ref["sha256"]
            or registration.get("binding") != source.get("source_action_supplement")):
        raise ValueError("Supplement registration or exact original result/source pins changed")
    for record in (base, registration, old_registration):
        _boundary(record)
    immutable = ("start", "end", "source_days", "scope_members", "config", "spec", "parent_fixed_protocol",
        "study_plan", "study_plan_sha256", "source", "environment_decisions", "control_environment_decisions",
        "raw_top5_paths", "capacity_paths", "execution_optimization")
    if any(source.get(key) != base.get(key) for key in immutable):
        raise ValueError("Supplement cannot change the original 48-case observations, rules or cost settings")
    if [case["case_id"] for case in base["cases"]] != list(ABLATION_CASE_IDS):
        raise ValueError("All 48 original cases must remain pinned in their original order")
    base_progress, base_progress_ref = _read(Path(base_ref["path"]).parent / "progress.json")
    progress, progress_ref = _read(Path(source_ref["path"]).parent / "progress.json")
    for actual in (base_progress, progress):
        if (actual.get("status") != "completed" or actual.get("expected_cases") != 48
                or [row["case_id"] for row in actual.get("completed_cases", [])] != list(ABLATION_CASE_IDS)):
            raise ValueError("Both original and supplementary 48-case operations must be complete")
    if (registration.get("original_progress") != base_progress_ref["path"]
            or registration.get("original_progress_sha256") != base_progress_ref["sha256"]):
        raise ValueError("The exact originally registered 48-case progress evidence changed")
    if progress.get("replayed_case_count") != 2 or progress.get("reused_case_count") != 46:
        raise ValueError("Supplement progress does not prove exactly two replays and 46 retained cases")
    binding = source["source_action_supplement"]
    replayed, reused = binding.get("replayed_case_ids", []), binding.get("reused_case_ids", [])
    if (len(replayed) != 2 or len(reused) != 46 or set(replayed) & set(reused)
            or set(replayed) | set(reused) != set(ABLATION_CASE_IDS)
            or replayed != [case_id for case_id in ABLATION_CASE_IDS if case_id in replayed]
            or reused != [case_id for case_id in ABLATION_CASE_IDS if case_id in reused]
            or set(source.get("case_origins", {})) != set(ABLATION_CASE_IDS)
            or [case["case_id"] for case in source["cases"]] != list(ABLATION_CASE_IDS)):
        raise ValueError("Supplementary and retained case identity sets must form the original fixed 48 exactly once")
    old_cases = {case["case_id"]: case for case in base["cases"]}
    projections = {}
    for case in source["cases"]:
        case_id, origin = case["case_id"], source["case_origins"][case["case_id"]]
        old = old_cases[case_id]
        is_replayed = case_id in replayed
        if (origin.get("original_result") != base_ref["path"] or origin.get("original_result_sha256") != base_ref["sha256"]
                or origin.get("original_case_id") != case_id or origin.get("replayed_from_original_cash") is not is_replayed
                or origin.get("reused_original_result") is not (not is_replayed)
                or origin.get("checkpoint_recovery_performed") is not False):
            raise ValueError("A supplementary case origin does not match its exact old/new source")
        if is_replayed:
            prefix = _committed_prefix_check(old, case, base["source_days"])
            if origin.get("committed_prefix_check") != prefix or prefix.get("complete_account_snapshot_compared") is not True:
                raise ValueError("Supplementary evidence must preserve the original complete account prefix")
        elif case != old:
            raise ValueError("A retained case changed; new action evidence cannot be attributed to it")
        projections[case_id] = {"replayed": is_replayed, "original_result": base_ref["path"],
            "original_status": old["status"], "original_engine_sessions_committed": old["engine_sessions_committed"],
            "original_last_day": old["daily"][-1]["date"] if old["daily"] else None,
            "original_blocking_evidence": deepcopy(old.get("blocking_evidence")),
            "prefix_preserved": True if is_replayed else None, "new_action_evidence_applied": is_replayed}
    return _build_ablation_projection(supplement_result, reuse_projection, out, supplement={
        "source": source, "source_ref": source_ref, "base_ref": base_ref, "cases": projections,
        "additional_refs": [base_ref, base_progress_ref, registration_ref, old_registration_ref]})


def build(comparison_indexes, out):
    """Explicitly project Jan's 24+8 mapping and Feb's single-32 mapping."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Saved history projection requires a new version directory")
    paths = [Path(p).resolve() for p in comparison_indexes]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Explicit distinct saved comparison indexes are required")
    built_at, windows, payloads, source_cache, refs = _now(), [], [], {}, []
    for path in paths:
        index, index_ref = _read(path)
        _boundary(index)
        schema = index.get("schema")
        if schema == "saved_historical_identity_comparison_index_v1":
            rows = index["comparison"]  # Never substitute the discarded old-baseline identity rows.
        elif schema == "saved_historical_identity_standalone_month_comparison_v1":
            rows = index["case_rows"]
        else:
            raise ValueError("Unsupported explicit saved eight-type comparison index")
        mapping = {r["case_id"]: r for r in rows}
        if len(mapping) != 32 or len(rows) != 32 or set(mapping) != set(CASE_IDS):
            raise ValueError("Every original type/guard/capacity case is required exactly once")
        month = index["start"][:7]
        if index["end"][:7] != month or any(w["id"] == month for w in windows):
            raise ValueError("Distinct explicitly saved calendar-month windows are required")
        refs.append(index_ref)
        summaries, environments = [], None
        for case_id in CASE_IDS:
            summary = deepcopy(mapping[case_id])
            kind, guard, cap = case_id.split("__")
            if (summary["type"], summary["guard"], summary["capacity"]) != (kind, guard, cap):
                raise ValueError("Saved comparison case identity changed")
            source_path = Path(summary["source_result"]).resolve()
            if source_path not in source_cache:
                source_cache[source_path] = _read(source_path)
            source, source_ref = source_cache[source_path]
            _boundary(source)
            if (source.get("protocol", source.get("parent_fixed_protocol")) != PROTOCOL
                    or source["start"] != index["start"] or source["end"] != index["end"]
                    or source["source_days"] != index["source_days"]):
                raise ValueError("Comparison and original saved case window/protocol differ")
            cases = {c["case_id"]: c for c in source["cases"]}
            case = cases[case_id]
            if (case["metrics"] != summary["metrics"] or case["status"] != summary["status"]
                    or len(case["orders"]) != summary["order_count"]
                    or len(case["fills"]) != summary["fill_count"]
                    or case["residual_positions"] != summary["residual_positions"]):
                raise ValueError("Original saved metrics/orders/fills/residuals differ from the comparison")
            if source_ref not in refs:
                refs.append(source_ref)
            original_env = source["environment_decisions"]
            facts = {d: {k: v.get(k) for k in ("phase", "two_close_confirmation", "exact_previous_present")}
                for d, v in original_env.items()}
            if environments is None:
                environments = facts
            elif environments != facts:
                raise ValueError("Compared cases do not share the same saved environment observations")
            relative = Path("windows") / month / (case_id + ".json")
            detail = _detail(case, source, summary, built_at, [index_ref, source_ref])
            payloads.append((relative, detail))
            summaries.append(summary)
        decisions = [d for d in index["source_days"] if index["start"] <= d <= index["end"]]
        tails = [d for d in index["source_days"] if d > index["end"]]
        windows.append({"id": month, "label": f"{int(month[:4])}年{int(month[5:])}月",
            "start": index["start"], "end": index["end"], "decision_days": decisions, "tail_days": tails,
            "environment_counts": dict(Counter(environments[d]["phase"] for d in decisions)),
            "case_rows": summaries, "comparison_index_ref": index_ref,
            "source_mapping_preserved": True})
    for reference in refs:
        parent = Path(reference["path"]).parent
        if parent == out or parent in out.parents or out in parent.parents:
            raise ValueError("History projection must remain separate from original evidence")
    out.mkdir(parents=True)
    detail_refs = {}
    for relative, detail in payloads:
        reference = _save(out / relative, detail)
        detail_refs[str(relative).replace("\\", "/")] = reference["sha256"]
    for window in windows:
        for row in window["case_rows"]:
            relative = f"windows/{window['id']}/{row['case_id']}.json"
            row["detail_ref"] = {"path": relative, "sha256": detail_refs[relative]}
    windows.sort(key=lambda w: w["id"])
    index = {"schema": SCHEMA, "status": "saved_history_projection", "built_at": built_at,
        "windows": windows, "source_refs": refs, "source_originals_rechecked_at_read": False,
        "semantics": "explicit_as_built_projection_of_completed_research_not_current_signals_or_source_freshness", **FLAGS}
    index_ref = _save(out / "index.json", index)
    _save(out / "manifest.json", {"schema": MANIFEST_SCHEMA, "built_at": built_at,
        "index_ref": {"path": "index.json", "sha256": index_ref["sha256"]},
        "source_refs": refs, **FLAGS})
    return {"schema": MANIFEST_SCHEMA, "status": "saved_history_projection_built",
        "built_at": built_at, "directory": str(out), "windows": [w["id"] for w in windows],
        "case_count": len(payloads), "manifest_file": str(out / "manifest.json"), **FLAGS}


def _projected(root, reference):
    path = (root / reference["path"]).resolve()
    if root not in path.parents:
        raise ValueError("Saved history payload escaped its projection")
    value, actual = _read(path)
    if actual["sha256"] != reference["sha256"]:
        raise ValueError("Saved history projection bytes changed")
    return value


def _generation(root, name):
    # Generations are explicit sibling directories of current.json. A pointer
    # may not traverse, alias the root itself or follow a junction outside it.
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or "/" in name or "\\" in name or Path(name).is_absolute()):
        raise ValueError("A single relative saved-history generation directory is required")
    directory = (root / name).resolve()
    if directory.parent != root:
        raise ValueError("Saved-history generation escaped the projection root")
    return directory


def publish(directory, data_root):
    """Validate this complete projection, then atomically publish its pointer.

    This is an explicit local artifact publication. No original research source
    is reread; all projection case payloads are checked before changing current.
    """
    directory = Path(directory).resolve()
    root = (Path(data_root) / DEFAULT_SUBDIR.parent).resolve()
    if directory.parent != root or _generation(root, directory.name) != directory:
        raise ValueError("Published generation must be an explicit child of this data root")
    manifest, manifest_ref = _read(directory / "manifest.json")
    _boundary(manifest)
    if manifest.get("schema") not in {MANIFEST_SCHEMA, ABLATION_MANIFEST_SCHEMA}:
        raise ValueError("Unknown saved history projection manifest")
    index = _projected(directory, manifest["index_ref"])
    _boundary(index)
    expected_schema = ABLATION_SCHEMA if manifest["schema"] == ABLATION_MANIFEST_SCHEMA else SCHEMA
    if (index.get("schema") != expected_schema or index["built_at"] != manifest["built_at"]
            or not index.get("windows")):
        raise ValueError("Complete same-generation history index is required before publication")
    window_ids = [window["id"] for window in index["windows"]]
    ordered = ([window["id"] for window in sorted(index["windows"], key=lambda window: (window["start"], window["id"]))]
        if expected_schema == ABLATION_SCHEMA else sorted(window_ids))
    if len(window_ids) != len(set(window_ids)) or window_ids != ordered:
        raise ValueError("Unique chronological saved windows required before publication")
    count = 0
    for window in index["windows"]:
        expected_ids = _window_case_ids(window) if expected_schema == ABLATION_SCHEMA else CASE_IDS
        if [row["case_id"] for row in window["case_rows"]] != list(expected_ids):
            raise ValueError("All fixed original cases are required before publication")
        for summary in window["case_rows"]:
            detail = _projected(directory, summary["detail_ref"])
            _boundary(detail)
            expected_built = window.get("projection_built_at", index["built_at"]) if expected_schema == ABLATION_SCHEMA else index["built_at"]
            if (detail["built_at"] != expected_built or detail["case_id"] != summary["case_id"]
                    or detail["summary"] != {k: v for k, v in summary.items() if k != "detail_ref"}):
                raise ValueError("Published case payload does not belong to its exact saved summary")
            count += 1
    actual = _now()
    built = datetime.fromisoformat(index["built_at"].replace("Z", "+00:00"))
    if built.utcoffset() is None or built > datetime.fromisoformat(actual):
        raise ValueError("History build must precede the actual explicit publication clock")
    pointer = {"schema": POINTER_SCHEMA, "generation_directory": directory.name,
        "manifest_sha256": manifest_ref["sha256"], "actual_published_at": actual,
        "built_at": index["built_at"], "source_originals_rechecked_at_publication": False, **FLAGS}
    temporary = root / (".current-" + uuid4().hex + ".tmp")
    try:
        _save(temporary, pointer)
        temporary.replace(root / "current.json")
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"schema": POINTER_SCHEMA, "status": "saved_history_projection_published",
        "generation_directory": directory.name, "actual_published_at": actual,
        "built_at": index["built_at"], "window_ids": window_ids, "case_count": count,
        "pointer_ref": _read(root / "current.json")[1], **FLAGS}


def read_history(data_root, window_id=None, case_id=None):
    history_root = (Path(data_root) / DEFAULT_SUBDIR.parent).resolve()
    root = (Path(data_root) / DEFAULT_SUBDIR).resolve()
    result = {"schema": SCHEMA, "status": "unavailable", "read_at": _now(), "built_at": None,
        "windows": [], "selected_window_id": None, "selected_case_id": None, "detail": None,
        "source_originals_rechecked_at_read": False, "projection_bytes_verified": False, **FLAGS}
    try:
        pointer_file = history_root / "current.json"
        pointer = None
        if pointer_file.exists():
            pointer, pointer_ref = _read(pointer_file)
            _boundary(pointer)
            if pointer.get("schema") != POINTER_SCHEMA:
                raise ValueError("Unknown saved history publication pointer")
            root = _generation(history_root, pointer["generation_directory"])
            published = datetime.fromisoformat(pointer["actual_published_at"].replace("Z", "+00:00"))
            if published.utcoffset() is None or published > datetime.fromisoformat(result["read_at"]):
                raise ValueError("Saved history publication clock is missing or future")
            result.update(projection_generation=pointer["generation_directory"],
                projection_published_at=pointer["actual_published_at"], publication_ref=pointer_ref)
        else:
            result.update(projection_generation=root.name, projection_published_at=None, publication_ref=None)
        manifest, manifest_ref = _read(root / "manifest.json")
        if pointer and (manifest_ref["sha256"] != pointer["manifest_sha256"]
                or manifest["built_at"] != pointer["built_at"]):
            raise ValueError("Published history manifest bytes or build identity changed")
        if manifest.get("schema") not in {MANIFEST_SCHEMA, ABLATION_MANIFEST_SCHEMA}:
            raise ValueError("Unknown saved history projection manifest")
        _boundary(manifest)
        index = _projected(root, manifest["index_ref"])
        _boundary(index)
        expected_schema = ABLATION_SCHEMA if manifest["schema"] == ABLATION_MANIFEST_SCHEMA else SCHEMA
        if index.get("schema") != expected_schema or index["built_at"] != manifest["built_at"]:
            raise ValueError("Saved history index belongs to another projection")
        result.update(built_at=index["built_at"], windows=index["windows"],
            source_refs=index["source_refs"], projection_bytes_verified=True,
            semantics=index["semantics"])
        selected = next((w for w in index["windows"] if w["id"] == (window_id or index["windows"][-1]["id"])), None)
        if selected is None:
            return {**result, "status": "invalid_selection", "reason": "unknown_saved_window_id"}
        expected_ids = _window_case_ids(selected) if expected_schema == ABLATION_SCHEMA else CASE_IDS
        chosen = case_id or expected_ids[0]
        if chosen not in expected_ids:
            return {**result, "status": "invalid_selection", "reason": "unknown_fixed_case_id",
                "selected_window_id": selected["id"]}
        summary = next(r for r in selected["case_rows"] if r["case_id"] == chosen)
        detail = _projected(root, summary["detail_ref"])
        _boundary(detail)
        expected_built = selected.get("projection_built_at", index["built_at"]) if expected_schema == ABLATION_SCHEMA else index["built_at"]
        if detail["case_id"] != chosen or detail["built_at"] != expected_built:
            raise ValueError("Saved history case belongs to another selection or build")
        return {**result, "status": "saved_history_projection", "selected_window_id": selected["id"],
            "selected_case_id": chosen, "detail": detail}
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
        return {**result, "status": "unavailable", "reason": f"{type(error).__name__}: {error}",
            "detail": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison-index", action="append", type=Path)
    parser.add_argument("--ablation-result", type=Path, help="Explicit completed fixed-48 cross-month result")
    parser.add_argument("--supplement-result", type=Path, help="Explicit completed fixed-48 source-action supplement")
    parser.add_argument("--annual-result", type=Path, help="Explicit terminal native annual32 result, independent of saved 2026 windows")
    parser.add_argument("--case-annual-result", type=Path, help="Explicit terminal durable fixed32 year with registered source and completed progress")
    parser.add_argument("--case-partial-year-result", type=Path,
        help="Explicit terminal durable fixed32 result for the registered 2023 within-year interval")
    parser.add_argument("--partial-year-registration", type=Path,
        help="Original source registration required only with --case-partial-year-result")
    parser.add_argument("--native-supplement-result", type=Path, help="Explicit completed native32 source-only supplement; retain mixed case origins")
    parser.add_argument("--identity-overlay-result", type=Path, help="Explicit registered new identity8 plus exact retained parent24")
    parser.add_argument("--secondary-supplement-result", type=Path, help="Explicit secondary2/retained30 window; keep each latest source origin")
    parser.add_argument("--known-identity-continuation-result", action="append", type=Path,
        help="Explicit terminal known-positive four-case continuation; repeat for independent periods")
    parser.add_argument("--reuse-projection", type=Path, help="Existing original monthly display projection; no old result replay")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--publish", action="store_true", help="Explicitly publish the complete new projection")
    parser.add_argument("--data-root", type=Path, help="Required explicit trading data root when --publish is supplied")
    args = parser.parse_args(argv)
    if args.publish and args.data_root is None:
        parser.error("--publish requires an explicit --data-root")
    modes = [args.known_identity_continuation_result,args.case_annual_result,args.case_partial_year_result,args.identity_overlay_result,args.secondary_supplement_result,args.native_supplement_result,
             args.annual_result,args.supplement_result,args.ablation_result]
    if sum(value is not None for value in modes)>1:
        parser.error("Choose exactly one independent saved-result source mode")
    if (args.case_partial_year_result is None) != (args.partial_year_registration is None):
        parser.error("--case-partial-year-result and --partial-year-registration must be supplied together")
    if args.case_partial_year_result:
        if args.comparison_index or args.reuse_projection is None:
            parser.error("--case-partial-year-result requires only --reuse-projection")
        result = build_case_partial_year(args.case_partial_year_result,args.partial_year_registration,
            args.reuse_projection,args.out)
    elif args.known_identity_continuation_result:
        if args.comparison_index or args.reuse_projection is None:
            parser.error("--known-identity-continuation-result requires only --reuse-projection")
        result = build_known_scope_continuations(args.known_identity_continuation_result, args.reuse_projection, args.out)
    elif args.case_annual_result:
        if args.comparison_index or args.reuse_projection is None:
            parser.error("--case-annual-result requires only --reuse-projection")
        result = build_case_annual(args.case_annual_result,args.reuse_projection,args.out)
    elif args.identity_overlay_result or args.secondary_supplement_result:
        if args.comparison_index or args.reuse_projection is None:
            parser.error("New identity/secondary source modes require only --reuse-projection")
        result = (build_identity_overlay(args.identity_overlay_result,args.reuse_projection,args.out)
                  if args.identity_overlay_result else
                  build_secondary_supplement(args.secondary_supplement_result,args.reuse_projection,args.out))
    elif args.native_supplement_result:
        if args.annual_result or args.ablation_result or args.supplement_result or args.comparison_index or args.reuse_projection is None:
            parser.error("--native-supplement-result requires only --reuse-projection and no other source mode")
        result = build_native_supplement(args.native_supplement_result, args.reuse_projection, args.out)
    elif args.annual_result:
        if args.ablation_result or args.supplement_result or args.comparison_index or args.reuse_projection is None:
            parser.error("--annual-result requires only --reuse-projection and no other source mode")
        result = build_annual(args.annual_result, args.reuse_projection, args.out)
    elif args.supplement_result:
        if args.ablation_result or args.comparison_index or args.reuse_projection is None:
            parser.error("--supplement-result requires only --reuse-projection and no old result rebuilding mode")
        result = build_supplement(args.supplement_result, args.reuse_projection, args.out)
    elif args.ablation_result:
        if args.comparison_index or args.reuse_projection is None:
            parser.error("--ablation-result requires --reuse-projection and cannot rebuild --comparison-index")
        result = build_ablation(args.ablation_result, args.reuse_projection, args.out)
    else:
        if not args.comparison_index or args.reuse_projection is not None:
            parser.error("Supply explicit --comparison-index or the separate --ablation-result mode")
        result = build(args.comparison_index, args.out)
    if args.publish and result.get("status") != "pending":
        result["publication"] = publish(args.out, args.data_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0
