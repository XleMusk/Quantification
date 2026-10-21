"""Append original frozen Alpha158 inputs without consuming a paper account.

Missed days still need their own dated provider and frozen score batch for the
fixed causal-capacity prefix. They never acquire a historical publication.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..contracts import MARKET_TZ
from . import system_daily_refresh as daily
from .leader_close_daily import _saved_command
from .recent_alpha158_target_publication import PROTOCOL, _config, _model_contract
from .recent_source_alpha158 import METHOD_ID as SCORE_METHOD
from .recent_source_qlib_provider import METHOD_ID as PROVIDER_METHOD


SCHEMA = "alpha158_independent_close_production_v1"
PROFILE_KEY = "alpha158_production_profile"
STAGES = ("alpha_provider", "alpha_score", "alpha_publication")
DYNAMIC = {"alpha_provider": {"--package", "--out"},
    "alpha_score": {"--provider-view", "--start", "--end", "--out"},
    "alpha_publication": {"--batch-run", "--prefix-batch-run", "--out"}}
SUCCESS = {"completed_alpha158_source_context", "completed_alpha158_publication"}


def _one(command, flag):
    values = daily._values(command, flag)
    if len(values) != 1:
        raise ValueError("One original Alpha158 option required: " + flag)
    return values[0]


def _static(stage, command):
    result, index = [], 0
    while index < len(command):
        if command[index] in DYNAMIC[stage]:
            if index + 1 >= len(command) or command[index + 1].startswith("--"):
                raise ValueError("Incomplete original Alpha158 producer option")
            index += 2
        else:
            result.append(command[index]); index += 1
    return result


def _stamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Alpha158 evidence requires its actual timezone-aware clock")
    return stamp.astimezone(MARKET_TZ)


def _checked(reference):
    if daily._ref(reference["file"]) != reference:
        raise ValueError("Saved Alpha158 input or successful operation changed")
    return daily._read(reference["file"])


def _batch(reference, calendar, now):
    score = _checked(reference)
    provider_file = Path(score["provider_view"]).resolve() / "result.json"
    provider = daily._read(provider_file)
    computed, available = _stamp(score["actual_computed_at"]), _stamp(provider["created_at"])
    days, proofs = provider["recent_source_days"], provider["source_proofs"]
    if (score.get("schema") != "a_share_recent_source_alpha158_frozen_score_batch_v1"
            or score.get("status") != "completed_research_only_dated_score_batch"
            or score.get("method_id") != SCORE_METHOD
            or provider.get("schema") != "a_share_recent_complete_source_research_provider_v1"
            or provider.get("status") != "research_provider_completed"
            or provider.get("provider_method_id") != PROVIDER_METHOD
            or provider.get("calendar_sha256") != calendar["sha256"]
            or score["provider_result_sha256"] != daily._ref(provider_file)["sha256"]
            or score["source_proofs"] != proofs or [p["day"] for p in proofs] != days
            or not available <= computed <= now
            or not score["score_dates"] or score["score_dates"] != sorted(set(score["score_dates"]))
            or not set(score["score_dates"]) <= set(days)
            or score["score_dates"][-1] != days[-1]
            or any(record.get(k) is not False for record in (score, provider) for k in
                ("model_refit", "model_trained", "future_labels_used_for_score", "execution_admitted",
                 "selection_enabled", "strategy_validated"))
            or provider.get("recent_source_inventory_complete") is not True
            or provider.get("raw_prices_imputed") is not False
            or provider.get("recent_raw_original_fields_unchanged") is not True):
        raise ValueError("An own-day complete provider and pure frozen score batch with actual clocks are required")
    if any(_stamp(p.get("source_completed_at") or p["source_updated_at"]) > available for p in proofs):
        raise ValueError("Frozen scoring cannot precede its own source availability")
    score_file = Path(score["score_path"]).resolve()
    score_file.relative_to(Path(reference["file"]).parent)
    raw = Path(provider["recent_raw_observations_file"]).resolve()
    raw.relative_to(provider_file.parent)
    if (score["raw_observations_sha256"] != provider["recent_raw_observations_sha256"]
            or Path(score["raw_observations"]).resolve() != raw
            or daily._ref(raw)["sha256"] != provider["recent_raw_observations_sha256"]):
        raise ValueError("Alpha158 own dated raw observation bytes changed")
    return {"reference": reference, "score": score, "provider": provider,
        "provider_ref": daily._ref(provider_file), "scores_ref": daily._ref(score_file),
        "model": _model_contract(score), "days": days, "proofs": proofs}


def _publication(reference, batch_infos, day, execution, now):
    value = _checked(reference)
    current = [info for info in batch_infos if info["score"]["score_dates"] == [day]]
    if len(current) != 1:
        raise ValueError("Publication requires its exact saved single-day score batch")
    info = current[0]
    before = [b for b in batch_infos if max(b["score"]["score_dates"]) < day]
    stamp = _stamp(value["recorded_at"])
    source = value["source"]
    prefix = value["capacity_prefix"]
    if (value.get("schema") != "recent_alpha158_dated_target_publication_v1"
            or value.get("status") != "close_research_target_publication_only"
            or value.get("source_close_day") != day or value.get("proposed_execution_day") != execution
            or stamp.date().isoformat() != day or stamp > now
            or stamp < _stamp(day + "T15:05:00+08:00")
            or not _stamp(info["score"]["actual_computed_at"]) <= stamp
            or value.get("execution_binding_pending") is not False
            or value.get("publication_time_backfilled") is not False
            or value.get("execution_day_rolled_forward") is not False
            or value.get("case_count") != 2 or value.get("candidate_panel_only") is not True
            or [case.get("case") for case in value.get("cases", [])] != [case["id"] for case in PROTOCOL["cases"]]
            or value.get("protocol") != PROTOCOL or value.get("config") != _config(100000, 5).to_dict()
            or value.get("case_selection_performed") is not False
            or value.get("frozen_model_contract") != info["model"]
            or any(value.get(k) is not False for k in ("execution_admitted", "selection_enabled",
                "strategy_validated", "model_refit", "model_trained", "future_labels_used_for_score",
                "orders_generated", "account_created", "persistent_account_used", "engine_ledger_replay_completed"))
            or Path(source["batch_run"]).resolve() != Path(info["reference"]["file"])
            or source["batch_sha256"] != info["reference"]["sha256"]
            or source["source_proofs"] != info["proofs"]
            or source["score_sha256"] != info["scores_ref"]["sha256"]
            or source["provider_result_sha256"] != info["provider_ref"]["sha256"]
            or source["score_computed_at"] != info["score"]["actual_computed_at"]
            or source["source_available_at"] != info["provider"]["created_at"]
            or value["score_computed_at"] != info["score"]["actual_computed_at"]
            or value["provider_ready_at"] != info["provider"]["created_at"]
            or prefix.get("old_scores_recomputed") is not False or prefix.get("capacity_reset") is not False
            or prefix["score_dates"] != info["days"][:-1]
            or [(Path(p["batch_run"]).resolve(), p["batch_sha256"]) for p in prefix["batches"]]
                != [(Path(b["reference"]["file"]), b["reference"]["sha256"]) for b in before]
            or [p["score_sha256"] for p in prefix["batches"]] != [b["scores_ref"]["sha256"] for b in before]):
        raise ValueError("Only the original fixed-model, full-prefix, actual same-day Alpha158 publication is usable")
    return value


def _fixed_refs(commands):
    refs = {}
    for command in commands.values():
        for flag in ("--calendar", "--history-snapshot", "--model-run", "--ipo-evidence", "--research-master-run"):
            for file in daily._values(command, flag):
                path = Path(file).resolve()
                if path.is_dir():
                    path /= "manifest.json"
                refs[str(path)] = daily._ref(path)
    return list(refs.values())


def _bootstrap(head, registration, sessions, now):
    core = head["profile"]
    commands = {key: deepcopy(core["templates"][key]) for key in STAGES}
    original = registration["initial_profile"]["templates"]
    if any(_static(key, commands[key]) != _static(key, original[key]) for key in STAGES):
        raise ValueError("The independent producer must retain the original frozen Alpha158 commands")
    batches = [daily._ref(file) for file in [*daily._values(commands["alpha_publication"], "--prefix-batch-run"),
        _one(commands["alpha_publication"], "--batch-run")]]
    packages = [str(Path(p).resolve()) for p in daily._values(commands["alpha_provider"], "--package")]
    refs = [{"day": day, "source_folder": package, "plan": daily._ref(Path(package) / "plan.json"),
        "receipt": daily._ref(Path(package) / "receipt.json")}
        for day, package in zip(core["source_days"], packages)]
    infos = [_batch(ref, registration["calendar"], now) for ref in batches]
    latest = infos[-1]
    paths = core["output_paths"]
    if (Path(paths["alpha_provider"]["result"]).resolve() != Path(latest["provider_ref"]["file"])
            or daily._ref(paths["alpha_score"]["result"]) != batches[-1]
            or daily._ref(paths["alpha_score"]["scores"]) != latest["scores_ref"]):
        raise ValueError("Original Alpha158 output paths differ from its actual saved provider/score")
    return {"schema": SCHEMA, "system_registration": head["registration"],
        "source_day": core["last_source_day"], "source_days": list(core["source_days"]),
        "packages": packages, "source_refs": refs, "source_proofs": latest["proofs"],
        "commands": commands, "fixed_refs": _fixed_refs(commands), "score_batches": batches,
        "model_contract": latest["model"], "latest_provider": latest["provider_ref"],
        "latest_publication": daily._ref(paths["alpha_publication"]["result"]),
        "publication_source_day": core["last_source_day"], "missing_publication_days": [],
        "operations": [], **daily.FLAGS}


def _validate(profile, head, registration, sessions, now):
    original = registration["initial_profile"]["templates"]
    days, commands = profile["source_days"], profile["commands"]
    packages = profile["packages"]
    original_packages = [str(Path(p).resolve()) for p in daily._values(original["alpha_provider"], "--package")]
    if (profile.get("schema") != SCHEMA or profile.get("system_registration") != head["registration"]
            or set(commands) != set(STAGES)
            or any(_static(k, commands[k]) != _static(k, original[k]) for k in STAGES)
            or days != [d for d in sessions if days[0] <= d <= profile["source_day"]]
            or packages[:len(original_packages)] != original_packages
            or daily._values(commands["alpha_provider"], "--package") != packages
            or [r["day"] for r in profile["source_refs"]] != days
            or [r["source_folder"] for r in profile["source_refs"]] != packages
            or profile["missing_publication_days"] != sorted(set(profile["missing_publication_days"]))
            or any(d not in days for d in profile["missing_publication_days"])
            or profile["publication_source_day"] not in days
            or profile["publication_source_day"] in profile["missing_publication_days"]
            or any(profile.get(k) is not v for k,v in daily.FLAGS.items())):
        raise ValueError("Independent Alpha158 production changed its original method or source prefix")
    for ref in [*profile["fixed_refs"], *profile["operations"],
            *(ref for row in profile["source_refs"] for ref in (row["plan"], row["receipt"]))]:
        _checked(ref)
    infos = [_batch(ref, registration["calendar"], now) for ref in profile["score_batches"]]
    covered = []
    for info in infos:
        length = len(info["days"])
        if (info["model"] != profile["model_contract"] or info["days"] != days[:length]
                or info["proofs"] != profile["source_proofs"][:length]
                or set(covered).intersection(info["score"]["score_dates"])):
            raise ValueError("Frozen model or exact own-day score/source prefix changed")
        covered.extend(info["score"]["score_dates"])
        if info["score"]["score_dates"][-1] > profile["publication_source_day"]:
            _unpublished_score(info, profile, head, sessions)
    if (covered != days or infos[-1]["provider_ref"] != profile["latest_provider"]
            or [str(Path(p["source_folder"]).resolve()) for p in profile["source_proofs"]] != packages
            or daily._values(commands["alpha_publication"], "--prefix-batch-run")
                != [b["file"] for b in profile["score_batches"][:-1]]
            or _one(commands["alpha_publication"], "--batch-run") != profile["score_batches"][-1]["file"]
            or _one(commands["alpha_score"], "--start") != profile["source_day"]
            or _one(commands["alpha_score"], "--end") != profile["source_day"]):
        raise ValueError("Every original provider day needs exactly one saved score prefix; no gap or reset")
    pubday = profile["publication_source_day"]
    _publication(profile["latest_publication"], infos, pubday, sessions[sessions.index(pubday)+1], now)


def _unpublished_score(info, profile, head, sessions):
    """Reuse the original child terminal's bytes before a publication pins them."""
    day = info["score"]["score_dates"][-1]
    directory = Path(head["registration"]["file"]).parent
    operation = directory / "alpha158_context" / day.replace("-", "")
    if (info["score"]["score_dates"] != [day]
            or Path(info["reference"]["file"]) != operation / "artifacts/alpha_score/result.json"):
        raise ValueError("Unpublished context requires its original exact Alpha158 operation")
    plan_file = operation / "plan.json"
    plan = daily._read(plan_file)
    parent, source = plan["parent_profile"], plan["source"]
    index = profile["source_days"].index(day)
    package = Path(profile["packages"][index])
    if (plan.get("schema") != SCHEMA or plan.get("day") != day or plan.get("source_only") is not True
            or plan.get("system_registration") != head["registration"]
            or plan.get("execution_day") != sessions[sessions.index(day)+1]
            or parent["source_day"] != sessions[sessions.index(day)-1]
            or parent["source_days"] != profile["source_days"][:index]
            or parent["packages"] != profile["packages"][:index]
            or parent["score_batches"] != profile["score_batches"][:len(parent["score_batches"])]
            or parent["model_contract"] != profile["model_contract"]
            or source["day"] != day or Path(source["source_folder"]) != package
            or source["raw_sha256"] != info["proofs"][-1]["raw_daily_sha256"]
            or source["roster_raw_sha256"] != info["proofs"][-1]["roster_raw_sha256"]
            or any(plan.get(key) is not value for key,value in daily.FLAGS.items())):
        raise ValueError("Unpublished Alpha158 score lost its original source-only parent")
    _, steps = _steps(parent, day, package, operation, True)
    step = next(step for step in steps if step["id"] == "alpha_score")
    step["binding"] = {"kind": SCHEMA, "system_registration": head["registration"],
        "source": source, "plan": daily._ref(plan_file)}
    saved = daily._read(operation / "commands/alpha_score.json")
    _, _, _, outputs = daily._terminal(step, operation / "commands", day, saved)
    if outputs != [info["reference"], info["scores_ref"]]:
        raise ValueError("Unpublished Alpha158 score bytes differ from its original successful child terminal")


