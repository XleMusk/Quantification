"""One explicit native close through saved features, frozen scores and plans.

This producer chain never trains, advances an account or contacts a provider.
Existing dated artifacts may be explicitly adopted; unknown/failed work is not
silently rerun. The two policy plans retain their separate fixed identities.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

from ..contracts import MARKET_TZ
from ..worker import lock
from . import shortline_native_incremental as incremental
from . import shortline_native_preopen as preopen
from . import shortline_native_scoring as scoring
from .native_history_chain_source import NativeHistoryChainSource


SCHEMA = "native_shortline_daily_producer_operation_v1"
METHOD_ID = "native_incremental_frozen_score_two_fixed_preopen_policies_v1"
SOURCE_ONLY_STATUS = "saved_native_source_context_without_publication"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "model_refit": False,
    "future_labels_used_for_score": False, "network_accessed": False,
    "engine_advanced": False, "ledger_mutated": False, "account_accessed": False,
    "orders_generated": False, "old_outputs_modified": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _checked(reference):
    if _ref(reference["file"]) != reference:
        raise ValueError("A saved daily producer input or artifact changed")
    return Path(reference["file"])


def _atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
        allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _folder_refs(folder, names):
    folder = Path(folder).resolve()
    return {"directory": str(folder), "files": {
        name: _ref(folder / name) for name in names}}


def _model_ref(model):
    path = Path(model).resolve()
    path = path / "manifest.json" if path.is_dir() else path
    record = _read(path)
    files = [path]
    for row in record["models"]:
        for kind in ("model", "preprocessing"):
            file = (path.parent / row[kind + "_file"]).resolve()
            if file.parent != path.parent:
                raise ValueError("Frozen model files must remain in their existing generation")
            reference = _ref(file)
            if reference["sha256"] != row[kind + "_sha256"]:
                raise ValueError("Frozen model or preprocessing bytes changed")
            files.append(file)
    return {"manifest": _ref(path), "files": [_ref(file) for file in dict.fromkeys(files)]}


def _inputs(day, checkpoint, base_view, extension_views, full_closes,
        calendar, model, ipo_evidence, master_run, reuse):
    checkpoint = Path(checkpoint).resolve()
    parent_receipt = checkpoint.parent / "result.json" if _read(checkpoint).get("schema") == incremental.STATE_SCHEMA else checkpoint
    return {"day": day, "checkpoint": _ref(checkpoint),
        "checkpoint_receipt": _ref(parent_receipt),
        "base_view": _folder_refs(base_view, ("result.json", "plan.json", "receipts.json")),
        "extension_views": [_folder_refs(path, ("result.json", "plan.json", "receipts.json"))
            for path in extension_views],
        "full_closes": [_folder_refs(path, ("plan.json", "receipt.json")) for path in full_closes],
        "calendar": _ref(calendar), "model": _model_ref(model),
        "ipo_evidence": _ref(ipo_evidence), "master_run": _ref(master_run),
        "reuse": {key: _ref(path) for key, path in sorted(reuse.items())}}


def _validate_features(path, day, checkpoint):
    path = Path(path).resolve()
    result = _read(path)
    state, _ = incremental._load_checkpoint(path, datetime.now(MARKET_TZ))
    original, original_receipt = incremental._load_checkpoint(checkpoint, datetime.now(MARKET_TZ))
    if (result.get("schema") != incremental.RESULT_SCHEMA or result.get("source_day") != day
            or result.get("last_source_day") != day or state["day"] != day
            or result.get("read_days") != [day] or result.get("read_sessions") != 1
            or result.get("feature_history_recomputed") is not False
            or (original["day"] != day and result.get("parent_checkpoint_receipt") != original_receipt)
            or (original["day"] == day and Path(result["checkpoint_file"]).resolve() != Path(checkpoint).resolve())):
        raise ValueError("Daily features must append the exact supplied state, or explicitly adopt that same-day state")
    files = [path, Path(result["checkpoint_file"])]
    for field, hash_field in (("diagnostic_features_path", "diagnostic_features_sha256"),
            ("latest_features_path", "latest_features_sha256")):
        if result.get(field):
            file = Path(result[field]).resolve()
            if _ref(file)["sha256"] != result[hash_field]:
                raise ValueError("Saved daily feature CSV changed")
            files.append(file)
    return {"result": str(path), "checkpoint": result["checkpoint_file"],
        "score_input_published": result["score_input_published"],
        "eligible_stage123_rows": result["eligible_stage123_rows"],
        "feature_sha256": result.get("latest_features_sha256"),
        "refs": [_ref(file) for file in dict.fromkeys(files)]}


def _validate_score(path, publication, day, features, model):
    path, publication = Path(path).resolve(), Path(publication).resolve()
    result = _read(path)
    saved = preopen._saved_scores(publication, datetime.now(MARKET_TZ))
    manifest = Path(model).resolve()
    manifest = manifest / "manifest.json" if manifest.is_dir() else manifest
    if (Path(result["panel_result"]).resolve() != Path(features["result"]).resolve()
            or Path(result["model_manifest"]).resolve() != manifest
            or Path(saved["publication"]["batch_result_file"]).resolve() != path
            or saved["source_day"] != day):
        raise ValueError("Frozen scores must retain this exact D feature result and model generation")
    for proof in saved["proofs"].values():
        source = proof["feature_source"]
        if (source["source_sha256"] != features["feature_sha256"]
                or Path(proof["manifest_file"]).resolve() != manifest
                or proof["manifest_sha256"] != _ref(manifest)["sha256"]):
            raise ValueError("Saved score feature/model prefix differs from the supplied producer inputs")
    files = [publication, path]
    files.extend(Path(saved["publication"]["score_batch_directory"]) / row["path"]
        for row in saved["publication"]["files"])
    return {"result": str(path), "publication": str(publication),
        "published_at": saved["publication"]["published_at"],
        "refs": [_ref(file) for file in dict.fromkeys(files)]}


def _validate_plan(path, policy_id, day, scores, calendar, ipo_evidence, master_run):
    path = Path(path).resolve()
    saved = preopen.load_preopen_plan(path)
    record, registration = saved["record"], saved["registration"]
    if (record["policy_id"] != policy_id or saved["source_day"] != day
            or record["source_publication"] != preopen._read(scores["publication"])[1]
            or Path(record["calendar"]["file"]).resolve() != Path(calendar).resolve()):
        raise ValueError("Daily plan must retain its exact policy, source publication and calendar")
    if policy_id == preopen.QUALIFIED_POLICY_ID:
        expected = {"ipo_evidence": preopen._read(ipo_evidence)[1],
            "research_master_run": preopen._read(master_run)[1]}
        if registration["qualification_inputs"] != expected:
            raise ValueError("Qualified plan changed its explicit IPO or research-master inputs")
    files = [path] + [(path.parent / row["path"]).resolve() for row in record["files"]]
    return {"plan": str(path), "policy_id": policy_id, "source_day": day,
        "execution_day": saved["execution_day"], "published_at": record["published_at"],
        "refs": [_ref(file) for file in files]}


class _Stopped(Exception):
    pass


def _stage(operation, receipt, name, artifact, producer, validator, *, reused=False):
    previous = receipt["stages"].get(name)
    if previous and previous["status"] == "completed":
        for reference in previous["artifact"]["refs"]:
            _checked(reference)
        return previous["artifact"]
    if previous and previous["status"] == "failed":
        raise _Stopped(name + ": failed_same_input_no_retry")
    if previous:
        if not Path(artifact).is_file():
            raise _Stopped(name + ": unresolved_same_input_no_retry")
        # A complete saved artifact may be committed after interruption, but
        # the producer itself is never invoked a second time.
        try:
            value = validator()
        except Exception as error:
            receipt["stages"][name].update(status="failed", failed_at=_now(),
                error=f"{type(error).__name__}: {error}", producer_repeated=False)
            _atomic(operation / "receipt.json", receipt)
            raise _Stopped(name + ": " + receipt["stages"][name]["error"]) from error
        receipt["stages"][name].update(status="completed", completed_at=_now(),
            artifact=value, recovered_saved_artifact=True, producer_repeated=False)
        _atomic(operation / "receipt.json", receipt)
        return value
    receipt["stages"][name] = {"status": "started", "started_at": _now(),
        "artifact_file": str(Path(artifact).resolve()), "adopted_existing": reused,
        "producer_called": False, "execution_progress_unknown": not reused}
    _atomic(operation / "receipt.json", receipt)
    try:
        if not reused:
            receipt["stages"][name]["producer_called"] = True
            _atomic(operation / "receipt.json", receipt)
            producer()
        value = validator()
        receipt["stages"][name].update(status="completed", artifact=value,
            completed_at=_now(), execution_progress_unknown=False)
        _atomic(operation / "receipt.json", receipt)
        return value
    except Exception as error:
        receipt["stages"][name].update(status="failed", failed_at=_now(),
            error=f"{type(error).__name__}: {error}")
        _atomic(operation / "receipt.json", receipt)
        raise _Stopped(name + ": " + receipt["stages"][name]["error"]) from error


def run(day, checkpoint, *, base_view, extension_views, full_closes, calendar,
        model, ipo_evidence, master_run, out, reuse=None, source_only=False):
    """Advance one true new D, or adopt an explicit unchanged saved D chain.

    ``model`` is one existing six-model generation, preserving LR and LGBM.
    Optional ``reuse`` keys are features_result, score_result,
    score_publication, plan_v1 and plan_qualified. A continuing call uses the
    same ``out``; failures do not authorize another producer invocation.
    ``source_only`` explicitly records an already missed publication window;
    it appends only feature state, never frozen scores or portfolio plans.
    """
    if type(source_only) is not bool:
        raise ValueError("Source-only mode must be an explicit boolean")
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("One exact canonical native source day is required")
    if datetime.now(MARKET_TZ) < datetime.fromisoformat(day + "T15:00:00+08:00"):
        raise ValueError("A daily producer cannot manufacture a future or unclosed source day")
    close_days = [_read(Path(path) / "plan.json")["day"] for path in full_closes]
    if not close_days or close_days != sorted(set(close_days)) or close_days[-1] != day:
        raise ValueError("The explicit full-close prefix must end at the one exact requested D")
    reuse = dict(reuse or {})
    allowed = {"features_result", "score_result", "score_publication", "plan_v1", "plan_qualified"}
    gap = None
    if source_only:
        if set(reuse) - {"features_result"}:
            raise ValueError("Source-only mode may reuse only an exact saved features_result")
        execution_day, calendar_ref = preopen._calendar(calendar, day)
        deadline = execution_day + "T09:30:00+08:00"
        if datetime.fromisoformat(_now()) < datetime.fromisoformat(deadline):
            raise ValueError("Source-only mode requires the original exact next-opening publication window to have elapsed")
        gap = {"source_day": day, "missed_execution_day": execution_day,
            "publication_deadline": deadline, "calendar": calendar_ref,
            "reason": "original_next_open_publication_window_elapsed"}
    if set(reuse) - allowed or ("score_result" in reuse) != ("score_publication" in reuse):
        raise ValueError("Reuse requires explicit known artifact keys and the score result/publication pair")
    operation = Path(out).resolve()
    inputs = _inputs(day, checkpoint, base_view, extension_views, full_closes,
        calendar, model, ipo_evidence, master_run, reuse)
    if source_only:
        # Keep all default input hashes/receipts unchanged; opt-in mode is an
        # immutable part of this operation and cannot later turn into scoring.
        inputs = {**inputs, "source_only": True, "publication_gap": gap}
    input_id = sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    protected = [Path(checkpoint).resolve().parent, Path(base_view).resolve(),
        *[Path(path).resolve() for path in extension_views], *[Path(path).resolve() for path in full_closes],
        Path(calendar).resolve().parent,
        Path(model).resolve().parent if Path(model).suffix.lower() == ".json" else Path(model).resolve(),
        Path(ipo_evidence).resolve().parent,
        Path(master_run).resolve().parent, *[Path(path).resolve().parent for path in reuse.values()]]
    if any(operation == path or operation in path.parents or path in operation.parents for path in protected):
        raise ValueError("Daily operation must remain separate from all saved inputs and artifact packages")
    receipt_path = operation / "receipt.json"
    if receipt_path.exists():
        receipt = _read(receipt_path)
        if (receipt.get("schema") != SCHEMA or receipt.get("method_id") != METHOD_ID
                or receipt.get("input_id") != input_id or receipt.get("inputs") != inputs
                or any(receipt.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("A daily operation cannot replace its original date, checkpoint or input prefix")
    else:
        if operation.exists():
            raise FileExistsError("An operation without its original receipt has unknown progress")
        operation.mkdir(parents=True)
        receipt = {"schema": SCHEMA, "method_id": METHOD_ID, "input_id": input_id,
            "inputs": inputs, "source_day": day, "status": "started", "created_at": _now(),
            "stages": {}, **FLAGS}
        _atomic(receipt_path, receipt)
    with lock(operation / "operation.lock"):
        receipt = _read(receipt_path)
        if receipt["input_id"] != input_id:
            raise ValueError("Daily operation changed before execution")
        try:
            features_path = Path(reuse.get("features_result", operation / "features" / "result.json"))
            def features_producer():
                with NativeHistoryChainSource(base_view, extension_views, full_closes, calendar=calendar) as source:
                    incremental.advance_day(source, checkpoint, day, features_path.parent)
            features = _stage(operation, receipt, "features", features_path, features_producer,
                lambda: _validate_features(features_path, day, checkpoint), reused="features_result" in reuse)
            receipt["latest_checkpoint"] = features["checkpoint"]
            if source_only:
                feature_result = _read(features["result"])
                receipt.update(status=SOURCE_ONLY_STATUS, source_only=True, signals_published=False,
                    missing_plan_day=day, publication_gap={**gap, "observed_at": receipt["created_at"]},
                    reason=gap["reason"], source_proof=feature_result["source_proof"],
                    feature_computed_at=feature_result["computed_at"],
                    feature_available_at=feature_result["available_at"],
                    feature_result_ref=_ref(features["result"]),
                    score_publication_created=False, plan_publication_created=False,
                    no_change_manufactured=False, empty_target_manufactured=False,
                    completed_at=receipt.get("completed_at") or _now())
                _atomic(receipt_path, receipt)
                return {**receipt, "operation": str(receipt_path)}
            if not features["score_input_published"] or features["eligible_stage123_rows"] == 0:
                receipt.update(status="saved_state_without_scorable_input_no_plan",
                    reason="operational_history_unknown" if not features["score_input_published"] else "no_known_eligible_stage123_events",
                    no_change_manufactured=False, empty_target_manufactured=False, completed_at=_now())
                _atomic(receipt_path, receipt)
                return {**receipt, "operation": str(receipt_path)}
            score_path = Path(reuse.get("score_result", operation / "scores" / "result.json"))
            publication_path = Path(reuse.get("score_publication", score_path.parent / "publication.json"))
            scores = _stage(operation, receipt, "frozen_scores", publication_path,
                lambda: scoring.score_saved_generation(model, features_path, score_path.parent),
                lambda: _validate_score(score_path, publication_path, day, features, model),
                reused="score_result" in reuse)
            plans = {}
            for name, policy_id in (("plan_v1", preopen.POLICY_ID),
                    ("plan_qualified", preopen.QUALIFIED_POLICY_ID)):
                path = Path(reuse.get(name, operation / name / "plan.json"))
                options = {} if name == "plan_v1" else {"ipo_evidence": ipo_evidence,
                    "research_master_run": master_run}
                plans[name] = _stage(operation, receipt, name, path,
                    lambda path=path, options=options: preopen.build_preopen_plan(publication_path,
                        calendar, path.parent, **options),
                    lambda path=path, policy_id=policy_id: _validate_plan(path, policy_id,
                        day, scores, calendar, ipo_evidence, master_run), reused=name in reuse)
            receipt.update(status="completed_saved_native_daily_features_scores_two_policies",
                score_publication=scores["publication"], plans={name: value["plan"] for name, value in plans.items()},
                completed_at=receipt.get("completed_at") or _now())
        except _Stopped as error:
            receipt.update(status="stopped_failed_or_unresolved_step_no_retry", reason=str(error))
        _atomic(receipt_path, receipt)
    return {**receipt, "operation": str(receipt_path)}


def _values(argv, flag):
    return [argv[index + 1] for index, value in enumerate(argv) if value == flag]


def _one(argv, flag):
    values = _values(argv, flag)
    if len(values) != 1:
        raise ValueError("Saved native command needs exactly one " + flag)
    return values[0]


def _replace_values(argv, flag, values):
    output, inserted, index = [], False, 0
    while index < len(argv):
        if argv[index] == flag:
            if not inserted:
                for value in values:
                    output.extend((flag, str(value)))
                inserted = True
            index += 2
        else:
            output.append(argv[index])
            index += 1
    if not inserted:
        for value in values:
            output.extend((flag, str(value)))
    return output


def bootstrap_profile(receipt_root=None):
    """Read the successful 16 October command receipts; run no producer.

    This is an explicit inception helper, not a latest-directory/mtime search.
    Later dates use the returned next_profile rather than this old anchor.
    """
    directory = (Path(receipt_root).resolve() if receipt_root is not None else
        Path(__file__).resolve().parents[2] / "outputs/daily_refresh/20261016_v1")
    files = {name: directory / ("native_" + name + "_receipt.json") for name in
        ("daily_features", "frozen_score", "plan_v1", "plan_ipoqualified_v1",
         "forward_v1", "forward_ipoqualified_v1")}
    records = {name: _read(path) for name, path in files.items()}
    if any(record.get("returncode") != 0 or record.get("execution_admitted") is not False
            for record in records.values()):
        raise ValueError("Bootstrap requires the exact successful saved native command receipts")
    features, score = records["daily_features"]["argv"], records["frozen_score"]["argv"]
    features_result = Path(_one(features, "--out")) / "result.json"
    result = _read(features_result)
    day = result["last_source_day"]
    if day != "2026-10-16" or result.get("score_input_published") is not True:
        raise ValueError("Bootstrap anchor must remain the already completed 16 October state")
    plans = {"v1": str(Path(_one(records["plan_v1"]["argv"], "--out")) / "plan.json"),
        "qualified": str(Path(_one(records["plan_ipoqualified_v1"]["argv"], "--out")) / "plan.json")}
    templates = {}
    for kind, name in (("v1", "forward_v1"), ("qualified", "forward_ipoqualified_v1")):
        argv = list(records[name]["argv"])
        prefix = _values(argv, "--plan") + [plans[kind]]
        if len(prefix) != len(set(prefix)):
            raise ValueError("Bootstrap cannot duplicate a saved plan")
        argv = _replace_values(argv, "--plan", prefix)
        templates[kind] = {"argv": argv, "run_dir": _one(argv, "--run"),
            "plan_prefix": prefix, "raw_close_prefix": _values(argv, "--raw-close"),
            "result_path_must_be_replaced_before_call": True}
    qualified = records["plan_ipoqualified_v1"]["argv"]
    return {"schema": "native_shortline_daily_profile_v1", "method_id": METHOD_ID,
        "source_day": day, "checkpoint": result["checkpoint_file"],
        "base_view": _one(features, "--base-view"),
        "extension_views": _values(features, "--extension-view"),
        "full_closes": _values(features, "--full-close"), "calendar": _one(features, "--calendar"),
        "model": _one(score, "--model"), "ipo_evidence": _one(qualified, "--ipo-evidence"),
        "master_run": _one(qualified, "--research-master-run"),
        "score_publication": str(Path(_one(score, "--out")) / "publication.json"),
        "latest_plans": plans, "forward_templates": templates,
        "chain_has_missing_plan": False, "missing_plan_days": [],
        "bootstrap_receipts": {name: _ref(path) for name, path in files.items()},
        "checkpoint_ref": _ref(result["checkpoint_file"]),
        "bootstrap_only_read_saved_files": True, **FLAGS}


def _continued_profile(profile, day, package, result):
    """Derive the same producer continuation after execution or saved recovery."""
    next_profile = deepcopy(profile)
    if result["status"] in {"completed_saved_native_daily_features_scores_two_policies",
            "saved_state_without_scorable_input_no_plan", SOURCE_ONLY_STATUS}:
        package = str(Path(package).resolve())
        next_profile.update(source_day=day, checkpoint=result["latest_checkpoint"],
            full_closes=[*profile["full_closes"], package],
            checkpoint_ref=_ref(result["latest_checkpoint"]), last_operation=result["operation"])
        if result["status"] in {"saved_state_without_scorable_input_no_plan", SOURCE_ONLY_STATUS}:
            next_profile["chain_has_missing_plan"] = True
            next_profile["missing_plan_days"] = [*profile["missing_plan_days"], day]
        else:
            plans = {"v1": result["plans"]["plan_v1"], "qualified": result["plans"]["plan_qualified"]}
            next_profile.update(latest_plans=plans, score_publication=result["score_publication"])
            for kind, template in next_profile["forward_templates"].items():
                template["plan_prefix"].append(plans[kind])
                template["raw_close_prefix"].append(package)
                template["argv"] = _replace_values(template["argv"], "--plan", template["plan_prefix"])
                template["argv"] = _replace_values(template["argv"], "--raw-close", template["raw_close_prefix"])
                template["argv"] = _replace_values(template["argv"], "--through", [day])
    return next_profile


def run_profile(profile, day, package, out, *, source_only=False):
    """Produce the exact next D and return an explicit continuation profile.

    Forward templates are instructions for the caller, never executed here.
    Their --result-json must always be replaced with a new external result.
    """
    if type(source_only) is not bool:
        raise ValueError("Source-only mode must be an explicit boolean")
    if (profile.get("schema") != "native_shortline_daily_profile_v1"
            or profile.get("method_id") != METHOD_ID
            or any(profile.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("An unchanged native producer profile is required")
    _checked(profile["checkpoint_ref"])
    if day <= profile["source_day"]:
        raise ValueError("Existing native source days must be reused, never re-produced")
    package = str(Path(package).resolve())
    if package in [str(Path(path).resolve()) for path in profile["full_closes"]]:
        raise ValueError("The exact new D package cannot duplicate an old source")
    closes = [*profile["full_closes"], package]
    options = {"source_only": True} if source_only else {}
    result = run(day, profile["checkpoint"], base_view=profile["base_view"],
        extension_views=profile["extension_views"], full_closes=closes,
        calendar=profile["calendar"], model=profile["model"],
        ipo_evidence=profile["ipo_evidence"], master_run=profile["master_run"], out=out, **options)
    return {"result": result, "next_profile": _continued_profile(profile, day, package, result)}


def recover_saved_profile(profile, day, package, out, *, source_only):
    """Read and validate one complete child receipt; never invoke a producer.

    This closes only the interruption before a parent saved its continuation.
    Missing, partial, failed or changed artifacts are rejected without repair.
    """
    if (type(source_only) is not bool or profile.get("schema") != "native_shortline_daily_profile_v1"
            or profile.get("method_id") != METHOD_ID
            or any(profile.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Saved recovery requires the unchanged native profile and explicit original mode")
    _checked(profile["checkpoint_ref"])
    package, operation = Path(package).resolve(), Path(out).resolve()
    if (date.fromisoformat(day).isoformat() != day or day <= profile["source_day"]
            or _read(package / "plan.json").get("day") != day
            or str(package) in [str(Path(path).resolve()) for path in profile["full_closes"]]):
        raise ValueError("Saved recovery cannot replace or repeat the original source day")
    inputs = _inputs(day, profile["checkpoint"], profile["base_view"], profile["extension_views"],
        [*profile["full_closes"], str(package)], profile["calendar"], profile["model"],
        profile["ipo_evidence"], profile["master_run"], {})
    gap = None
    if source_only:
        execution_day, calendar_ref = preopen._calendar(profile["calendar"], day)
        gap = {"source_day": day, "missed_execution_day": execution_day,
            "publication_deadline": execution_day + "T09:30:00+08:00", "calendar": calendar_ref,
            "reason": "original_next_open_publication_window_elapsed"}
        inputs = {**inputs, "source_only": True, "publication_gap": gap}
    receipt_file = operation / "receipt.json"
    receipt = _read(receipt_file)
    allowed_status = ({SOURCE_ONLY_STATUS} if source_only else {
        "completed_saved_native_daily_features_scores_two_policies", "saved_state_without_scorable_input_no_plan"})
    no_scorable_plan = not source_only and receipt.get("status") == "saved_state_without_scorable_input_no_plan"
    expected_id = sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    required_stages = {"features"} if source_only or no_scorable_plan else {"features", "frozen_scores", "plan_v1", "plan_qualified"}
    if (receipt.get("schema") != SCHEMA or receipt.get("method_id") != METHOD_ID
            or receipt.get("source_day") != day or receipt.get("status") not in allowed_status
            or receipt.get("inputs") != inputs or receipt.get("input_id") != expected_id
            or set(receipt.get("stages", {})) != required_stages
            or any(receipt.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Saved native child is not the complete exact original input/mode receipt")

    def saved_stage(name, artifact, validator):
        stage = receipt["stages"][name]
        if (stage.get("status") != "completed" or stage.get("execution_progress_unknown") is not False
                or stage.get("artifact_file") != str(Path(artifact).resolve())):
            raise ValueError("Saved native child contains a partial or differently bound stage")
        for reference in stage.get("artifact", {}).get("refs", []):
            _checked(reference)
        validated = validator()
        if stage.get("artifact") != validated:
            raise ValueError("Saved native stage artifacts differ from their exact validated contracts")
        return validated

    features_file = operation / "features/result.json"
    features = saved_stage("features", features_file,
        lambda: _validate_features(features_file, day, profile["checkpoint"]))
    if receipt.get("latest_checkpoint") != features["checkpoint"]:
        raise ValueError("Saved native continuation changed its exact feature checkpoint")
    if source_only:
        feature_result = _read(features_file)
        if (receipt.get("source_only") is not True or receipt.get("signals_published") is not False
                or receipt.get("publication_gap") != {**gap, "observed_at": receipt["created_at"]}
                or datetime.fromisoformat(receipt["created_at"]) < datetime.fromisoformat(gap["publication_deadline"])
                or receipt.get("missing_plan_day") != day or receipt.get("reason") != gap["reason"]
                or receipt.get("source_proof") != feature_result["source_proof"]
                or receipt.get("feature_computed_at") != feature_result["computed_at"]
                or receipt.get("feature_available_at") != feature_result["available_at"]
                or receipt.get("feature_result_ref") != _ref(features_file)
                or any(receipt.get(key) is not False for key in ("score_publication_created", "plan_publication_created"))
                or "plans" in receipt or "score_publication" in receipt
                or any((operation / name).exists() for name in ("scores", "plan_v1", "plan_qualified"))):
            raise ValueError("Saved source-only child lost its actual gap, feature provenance or no-publication boundary")
    elif no_scorable_plan:
        reason = ("operational_history_unknown" if not features["score_input_published"]
            else "no_known_eligible_stage123_events" if features["eligible_stage123_rows"] == 0 else None)
        if (reason is None or receipt.get("reason") != reason or receipt.get("source_only") is True
                or any(receipt.get(key) is not False for key in ("no_change_manufactured", "empty_target_manufactured"))
                or "plans" in receipt or "score_publication" in receipt
                or any((operation / name).exists() for name in ("scores", "plan_v1", "plan_qualified"))):
            raise ValueError("Saved no-scorable-plan child changed its exact feature reason or no-publication boundary")
    else:
        score_file, publication = operation / "scores/result.json", operation / "scores/publication.json"
        scores = saved_stage("frozen_scores", publication,
            lambda: _validate_score(score_file, publication, day, features, profile["model"]))
        if receipt.get("source_only") is True or receipt.get("score_publication") != scores["publication"]:
            raise ValueError("Saved publication child changed its original mode or score publication")
        plans = {}
        for name, policy_id in (("plan_v1", preopen.POLICY_ID), ("plan_qualified", preopen.QUALIFIED_POLICY_ID)):
            path = operation / name / "plan.json"
            plans[name] = saved_stage(name, path, lambda path=path, policy_id=policy_id:
                _validate_plan(path, policy_id, day, scores, profile["calendar"],
                    profile["ipo_evidence"], profile["master_run"]))["plan"]
        if receipt.get("plans") != plans:
            raise ValueError("Saved publication child changed either exact original policy plan")
    result = {**receipt, "operation": str(receipt_file)}
    return {"result": result, "next_profile": _continued_profile(profile, day, package, result)}
