"""Independent saved-source and actual-publication progress for the fixed 20 cases.

Reuses leader-close-publication and the existing command supervisor. This path
cannot consume, repair, register, or reset an account.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ..contracts import MARKET_TZ
from . import system_daily_refresh as daily
from .leader_close_publication import _stamp


SCHEMA = "leader20_independent_close_production_v1"
PROFILE_KEY = "leader20_production_profile"
SUCCESS = {"completed_leader20_source_context", "completed_leader20_publication"}


def _static(command):
    result, index = [], 0
    while index < len(command):
        if command[index] in {"--package", "--decision-day", "--out"}:
            if index + 1 >= len(command) or command[index + 1].startswith("--"):
                raise ValueError("Incomplete original leader publication option")
            index += 2
        else:
            result.append(command[index])
            index += 1
    return result


def _one(command, flag):
    values = daily._values(command, flag)
    if len(values) != 1:
        raise ValueError("One original leader publication option required: " + flag)
    return values[0]


def _publication(path, day, execution, calendar_ref, command, now):
    value = daily._read(path)
    stamp = _stamp(value["recorded_at"])
    proof = value["publication_input_bundle"]
    cohort = value["frozen_cohort_registration"]
    if (value.get("schema") != "leader_close_target_publication_v1"
            or value.get("status") != "close_research_target_publication_only"
            or value.get("source_close_day") != day or value.get("proposed_execution_day") != execution
            or stamp.date().isoformat() != day or stamp > now
            or stamp < datetime.fromisoformat(day + "T15:05:00+08:00")
            or value.get("execution_binding_pending") is not False
            or value.get("execution_day_rolled_forward") is not False
            or value.get("publication_time_backfilled") is not False
            or value.get("ledger_checkpoint") is not None
            or value.get("case_count") != 20
            or value.get("candidate_panel_only") is not True
            or any(value.get(key) is not False for key in
                ("execution_admitted", "selection_enabled", "strategy_validated", "orders_generated", "account_created"))
            or Path(cohort["file"]).resolve() != Path(_one(command, "--cohort-registration")).resolve()
            or daily._ref(cohort["file"])["sha256"] != cohort["sha256"]
            or daily._ref(proof["file"])["sha256"] != proof["sha256"]):
        raise ValueError("Only an actual same-day fixed-cohort publication may advance the publication cursor")
    bundle = daily._read(proof["file"])
    if (Path(proof["file"]).resolve().parent.parent != Path(path).resolve().parent
            or _stamp(proof["recorded_at"]) > stamp
            or bundle["recorded_at"] != proof["recorded_at"]
            or daily._ref(bundle["raw_bars_file"])["sha256"] != bundle["raw_bars_sha256"]
            or bundle["bundle"]["calendar_sha256"] != calendar_ref["sha256"]):
        raise ValueError("The publication must retain its own earlier immutable input bundle")
    return value, bundle


def _bootstrap(head, registration, sessions, now):
    profile = head["profile"]
    command = deepcopy(profile["templates"]["pub20"])
    original = registration["initial_profile"]["templates"]["pub20"]
    if (command[0] != "leader-close-publication" or _static(command) != _static(original)
            or daily._values(command, "--package")[:len(daily._values(original, "--package"))]
                != daily._values(original, "--package")):
        raise ValueError("The independent producer must retain its original frozen cohort and source prefix")
    day = profile["last_source_day"]
    publication = profile["output_paths"]["pub20"]["publication"]
    _, bundle = _publication(publication, day, sessions[sessions.index(day) + 1],
        registration["calendar"], command, now)
    packages = daily._values(command, "--package")
    sources = bundle["bundle"]["source_packages"]
    selected = []
    for package in packages:
        rows = [row for row in sources if Path(row["folder"]).resolve() == Path(package).resolve()]
        if len(rows) != 1:
            raise ValueError("The original saved publication lost an exact source package")
        row = rows[0]
        selected.append({"day": row["day"], "source_folder": str(Path(package).resolve()),
            "plan_sha256": row["plan_sha256"], "receipt_sha256": row["receipt_sha256"],
            "raw_sha256": row["raw_daily_sha256"], "roster_raw_sha256": row["roster_raw_sha256"]})
    refs = [daily._ref(_one(command, flag)) for flag in
        ("--protocol", "--calendar", "--cohort-registration")]
    return {"schema": SCHEMA, "system_registration": head["registration"], "source_day": day,
        "source_days": [row["day"] for row in selected], "packages": [row["source_folder"] for row in selected],
        "source_proofs": selected, "command": command, "fixed_refs": refs,
        "latest_publication": daily._ref(publication), "publication_source_day": day,
        "missing_publication_days": [], "operations": [], **daily.FLAGS}


def _validate(profile, head, registration, sessions, now):
    original = registration["initial_profile"]["templates"]["pub20"]
    if (profile.get("schema") != SCHEMA or profile.get("system_registration") != head["registration"]
            or _static(profile["command"]) != _static(original)
            or any(profile.get(key) is not value for key, value in daily.FLAGS.items())
            or daily._values(profile["command"], "--package") != profile["packages"]
            or profile["packages"][:len(daily._values(original, "--package"))]
                != daily._values(original, "--package")
            or profile["source_days"] != [row["day"] for row in profile["source_proofs"]]
            or profile["packages"] != [row["source_folder"] for row in profile["source_proofs"]]
            or profile["source_days"] != [day for day in sessions
                if profile["source_days"][0] <= day <= profile["source_day"]]
            or profile["missing_publication_days"] != sorted(set(profile["missing_publication_days"]))
            or any(day not in profile["source_days"] for day in profile["missing_publication_days"])):
        raise ValueError("Independent leader production changed its registered method or contiguous source prefix")
    for reference in profile["fixed_refs"] + profile["operations"]:
        if daily._ref(reference["file"]) != reference:
            raise ValueError("A saved leader production input or completed operation changed")
    for row in profile["source_proofs"]:
        folder = Path(row["source_folder"])
        if (daily._ref(folder / "plan.json")["sha256"] != row["plan_sha256"]
                or daily._ref(folder / "receipt.json")["sha256"] != row["receipt_sha256"]):
            raise ValueError("An original leader source receipt changed")
    latest = profile["latest_publication"]
    if daily._ref(latest["file"]) != latest:
        raise ValueError("The latest saved leader publication changed")
    pub_day = profile["publication_source_day"]
    if pub_day not in profile["source_days"] or pub_day in profile["missing_publication_days"]:
        raise ValueError("The publication cursor contradicts the saved source or missed-publication days")
    _publication(latest["file"], pub_day, sessions[sessions.index(pub_day) + 1],
        registration["calendar"], profile["command"], now)


def _saved_command(step, operation, day, *, allow_start=True):
    files = daily.command_child.paths(operation / "commands", step["id"])
    receipt_file = operation / "commands" / (step["id"] + ".json")
    if not receipt_file.exists():
        if not allow_start:
            return {"status": "missed_publication_window_no_producer_started"}
        return daily._run_command(step, operation / "commands", day)
    receipt = daily._read(receipt_file)
    if receipt.get("command") != step["command"]:
        raise ValueError("A saved leader producer cannot change its original command")
    if receipt.get("status") == "completed":
        daily._terminal(step, operation / "commands", day, receipt)
        return receipt
    if files["terminal"].exists():
        try:
            _, terminal_ref, terminal, outputs = daily._terminal(step, operation / "commands", day, receipt)
        except (OSError, KeyError, TypeError, ValueError):
            return receipt
        return {**receipt, "status": "completed", "exit_code": terminal["exit_code"],
            "execution_progress_unknown": False, "child_terminal_ref": terminal_ref,
            "output_refs": outputs, "saved_success_adopted_without_production": True}
    return receipt


def advance(run_dir, day, package, *, source_only):
    daily._exact_day(day)
    if type(source_only) is not bool:
        raise ValueError("Choose source-only context or an actual current publication")
    directory, package = daily.opening_daily._destination(run_dir), Path(package).resolve()
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        reference = registration["calendar"]
        if daily._ref(reference["file"]) != reference:
            raise ValueError("The original exchange calendar changed")
        calendar = daily._read(reference["file"])
        sessions, previous, state = daily._calendar_view(calendar,
            f"{calendar['year']}-01-01", f"{calendar['year']}-12-31", [])
        if state != "pinned_static_exchange_calendar" or day not in sessions or day == sessions[-1]:
            raise ValueError("An exact source day and next exchange session are required")
        execution = sessions[sessions.index(day) + 1]
        now = datetime.now(MARKET_TZ)
        source = daily._source(package, reference, sessions, now)["proof"]
        if source["day"] != day or Path(source["source_folder"]).resolve() != package:
            raise ValueError("The close source does not match the requested exact day")
        operation = directory / ("leader20_context" if source_only else "leader20_production") / day.replace("-", "")
        plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
        files = daily.command_child.paths(operation / "commands", "pub20")
        saved_child = all(files[key].exists() for key in ("request", "started", "terminal"))
        if not receipt_file.exists() and not saved_child and ((source_only and now < datetime.fromisoformat(execution + "T09:30:00+08:00"))
                or (not source_only and (now.date().isoformat() != day
                    or now < datetime.fromisoformat(day + "T15:05:00+08:00")))):
            raise ValueError("Missed windows allow context only; actual publication requires the real source day after 15:05")
        peer = directory / ("leader20_production" if source_only else "leader20_context") / day.replace("-", "")
        original_commands = directory / "operations" / day.replace("-", "") / "commands"
        original_claims = [original_commands / "pub20.json",
            *daily.command_child.paths(original_commands, "pub20").values()]
        if peer.exists() or any(file.exists() for file in original_claims):
            raise ValueError("An original producer operation exists; no mode-based retry or second publication")
        if operation.exists() and not plan_file.exists():
            raise ValueError("An unresolved leader operation cannot be replaced")
        active = head.get(PROFILE_KEY)
        if active is None:
            active = _bootstrap(head, registration, sessions, now)
        _validate(active, head, registration, sessions, now)
        plan = daily._read(plan_file) if plan_file.exists() else {
            "schema": SCHEMA, "day": day, "execution_day": execution, "source_only": source_only,
            "system_registration": head["registration"], "source": source, "parent_profile": deepcopy(active), **daily.FLAGS}
        parent = plan["parent_profile"]
        if (plan.get("schema") != SCHEMA or plan.get("day") != day
                or plan.get("source_only") is not source_only or plan.get("execution_day") != execution
                or plan.get("source") != source or plan.get("system_registration") != head["registration"]
                or parent["source_day"] != previous[day]
                or any(plan.get(key) is not value for key, value in daily.FLAGS.items())):
            raise ValueError("Leader production cannot change or skip its original parent, day, source or mode")
        _validate(parent, head, registration, sessions, now)
        daily._separate(operation, package, *_input_paths(parent))
        if not plan_file.exists():
            daily._atomic(plan_file, plan)
        receipt = daily._read(receipt_file) if receipt_file.exists() else {
            "schema": SCHEMA, "day": day, "source_only": source_only, "plan_ref": daily._ref(plan_file),
            "started_at": daily._now(), "system_source_cursor_advanced": False, "engine_advanced": False,
            "whole_system_daily_complete": False, "automatic_retry": False, **daily.FLAGS}
        if (receipt.get("schema") != SCHEMA or receipt.get("day") != day
                or receipt.get("plan_ref") != daily._ref(plan_file) or receipt.get("source_only") is not source_only
                or any(receipt.get(key) is not value for key, value in daily.FLAGS.items())):
            raise ValueError("The original leader production receipt changed")
        if receipt.get("status") in SUCCESS:
            expected = deepcopy(receipt["next_profile"])
            expected_command = daily._set(parent["command"], "--package", [*parent["packages"], str(package)])
            expected_command = daily._set(expected_command, "--decision-day", [day])
            expected_command = daily._set(expected_command, "--out", [operation / "artifacts/pub20"])
            complete = {**deepcopy(parent), "source_day": day,
                "source_days": [*parent["source_days"], day], "packages": [*parent["packages"], str(package)],
                "source_proofs": [*parent["source_proofs"], source], "command": expected_command}
            if source_only:
                complete["missing_publication_days"].append(day)
            else:
                publication = operation / "artifacts/pub20/publication.json"
                _publication(publication, day, execution, reference, expected_command, now)
                complete.update(latest_publication=daily._ref(publication), publication_source_day=day)
            expected_status = "completed_leader20_source_context" if source_only else "completed_leader20_publication"
            if (expected != complete or receipt["status"] != expected_status
                    or receipt.get("signals_published") is not (not source_only)):
                raise ValueError("The completed result changed its exact source/publication continuation")
            expected["operations"].append(daily._ref(receipt_file))
            if active != expected:
                if active != parent:
                    raise ValueError("Completed leader production cannot roll a later head backwards")
                head[PROFILE_KEY] = deepcopy(expected)
                daily._atomic(directory / "head.json", head)
            return {**receipt, "reused_saved_result": True, "producer_called": False}
        if active != parent:
            raise ValueError("The independent leader profile moved before this operation")
        command = daily._set(parent["command"], "--package", [*parent["packages"], str(package)])
        command = daily._set(command, "--decision-day", [day])
        command = daily._set(command, "--out", [operation / "artifacts/pub20"])
        publication = operation / "artifacts/pub20/publication.json"
        updated = {**deepcopy(parent), "source_day": day, "source_days": [*parent["source_days"], day],
            "packages": [*parent["packages"], str(package)], "source_proofs": [*parent["source_proofs"], source],
            "command": command}
        receipt["producer_called"] = False
        if source_only:
            updated["missing_publication_days"].append(day)
            receipt.update(status="completed_leader20_source_context", signals_published=False)
        else:
            daily._atomic(receipt_file, receipt)
            step = {"id": "pub20", "command": command, "requires": [],
                "expected_files": [str(publication), str(publication.parent / "input_bundle/bundle.json")],
                "binding": {"kind": "leader20_independent_production", "registration": head["registration"],
                    "source": source, "plan": daily._ref(plan_file)}}
            allow_start = now.date().isoformat() == day and now >= datetime.fromisoformat(day + "T15:05:00+08:00")
            receipt["producer_called"] = allow_start and not (operation / "commands/pub20.json").exists()
            receipt["command_result"] = _saved_command(step, operation, day, allow_start=allow_start)
            daily._atomic(receipt_file, receipt)
            if receipt["command_result"]["status"] != "completed":
                receipt.update(status="partial_leader20_production_no_retry", signals_published=False)
                daily._atomic(receipt_file, receipt)
                return receipt
            try:
                _publication(publication, day, execution, reference, command, datetime.now(MARKET_TZ))
            except (OSError, KeyError, TypeError, ValueError) as error:
                receipt.update(status="partial_leader20_production_no_retry", signals_published=False,
                    error=f"{type(error).__name__}: {error}")
                daily._atomic(receipt_file, receipt)
                return receipt
            updated.update(latest_publication=daily._ref(publication), publication_source_day=day)
            receipt.update(status="completed_leader20_publication", signals_published=True)
        receipt.update(completed_at=daily._now(), next_profile=deepcopy(updated), original_accounts_advanced=False)
        # Profile holds only earlier receipts, avoiding a self-referential hash.
        daily._atomic(receipt_file, receipt)
        updated["operations"].append(daily._ref(receipt_file))
        before = deepcopy(head)
        head[PROFILE_KEY] = updated
        assert {k:v for k,v in before.items() if k != PROFILE_KEY} == {
            k:v for k,v in head.items() if k != PROFILE_KEY}
        daily._atomic(directory / "head.json", head)
        return {**receipt, "next_profile": updated}


def _input_paths(profile):
    return [*profile["packages"], *[r["file"] for r in profile["fixed_refs"]],
        Path(profile["latest_publication"]["file"]).parent, _one(profile["command"], "--history-snapshot")]
