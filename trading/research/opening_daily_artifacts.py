"""Existing opening producers and small-file adoption for the daily coordinator."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from .stockdb_fixed_daily import _atomic, _checked, _reference

FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "prefill_allowed": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _head(route, source_ref):
    path = Path(route["source"]).resolve()/"head.json"
    if Path(source_ref["path"]).resolve() != path:
        raise ValueError("Opening route points at a different saved head")
    head = _read(_checked(source_ref))
    schemas = {"fixed": "stockdb_fixed_persistent_daily_research_v1",
        "eight_type": "leader_eight_type_published_close_shared_ledger_checkpoint_v1",
        "dated_catchup": "leader_dated_catchup_shared_engine_checkpoint_v1",
        "eight_type_residual": "leader_eight_type_residual_shared_engine_checkpoint_v1",
        "known_identity": "leader_known_identity_shared_engine_checkpoint_v1",
        "leader20": "leader_published_close_shared_ledger_checkpoint_v1",
        "alpha158": "recent_alpha158_published_close_shared_ledger_checkpoint_v1",
        "native_preopen": "native_saved_opening_source_v1",
        "native_lowvol": "native_lowvol_saved_opening_source_v1",
        "registered_research": "registered_research_saved_opening_source_v1"}
    if (head.get("schema") != schemas.get(route["family"])
            or head.get("candidate_panel_only") is not True
            or any(head.get(key) is not False for key in
                   ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Opening route source schema or research boundary changed")
    if route["family"] == "dated_catchup":
        cases = ["sector_catchup_leader__" + guard + "__" + cap
            for guard in ("unguarded", "two_close_envguard")
            for cap in ("causal_first_seen_cap20", "uncapped")]
        if list(head.get("cases", {})) != cases:
            raise ValueError("Dated catchup opening requires its fixed four separate cases")
    if route["family"] == "eight_type_residual":
        from .leader_eight_type_paper_view import CASE_KEYS
        if list(head.get("cases", {})) != ["__".join(key) for key in CASE_KEYS]:
            raise ValueError("Residual-exit opening requires its fixed thirty-two separate cases")
    if route["family"] == "known_identity":
        cases = ["cross_regime_candidate__" + guard + "__" + cap
            for guard in ("unguarded", "two_close_envguard")
            for cap in ("causal_first_seen_cap20", "uncapped")]
        if list(head.get("cases", {})) != cases:
            raise ValueError("Known-identity opening requires its fixed four separate cases")
    return head


def _refs(artifacts):
    result = {}
    for key, value in artifacts.items():
        if isinstance(value, dict):
            _checked(value)
            result[key] = value
        else:
            result[key] = _reference(value)
    return result


def _product(route, source_ref, refs):
    head = _head(route, source_ref)
    family = route["family"]
    saved_day, next_session, _ = _schedule(route, head)
    if family == "fixed":
        preview = _read(_checked(refs["preview"]))
        intent = _read(_checked(refs["intent"]))
        if (preview.get("status") != "paper_preview_only" or preview.get("execution_admitted") is not False
                or preview.get("task_method") != "saved_fixed_account_next_session_v1"
                or preview["strategy_id"] != route["signal"]
                or preview["source_cutoff"] != head["last_completed_day"]
                or Path(preview["source_run"]).resolve() != Path(head["source"]["run"]["path"]).resolve()
                or intent.get("schema") != "saved_fixed_account_intent_v1"
                or intent.get("strategy_id") != route["signal"]
                or intent.get("source_day") != preview["source_cutoff"]
                or intent.get("execution_day") != preview["execution_day"]
                or any(intent.get(key) is not flag for key, flag in FLAGS.items())
                or any(intent["refs"][key] != head["source"][old] for key, old in
                    (("source_run", "run"), ("account", "account"), ("protocol", "protocol"), ("replay", "replay")))):
            raise ValueError("Existing fixed opening artifacts belong to a different saved source")
        day, execution = preview["source_cutoff"], preview["execution_day"]
        expected_ids, count = [route["signal"]], 1
        ready_count, unavailable_count = 1, 0
        result_status = "paper_candidates_only"
    else:
        result = _read(_checked(refs["result"]))
        case_ids = head["case_ids"] if family in {"native_preopen", "native_lowvol", "registered_research"} else list(head["cases"])
        if (result.get("source_family", "eight_type") != family
                or result.get("source_head_sha256") != source_ref["sha256"]
                or Path(result["source_run_directory"]).resolve() != Path(route["source"]).resolve()
                or any(result.get(key) is not flag for key, flag in FLAGS.items())
                or result.get("status") not in {"paper_candidates_only", "not_ready"}
                or [row["case_id"] for row in result["cases"]] != case_ids
                or result["case_count"] != len(case_ids)):
            raise ValueError("Existing opening result belongs to a different saved source")
        if family in {"native_preopen", "native_lowvol"} and result.get("price_evidence") is not None:
            evidence = _read(_checked(result["price_evidence"]))
            if (evidence.get("source_day") != result["source_cutoff"]
                    or evidence.get("execution_admitted") is not False):
                raise ValueError("Native opening price evidence belongs to another source day or scope")
        day, execution = result["source_cutoff"], result["execution_day"]
        ready = [row for row in result["cases"] if row["status"] == "paper_preview_only"]
        expected_ids = [(row["strategy_id"] if family in {"native_preopen", "native_lowvol", "registered_research"} else
                         ("alpha158:" if family == "alpha158" else "") + row["case_id"]) for row in ready]
        count = result["case_count"]
        ready_count, unavailable_count = result["preview_count"], result["not_ready_count"]
        result_status = result["status"]
        if result.get("opening_pack"):
            if Path(result["opening_pack"]).resolve() != Path(refs["opening_pack"]["path"]).resolve():
                raise ValueError("Saved result and opening pack references differ")
        elif "opening_pack" in refs:
            raise ValueError("A not-ready source result cannot acquire an unrelated opening pack")
    if execution != next_session or (day is not None and day != saved_day):
        raise ValueError("Saved opening artifacts do not use the source's exact next session")
    order_count = None
    if "opening_pack" in refs:
        pack = _read(_checked(refs["opening_pack"]))
        if (pack.get("schema") != "a_share_opening_pack_v1" or pack.get("status") != "paper_candidates_only"
                or pack.get("execution_admitted") is not False or pack.get("automatic_strategy_selection") is not False
                or pack.get("source_cutoff") != day or pack.get("execution_day") != execution
                or [row["strategy_id"] for row in pack["strategies"]] != expected_ids):
            raise ValueError("Opening package does not match the exact saved source cases/date")
        if family == "fixed" and Path(pack["strategies"][0]["preview"]).resolve() != Path(refs["preview"]["path"]).resolve():
            raise ValueError("Fixed package references a different preview")
        order_count = pack["candidate_order_count"]
    return {"source_head": source_ref, "source_day": day, "execution_day": execution,
        "status": result_status, "family": family, "case_count": count,
        "ready_case_count": ready_count, "not_ready_case_count": unavailable_count,
        "candidate_order_count": order_count, "refs": refs,
        "account_accessed": False, "engine_advanced": False, "ledger_mutated": False, **FLAGS}


def adopt(route, source_ref, artifacts):
    """Bind existing completed artifacts; never regenerate them."""
    return _product(route, source_ref, _refs(artifacts))


def validate_product(route, source_ref, product):
    if product != _product(route, source_ref, product["refs"]):
        raise ValueError("Saved opening product changed after successful generation")


def _schedule(route, head):
    from .leader_daily_signals import _calendar_view
    if route["family"] in {"native_preopen", "native_lowvol", "registered_research"}:
        if route["family"] == "native_preopen":
            from .native_saved_opening import source_dependencies_current
        elif route["family"] == "registered_research":
            from .registered_research_saved_opening import source_dependencies_current
        else:
            from .native_lowvol_saved_opening import source_dependencies_current
        if not source_dependencies_current(Path(route["source"])/"head.json"):
            raise ValueError("Saved native account or plan changed; explicitly refresh its composite source")
        return head["source_day"], head["execution_day"], None
    if route["family"] == "fixed":
        record = _read(_checked(head["source"]["run"]))
        from .stockdb_fixed_daily import _calendar
        sessions = _calendar(record["month_end_schedule"])
        day = head["last_completed_day"]
        return day, next((value for value in sessions if value > day), None), record
    calendar = head["binding"]["calendar"]
    if route["family"] == "alpha158":
        path = Path(calendar)
        if sha256(path.read_bytes()).hexdigest() != head["binding"]["calendar_sha256"]:
            raise ValueError("Frozen model account calendar changed")
        calendar = _read(path)
    year = calendar["year"]
    sessions, _, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Exact saved exchange calendar required")
    days = [case["state"]["day"] for case in head["cases"].values() if case["state"].get("day")]
    if not days:
        raise ValueError("No processed research account day to preview")
    day = max(days)
    if day not in sessions:
        raise ValueError("Saved research cursor is outside its exact calendar")
    return day, next((value for value in sessions if value > day), None), None


def produce(route, source_ref, output_dir):
    """Only the coordinator's changed-source branch may call producers."""
    head = _head(route, source_ref)
    day, execution, record = _schedule(route, head)
    if execution is None:
        raise ValueError("Exact next exchange session is unavailable; do not roll dates")
    out = Path(output_dir).resolve()
    if out.exists():
        raise FileExistsError("Never regenerate an existing source-version output")
    out.mkdir(parents=True, exist_ok=False)
    if route["family"] == "fixed":
        from .saved_account_intent import export_request
        from .next_session_preview import build as preview
        from .opening_pack import build as opening
        run = head["source"]["run"]["path"]
        intent = export_request(run, route["signal"], execution)
        _atomic(out/"intent.json", intent)
        preview_dir, _ = preview(run, route["signal"], day, execution, saved_intent=intent)
        pack_dir, _ = opening([preview_dir/"result.json"], execution_day=execution, strategy_id=route["signal"])
        artifacts = {"preview": preview_dir/"result.json", "opening_pack": pack_dir/"opening_pack.json", "intent": out/"intent.json"}
        _atomic(out/"result.json", {"schema": "fixed_saved_opening_artifacts_v1", "source_head": source_ref,
            "source_day": day, "execution_day": execution, "created_at": datetime.now(MARKET_TZ).isoformat(),
            "artifacts": {key: str(value) for key, value in artifacts.items()}, **FLAGS})
        artifacts["result"] = out/"result.json"
    elif route["family"] in {"native_preopen", "native_lowvol", "registered_research"}:
        if route["family"] == "native_preopen":
            from .native_saved_opening import build_opening
        elif route["family"] == "registered_research":
            from .registered_research_saved_opening import build_opening
        else:
            from .native_lowvol_saved_opening import build_opening
        directory, result = build_opening(Path(route["source"])/"head.json", out/"package")
        artifacts = {"result": directory/"result.json"}
        if result["opening_pack"]:
            artifacts["opening_pack"] = result["opening_pack"]
    else:
        from .leader_eight_type_opening import build as published
        directory, result = published(route["source"], execution, out/"package", source_family=route["family"])
        artifacts = {"result": directory/"result.json"}
        if result["opening_pack"]:
            artifacts["opening_pack"] = result["opening_pack"]
    if _reference(Path(route["source"])/"head.json") != source_ref:
        raise ValueError("Saved source changed during opening production")
    return adopt(route, source_ref, artifacts)