def _steps(parent, day, package, operation, source_only):
    commands = deepcopy(parent["commands"])
    folders = {key: operation / "artifacts" / key for key in STAGES}
    for key in STAGES:
        commands[key] = daily._set(commands[key], "--out", [folders[key]])
    commands["alpha_provider"] = daily._set(commands["alpha_provider"], "--package", [*parent["packages"], package])
    commands["alpha_score"] = daily._set(commands["alpha_score"], "--provider-view", [folders["alpha_provider"]])
    for flag in ("--start", "--end"):
        commands["alpha_score"] = daily._set(commands["alpha_score"], flag, [day])
    commands["alpha_publication"] = daily._set(commands["alpha_publication"], "--prefix-batch-run",
        [ref["file"] for ref in parent["score_batches"]])
    commands["alpha_publication"] = daily._set(commands["alpha_publication"], "--batch-run", [folders["alpha_score"] / "result.json"])
    selected = STAGES[:-1] if source_only else STAGES
    return commands, [{"id": key, "command": commands[key], "requires": [] if index == 0 else [STAGES[index-1]],
        "expected_files": [str(folders[key] / "result.json"),
            *([str(folders[key] / "dated_scores.parquet")] if key == "alpha_score" else [])]}
        for index,key in enumerate(selected)]


