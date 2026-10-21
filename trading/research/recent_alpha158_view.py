"""Read latest saved frozen Alpha158 evidence; never score or advance accounts."""
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path


RUN = "20261015_1012_1014_v1"  # Legacy fixture name, never a source-selection pointer.
SCHEMA = "a_share_recent_source_alpha158_frozen_score_batch_v1"
METHOD_ID = "complete_recent_source_native_alpha158_active_scope_datewise_frozen_rank_v1"
PROVIDER_SCHEMA = "a_share_recent_complete_source_research_provider_v1"
PROVIDER_METHOD = "recent_complete_sources_native_prefix_pct_return_provider_v1"
BUILDING = "building_dated_native_features"
COMPLETED = "completed_research_only_dated_score_batch"
FAILED = "failed_recent_source_alpha158_scores"
FALSE_FLAGS = (
    "model_refit", "model_trained", "future_labels_used_for_score", "strategy_validated",
    "selection_enabled", "current_signal_admitted", "execution_admitted", "ledger_mutated",
    "target_holdings_generated", "paper_orders_generated", "ths_payload_generated", "provider_contacted",
    "historical_publication_verified", "pit_verified", "company_actions_verified",
    "engine_ledger_replay_completed",
)
SCORE_SCOPE = "exact_dated_provider_active_non_ST_positive_observed_OHLCV_amount_not_PIT"
GRID_SCOPE = "every_pinned_provider_symbol_times_requested_dates_full_prefix_for_expression_lookback"
RANK_SCOPE = "within_each_date_seed_percentile_mean_no_cross_date_ranking"
CASE_IDS = {"causal_first_seen_cap20", "uncapped"}
SYSTEM_DAILY_RUN = "20261017_v1"
ROLE_LEGACY = {"alpha_score": "research/recent_source_alpha158",
    "alpha_provider": "research/recent_source_qlib_provider"}


def _stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Saved Alpha158 clocks require an explicit timezone")
    return parsed


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Saved Alpha158 progress requires nonnegative integer counts")
    return value


def _role_path(path, role, data_root, project_root=None):
    """Allow only a legacy role or the fixed daily driver's exact artifact."""
    path = Path(path).resolve()
    legacy = ((Path(project_root).resolve() / "outputs/recent_alpha158_target_publication")
        if role == "alpha_publication" and project_root is not None else
        data_root / ROLE_LEGACY[role] if role in ROLE_LEGACY else None)
    if legacy is not None:
        try:
            relative = path.relative_to(legacy)
            if relative.parts and path.name == "result.json":
                return path
        except ValueError:
            pass
    operations = data_root / "research/system_daily" / SYSTEM_DAILY_RUN / "operations"
    try:
        relative = path.relative_to(operations)
        parts = relative.parts
        if (len(parts) == 4 and parts[1:] == ("artifacts", role, "result.json")
                and datetime.strptime(parts[0], "%Y%m%d").strftime("%Y%m%d") == parts[0]):
            return path
    except ValueError:
        pass
    raise ValueError(f"Saved Alpha158 {role} result escapes its exact registered artifact role")


def _role_extra_paths(data_root, role, project_root=None):
    operations = data_root / "research/system_daily" / SYSTEM_DAILY_RUN / "operations"
    return [_role_path(p, role, data_root, project_root)
        for p in sorted(operations.glob("*/artifacts/" + role + "/result.json"))]


def _latest(folder, filename, key, *, extra_paths=(), path_check=None):
    """Use saved semantic clocks, never mtime or an older-success fallback."""
    candidates = []
    for path in sorted(set(folder.glob("*/" + filename)) | set(extra_paths)):
        if path_check is not None:
            path = path_check(path)
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            candidates.append((key(record), path, record))
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Saved Alpha158 evidence cannot be ordered: {path}: {error}") from error
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], str(item[1])))
    if len(candidates) > 1 and candidates[-1][0] == candidates[-2][0]:
        raise ValueError("Ambiguous latest Alpha158 evidence has identical semantic date/clocks")
    return candidates[-1][1:]


def _batch_key(record):
    days = record["requested_feature_dates"]
    if not isinstance(days, list) or not days or days != sorted(set(days)):
        raise ValueError("Saved requested dates must be distinct and ordered")
    return date.fromisoformat(days[-1]), _stamp(record.get("finished_at") or record["started_at"])


