"""Adopt one old attached route using only its original successful children.

This is a saved-evidence projection. It never launches a command, creates a
publication, runs an Engine, or rebuilds an opening product.
"""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path

from . import system_daily_refresh as daily


SCHEMA = "system_daily_saved_route_recovery_v1"
ROUTES = {
    "dated_catchup": "leader_dated_catchup_daily",
    "residual_exit": "leader_eight_type_residual_daily",
    "known_identity": "leader_known_identity_daily",
    "entry_comparison": "leader_entry_comparison_daily",
    "public_seat": "seat_daily",
    "leader_type_router": "leader_type_router_daily",
    "router_repair": "leader_router_repair_daily",
}


def _recipe(route):
    return importlib.import_module("." + ROUTES[route], __package__)


def _exclusive_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _present_bindings(step, day, receipt_file, files):
    """Validate present partial evidence without mistaking it for a success."""
    spec, argv = daily._step_spec(step), daily._command(step["command"])
    receipt = daily._read(receipt_file) if receipt_file.is_file() else None
    request = daily._read(files["request"]) if files["request"].is_file() else None
    claim = daily._read(files["started"]) if files["started"].is_file() else None
    terminal = daily._read(files["terminal"]) if files["terminal"].is_file() else None
    refs = {"receipt": daily._ref(receipt_file) if receipt is not None else None,
        "request": daily._ref(files["request"]) if request is not None else None,
        "claim": daily._ref(files["started"]) if claim is not None else None,
        "terminal": daily._ref(files["terminal"]) if terminal is not None else None, "outputs": []}

    def reference(value, name, observed):
        if (not isinstance(value, dict) or value.get("file") != str(files[name].resolve())
                or not isinstance(value.get("sha256"), str)
                or (observed is not None and value != observed)):
            raise ValueError("Present saved evidence reference has different daily inputs or route binding")

    if receipt is not None:
        if (receipt.get("day") != day or receipt.get("step") != spec["id"]
                or receipt.get("command") != step["command"] or receipt.get("argv") != argv
                or receipt.get("expected_files") != spec["expected_files"]
                or ("binding" in receipt and receipt["binding"] != step.get("binding"))):
            raise ValueError("Present original receipt differs from the route day/step/command")
        reference(receipt.get("child_request_ref"), "request", refs["request"])
        if receipt.get("child_terminal_ref") is not None:
            reference(receipt["child_terminal_ref"], "terminal", refs["terminal"])
    if request is not None and (request.get("schema") != daily.command_child.REQUEST_SCHEMA
            or request.get("day") != day or request.get("step") != spec
            or request.get("argv") != argv or request.get("cwd") != str(daily.ROOT)
            or request.get("binding") != step.get("binding")
            or any(request.get(key) is not value for key, value in daily._command_flags(spec).items())):
        raise ValueError("Present original request differs from the saved route binding")
    request_refs = []
    if receipt is not None:
        request_refs.append(receipt["child_request_ref"])
    if claim is not None:
        if (claim.get("schema") != daily.command_child.START_SCHEMA
                or type(claim.get("supervisor_pid")) is not int or claim["supervisor_pid"] <= 0
                or (receipt is not None and receipt.get("child_pid") is not None
                    and receipt["child_pid"] != claim["supervisor_pid"])
                or ("day" in claim and claim["day"] != day)
                or ("step" in claim and claim["step"] != spec)
                or ("binding" in claim and claim["binding"] != step.get("binding"))
                or ("argv" in claim and claim["argv"] != argv)):
            raise ValueError("Present original child claim differs from the saved route binding")
        reference(claim.get("request_ref"), "request", refs["request"])
        request_refs.append(claim["request_ref"])
    if terminal is not None:
        if (terminal.get("schema") != daily.command_child.TERMINAL_SCHEMA
                or terminal.get("day") != day or terminal.get("step") != spec or terminal.get("argv") != argv
                or ("binding" in terminal and terminal["binding"] != step.get("binding"))
                or type(terminal.get("supervisor_pid")) is not int or terminal["supervisor_pid"] <= 0
                or (claim is not None and terminal["supervisor_pid"] != claim["supervisor_pid"])
                or (receipt is not None and receipt.get("child_pid") is not None
                    and receipt["child_pid"] != terminal["supervisor_pid"])
                or any(terminal.get(key) is not value for key, value in daily.FLAGS.items() if key != "network_accessed")):
            raise ValueError("Present original terminal differs from the saved route binding")
        reference(terminal.get("request_ref"), "request", refs["request"])
        reference(terminal.get("started_ref"), "started", refs["claim"])
        request_refs.append(terminal["request_ref"])
    if request_refs and any(value != request_refs[0] for value in request_refs[1:]):
        raise ValueError("Present saved child evidence has conflicting original request references")
    return receipt, terminal, refs