def _result(parent, commands, day, package, operation, source, source_only, registration, sessions, now):
    batch = daily._ref(operation / "artifacts/alpha_score/result.json")
    info = _batch(batch, registration["calendar"], now)
    if (info["days"] != [*parent["source_days"], day]
            or info["proofs"][:-1] != parent["source_proofs"]
            or info["score"]["score_dates"] != [day] or info["model"] != parent["model_contract"]
            or Path(info["proofs"][-1]["source_folder"]).resolve() != package
            or info["proofs"][-1]["raw_daily_sha256"] != source["raw_sha256"]
            or info["proofs"][-1]["roster_raw_sha256"] != source["roster_raw_sha256"]):
        raise ValueError("New Alpha158 provider/score did not retain the original source prefix and fixed model")
    updated = {**deepcopy(parent), "source_day": day, "source_days": [*parent["source_days"], day],
        "packages": [*parent["packages"], str(package)], "source_proofs": info["proofs"],
        "source_refs": [*parent["source_refs"], {"day": day, "source_folder": str(package),
            "plan": daily._ref(package / "plan.json"), "receipt": daily._ref(package / "receipt.json")}],
        "commands": commands, "latest_provider": info["provider_ref"],
        "score_batches": [*parent["score_batches"], batch]}
    if source_only:
        updated["missing_publication_days"].append(day)
    else:
        updated.update(latest_publication=daily._ref(operation / "artifacts/alpha_publication/result.json"),
            publication_source_day=day)
    return updated