def _saved_lifecycle(data_root, project_root, read_clock, batch_path):
    """Summarize separately saved publication/head evidence, without runner imports."""
    result = {"latest_publication": None, "saved_forward_trace": None,
        "saved_lifecycle_status": "project_output_root_not_supplied"}
    if project_root is None:
        return result
    pub_path, pub = _latest(Path(project_root).resolve() / "outputs/recent_alpha158_target_publication",
        "result.json", lambda r: (date.fromisoformat(r["source_close_day"]), _stamp(r["recorded_at"])),
        extra_paths=_role_extra_paths(data_root, "alpha_publication", project_root),
        path_check=lambda p: _role_path(p, "alpha_publication", data_root, project_root))
    if pub is None:
        return {**result, "saved_lifecycle_status": "no_saved_publication"}
    if (pub.get("schema") != "recent_alpha158_dated_target_publication_v1"
            or pub.get("status") not in ("close_research_target_publication_only", "late_context_only",
                                          "unbound_recording_date_context_only")
            or pub.get("publication_origin") != "local_recording_now"
            or any(pub.get(k) is not False for k in ("publication_time_backfilled", "execution_day_rolled_forward",
                "strategy_validated", "selection_enabled", "execution_admitted", "model_refit", "broker_interacted", "provider_contacted"))
            or not _stamp(pub["score_computed_at"]) <= _stamp(pub["recorded_at"]) <= read_clock
            or {c["case"] for c in pub["cases"]} != CASE_IDS or len(pub["cases"]) != 2):
        raise ValueError("Expected latest actual research-only Alpha158 publication")
    source_path = Path(pub["source"]["batch_run"]).resolve()
    _role_path(source_path, "alpha_score", data_root, project_root)
    if sha256(source_path.read_bytes()).hexdigest() != pub["source"]["batch_sha256"]:
        raise ValueError("Published Alpha158 source batch changed")
    result["latest_publication"] = {"source_file": str(pub_path), "status": pub["status"],
        "source_close_day": pub["source_close_day"], "recorded_at": pub["recorded_at"],
        "proposed_execution_day": pub["proposed_execution_day"],
        "execution_binding_pending": pub["execution_binding_pending"],
        "batch_run": str(source_path), "matches_displayed_score_batch": source_path == batch_path,
        "cases": [{"case": c["case"], "status": c["status"], "target_weights": c["target_weights"]}
                  for c in pub["cases"]]}

    def head_key(head):
        days = [row["date"] for case in head["cases"].values() for row in case["daily"]]
        clocks = [j["committed_at"] for case in head["cases"].values() for j in case["journal"]]
        return date.fromisoformat(max(days, default=head["first_decision_day"])), max(
            (_stamp(v) for v in clocks), default=_stamp(head["registered_at"]))

    head_path, head = _latest(data_root / "research/recent_alpha158_forward", "head.json", head_key)
    if head is None:
        return {**result, "saved_lifecycle_status": "publication_saved_no_forward_head"}
    if (head.get("schema") != "recent_alpha158_published_close_shared_ledger_checkpoint_v1"
            or set(head["cases"]) != CASE_IDS
            or head["binding"]["frozen_model_contract"] != pub["frozen_model_contract"]
            or any(head.get(k) is not False for k in ("strategy_validated", "selection_enabled", "execution_admitted",
                "model_refit", "broker_interacted", "provider_contacted", "user_account_mutated"))
            or head_key(head)[1] > read_clock):
        raise ValueError("Expected latest saved two-case Alpha158 research head")
    for reference in head["publications"]:
        saved_path = Path(reference["file"]).resolve()
        _role_path(saved_path, "alpha_publication", data_root, project_root)
        if sha256(saved_path.read_bytes()).hexdigest() != reference["sha256"]:
            raise ValueError("Saved forward publication binding changed")
    bound = any(Path(ref["file"]).resolve() == pub_path.resolve() for ref in head["publications"])
    cases = []
    for key, case in head["cases"].items():
        state = case["state"]
        daily = case["daily"]
        if ([r["date"] for r in daily] != [j["decision_day"] for j in case["journal"]]
                or state["day"] != (daily[-1]["date"] if daily else None)):
            raise ValueError("Saved Alpha158 daily/journal prefix differs")
        cases.append({"case": key, "account_id": case["context"]["account_id"],
            "last_processed_day": state["day"], "processed_decision_days": [r["date"] for r in daily],
            "cash_cents": state["cash"], "last_saved_equity": daily[-1]["equity"] if daily else None,
            "positive_positions": {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items()
                                   if sum(lot["qty"] for lot in lots) > 0},
            "orders": len(state["orders"]), "fills": len(state["fills"]),
            "pending_execution_day": (state.get("pending_published_close_intent") or {}).get("execution_not_before"),
            "actually_consumed_intents": [{"execution_day": r["date"],
                "decision_day": r["consumed_published_close_intent"]["decision_day"]}
                for r in daily if r.get("consumed_published_close_intent")],
            "blocking_evidence": case.get("blocking_evidence"), "error": case.get("error"), "metrics": None})
    result.update(saved_lifecycle_status="saved_publication_and_research_head",
        saved_forward_trace={"source_file": str(head_path), "registered_at": head["registered_at"],
            "latest_publication_bound": bound, "case_count": len(cases), "cases": cases,
            "saved_account_state_read": True,
            "metrics": None, "evidence_scope": "saved_research_ledger_only_not_execution_admission"})
    return result