def _saved_step(directory, day, step):
    """An absent or unsuccessful terminal is unknown, never a new command."""
    receipt_file = directory / (step["id"] + ".json")
    files = daily.command_child.paths(directory, step["id"])
    receipt, terminal, partial_refs = _present_bindings(step, day, receipt_file, files)
    missing = [name for name in ("request", "started", "terminal") if not files[name].is_file()]
    if not receipt_file.is_file() or missing:
        return {"status": "saved_terminal_unavailable_no_execution", "missing_terminal_evidence": missing,
            "execution_progress_unknown": True, "command_invoked": False}, partial_refs
    if (terminal.get("status") != "completed" or terminal.get("exit_code") != 0
            or terminal.get("execution_progress_unknown") is not False):
        return {"status": "saved_unsuccessful_terminal_no_execution", "execution_progress_unknown": True,
            "original_terminal_status": terminal.get("status"), "command_invoked": False}, partial_refs
    request_ref, terminal_ref, _, output_refs = daily._terminal(step, directory, day, receipt)
    observation = {**receipt, "status": "completed", "execution_progress_unknown": False,
        "exit_code": 0, "output_refs": output_refs, "saved_only_adoption": True,
        "command_invoked": False, "producer_repeated": False}
    evidence = {"receipt": daily._ref(receipt_file), "request": request_ref,
        "claim": daily._ref(files["started"]), "terminal": terminal_ref, "outputs": output_refs}
    return observation, evidence


def _invoke(recipe, route, profile, day, plan, out, run_step):
    if route == "public_seat":
        return recipe.run_profile(profile, day, plan["package"], out,
            run_step=run_step, catalog=plan["catalog"])
    refs = plan["source_refs"]
    return recipe.run_profile(profile, day, refs[0]["file"], refs[1]["file"], out, run_step=run_step)


def _apply_prepared(directory, head, route, prepared, evidence_directory):
    profile_key = route + "_profile"
    current = head[profile_key]
    if current not in (prepared["before_profile"], prepared["after_profile"]):
        raise ValueError("The route has advanced or differs from the saved recovery before/after states")
    _validate_prepared(head, route, prepared)
    updated = deepcopy(head)
    updated[profile_key] = deepcopy(prepared["after_profile"])
    if updated != head:
        daily._atomic(directory / "head.json", updated)
    result = {key: value for key, value in prepared.items() if key not in ("before_profile", "after_profile")}
    result.update(status="recovered_saved_route_complete" if prepared["route_daily_complete"]
        else "recovered_saved_route_partial", system_head_mutated=updated != head,
        only_route_profile_changed=True, global_source_day=head["last_source_day"],
        completed_at=daily._now())
    daily._atomic(evidence_directory / "receipt.json", result)
    return result


def _validate_prepared(head, route, prepared):
    references = [prepared[key] for key in ("registration", "attachment", "plan", "projected_route_receipt",
        "original_global_receipt")]
    for evidence in prepared["command_evidence"].values():
        if evidence is not None:
            references.extend(evidence[key] for key in ("receipt", "request", "claim", "terminal")
                if evidence[key] is not None)
            references.extend(evidence["outputs"])
    if prepared.get("original_route_receipt_backup"):
        references.append(prepared["original_route_receipt_backup"])
    if any(daily._ref(reference["file"]) != reference for reference in references):
        raise ValueError("Prepared route recovery evidence changed")
    if head["registration"] != prepared["registration"] or head[route + "_attachment"] != prepared["attachment"]:
        raise ValueError("Prepared recovery belongs to another system or attachment")