def advance(run_dir, day, package, *, source_only):
    daily._exact_day(day)
    if type(source_only) is not bool:
        raise ValueError("Choose source-only context or actual same-day publication")
    directory, package = daily.opening_daily._destination(run_dir), Path(package).resolve()
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        calendar = registration["calendar"]
        payload = _checked(calendar)
        sessions, previous, status = daily._calendar_view(payload,
            f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
        if status != "pinned_static_exchange_calendar" or day not in sessions or day == sessions[-1]:
            raise ValueError("Exact source and next exchange session required")
        execution, now = sessions[sessions.index(day)+1], datetime.now(MARKET_TZ)
        source = daily._source(package, calendar, sessions, now)["proof"]
        if source["day"] != day or Path(source["source_folder"]).resolve() != package:
            raise ValueError("Alpha158 input must be the exact requested complete close package")
        operation = directory / ("alpha158_context" if source_only else "alpha158_production") / day.replace("-", "")
        peer = directory / ("alpha158_production" if source_only else "alpha158_context") / day.replace("-", "")
        original = directory / "operations" / day.replace("-", "") / "commands"
        claims = [file for key in STAGES for file in [original / (key + ".json"),
            *daily.command_child.paths(original, key).values()]]
        if (peer.exists() or any(file.exists() for file in claims)
                or any((original.parent / "artifacts" / key).exists() for key in STAGES)):
            raise ValueError("An original Alpha158 attempt exists; no second producer or mode-based retry")
        plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
        if operation.exists() and not plan_file.exists():
            raise ValueError("An unresolved Alpha158 operation cannot be replaced")
        if not plan_file.exists() and ((source_only and now < _stamp(execution + "T09:30:00+08:00"))
                or (not source_only and (now.date().isoformat() != day or now < _stamp(day + "T15:05:00+08:00")))):
            raise ValueError("Missed days allow context only; publication requires its real source day after15:05")
        active = head.get(PROFILE_KEY)
        if active is None:
            active = _bootstrap(head, registration, sessions, now)
        _validate(active, head, registration, sessions, now)
        plan = daily._read(plan_file) if plan_file.exists() else {"schema": SCHEMA, "day": day,
            "execution_day": execution, "source_only": source_only, "source": source,
            "system_registration": head["registration"], "parent_profile": deepcopy(active), **daily.FLAGS}
        parent = plan["parent_profile"]
        if (plan.get("schema") != SCHEMA or plan.get("day") != day or plan.get("execution_day") != execution
                or plan.get("source_only") is not source_only or plan.get("source") != source
                or plan.get("system_registration") != head["registration"]
                or parent["source_day"] != previous[day]
                or any(plan.get(k) is not v for k,v in daily.FLAGS.items())):
            raise ValueError("Alpha158 operation cannot change or skip its original parent/source/mode")
        _validate(parent, head, registration, sessions, now)
        daily._separate(operation, package, *parent["packages"],
            *[ref["file"] for ref in parent["fixed_refs"]],
            *[Path(ref["file"]).parent for ref in parent["score_batches"]])
        commands, steps = _steps(parent, day, package, operation, source_only)
        for step in steps:
            step["binding"] = {"kind": SCHEMA, "system_registration": head["registration"],
                "source": source, "plan": daily._ref(plan_file) if plan_file.exists() else None}
        if not plan_file.exists():
            daily._atomic(plan_file, plan)
        for step in steps:
            step["binding"]["plan"] = daily._ref(plan_file)
        receipt = daily._read(receipt_file) if receipt_file.exists() else {"schema": SCHEMA, "day": day,
            "source_only": source_only, "plan_ref": daily._ref(plan_file), "started_at": daily._now(),
            "steps": {}, "signals_published": False, "automatic_retry": False,
            "system_source_cursor_advanced": False, "engine_advanced": False,
            "original_accounts_advanced": False, "whole_system_daily_complete": False, **daily.FLAGS}
        if (receipt.get("schema") != SCHEMA or receipt.get("day") != day
                or receipt.get("source_only") is not source_only or receipt.get("plan_ref") != daily._ref(plan_file)
                or any(receipt.get(k) is not v for k,v in daily.FLAGS.items())):
            raise ValueError("The original Alpha158 production receipt changed")
        completed = receipt.get("status") in SUCCESS
        if not completed and active != parent:
            raise ValueError("Independent Alpha158 profile advanced before this unfinished operation")
        started = False
        for step in steps:
            command_receipt = operation / "commands" / (step["id"] + ".json")
            # Saved terminals may be adopted later; new publication-mode work
            # never starts on a different recording day.
            allow = source_only or (datetime.now(MARKET_TZ).date().isoformat() == day
                and datetime.now(MARKET_TZ) >= _stamp(day + "T15:05:00+08:00"))
            if completed and not command_receipt.exists():
                raise ValueError("Completed Alpha158 operation lost its original child evidence")
            started = started or (allow and not command_receipt.exists())
            result = _saved_command(step, operation, day, allow_start=allow and not completed)
            receipt["steps"][step["id"]] = result
            if result.get("status") != "completed":
                receipt.update(status="partial_alpha158_production_no_retry", producer_called=started,
                    signals_published=False)
                daily._atomic(receipt_file, receipt)
                return receipt
            if not completed:
                daily._atomic(receipt_file, receipt)
        try:
            updated = _result(parent, commands, day, package, operation, source, source_only,
                registration, sessions, datetime.now(MARKET_TZ))
            _validate(updated, head, registration, sessions, datetime.now(MARKET_TZ))
        except (OSError, KeyError, TypeError, ValueError) as error:
            if completed:
                raise
            receipt.update(status="partial_alpha158_production_no_retry", producer_called=started,
                signals_published=False, error=f"{type(error).__name__}: {error}")
            daily._atomic(receipt_file, receipt)
            return receipt
        expected_status = "completed_alpha158_source_context" if source_only else "completed_alpha158_publication"
        if completed:
            if (receipt.get("next_profile") != updated or receipt["status"] != expected_status
                    or receipt.get("signals_published") is not (not source_only)):
                raise ValueError("Completed Alpha158 operation changed its saved continuation")
        else:
            receipt.update(status=expected_status, next_profile=deepcopy(updated), completed_at=daily._now(),
                producer_called=started, signals_published=not source_only)
            daily._atomic(receipt_file, receipt)
        updated["operations"].append(daily._ref(receipt_file))
        if active != updated:
            if active != parent:
                raise ValueError("A completed Alpha158 operation cannot roll a later profile backward")
            head[PROFILE_KEY] = updated
            daily._atomic(directory / "head.json", head)
        return {**receipt, "next_profile": updated, "reused_saved_result": completed,
            "producer_called": False if completed else started}
