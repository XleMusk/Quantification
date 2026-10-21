"""Supervised one-day disclosure collection and immutable catalog append.

There is no scheduler, retry loop or market-account runner here.  Planning and
status are read only.  Explicit execution uses the existing three CLIs, each
once, with an exact event day and the original catalog ancestry preserved.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import gzip
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT
from . import seat_daily as metadata

PLAN_SCHEMA = "public_seat_disclosure_daily_plan_v1"
RESULT_SCHEMA = "public_seat_disclosure_daily_result_v1"
CALENDAR = DATA_ROOT / "research/calendar/cn_a_share_2026_sse_v1.json"
CATALOG_ANCHOR = DATA_ROOT / "research/seat_disclosure_catalog/20261016_prospective_v1/catalog_v2.json"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "model_refit": False, "user_account_mutated": False}
_read, _ref, _hash, _atomic = metadata._read, metadata._ref, metadata._hash, metadata._atomic


def _now():
    return datetime.now(MARKET_TZ)


def _clock(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None or stamp > _now():
        raise ValueError("Disclosure metadata requires its aware actual clock")
    return stamp


def _source(path, day):
    root = Path(path).resolve()
    manifest_file = root / "manifest.json"
    if not manifest_file.is_file():
        return {"status": "missing" if not root.exists() else "existing_unknown_no_retry", "refs": []}
    manifest = _read(manifest_file)
    refs = [_ref(manifest_file)]
    query = manifest.get("query", {})
    if query.get("start") != day or query.get("end") != day or query.get("codes") != []:
        raise ValueError("The source must be this exact unfiltered event day")
    if manifest.get("status") != "ready":
        return {"status": "failed_no_retry" if manifest.get("status") == "failed" else "pending_no_retry",
            "source_status": manifest.get("status"), "error": manifest.get("error"), "refs": refs}
    if (query.get("source") != "Eastmoney" or query.get("pagination_version") != 2
            or manifest.get("completeness") != "provider_pages_only"
            or manifest.get("publication_timestamps_available") is not False
            or set(manifest.get("reports", {})) != {"summary", "buy", "sell"}):
        raise ValueError("The three original provider reports and pagination contract are required")
    _clock(manifest["completed_at"])
    for kind, report in manifest["reports"].items():
        pages, rows = report.get("pages"), report.get("rows")
        if type(pages) is not int or not 1 <= pages <= 100 or type(rows) is not int or rows < 0:
            raise ValueError("Saved source page/row counts are not complete")
        for page in range(1, pages + 1):
            refs.append(_ref(root / f"{kind}_{page:04d}.json.gz"))
    normalization = root / "normalization.json"
    if not normalization.is_file() or not (root / "events.parquet").is_file():
        return {"status": "collector_normalization_unresolved_no_retry", "refs": refs}
    normalized = _read(normalization)
    if (normalized.get("raw_future_performance_fields_used") is not False
            or normalized.get("publication_timestamps_available") is not False):
        raise ValueError("Saved normalization must preserve source and future-label boundaries")
    refs.append(_ref(normalization))
    return {"status": "ready", "source_available_at": manifest["completed_at"],
        "reports": manifest["reports"], "refs": refs,
        "official_market_coverage_verified": False}


def _batch(path, day, source):
    file = Path(path)
    if not file.is_file():
        return {"status": "existing_unknown_no_retry" if file.parent.exists() else "missing", "refs": []}
    item = _read(file)
    if (item.get("schema") != "public_seat_disclosure_batch_v1" or item.get("event_day") != day
            or item.get("status") not in {"completed_empty", "completed_nonempty"}
            or str(Path(item.get("source_root", "")).resolve()) != str(Path(source).resolve())):
        raise ValueError("Saved batch must bind the exact successful source and event day")
    _clock(item["created_at"])
    _clock(item["source_available_at"])
    return {"status": "ready", "reference": _ref(file), "source_available_at": item["source_available_at"],
        "batch_id": item["batch_id"], "refs": [_ref(file)]}


def _separate(out, paths):
    for file in paths:
        source = Path(file).resolve()
        if out == source or out in source.parents or source in out.parents:
            raise ValueError("Disclosure operation must remain outside its saved sources/catalog inputs")


def _recipe(before, day, prior_catalog, out, calendar, source, batch, saved_catalog, completion_receipt):
    out, source, batch = map(lambda p: Path(p).resolve(), (out, source, batch))
    catalog = Path(saved_catalog).resolve() if saved_catalog else out / "artifacts/catalog/catalog.json"
    steps = []
    if before["already_cataloged"]:
        catalog = Path(prior_catalog).resolve()
    elif before["saved_catalog"]:
        catalog = Path(saved_catalog).resolve()
    elif (before["source"]["status"] in {"missing", "ready"}
            and before["batch"]["status"] in {"missing", "ready"}):
        if before["source"]["status"] == "missing":
            steps.append({"id": "seat_disclosure_collect", "requires": [], "command": ["seat-collect",
                "--start", day, "--end", day, "--out", str(source), "--max-pages", "3", "--execute"],
                "expected_files": [str(source / "manifest.json"), str(source / "normalization.json"), str(source / "events.parquet")]})
        if before["batch"]["status"] == "missing":
            command = ["seat-disclosure-catalog", "batch", "--source", str(source), "--out", str(batch.parent)]
            if completion_receipt:
                command += ["--completion-receipt", str(Path(completion_receipt).resolve())]
            steps.append({"id": "seat_disclosure_batch", "requires": [s["id"] for s in steps],
                "command": command, "expected_files": [str(batch)]})
        steps.append({"id": "seat_disclosure_catalog", "requires": [s["id"] for s in steps],
            "command": ["seat-disclosure-catalog", "append", "--batch", str(batch),
                "--parent", str(Path(prior_catalog).resolve()), "--out", str(catalog)], "expected_files": [str(catalog)]})
    return {"schema": PLAN_SCHEMA, "day": day, "prior_catalog": _ref(prior_catalog), "calendar": _ref(calendar),
        "out": str(out), "source": str(source), "batch": str(batch), "catalog": str(catalog),
        "completion_receipt": str(Path(completion_receipt).resolve()) if completion_receipt else None,
        "saved_catalog": str(Path(saved_catalog).resolve()) if saved_catalog else None,
        "before": deepcopy(before), "before_sha256": _hash(before), "source_refs": deepcopy(before["source_refs"]),
        "steps": steps, "status": "planned_no_execution", "supplier_not_before": day + "T18:30:00+08:00",
        "automatic_retry": False, "new_source_official_market_coverage_verified": False, **FLAGS}


def next_plan(day, prior_catalog, out, *, calendar=CALENDAR, source=None, batch=None,
        saved_catalog=None, completion_receipt=None):
    out = Path(out).resolve()
    sessions, _ = metadata._sessions(calendar)
    if day not in sessions:
        raise ValueError("An exact real exchange trading day is required")
    prior = metadata._catalog(prior_catalog, _ref(CATALOG_ANCHOR))
    if any(d > day for d in prior["event_days"]):
        raise ValueError("A future event catalog cannot supply an earlier daily operation")
    source = Path(source).resolve() if source else out / "artifacts/source"
    batch = Path(batch).resolve() if batch else out / "artifacts/batch/batch.json"
    external = [prior_catalog, calendar]
    if source != out / "artifacts/source":
        external.append(source)
    if batch != out / "artifacts/batch/batch.json":
        external.append(batch.parent)
    if saved_catalog:
        external.append(Path(saved_catalog).parent)
    if completion_receipt:
        external.append(Path(completion_receipt).parent)
    _separate(out, external)
    already = day in prior["event_days"]
    source_observation = {"status": "not_required_existing_catalog", "refs": []} if already else _source(source, day)
    batch_observation = {"status": "not_required_existing_catalog", "refs": []} if already else _batch(batch, day, source)
    source_refs = [_ref(prior_catalog), _ref(calendar), *prior["refs"], *source_observation["refs"], *batch_observation["refs"]]
    saved = None
    if saved_catalog:
        saved = metadata._catalog(saved_catalog, prior["reference"])
        if day not in saved["event_days"]:
            raise ValueError("Saved completed catalog does not contain this exact event day")
        source_refs += saved["refs"]
    if completion_receipt:
        source_refs.append(_ref(completion_receipt))
    before = {"already_cataloged": already, "source": source_observation, "batch": batch_observation,
        "saved_catalog": saved["reference"] if saved else None,
        "prior_event_days": prior["event_days"], "source_refs": list({r["file"]: r for r in source_refs}.values())}
    return _recipe(before, day, prior_catalog, out, calendar, source, batch, saved_catalog, completion_receipt)


def validate_plan(plan, day, prior_catalog, out, *, calendar=CALENDAR, source=None, batch=None,
        saved_catalog=None, completion_receipt=None):
    before = plan.get("before")
    if not isinstance(before, dict) or _hash(before) != plan.get("before_sha256"):
        raise ValueError("Saved disclosure plan's original observation changed")
    for reference in before["source_refs"]:
        metadata._verify(reference)
    # Outputs may now exist. Reconstruct from the original before-observation,
    # never convert a submitted collector into a fresh cache or another query.
    expected = _recipe(before, day, prior_catalog, out, calendar,
        source or Path(out) / "artifacts/source", batch or Path(out) / "artifacts/batch/batch.json",
        saved_catalog, completion_receipt)
    if plan != expected:
        raise ValueError("Saved disclosure day/source/catalog/command plan cannot change")
    return plan


def status(out):
    """Read saved observation only; no validation loaders or producers run."""
    root = Path(out)
    file = root / "result.json"
    return _read(file) if file.is_file() else {"schema": RESULT_SCHEMA, "status": "not_run",
        "daily_complete": False, "catalog": None, "network_accessed": False, "network_access_unknown": False, **FLAGS}


def _network(source):
    for file in sorted(Path(source).glob("*.json.gz")):
        try:
            with gzip.open(file, "rt", encoding="utf-8") as stream:
                doc = json.load(stream)
            if doc.get("request") and doc.get("response", {}).get("success") is True:
                _clock(doc["retrieved_at"])
                return True
        except (KeyError, ValueError, OSError, EOFError):
            continue
    return None


def run(day, prior_catalog, out, *, run_step, execute=False, calendar=CALENDAR,
        source=None, batch=None, saved_catalog=None, completion_receipt=None,
        resume_saved_recovery=False, recover_step=None):
    out = Path(out).resolve()
    options = dict(calendar=calendar, source=source, batch=batch,
        saved_catalog=saved_catalog, completion_receipt=completion_receipt)
    plan_file, result_file = out / "plan.json", out / "result.json"
    if plan_file.is_file():
        plan = validate_plan(_read(plan_file), day, prior_catalog, out, **options)
    else:
        plan = next_plan(day, prior_catalog, out, **options)
    if not execute:
        return {**plan, "ready_for_execute": _now().date().isoformat() == day
            and _now() >= datetime.fromisoformat(plan["supplier_not_before"]),
            "network_accessed": False, "network_access_unknown": False}
    resumed = None
    recovery_before = None
    if result_file.is_file():
        saved = _read(result_file)
        if saved.get("plan") != _ref(plan_file):
            raise ValueError("Disclosure result no longer binds its exact command plan")
        if saved.get("daily_complete"):
            return saved
        if saved.get("completed_at") or any(v.get("status") != "completed" for v in saved.get("steps", {}).values()):
            if not resume_saved_recovery or recover_step is None:
                return saved  # Never retry a failed/unknown child automatically.
            attempted = [step for step in plan["steps"] if step["id"] in saved["steps"]
                and saved["steps"][step["id"]].get("status") not in {"completed", "blocked_by_prior_stage"}]
            if not attempted:
                return saved
            adopted = {}
            for step in attempted:
                observed = recover_step(step)  # Root callback only reads exact saved recovery.
                if (not isinstance(observed, dict) or observed.get("status") != "completed"
                        or observed.get("recovered_saved_child") is not True
                        or observed.get("producer_repeated") is not False):
                    return saved
                adopted[step["id"]] = {**observed,
                    "output_refs": [_ref(file) for file in step["expected_files"]]}
            resumed, recovery_before = deepcopy(saved), deepcopy(saved)
            resumed["steps"].update(adopted)
    fresh_work = resumed is None or any(resumed["steps"].get(s["id"], {}).get("status") != "completed" for s in plan["steps"])
    if fresh_work and (_now().date().isoformat() != day or _now() < datetime.fromisoformat(plan["supplier_not_before"])):
        raise ValueError("Explicit collection requires the actual D supplier window after18:30; no backfill")
    if not plan_file.is_file():
        _atomic(plan_file, plan)
    result = resumed if resumed is not None else (_read(result_file) if result_file.is_file() else {"schema": RESULT_SCHEMA,
        "day": day, "plan": _ref(plan_file), "prior_catalog": plan["prior_catalog"], "catalog": None,
        "source": plan["source"], "batch": plan["batch"], "steps": {}, "daily_complete": False,
        "network_accessed": False, "network_access_unknown": False, "automatic_retry": False,
        "official_market_coverage_verified": False, "started_at": _now().isoformat(), **FLAGS})
    if recovery_before is not None:
        number = len(result.get("recovery_history", [])) + 1
        original_file = out / f"recovery_before_{number:04d}.json"
        _atomic(original_file, recovery_before)
        result.setdefault("recovery_history", []).append({"before_result": _ref(original_file),
            "adopted_at": _now().isoformat(), "recovered_steps": list(adopted), "producer_repeated": False})
    _atomic(result_file, result)
    before = plan["before"]
    if not plan["steps"] and not (before["already_cataloged"] or before["saved_catalog"]):
        result.update(status="source_failed_or_unknown_no_retry", source_observation=before["source"])
    else:
        for step in plan["steps"]:
            if result["steps"].get(step["id"], {}).get("status") == "completed":
                continue
            if any(result["steps"].get(key, {}).get("status") != "completed" for key in step["requires"]):
                result["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
                continue
            result["steps"][step["id"]] = {"status": "started_progress_unknown_no_retry"}
            _atomic(result_file, result)
            try:
                observed = run_step(step)
                if observed.get("status") == "completed":
                    observed = {**observed, "output_refs": [_ref(file) for file in step["expected_files"]]}
                result["steps"][step["id"]] = observed
            except Exception as error:
                result["steps"][step["id"]] = {"status": "runner_failed_progress_unknown_no_retry",
                    "error": type(error).__name__ + ": " + str(error), "execution_progress_unknown": True}
            _atomic(result_file, result)
        completed = all(result["steps"].get(step["id"], {}).get("status") == "completed" for step in plan["steps"])
        if completed:
            try:
                final = metadata._catalog(plan["catalog"], plan["prior_catalog"])
                if day not in final["event_days"]:
                    raise ValueError("Completed append did not save this exact event day")
                result.update(status="completed_saved_disclosure_catalog_research_only", daily_complete=True,
                    catalog=final["reference"], event_days=final["event_days"],
                    source_available_at=next(b["source_available_at"] for b in final["batches"] if b["event_day"] == day))
            except Exception as error:
                result.update(status="saved_outputs_catalog_validation_failed_no_retry", error=type(error).__name__ + ": " + str(error))
        else:
            result["status"] = "failed_or_unresolved_saved_attempt_no_retry"
    if "seat_disclosure_collect" in result["steps"]:
        result["network_accessed"] = _network(plan["source"])
        result["network_access_unknown"] = result["network_accessed"] is None
    result["completed_at"] = _now().isoformat()
    _atomic(result_file, result)
    return result