def recent_alpha158_view(data_root, *, project_root=None, now=None):
    """Read the latest semantic batch and optional separately saved lifecycle evidence."""
    read_clock = now or datetime.now().astimezone()
    _stamp(read_clock.isoformat())
    data_root = Path(data_root).resolve()
    folder = data_root / "research/recent_source_alpha158"
    path, record = _latest(folder, "result.json", _batch_key,
        extra_paths=_role_extra_paths(data_root, "alpha_score", project_root),
        path_check=lambda p: _role_path(p, "alpha_score", data_root, project_root))
    operation = {**{key: False for key in FALSE_FLAGS}, "candidate_panel_only": True,
        "research_only": True, "score_run_started": False, "account_accessed": False,
        "orders_generated": False, "old_scores_modified": False, "old_scores_preserved": True,
        "fallback_to_old_native": False, "rankings_exposed": False}
    base = {"schema": "recent_alpha158_read_only_view_v1", "source_file": str(path) if path else None,
        "source_selection": "latest_requested_close_then_saved_terminal_or_start_clock_no_success_fallback",
        "operation_flags_scope": "score_batch_and_this_saved_file_read_not_saved_forward_history",
        "read_at": read_clock.isoformat(), "coverage": [], "score_dates": [], "score_rows": None,
        **operation}
    if path is None:
        return {**base, "status": "unavailable", "reason": "recent_source_batch_not_yet_saved"}
    _role_path(path, "alpha_score", data_root, project_root)
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("status") not in (BUILDING, COMPLETED, FAILED)
            or record.get("candidate_panel_only") is not True
            or record.get("cross_lineage_diagnostic") is not True
            or any(record.get(key) is not False for key in FALSE_FLAGS)
            or record.get("feature_count") != 158 or record.get("score_scope") != SCORE_SCOPE
            or record.get("native_grid_scope") != GRID_SCOPE or record.get("score_aggregation") != RANK_SCOPE):
        raise ValueError("Expected the new-method frozen research-only Alpha158 batch")
    started = _stamp(record["started_at"])
    available = _stamp(record["source_available_at"])
    if not available <= started <= read_clock:
        raise ValueError("Saved provider/start clocks exceed actual Alpha158 read time")
    requested = record["requested_feature_dates"]
    if (not isinstance(requested, list) or not requested or requested != sorted(set(requested))
            or any(_stamp(day + "T15:00:00+08:00") > started for day in requested)):
        raise ValueError("Requested feature dates must be distinct completed closes")
    for day in requested:
        date.fromisoformat(day)
    provider_path = Path(record["provider_view"]).resolve() / "result.json"
    _role_path(provider_path, "alpha_provider", data_root, project_root)
    provider_bytes = provider_path.read_bytes()
    if sha256(provider_bytes).hexdigest() != record["provider_result_sha256"]:
        raise ValueError("Pinned provider result changed")
    provider = json.loads(provider_bytes.decode("utf-8-sig"))
    if (provider.get("schema") != PROVIDER_SCHEMA or provider.get("provider_method_id") != PROVIDER_METHOD
            or provider.get("status") != "research_provider_completed"
            or provider.get("provider_method_id") != record.get("provider_method_id")
            or _stamp(provider["created_at"]) != available
            or not set(requested) <= set(provider["recent_source_days"])
            or any(provider.get(key) is not False for key in
                ("model_trained", "model_refit", "execution_admitted", "strategy_validated", "selection_enabled",
                 "future_labels_used_for_score", "pit_verified", "company_actions_verified"))
            or provider.get("normalization_matches_original_qfq_factor_training") is not False
            or provider.get("prior_scores_modified") is not False):
        raise ValueError("Pinned new mixed-prefix research provider required")
    total = _count(provider["symbols"])
    completed = _count(record["native_symbols_completed"])
    rows = _count(record["native_feature_rows"])
    parts = record["partitions"]
    if (not total or not isinstance(parts, list) or completed > total
            or sum(len(part["symbols"]) for part in parts) != completed
            or sum(_count(part["rows"]) for part in parts) != rows
            or rows != completed * len(requested)):
        raise ValueError("Saved native progress counts contradict full date/symbol grid")
    coverage = record["coverage"]
    if not isinstance(coverage, list) or [row["day"] for row in coverage] != requested:
        raise ValueError("Coverage must preserve every requested source date")
    for row in coverage:
        full, active, excluded = (_count(row[key]) for key in
            ("full_dated_roster_rows", "active_non_ST_valid_observations", "not_scored_count"))
        if active + excluded != full or active > total:
            raise ValueError("Active scoring scope must reconcile to the exact dated roster")
        for key in ("suspended_rows_retained_unscored", "ST_rows_retained_unscored"):
            if _count(row[key]) > full:
                raise ValueError("Dated exclusion count exceeds its own roster")
    terminal = record.get("finished_at")
    if record["status"] != BUILDING and (not terminal or not started <= _stamp(terminal) <= read_clock):
        raise ValueError("Alpha158 terminal clock must follow its actual start")
    score_dates, score_rows, actual = [], None, None
    if record["status"] == COMPLETED:
        score_dates, score_rows, actual = record["score_dates"], _count(record["score_rows"]), record["actual_computed_at"]
        if (score_dates != requested or completed != total or record["native_symbols"] != total
                or _stamp(actual) != _stamp(terminal)
                or score_rows != sum(row["active_non_ST_valid_observations"] for row in coverage)):
            raise ValueError("Completed scores must cover the full dated active scope at the actual terminal clock")
        for row in coverage:
            if (row.get("native_grid_rows") != total
                    or row.get("unobserved_or_out_of_scope_grid_rows") != total - row["active_non_ST_valid_observations"]):
                raise ValueError("Completed coverage must separate full grid from active scored observations")
            for key in ("missing_feature_cells", "all_missing_active_feature_rows"):
                _count(row[key])
    return {**base, "status": record["status"], "method_id": record["method_id"],
        "started_at": record["started_at"], "finished_at": terminal,
        "actual_computed_at": actual, "source_available_at": record["source_available_at"],
        "requested_feature_dates": requested, "score_dates": score_dates, "score_rows": score_rows,
        "partitions_completed": len(parts), "native_symbols_completed": completed, "native_symbols_total": total,
        "native_feature_rows": rows, "native_grid_expected_rows": total * len(requested),
        "feature_count": 158, "coverage": coverage, "cross_lineage_diagnostic": True,
        "provider_source_file": str(provider_path), "provider_method_id": provider["provider_method_id"],
        "base_history_end": provider["base_history_end"], "recent_source_days": provider["recent_source_days"],
        "provider_calendar_start": record["provider_calendar_start"], "provider_calendar_end": record["provider_calendar_end"],
        "provider_dates": record["provider_dates"], "score_scope": SCORE_SCOPE, "native_grid_scope": GRID_SCOPE,
        "score_aggregation": RANK_SCOPE, "model_run": record["model_run"],
        "error": record.get("error") if record["status"] == FAILED else None,
        **_saved_lifecycle(data_root, project_root, read_clock, path.resolve())}
