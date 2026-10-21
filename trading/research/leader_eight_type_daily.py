"""Continue original eight-type inputs/publications without consuming accounts.

One missed source day may be appended as late context. It is never converted to
a historical publication, and it does not authorize a skipped account session.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..contracts import MARKET_TZ
from . import system_daily_refresh as daily
from .leader_close_daily import _saved_command

SCHEMA = "eight32_independent_close_production_v1"
PROFILE_KEY = "eight32_production_profile"
STAGES = ("extras", "preview", "environment", "observe", "eight_inputs", "paper32")
DYNAMIC = {
    "extras": {"--package", "--out"}, "preview": {"--package", "--out"},
    "environment": {"--preview", "--bulk-extras", "--out"},
    "observe": {"--analysis"},
    "eight_inputs": {"--append-from", "--preview", "--turnover-source", "--out"},
    "paper32": {"--panel", "--out"}}
SUCCESS = {"completed_eight32_source_context", "completed_eight32_publication"}


def _one(command, flag):
    values = daily._values(command, flag)
    if len(values) != 1:
        raise ValueError("One original eight-type option required: " + flag)
    return values[0]


def _static(step, command):
    result, index = [], 0
    while index < len(command):
        if command[index] in DYNAMIC[step]:
            if index + 1 >= len(command) or command[index + 1].startswith("--"):
                raise ValueError("Incomplete eight-type producer option")
            index += 2
        else:
            result.append(command[index]); index += 1
    return result


def _manifest(reference, days, packages):
    if daily._ref(reference["file"]) != reference:
        raise ValueError("The saved eight-type input manifest changed")
    value = daily._read(reference["file"])
    if (value["source_days"] != days or [r["day"] for r in value["source_proofs"]] != days
            or [str(Path(r["source_folder"]).resolve()) for r in value["source_proofs"]] != packages
            or daily._ref(value["raw_file"])["sha256"] != value["raw_sha256"]):
        raise ValueError("Eight-type input/source/raw prefix changed")
    return value


def _publication(reference, manifest_ref, day, execution, now):
    if daily._ref(reference["file"]) != reference:
        raise ValueError("The saved eight-type publication changed")
    value = daily._read(reference["file"])
    stamp = datetime.fromisoformat(value["recorded_at"])
    if (stamp.utcoffset() is None or stamp > now or stamp.date().isoformat() != day
            or stamp < datetime.fromisoformat(day + "T15:05:00+08:00")
            or value.get("schema") != "leader_eight_type_unbound_close_paper_plan_v1"
            or value.get("status") != "unbound_current_close_paper_research"
            or value.get("source_close_day") != day or value.get("execution_day_bound") != execution
            or value.get("proposed_execution_day") != execution or value.get("case_count") != 32
            or value.get("candidate_panel_only") is not True
            or any(value.get(k) is not False for k in ("execution_admitted", "selection_enabled",
                "strategy_validated", "engine_advanced", "orders_generated", "account_created",
                "publication_time_backfilled", "execution_day_rolled_forward"))
            or Path(value["source"]["input_manifest"]).resolve() != Path(manifest_ref["file"]).resolve()
            or value["source"]["input_manifest_sha256"] != manifest_ref["sha256"]
            or datetime.fromisoformat(value["source"]["input_recorded_at"]) > stamp):
        raise ValueError("Only an actual same-day original32 publication may advance its publication cursor")
    return value


def _bootstrap(head, registration, sessions, now):
    core = head["profile"]
    commands = {k: deepcopy(core["templates"][k]) for k in STAGES}
    packages = [str(Path(p).resolve()) for p in daily._values(commands["preview"], "--package")]
    manifest = daily._ref(core["output_paths"]["eight_inputs"]["manifest"])
    record = _manifest(manifest, core["source_days"], packages)
    publication = daily._ref(core["output_paths"]["paper32"]["publication"])
    day = core["last_source_day"]
    _publication(publication, manifest, day, sessions[sessions.index(day) + 1], now)
    refs = {}
    for command in commands.values():
        for flag in ("--calendar", "--ipo-evidence", "--research-master-run", "--industry-snapshot",
                     "--protocol", "--registry", "--identity-registry", "--history-snapshot"):
            for file in daily._values(command, flag):
                path = Path(file).resolve()
                if path.is_dir():
                    path = path / "manifest.json"
                refs[str(path)] = daily._ref(path)
    sources = [{"day": day, "source_folder": package,
        "plan": daily._ref(Path(package) / "plan.json"),
        "receipt": daily._ref(Path(package) / "receipt.json")}
        for day, package in zip(record["source_days"], packages)]
    return {"schema": SCHEMA, "system_registration": head["registration"], "source_day": day,
        "source_days": list(core["source_days"]), "packages": packages, "source_refs": sources,
        "commands": commands, "fixed_refs": list(refs.values()), "latest_manifest": manifest,
        "latest_publication": publication, "publication_source_day": day,
        "publication_manifest": manifest, "missing_publication_days": [], "operations": [], **daily.FLAGS}


def _validate(profile, head, registration, sessions, now):
    original = registration["initial_profile"]["templates"]
    days = profile["source_days"]
    if (profile.get("schema") != SCHEMA or profile.get("system_registration") != head["registration"]
            or set(profile["commands"]) != set(STAGES)
            or any(_static(k, profile["commands"][k]) != _static(k, original[k]) for k in STAGES)
            or days != [d for d in sessions if days[0] <= d <= profile["source_day"]]
            or daily._values(profile["commands"]["preview"], "--package") != profile["packages"]
            or days != [r["day"] for r in profile["source_refs"]]
            or profile["packages"] != [r["source_folder"] for r in profile["source_refs"]]
            or profile["missing_publication_days"] != sorted(set(profile["missing_publication_days"]))
            or any(d not in days for d in profile["missing_publication_days"])
            or profile["publication_source_day"] not in days
            or profile["publication_source_day"] in profile["missing_publication_days"]
            or any(profile.get(k) is not v for k, v in daily.FLAGS.items())):
        raise ValueError("Independent eight-type production changed its original method/source prefix")
    for ref in [*profile["fixed_refs"], *profile["operations"],
            *(ref for source in profile["source_refs"] for ref in (source["plan"], source["receipt"]))]:
        if daily._ref(ref["file"]) != ref:
            raise ValueError("An original eight-type input or successful operation changed")
    _manifest(profile["latest_manifest"], days, profile["packages"])
    _publication(profile["latest_publication"], profile["publication_manifest"], profile["publication_source_day"],
        sessions[sessions.index(profile["publication_source_day"]) + 1], now)


def _steps(parent, day, package, operation, source_only):
    commands = deepcopy(parent["commands"])
    folders = {key: operation / "artifacts" / key for key in STAGES}
    packages = [*parent["packages"], str(package)]
    for key in ("extras", "preview", "environment", "eight_inputs", "paper32"):
        commands[key] = daily._set(commands[key], "--out", [folders[key]])
    commands["extras"] = daily._set(commands["extras"], "--package", [package])
    commands["preview"] = daily._set(commands["preview"], "--package", packages)
    commands["environment"] = daily._set(commands["environment"], "--preview",
        [folders["preview"] / d.replace("-", "") for d in [*parent["source_days"], day]])
    commands["environment"] = daily._set(commands["environment"], "--bulk-extras", [folders["extras"] / "result.json"])
    commands["observe"] = daily._set(commands["observe"], "--analysis", [folders["environment"] / "result.json"])
    for flag, values in (("--append-from", [parent["latest_manifest"]["file"]]),
            ("--preview", [folders["preview"] / day.replace("-", "")]),
            ("--turnover-source", [folders["extras"] / "result.json"])):
        commands["eight_inputs"] = daily._set(commands["eight_inputs"], flag, values)
    commands["paper32"] = daily._set(commands["paper32"], "--panel", [folders["eight_inputs"] / "manifest.json"])
    registry = Path(_one(commands["observe"], "--registry")).parent
    outputs = {"extras": [folders["extras"] / "result.json"], "preview": [folders["preview"] / "result.json"],
        "environment": [folders["environment"] / "result.json"],
        "observe": [registry / "observations" / day.replace("-", "") / name for name in
            ("observation.json", "observation_receipt.json")],
        "eight_inputs": [folders["eight_inputs"] / "manifest.json", folders["eight_inputs"] / "raw_daily.parquet"],
        "paper32": [folders["paper32"] / "paper_plan.json"]}
    depends = {"extras": [], "preview": [], "environment": ["extras", "preview"],
        "observe": ["environment"], "eight_inputs": ["observe", "preview", "extras"], "paper32": ["eight_inputs"]}
    selected = STAGES[:-1] if source_only else STAGES
    return commands, [{"id": key, "command": commands[key], "requires": depends[key],
        "expected_files": [str(p) for p in outputs[key]]} for key in selected]


def advance(run_dir, day, package, *, source_only):
    daily._exact_day(day)
    if type(source_only) is not bool:
        raise ValueError("Choose explicit source-only context or current publication")
    directory, package = daily.opening_daily._destination(run_dir), Path(package).resolve()
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        reference = registration["calendar"]
        if daily._ref(reference["file"]) != reference:
            raise ValueError("The original exchange calendar changed")
        calendar = daily._read(reference["file"])
        sessions, previous, state = daily._calendar_view(calendar, f"{calendar['year']}-01-01",
            f"{calendar['year']}-12-31", [])
        if state != "pinned_static_exchange_calendar" or day not in sessions or day == sessions[-1]:
            raise ValueError("An exact source session and next execution session are required")
        execution, now = sessions[sessions.index(day) + 1], datetime.now(MARKET_TZ)
        source = daily._source(package, reference, sessions, now)["proof"]
        if source["day"] != day or Path(source["source_folder"]).resolve() != package:
            raise ValueError("Eight-type production requires the exact complete source-D close")
        operation = directory / ("eight32_context" if source_only else "eight32_production") / day.replace("-", "")
        plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
        if not plan_file.exists() and ((source_only and now < datetime.fromisoformat(execution + "T09:30:00+08:00"))
                or (not source_only and (now.date().isoformat() != day
                    or now < datetime.fromisoformat(day + "T15:05:00+08:00")))):
            raise ValueError("Missed windows permit context only; publication requires the same source day after15:05")
        peer = directory / ("eight32_production" if source_only else "eight32_context") / day.replace("-", "")
        original = directory / "operations" / day.replace("-", "") / "commands"
        if peer.exists() or any((original / (key + ".json")).exists() or any(
                file.exists() for file in daily.command_child.paths(original, key).values()) for key in STAGES):
            raise ValueError("An original eight-type producer claim exists; no second path or mode retry")
        if operation.exists() and not plan_file.exists():
            raise ValueError("An unresolved eight-type operation cannot be replaced")
        active = head.get(PROFILE_KEY) or _bootstrap(head, registration, sessions, now)
        _validate(active, head, registration, sessions, now)
        plan = daily._read(plan_file) if plan_file.exists() else {"schema": SCHEMA, "day": day,
            "source": source, "source_only": source_only, "execution_day": execution,
            "system_registration": head["registration"], "parent_profile": deepcopy(active), **daily.FLAGS}
        parent = plan["parent_profile"]
        if (plan.get("schema") != SCHEMA or plan.get("day") != day or plan.get("source") != source
                or plan.get("source_only") is not source_only or plan.get("execution_day") != execution
                or plan.get("system_registration") != head["registration"] or parent["source_day"] != previous[day]
                or any(plan.get(k) is not v for k, v in daily.FLAGS.items())):
            raise ValueError("Eight-type production cannot change or skip its original source/profile/mode")
        _validate(parent, head, registration, sessions, now)
        daily._separate(operation, package, *parent["packages"], Path(parent["latest_manifest"]["file"]).parent,
            *[r["file"] for r in parent["fixed_refs"]])
        commands, steps = _steps(parent, day, package, operation, source_only)
        if not plan_file.exists():
            daily._atomic(plan_file, plan)
        receipt = daily._read(receipt_file) if receipt_file.exists() else {"schema": SCHEMA, "day": day,
            "source_only": source_only, "plan_ref": daily._ref(plan_file), "started_at": daily._now(), "steps": {},
            "system_source_cursor_advanced": False, "engine_advanced": False, "original_accounts_advanced": False,
            "whole_system_daily_complete": False, "automatic_retry": False, **daily.FLAGS}
        if (receipt.get("schema") != SCHEMA or receipt.get("day") != day
                or receipt.get("plan_ref") != daily._ref(plan_file) or receipt.get("source_only") is not source_only
                or any(receipt.get(k) is not v for k, v in daily.FLAGS.items())):
            raise ValueError("The saved eight-type operation changed")

        def continuation():
            latest = daily._ref(operation / "artifacts/eight_inputs/manifest.json")
            _manifest(latest, [*parent["source_days"], day], [*parent["packages"], str(package)])
            updated = {**deepcopy(parent), "source_day": day, "source_days": [*parent["source_days"], day],
                "packages": [*parent["packages"], str(package)], "commands": commands, "latest_manifest": latest,
                "source_refs": [*parent["source_refs"], {"day": day, "source_folder": str(package),
                    "plan": daily._ref(package / "plan.json"), "receipt": daily._ref(package / "receipt.json")} ]}
            if source_only:
                updated["missing_publication_days"].append(day)
            else:
                publication = daily._ref(operation / "artifacts/paper32/paper_plan.json")
                _publication(publication, latest, day, execution, datetime.now(MARKET_TZ))
                updated.update(latest_publication=publication, publication_source_day=day, publication_manifest=latest)
            return updated

        if receipt.get("status") in SUCCESS:
            updated = continuation()
            if receipt.get("next_profile") != updated or receipt.get("signals_published") is not (not source_only):
                raise ValueError("A completed eight-type continuation changed")
            updated["operations"].append(daily._ref(receipt_file))
            if active != updated:
                if active != parent:
                    raise ValueError("A successful result cannot roll a later eight-type head backwards")
                head[PROFILE_KEY] = updated
                daily._atomic(directory / "head.json", head)
            return {**receipt, "reused_saved_result": True, "producer_called": False}
        if active != parent:
            raise ValueError("The eight-type profile moved before its exact producer operation")
        called = False
        for step in steps:
            if any(receipt["steps"].get(key, {}).get("status") != "completed" for key in step["requires"]):
                break
            bound = {**step, "binding": {"kind": "eight32_independent_production", "registration": head["registration"],
                "source": source, "plan": daily._ref(plan_file)}}
            now = datetime.now(MARKET_TZ)
            allow = source_only or (now.date().isoformat() == day
                and now >= datetime.fromisoformat(day + "T15:05:00+08:00"))
            called = called or (allow and not (operation / "commands" / (step["id"] + ".json")).exists())
            receipt["steps"][step["id"]] = _saved_command(bound, operation, day, allow_start=allow)
            daily._atomic(receipt_file, receipt)
            if receipt["steps"][step["id"]]["status"] != "completed":
                break
        try:
            if any(receipt["steps"].get(step["id"], {}).get("status") != "completed" for step in steps):
                raise ValueError("An exact producer stage lacks successful terminal evidence")
            updated = continuation()
        except (OSError, KeyError, TypeError, ValueError) as error:
            receipt.update(status="partial_eight32_production_no_retry", signals_published=False,
                error=f"{type(error).__name__}: {error}")
            daily._atomic(receipt_file, receipt)
            return {**receipt, "producer_called": called}
        receipt.update(status="completed_eight32_source_context" if source_only else "completed_eight32_publication",
            signals_published=not source_only, completed_at=daily._now(), next_profile=deepcopy(updated))
        daily._atomic(receipt_file, receipt)
        updated["operations"].append(daily._ref(receipt_file))
        before = deepcopy(head)
        head[PROFILE_KEY] = updated
        assert {k:v for k,v in before.items() if k != PROFILE_KEY} == {k:v for k,v in head.items() if k != PROFILE_KEY}
        daily._atomic(directory / "head.json", head)
        return {**receipt, "producer_called": called}