def recover_route(run_dir, day, route):
    """Recover exactly one attached D even when the global source is later.

    The original global receipt and command receipts are immutable. The old
    route receipt is backed up before the original recipe projects new status.
    A prepared adoption permits an interrupted head write to resume without
    calling that recipe a second time.
    """
    if route not in ROUTES:
        raise ValueError("Only explicitly attached research routes can be recovered")
    daily._exact_day(day)
    directory = daily.opening_daily._destination(run_dir)
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        profile_key = route + "_profile"
        if head.get(profile_key) is None:
            raise ValueError("The original explicit route attachment is required")
        profile = head[profile_key]
        if profile.get("last_source_day", day) > day:
            raise ValueError("A route already beyond D cannot be rolled back")
        operation = directory / "operations" / day.replace("-", "")
        out = operation / route
        plan_file = out / "plan.json"
        plan = daily._read(plan_file)  # absent plans must never be generated here
        if plan.get("source_day") != day or not plan.get("steps"):
            raise ValueError("Recovery requires the exact original nonempty attached route plan")
        bound_steps = {}
        for step in plan["steps"]:
            original_operation, bound = daily._recovery_step(directory, day, step["id"], head,
                registration, allow_historical_attached=True)
            if original_operation != operation or bound["binding"]["kind"] != route or step["id"] in bound_steps:
                raise ValueError("Saved step belongs to another operation or route")
            bound_steps[step["id"]] = bound
        evidence_directory = directory / "route_recoveries" / day.replace("-", "") / route
        prepared_file = evidence_directory / "adoption.json"
        final_file = evidence_directory / "receipt.json"
        if prepared_file.exists():
            prepared = daily._read(prepared_file)
            if prepared.get("schema") != SCHEMA or prepared.get("route") != route or prepared.get("day") != day:
                raise ValueError("Saved recovery belongs to another route/day")
            if daily._ref(plan_file) != prepared["plan"]:
                raise ValueError("The original recovery plan changed")
            if final_file.exists():
                # Validate the prepared evidence even on a no-write repeat.
                if profile != prepared["after_profile"]:
                    raise ValueError("Final recovery receipt requires its exact adopted route profile")
                _validate_prepared(head, route, prepared)
                return {**daily._read(final_file), "reused_saved_recovery": True, "system_head_mutated": False}
            return _apply_prepared(directory, head, route, prepared, evidence_directory)
        if (evidence_directory / "claim.json").exists():
            return {"schema": SCHEMA, "status": "unknown_interrupted_route_projection_no_retry",
                "route": route, "day": day, "system_head_mutated": False,
                "execution_progress_unknown": True, **daily.FLAGS}
        observations, commands = {}, {}
        for step in plan["steps"]:
            observations[step["id"]], commands[step["id"]] = _saved_step(
                operation / "commands", day, bound_steps[step["id"]])
        # No incomplete publisher is executed, replaced, or backdated. Existing
        # consumers can remain unavailable while a complete publication prefix
        # is adopted, exactly as in the original recipes.
        producer_ids = [step["id"] for step in plan["steps"] if not step["id"].endswith("_forward")]
        if not producer_ids or any(observations[key]["status"] != "completed" for key in producer_ids):
            return {"schema": SCHEMA, "status": "unknown_saved_route_publication_incomplete",
                "route": route, "day": day, "steps": observations,
                "producer_complete": False, "consumer_complete": False,
                "system_head_mutated": False, "execution_progress_unknown": True, **daily.FLAGS}
        recipe = _recipe(route)
        original_receipt = out / "receipt.json"
        original_bytes = original_receipt.read_bytes() if original_receipt.exists() else None
        original_head = deepcopy(head)
        global_receipt_ref = daily._ref(operation / "receipt.json")
        evidence_directory.mkdir(parents=True, exist_ok=True)
        _exclusive_json(evidence_directory / "claim.json", {"schema": SCHEMA, "route": route,
            "day": day, "started_at": daily._now(), "plan": daily._ref(plan_file), **daily.FLAGS})
        backup = None
        if original_bytes is not None:
            backup_path = evidence_directory / "original_route_receipt.json"
            with backup_path.open("xb") as handle:
                handle.write(original_bytes)
            backup = daily._ref(backup_path)

        def saved_only(step):
            if step["id"] not in bound_steps or daily._step_spec(step) != daily._step_spec(bound_steps[step["id"]]):
                raise ValueError("The original recipe requested an unregistered recovery command")
            return deepcopy(observations[step["id"]])

        try:
            answer = _invoke(recipe, route, profile, day, plan, out, saved_only)
            after = answer.get("next_profile")
            if after is None or after.get("last_source_day") != day:
                raise ValueError("Saved successful publication did not produce the exact route prefix")
            if daily._load(directory)[0] != original_head:
                raise ValueError("System head changed during the saved-only route projection")
            if daily._ref(operation / "receipt.json") != global_receipt_ref:
                raise ValueError("Saved-only recovery changed the original global receipt")
            result = answer["result"]
            complete = (result.get("daily_complete") is True if route == "public_seat" else
                result.get("producer_complete") is True and
                (result.get("consumer_required") is False or result.get("consumer_complete") is True))
            prepared = {"schema": SCHEMA, "route": route, "day": day, "prepared_at": daily._now(),
                "registration": head["registration"], "attachment": head[route + "_attachment"],
                "plan": daily._ref(plan_file), "original_route_receipt_backup": backup,
                "original_global_receipt": global_receipt_ref,
                "projected_route_receipt": daily._ref(original_receipt), "command_evidence": commands,
                "before_profile": deepcopy(profile), "after_profile": deepcopy(after),
                "producer_complete": result.get("producer_complete"),
                "consumer_complete": result.get("consumer_complete"), "route_daily_complete": complete,
                "route_result_status": result.get("status"), "global_receipt_unchanged": True,
                "saved_consumer_result": result.get("consumer_result"),
                "saved_consumer_status": result.get("consumer_status"),
                "saved_consumer_execution_progress_unknown": result.get("execution_progress_unknown"),
                "saved_branch_observations": deepcopy(result.get("branches")) if route == "public_seat" else None,
                "opening_refresh_required": True,
                "new_commands_invoked": False, "opening_rebuilt": False, "engine_advanced": False,
                "user_account_mutated": False, **daily.FLAGS}
            _exclusive_json(prepared_file, prepared)
        except Exception as error:
            _exclusive_json(evidence_directory / "projection_failure.json", {"schema": SCHEMA,
                "status": "unknown_route_projection_failed_no_retry", "route": route, "day": day,
                "error": type(error).__name__ + ": " + str(error), "system_head_mutated": False,
                "original_route_receipt_backup": backup, "execution_progress_unknown": True, **daily.FLAGS})
            raise
        return _apply_prepared(directory, head, route, prepared, evidence_directory)
