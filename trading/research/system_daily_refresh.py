"""One local complete close through the existing registered research routes.

The recipe reuses the successful production commands and original account
registrations. There is no provider login, new model fit, broker operation or
implicit migration of the old fixed engineering account.

Future child commands save independent terminal evidence. ``recover-step``
adopts only an exact successful saved command; it never runs a producer or
advances an account. Older interrupted attempts without that evidence, and
the separate in-process low-volatility consumer, remain unresolved.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT, ROOT
from . import opening_daily, system_daily_command_child as command_child
from .shortline_native_preopen_forward import _lock, _source
from .leader_daily_signals import _calendar_view


SCHEMA = "a_share_existing_routes_daily_refresh_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "broker_interacted": False,
    "model_refit": False, "network_accessed": False, "user_account_mutated": False}
ALLOWED = {"baostock-bulk-extras", "leader-close-publication", "leader-recent-source-preview",
    "leader-eight-type-forward-register",
    "leader-environment", "leader-identity", "leader-identity-episodes", "leader-eight-type-inputs",
    "leader-eight-type-paper", "leader-eight-type-forward", "leader-published-forward",
    "recent-source-qlib-provider", "recent-source-alpha158", "recent-source-alpha158-target-publication",
    "recent-source-alpha158-forward-run", "shortline-native-preopen-forward",
    "leader-dated-catchup-inputs", "leader-dated-catchup-paper",
    "leader-dated-identity", "leader-dated-catchup-forward", "leader-eight-type-residual",
    "leader-known-identity-paper", "leader-entry-confirmation-inputs", "leader-entry-comparison",
    "leader-entry-comparison-forward", "seat-append-source-audit", "seat-append-dated-inputs",
    "seat-append-forward", "seat-hypothesis-dated-inputs", "seat-hypothesis-dated-forward", "seat-daily",
    "seat-collect", "seat-disclosure-catalog", "leader-type-router-paper", "leader-type-router-forward",
    "native-intraday-paper", "leader-type-router-runtime-repair"}


def _attached_recipe(kind):
    if kind == "entry_comparison":
        from . import leader_entry_comparison_daily
        return leader_entry_comparison_daily
    if kind == "public_seat":
        from . import seat_daily
        return seat_daily
    if kind == "leader_type_router":
        from . import leader_type_router_daily
        return leader_type_router_daily
    if kind == "router_repair":
        from . import leader_router_repair_daily
        return leader_router_repair_daily
    raise ValueError("Unknown registered daily route")


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _separate(out, *inputs):
    for source in inputs:
        path = Path(source).resolve()
        if out == path or out in path.parents or path in out.parents:
            raise ValueError("Daily operation outputs must remain separate from existing source/account directories")


def _input_directories(profile, native, routes, lowvol, *, recent_action_extension=None):
    paths = [native["base_view"], *native["extension_views"], *native["full_closes"],
        Path(native["checkpoint"]).parent, Path(native["ipo_evidence"]).parent,
        Path(native["master_run"]).parent, lowvol["action_event_view"], lowvol["action_reference_view"],
        lowvol["run_directory"], lowvol["source_directory"]]
    model = Path(native["model"])
    paths.append(model if model.is_dir() else model.parent)
    paths.extend(value["run_dir"] for value in native["forward_templates"].values())
    paths.extend(route["source"] for route in routes)
    extension = recent_action_extension or lowvol.get("recent_action_extension")
    if extension is not None:
        from .recent_action_extension import extension_readiness
        observation = extension_readiness(extension, lowvol["action_event_view"],
            lowvol["action_reference_view"], native["calendar"])
        paths.extend(observation["input_directories"])
    for command in profile["templates"].values():
        for flag in ("--history-snapshot", "--protocol", "--cohort-registration", "--registration",
                     "--calendar", "--ipo-evidence", "--research-master-run", "--identity-registry",
                     "--industry-snapshot", "--model-run", "--run-dir", "--append-from"):
            for value in _values(command, flag):
                path = Path(value)
                paths.append(path if path.is_dir() else path.parent)
    return paths


def _values(argv, flag):
    return [argv[i+1] for i, value in enumerate(argv[:-1]) if value == flag]


def _set(argv, flag, values):
    result, index, insertion = [], 0, None
    while index < len(argv):
        if argv[index] == flag:
            insertion = len(result) if insertion is None else insertion
            index += 2
        else:
            result.append(argv[index]); index += 1
    insertion = len(result) if insertion is None else insertion
    result[insertion:insertion] = [token for value in values for token in (flag, str(value))]
    return result


def _command(argv):
    argv = list(argv)
    if any(Path(token).name == "run_committee.py" for token in argv):
        index = next(i for i, token in enumerate(argv) if Path(token).name == "run_committee.py")
        argv = argv[index+1:]
    if not argv or argv[0] not in ALLOWED or any(value in argv for value in
            ("--save-next", "--refit", "--train", "--submit", "--live")):
        raise ValueError("Only the fixed existing research production commands are supported")
    if argv[0] == "leader-eight-type-residual" and argv[1:2] not in (["publish"], ["forward"]):
        raise ValueError("The attached residual method may only publish or consume its existing registration")
    if argv[0] == "leader-known-identity-paper" and argv[1:2] not in (["publish"], ["forward"]):
        raise ValueError("The attached known-identity method may only publish or consume its existing registration")
    if argv[0] == "leader-entry-comparison" and argv[1:2] != ["publish"]:
        raise ValueError("The attached entry comparison may only publish its existing registration")
    if ((argv[0] == "leader-type-router-paper" and argv[1:2] != ["publish"])
            or (argv[0] == "leader-type-router-forward" and argv[1:2] != ["run"])):
        raise ValueError("The attached router may only publish or consume its existing registration")
    if argv[0] == "seat-daily" and argv[1:2] != ["project-static-audit"]:
        raise ValueError("The seat helper may only project the saved exact source audit")
    if argv[0] == "seat-disclosure-catalog" and argv[1:2] not in (["batch"], ["append"]):
        raise ValueError("The daily source may only build or append its exact saved disclosure batch")
    if argv[0] == "seat-collect" and ("--execute" not in argv or _values(argv, "--max-pages") != ["3"]
            or len(_values(argv, "--start")) != 1 or _values(argv, "--start") != _values(argv, "--end")):
        raise ValueError("Daily disclosure collection requires one exact day and the fixed three-page bound")
    if argv[0] == "native-intraday-paper" and (_values(argv, "--phase") != ["closing"]
            or any(len(_values(argv, flag)) != 1 for flag in ("--run-dir", "--plan", "--prior-package", "--quote"))):
        raise ValueError("Daily refresh may only close an existing explicit native PAPER session")
    if argv[0] == "leader-type-router-runtime-repair":
        if argv[1:2] not in (["publish"], ["forward"]):
            raise ValueError("The daily supervisor may only publish/consume an existing explicit runtime repair")
        return [str(ROOT / "env/python.exe"), "-B", "-X", "utf8", "-m",
            "trading.research.leader_type_router_runtime_repair", *argv[1:]]
    return [str(ROOT / "env/python.exe"), "-B", "-X", "utf8", str(ROOT / "run_committee.py"), *argv]


def _command_flags(step):
    if step["command"][0] in {"seat-append-source-audit", "seat-collect"}:
        # This one existing provider command is explicitly allowed. A saved
        # request cannot claim that its future HTTP request did or did not run.
        return {**FLAGS, "network_accessed": None, "network_access_unknown": True}
    return dict(FLAGS)


def _command_network_observation(step):
    if step["command"][0] == "seat-collect":
        from .seat_disclosure_daily import _network
        observed = _network(Path(step["expected_files"][0]).parent)
        return {"network_accessed": observed, "network_access_unknown": observed is None}
    if step["command"][0] != "seat-append-source-audit":
        return {"network_accessed": False}
    outcome = Path(step["expected_files"][0])
    record = _read(outcome) if outcome.exists() else {}
    known = type(record.get("network_accessed")) is bool
    return {"network_accessed": record["network_accessed"] if known else None,
        "network_access_unknown": not known}


def _outputs(step, day):
    refs = [_ref(file) for file in step["expected_files"]]
    if step["command"][0] == "leader-type-router-runtime-repair":
        from . import leader_type_router_runtime_repair as repair
        record = _read(_values(step["command"], "--registration")[0])
        observed = _read(step["expected_files"][0])
        marker = repair._marker(record, repair.paper._pin(_values(step["command"], "--registration")[0]))
        if (observed.get("runtime_repair") != marker
                or observed.get("candidate_panel_only") is not True
                or any(observed.get(k) is not False for k in
                    ("execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("New runtime-repair output lost its exact independent registration")
        expected = (repair.paper.PUBLICATION_SCHEMA if step["command"][1] == "publish"
            else "leader_type_router_daily_forward_result_v1")
        if observed.get("schema") != expected:
            raise ValueError("New runtime-repair command must save its actual publication or forward result")
    if step["command"][0] == "native-intraday-paper":
        from .. import native_intraday_paper as paper
        directory = Path(_values(step["command"], "--run-dir")[0]).resolve()
        if step["expected_files"] != [str(directory / "head.json")]:
            raise ValueError("PAPER close must save only its own existing head")
        head = _read(directory / "head.json")
        paper._validate_shape(head, directory)
        paper._verify_last_journal(head, directory)
        if head["phase"] != "closing" or head["execution_day"] != day:
            raise ValueError("Native PAPER did not finish this exact session")
    if step["command"][0] in {"leader-eight-type-forward", "leader-published-forward", "recent-source-alpha158-forward-run",
            "shortline-native-preopen-forward"}:
        head = _read(step["expected_files"][0])
        per_case_generation = False
        if step["command"][0] == "leader-eight-type-forward":
            from . import leader_eight_type_forward_run as forward
            explicit_registration = _values(step["command"], "--registration")
            if len(explicit_registration) > 1:
                raise ValueError("Only one forward registration is supported")
            registration_file = Path(explicit_registration[0]) if explicit_registration else forward.DEFAULT_REGISTRATION
            registered = _read(registration_file)
            if (any(key in registered for key in ("generation_method_id", "initial_quality_scope", "registered_run_dir"))
                    or registered.get("generation", {}).get("kind") == "independent_new_research_generation"):
                directory = str(Path(step["expected_files"][0]).resolve().parent)
                binding = head.get("binding", {})
                generation = binding.get("explicit_new_generation", {})
                if (registered.get("generation_method_id") != forward.NEW_GENERATION_METHOD
                        or head.get("schema") != forward.SCHEMA or set(head.get("cases", {})) != set(forward.CASE_IDS)
                        or any(head.get(key) is not value for key, value in forward.FLAGS.items())
                        or registered.get("initial_quality_scope") != "per_case"
                        or registered.get("registered_run_dir") != directory
                        or generation.get("method_id") != forward.NEW_GENERATION_METHOD
                        or generation.get("initial_quality_scope") != "per_case"
                        or generation.get("registered_run_dir") != directory
                        or binding.get("run_directory") != directory
                        or binding.get("config") != registered.get("config")
                        or binding.get("forward_method_registration", {}).get("sha256") != _ref(registration_file)["sha256"]
                        or any(c["state"].get("day") and c["state"]["day"] > day for c in head["cases"].values())
                        or any(c["state"].get("day") != day and not (c.get("blocking_evidence") or c.get("error"))
                               for c in head["cases"].values())):
                    raise ValueError("New generation must preserve exact registration, partial-case evidence and requested date")
                per_case_generation = True
        if not per_case_generation and (not head.get("cases") or any(c["state"].get("day") != day for c in head["cases"].values())):
            raise ValueError("The existing account did not commit the exact requested source day")
    if step["command"][0] == "leader-dated-catchup-forward":
        # A successful command may accurately report blocked/partial books.
        # Its consumer readiness is evaluated separately by the dated recipe.
        result = _read(step["expected_files"][0])
        if (result.get("schema") != "leader_dated_catchup_shared_engine_result_v1"
                or result.get("candidate_panel_only") is not True
                or any(result.get(key) is not False for key in
                       ("execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Dated catchup command must save its actual research-only result")
    if step["command"][:2] == ["leader-eight-type-residual", "forward"]:
        result = _read(step["expected_files"][0])
        if (result.get("schema") != "leader_eight_type_residual_shared_engine_result_v1"
                or result.get("candidate_panel_only") is not True
                or any(result.get(key) is not False for key in
                       ("execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Residual-exit command must save its actual research-only result")
    if step["command"][:2] == ["leader-known-identity-paper", "forward"]:
        result = _read(step["expected_files"][0])
        if (result.get("schema") != "leader_known_identity_shared_engine_result_v1"
                or result.get("candidate_panel_only") is not True
                or any(result.get(key) is not False for key in
                       ("execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Known-identity command must save its actual research-only result")
    schemas = {"leader-entry-comparison-forward": "leader_entry_comparison_forward_result_v1",
        "seat-hypothesis-dated-forward": "public_seat_four_arm_dated_forward_result_v1",
        "seat-append-forward": "public_seat_append_four_arm_forward_result_v1"}
    if step["command"][0] == "leader-type-router-forward":
        from .leader_type_router_forward import RESULT_SCHEMA as router_result_schema
        schemas["leader-type-router-forward"] = router_result_schema
    if step["command"][0] in schemas:
        result = _read(step["expected_files"][0])
        if (result.get("schema") != schemas[step["command"][0]]
                or result.get("candidate_panel_only") is not True
                or any(result.get(key) is not False for key in
                       ("execution_admitted", "selection_enabled", "strategy_validated"))):
            raise ValueError("Registered forward command must save its actual research-only result")
    return refs


def _exact_day(day):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("An exact ISO source date is required")
    return day


def _step_spec(step):
    if (not re.fullmatch(r"[a-z][a-z0-9_]*", step["id"])
            or not isinstance(step["command"], list) or not step["command"]
            or not all(isinstance(value, str) for value in step["command"])
            or not isinstance(step["expected_files"], list) or not step["expected_files"]):
        raise ValueError("One exact saved daily step is required")
    return {"id": step["id"], "command": list(step["command"]),
        "expected_files": [str(Path(path).resolve()) for path in step["expected_files"]]}


def _terminal(step, directory, day, receipt):
    """Validate an actual exit and the same output bytes at exit and adoption."""
    spec = _step_spec(step)
    files = command_child.paths(directory, spec["id"])
    request_ref, terminal_ref = _ref(files["request"]), _ref(files["terminal"])
    request, terminal = _read(files["request"]), _read(files["terminal"])
    if (receipt.get("child_request_ref") != request_ref
            or (receipt.get("child_terminal_ref") is not None
                and receipt["child_terminal_ref"] != terminal_ref)
            or receipt.get("day") != day or receipt.get("step") != spec["id"]
            or receipt.get("argv") != request.get("argv")
            or receipt.get("expected_files") != spec["expected_files"]
            or request.get("schema") != command_child.REQUEST_SCHEMA
            or request.get("day") != day or request.get("step") != spec
            or request.get("argv") != _command(spec["command"])
            or request.get("cwd") != str(ROOT)
            or request.get("binding") != step.get("binding")
            or terminal.get("schema") != command_child.TERMINAL_SCHEMA
            or terminal.get("request_ref") != request_ref
            or terminal.get("started_ref") != _ref(files["started"])
            or terminal.get("day") != day or terminal.get("step") != spec
            or terminal.get("argv") != request["argv"]
            or any(request.get(key) is not value for key, value in _command_flags(spec).items())
            or any(terminal.get(key) is not value for key, value in FLAGS.items() if key != "network_accessed")
            or any(terminal.get(key) is not value for key, value in _command_network_observation(spec).items())):
        raise ValueError("The saved child terminal evidence belongs to different daily inputs")
    started = _read(files["started"])
    if (started.get("schema") != command_child.START_SCHEMA
            or started.get("request_ref") != request_ref
            or type(started.get("supervisor_pid")) is not int or started["supervisor_pid"] <= 0
            or started.get("supervisor_pid") != terminal.get("supervisor_pid")
            or (receipt.get("child_pid") is not None
                and receipt["child_pid"] != terminal.get("supervisor_pid"))):
        raise ValueError("The saved terminal has no matching original child claim")
    if (terminal.get("status") != "completed" or type(terminal.get("exit_code")) is not int
            or terminal["exit_code"] != 0
            or terminal.get("execution_progress_unknown") is not False):
        raise ValueError("Recovery requires a completed child with an observed exit zero")
    outputs = _outputs(spec, day)
    if terminal.get("output_refs") != outputs:
        raise ValueError("Daily outputs differ from the bytes saved at the actual child exit")
    return request_ref, terminal_ref, terminal, outputs


def _recovered_command(step, directory, day, receipt):
    path = command_child.paths(directory, step["id"])["recovery"]
    if not path.exists():
        return None
    recovery = _read(path)
    request_ref, terminal_ref, terminal, outputs = _terminal(step, directory, day, receipt)
    if (recovery.get("schema") != "system_daily_command_recovery_v1"
            or recovery.get("original_receipt") != _ref(directory / (step["id"] + ".json"))
            or recovery.get("request_ref") != request_ref or recovery.get("terminal_ref") != terminal_ref
            or recovery.get("step") != _step_spec(step) or recovery.get("day") != day
            or recovery.get("binding") != step.get("binding") or recovery.get("output_refs") != outputs
            or any(recovery.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("The separate recovery record changed or belongs to another step")
    return {**receipt, "status": "completed", "execution_progress_unknown": False,
        "exit_code": terminal["exit_code"], "output_refs": outputs,
        "recovered_saved_child": True, "producer_repeated": False, "recovery_ref": _ref(path)}


def _run_command(step, directory, day):
    _exact_day(day)
    spec = _step_spec(step)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    receipt_file = directory / (step["id"] + ".json")
    if receipt_file.exists():
        receipt = _read(receipt_file)
        if receipt.get("command") != step["command"]:
            raise ValueError("An existing step cannot change its original command")
        recovered = _recovered_command(step, directory, day, receipt)
        if recovered is not None:
            return recovered
        if receipt["status"] == "completed":
            if _outputs(step, day) != receipt["output_refs"]:
                raise ValueError("A completed daily stage output changed")
            if "child_request_ref" in receipt:
                _terminal(step, directory, day, receipt)
        return receipt  # failed/started never launch a second child
    argv = _command(step["command"])
    stdout, stderr = directory / (step["id"] + ".stdout.txt"), directory / (step["id"] + ".stderr.txt")
    files = command_child.paths(directory, spec["id"])
    if any(files[name].exists() for name in ("request", "started", "terminal", "recovery")):
        raise ValueError("An existing child request cannot launch a second command")
    request = {"schema": command_child.REQUEST_SCHEMA, "day": day, "step": spec,
        "argv": argv, "cwd": str(ROOT), "binding": step.get("binding"),
        "created_at": _now(), **_command_flags(spec)}
    _atomic(files["request"], request)
    receipt = {"schema": SCHEMA, "step": step["id"], "status": "started", "day": day,
        "execution_progress_unknown": True, "command": step["command"], "argv": argv,
        "expected_files": spec["expected_files"], "child_request_ref": _ref(files["request"]),
        "started_at": _now(), "parent_pid": os.getpid(), "automatic_retry": False, **_command_flags(spec)}
    _atomic(receipt_file, receipt)
    try:
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            supervisor = [str(ROOT / "env/python.exe"), "-B", "-X", "utf8", "-m",
                "trading.research.system_daily_command_child", "--request", str(files["request"]),
                "--request-sha256", receipt["child_request_ref"]["sha256"]]
            process = subprocess.Popen(supervisor, cwd=ROOT, stdout=out, stderr=err)
            receipt.update(child_pid=process.pid, child_created_at=_now())
            _atomic(receipt_file, receipt)
            code = process.wait()
        receipt.update(exit_code=code, supervisor_exit_code=code, terminal_observed_at=_now(),
            stdout=str(stdout), stderr=str(stderr))
        if not files["terminal"].exists():
            if code == 0:
                _outputs(spec, day)
            raise ValueError("No independent command terminal receipt; execution progress remains unknown")
        terminal = _read(files["terminal"])
        receipt.update(exit_code=terminal.get("exit_code"),
            execution_progress_unknown=terminal.get("execution_progress_unknown", True),
            child_terminal_ref=_ref(files["terminal"]), **_command_network_observation(spec))
        if terminal.get("status") != "completed":
            receipt.update(status="failed_no_automatic_retry", error=terminal.get("error") or
                "The child command did not complete successfully")
        else:
            _, _, _, outputs = _terminal(step, directory, day, receipt)
            if code != 0:
                raise ValueError("The supervisor exit disagrees with its saved successful command")
            receipt.update(output_refs=outputs, status="completed", execution_progress_unknown=False)
    except Exception as error:
        receipt.update(status="failed_or_interrupted_no_automatic_retry", error=f"{type(error).__name__}: {error}")
    _atomic(receipt_file, receipt)
    return receipt


def _native_forward_step(profile, key, day, package, operation):
    template = profile["forward_templates"][key]
    args = _command(template["argv"])[5:]
    closes = list(dict.fromkeys([*_values(args, "--raw-close"), str(Path(package).resolve())]))
    args = _set(args, "--raw-close", closes)
    args = _set(args, "--through", [day])
    args = _set(args, "--result-json", [operation / ("native_forward_" + key + "_result.json")])
    return {"id": "native_forward_" + key, "command": args,
        "expected_files": [str(Path(template["run_dir"]).resolve() / "head.json")]}


def _native_settlement_terminal(step, commands, day, receipt):
    evidence = _terminal(step, commands, day, receipt)
    account = _read(step["expected_files"][0])
    result = _read(step["expected_files"][1])
    summaries = result.get("cases", [])
    if (result.get("schema") != "native_shortline_preopen_forward_result_v1"
            or result.get("status") not in {"advanced_isolated_native_research_books", "already_consumed_original_plan_prefix"}
            or result.get("checkpoint") != step["expected_files"][0]
            or result.get("run_directory") != str(Path(step["expected_files"][0]).parent)
            or result.get("case_count") != 2 or len(summaries) != 2
            or {case.get("model_kind") for case in summaries} != {"lr", "lgbm"}
            or any(case.get("last_processed_day") != day for case in summaries)
            or set(account.get("cases", {})) != {"lr", "lgbm"}
            or any(case["state"].get("day") != day for case in account["cases"].values())
            or result.get("method_id") != account.get("binding", {}).get("method_id")
            or result.get("policy_id") != account.get("binding", {}).get("policy_id")):
        raise ValueError("Native terminal/result must prove both original model accounts settled exact execution E")
    return evidence


def _native_settlement_policy(head, registration, key, day, package, operation, source, sessions, previous, now):
    from .shortline_native_preopen import load_preopen_plan
    from .shortline_native_preopen_forward import _panel_link

    profile = deepcopy(head["native_profile"])
    template = profile["forward_templates"][key]
    # Validate each policy against its own original registration. A broken
    # policy must not stop the other original account from being settled.
    profile["forward_templates"] = {key: template}
    configured = deepcopy(registration)
    configured["initial_native_profile"]["forward_templates"] = {
        key: registration["initial_native_profile"]["forward_templates"][key]}
    validation_head = {**head, "native_profile": profile}
    command_plan = {"schema": "system_daily_native_command_plan_v1", "day": day,
        "registration": head["registration"], "source": source,
        "parent_profile": profile, "profile": profile,
        "steps": [_step_spec(_native_forward_step(profile, key, day, package, operation))], **FLAGS}
    _validate_native_command_plan(command_plan, validation_head, configured, operation, day, source)
    progress = head.get("native_forward_progress", {}).get(key)
    if progress and progress["status"] not in {"completed", "exact_execution_day_already_committed"}:
        raise ValueError("Unresolved original native attempt cannot be retried by settlement")
    original = operation.parent.parent / "operations" / day.replace("-", "") / "commands"
    original_files = command_child.paths(original, "native_forward_" + key)
    if (original / ("native_forward_" + key + ".json")).exists() or any(p.exists() for p in original_files.values()):
        raise ValueError("An original daily native attempt already exists; no second attempt is allowed")
    plans = [load_preopen_plan(path) for path in template["plan_prefix"]]
    days = [plan["source_day"] for plan in plans]
    source_day = previous[day]
    if (not days or days != [d for d in sessions if days[0] <= d <= source_day]
            or plans[-1]["execution_day"] != day
            or str(Path(profile["latest_plans"][key]).resolve()) != plans[-1]["plan_ref"]["file"]
            or any(any(plan["calendar"].get(name) != value for name, value in registration["calendar"].items())
                or previous.get(plan["execution_day"]) != plan["source_day"] for plan in plans)):
        raise ValueError("Settlement cannot skip, roll, replace or backfill an original saved plan")
    sources = {day: source}
    for folder in template["raw_close_prefix"]:
        proof = _source(Path(folder).resolve(), registration["calendar"], sessions, now)["proof"]
        if proof["day"] in sources and sources[proof["day"]] != proof:
            raise ValueError("A different raw source already occupies this exact day")
        if proof["day"] > day:
            raise ValueError("Settlement cannot use a future raw prefix")
        sources[proof["day"]] = proof
    for plan in plans:
        expected = _panel_link(plan, now)["source_close_proof"]
        proof = sources.get(plan["source_day"])
        if proof is None or any(proof.get(name) != value for name, value in expected.items()):
            raise ValueError("Saved plan and supplied original raw source differ")
    step = _native_forward_step(profile, key, day, package, operation)
    step["expected_files"].append(_values(step["command"], "--result-json")[0])
    account_file = Path(step["expected_files"][0])
    account = _read(account_file)
    account_days = {case["state"].get("day") for case in account.get("cases", {}).values()}
    commands = operation / "commands"
    attempt = commands / (step["id"] + ".json")
    if (set(account.get("cases", {})) != {"lr", "lgbm"}
            or account_days not in ({source_day}, {day})
            or (account_days == {day} and not attempt.exists())
            or account["raw_sources"] != [sources[d] for d in sorted(sources) if d <= next(iter(account_days))]):
        raise ValueError("Existing native account must retain its exact consecutive raw/account prefix")
    _separate(operation, package, account_file.parent,
        *[Path(plan["plan_ref"]["file"]).parent for plan in plans])
    args = step["command"]
    inputs = {"plans": [plan["plan_ref"] for plan in plans], "template": template,
        "raw_sources": [sources[d] for d in sorted(sources)],
        "ipo_proof": _ref(_values(args, "--ipo-proof")[0]),
        "master_run": _ref(_values(args, "--research-master-run")[0])}
    return step, inputs, _ref(account_file)


def settle_native(run_dir, day, package, *, policy=None):
    """Consume saved D->E plans in the two existing accounts; publish no E signal."""
    _exact_day(day)
    if policy not in (None, "v1", "qualified"):
        raise ValueError("Native settlement supports only the two original policies")
    directory = opening_daily._destination(run_dir)
    package = Path(package).resolve()
    keys = (policy,) if policy else ("v1", "qualified")
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        calendar_ref = registration["calendar"]
        if _ref(calendar_ref["file"]) != calendar_ref:
            raise ValueError("Registered exchange calendar changed")
        calendar = _read(calendar_ref["file"])
        year = calendar["year"]
        sessions, previous, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
        source_day = previous.get(day)
        profile = head["native_profile"]
        if (status != "pinned_static_exchange_calendar" or day not in sessions
                or profile["source_day"] != source_day
                or profile.get("chain_has_missing_plan") or profile.get("missing_plan_days")):
            raise ValueError("Settlement requires saved latest source-D plans for exact adjacent execution E")
        now = datetime.now(MARKET_TZ)
        source = _source(package, calendar_ref, sessions, now)["proof"]
        if source["day"] != day or Path(source["source_folder"]).resolve() != package:
            raise ValueError("Settlement requires the exact complete execution-E raw close")
        original_receipt = directory / "operations" / day.replace("-", "") / "receipt.json"
        if original_receipt.exists() and _read(original_receipt).get("source") != source:
            raise ValueError("A different source already occupies the original exact daily operation")
        operation = directory / "settlements" / day.replace("-", "")
        plan_file = operation / "plan.json"
        saved_plan = {"schema": "system_daily_native_settlement_plan_v1", "day": day,
            "source_day": source_day, "registration": head["registration"], "source": source, **FLAGS}
        if plan_file.exists() and _read(plan_file) != saved_plan:
            raise ValueError("Settlement source or original registration changed")
        if operation.exists() and not plan_file.exists():
            raise ValueError("An incomplete settlement operation cannot be silently replaced")
        steps, inputs, before, prior, blocked = {}, {}, {}, {}, {}
        # Preflight every selected policy before starting either consumer.
        for key in keys:
            try:
                step, values, account_ref = _native_settlement_policy(head, registration, key, day,
                    package, operation, source, sessions, previous, now)
                input_file = operation / (key + "_inputs.json")
                if input_file.exists() and _read(input_file) != values:
                    raise ValueError("Settlement plan, raw prefix or fixed IPO/master input changed")
                attempt = operation / "commands" / (step["id"] + ".json")
                files = command_child.paths(attempt.parent, step["id"])
                if attempt.exists():
                    receipt = _read(attempt)
                    if receipt.get("command") != step["command"]:
                        raise ValueError("An existing settlement attempt changed its command")
                    if receipt.get("status") == "completed":
                        if not input_file.exists() or not plan_file.exists():
                            raise ValueError("A completed settlement lost its original bound inputs")
                        step["binding"] = {"kind": "native_settlement", "registration": head["registration"],
                            "source": source, "plan": _ref(plan_file), "policy_inputs": _ref(input_file)}
                        _native_settlement_terminal(step, attempt.parent, day, receipt)
                    prior[key] = receipt
                elif any(p.exists() for p in files.values()):
                    raise ValueError("An orphan settlement child claim cannot launch another attempt")
                steps[key], inputs[key], before[key] = step, values, account_ref
            except (ValueError, KeyError, TypeError, OSError) as error:
                blocked[key] = {"status": "blocked_native_settlement_no_automatic_retry",
                    "error": f"{type(error).__name__}: {error}", "consumer_called": False}
        if not plan_file.exists():
            _atomic(plan_file, saved_plan)
        for key in steps:
            input_file = operation / (key + "_inputs.json")
            if not input_file.exists():
                _atomic(input_file, inputs[key])
            steps[key]["binding"] = {"kind": "native_settlement", "registration": head["registration"],
                "source": source, "plan": _ref(plan_file), "policy_inputs": _ref(input_file)}
        result_file = operation / "receipt.json"
        result = _read(result_file) if result_file.exists() else {"schema": "system_daily_native_settlement_result_v1",
            "day": day, "source_day": source_day, "plan_ref": _ref(plan_file), "policies": {},
            "system_head_ref": _ref(directory / "head.json"), "system_source_cursor_advanced": False,
            "signals_published": False, "whole_system_daily_complete": False, "automatic_retry": False, **FLAGS}
        result["policies"].update(blocked)
        called = False
        for key, step in steps.items():
            receipt = prior.get(key)
            if receipt is None:
                called = True
                receipt = _run_command(step, operation / "commands", day)
            if receipt.get("status") == "completed":
                try:
                    request_ref, terminal_ref, _, outputs = _native_settlement_terminal(
                        step, operation / "commands", day, receipt)
                    result["policies"][key] = {"status": "completed", "request_ref": request_ref,
                        "terminal_ref": terminal_ref, "consumer_head_ref": outputs[0], "result_ref": outputs[1],
                        "consumer_head_before": result["policies"].get(key, {}).get("consumer_head_before", before[key])}
                except (ValueError, KeyError, TypeError, OSError) as error:
                    result["policies"][key] = {"status": "failed_native_settlement_validation_no_automatic_retry",
                        "error": f"{type(error).__name__}: {error}",
                        "command_receipt_ref": _ref(operation / "commands" / (step["id"] + ".json"))}
            else:
                result["policies"][key] = {"status": receipt.get("status", "unknown_no_automatic_retry"),
                    "command_receipt_ref": _ref(operation / "commands" / (step["id"] + ".json")),
                    "execution_progress_unknown": receipt.get("execution_progress_unknown", True)}
            _atomic(result_file, result)
        complete = all(result["policies"][key]["status"] == "completed" for key in keys)
        result["status"] = "completed_native_settlement" if complete else "partial_native_settlement_no_automatic_retry"
        _atomic(result_file, result)
        return {**result, "consumer_called": called, "selected_policies": list(keys),
            "status": "already_completed_native_settlement" if complete and not called else result["status"],
            "operation_result_ref": _ref(result_file)}


def _static_native_command(argv):
    result, index = [], 0
    argv = _command(argv)[5:]
    while index < len(argv):
        if argv[index] in {"--plan", "--raw-close", "--through", "--result-json"}:
            index += 2
        else:
            result.append(argv[index]); index += 1
    return result


def _validate_native_command_plan(plan, head, registration, operation, day, source):
    if (plan.get("schema") != "system_daily_native_command_plan_v1" or plan.get("day") != day
            or plan.get("registration") != head["registration"] or plan.get("source") != source
            or not isinstance(plan.get("profile", {}).get("source_day"), str)
            or plan["profile"]["source_day"] > day
            or head["native_profile"] not in (plan.get("parent_profile"), plan.get("profile"))
            or any(plan.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("The saved native command plan belongs to another registered daily state")
    profile = plan["profile"]
    initial = registration["initial_native_profile"]
    if set(profile["forward_templates"]) != set(initial["forward_templates"]):
        raise ValueError("Native command recovery cannot introduce another account")
    for key, template in profile["forward_templates"].items():
        original = initial["forward_templates"][key]
        if (Path(template["run_dir"]).resolve() != Path(original["run_dir"]).resolve()
                or _values(_command(template["argv"])[5:], "--run") != [original["run_dir"]]
                or _static_native_command(template["argv"]) != _static_native_command(original["argv"])):
            raise ValueError("The saved native command changed its registered account or original template")
        for field, option in (("plan_prefix", "--plan"), ("raw_close_prefix", "--raw-close")):
            prefix = template[field]
            if (prefix[:len(original[field])] != original[field]
                    or len(prefix) != len(set(prefix))
                    or _values(_command(template["argv"])[5:], option) != prefix):
                raise ValueError("The saved native template changed its original source/plan prefix")
    expected = [_step_spec(_native_forward_step(profile, key, day, source["source_folder"], operation))
        for key in profile["forward_templates"]]
    if plan.get("steps") != expected:
        raise ValueError("The native command plan does not retain its exact registered run/head and source day")
    return plan


def _native_command_plan(head, registration, parent_profile, operation, day, source):
    path = operation / "native_command_plan.json"
    if path.exists():
        plan = _read(path)
    else:
        plan = {"schema": "system_daily_native_command_plan_v1", "day": day,
            "registration": head["registration"], "source": source,
            "parent_profile": deepcopy(parent_profile), "profile": deepcopy(head["native_profile"]),
            "steps": [_step_spec(_native_forward_step(head["native_profile"], key, day,
                source["source_folder"], operation)) for key in head["native_profile"]["forward_templates"]],
            **FLAGS}
        _validate_native_command_plan(plan, head, registration, operation, day, source)
        _atomic(path, plan)
    _validate_native_command_plan(plan, head, registration, operation, day, source)
    return path, plan


def _native_continuation(receipt, operation, day, package):
    attempt = receipt["native_production_attempt"]
    continuation = receipt["native_production_continuation"]
    evidence = continuation["producer_receipt"]
    result = receipt["native_production"]
    next_profile = continuation["next_profile"]
    if (attempt.get("status") != "completed"
            or _ref(evidence["file"]) != evidence
            or Path(evidence["file"]).resolve() != operation / "native/receipt.json"
            or _read(evidence["file"]) != {k: v for k, v in result.items() if k != "operation"}
            or result.get("source_day") != day
            or result.get("status") not in {"completed_saved_native_daily_features_scores_two_policies",
                "saved_state_without_scorable_input_no_plan", "saved_native_source_context_without_publication"}
            or next_profile["source_day"] != day
            or next_profile["full_closes"] != [*attempt["parent_profile"]["full_closes"], str(Path(package).resolve())]
            or _ref(next_profile["checkpoint"]) != next_profile["checkpoint_ref"]):
        raise ValueError("Saved native continuation lacks its exact successful producer receipt")
    return next_profile


def _native_input_profile(head, registration, operation, day, source, package):
    """Protect the original inputs, not this operation's own saved checkpoint."""
    plan_file = operation / "native_command_plan.json"
    if plan_file.is_file():
        plan = _validate_native_command_plan(_read(plan_file), head, registration, operation, day, source)
        return plan["parent_profile"]
    receipt_file = operation / "receipt.json"
    if receipt_file.is_file():
        receipt = _read(receipt_file)
        attempt = receipt.get("native_production_attempt")
        if attempt is not None:
            if (receipt.get("schema") != SCHEMA or receipt.get("day") != day or receipt.get("source") != source
                    or any(receipt.get(key) is not value for key, value in FLAGS.items())):
                raise ValueError("Saved native inputs belong to another exact daily operation")
            parent = attempt["parent_profile"]
            if head["native_profile"] == parent:
                return parent
            if head["native_profile"] == _native_continuation(receipt, operation, day, package):
                return parent
            raise ValueError("Saved native input profile does not match its original producer continuation")
    return head["native_profile"]


def _native_production_refresh(head, receipt, operation, day, package, previous_day, produce):
    """Attempt one exact native source day; a saved failure never calls it again."""
    if head["native_profile"]["source_day"] == day:
        receipt.setdefault("native_production", {"status": "saved_exact_source_day_reused_without_rescoring"})
        return
    if head["native_profile"]["source_day"] != previous_day:
        receipt["native_production"] = {"status": "blocked_by_missing_exact_native_source_day",
            "last_source_day": head["native_profile"]["source_day"], "required_previous_day": previous_day,
            "producer_called": False, "automatic_retry": False}
        return
    attempt = receipt.get("native_production_attempt")
    if attempt is not None:
        # A parent can stop after saving the successful continuation but before
        # installing its head. Adopt only its unchanged original producer receipt.
        continuation = receipt.get("native_production_continuation")
        if continuation is not None and attempt.get("status") == "completed":
            if attempt["parent_profile"] != head["native_profile"]:
                raise ValueError("Saved native continuation lacks its exact successful producer receipt")
            head["native_profile"] = deepcopy(_native_continuation(receipt, operation, day, package))
        else:
            receipt.setdefault("native_production", {"status": "unknown_native_production_attempt_no_retry",
                "producer_called": False, "automatic_retry": False})
        return
    receipt["native_production_attempt"] = {"status": "started", "started_at": _now(),
        "parent_profile": deepcopy(head["native_profile"]), "automatic_retry": False}
    _atomic(operation / "receipt.json", receipt)
    try:
        produced = produce(head["native_profile"], day, package, operation / "native")
        result, next_profile = produced["result"], produced.get("next_profile")
        receipt["native_production"] = result
        if result.get("status") in {"completed_saved_native_daily_features_scores_two_policies",
                "saved_state_without_scorable_input_no_plan", "saved_native_source_context_without_publication"}:
            if next_profile is None or next_profile["source_day"] != day:
                raise ValueError("Successful native production lacks its exact next-day profile")
            evidence_file = Path(result["operation"]) if result.get("operation") else None
            if evidence_file is not None:
                if (evidence_file.resolve() != operation / "native/receipt.json"
                        or _read(evidence_file) != {k: v for k, v in result.items() if k != "operation"}):
                    raise ValueError("Native producer returned another operation receipt")
                receipt["native_production_continuation"] = {"next_profile": deepcopy(next_profile),
                    "producer_receipt": _ref(evidence_file)}
            head["native_profile"] = next_profile
        receipt["native_production_attempt"].update(status="completed", completed_at=_now())
    except Exception as error:
        receipt["native_production"] = {"status": "failed_native_production_no_automatic_retry",
            "error": f"{type(error).__name__}: {error}", "automatic_retry": False}
        receipt["native_production_attempt"].update(status="failed", completed_at=_now())
    _atomic(operation / "receipt.json", receipt)


def advance_native(run_dir, day, package, *, source_only):
    """Continue only native production; retain the core cursor and original books."""
    from .native_shortline_daily import recover_saved_profile, run_profile
    _exact_day(day)
    if not isinstance(source_only, bool):
        raise ValueError("Choose explicit source-only recovery or current publication")
    directory, package = opening_daily._destination(run_dir), Path(package).resolve()
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        calendar_ref = registration["calendar"]
        if _ref(calendar_ref["file"]) != calendar_ref:
            raise ValueError("Registered exchange calendar changed")
        calendar = _read(calendar_ref["file"])
        year = calendar["year"]
        sessions, previous, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
        if status != "pinned_static_exchange_calendar" or day not in sessions or sessions[-1] == day:
            raise ValueError("An exact source session and its known next execution day are required")
        execution_day = sessions[sessions.index(day) + 1]
        now = datetime.now(MARKET_TZ)
        deadline = datetime.fromisoformat(execution_day + "T09:30:00+08:00")
        source = _source(package, calendar_ref, sessions, now)["proof"]
        if source["day"] != day or Path(source["source_folder"]).resolve() != package:
            raise ValueError("Native production requires the exact complete source-D close")
        operation = directory / ("native_context" if source_only else "native_production") / day.replace("-", "")
        plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
        if not receipt_file.exists() and (now < datetime.fromisoformat(day + "T15:00:00+08:00")
                or (source_only and now < deadline) or (not source_only and now >= deadline)):
            raise ValueError("Only missed publication windows permit source-only recovery; current publication must precede E09:30")
        for parent_folder in ("operations", "native_context", "native_production"):
            peer = directory / parent_folder / day.replace("-", "")
            if peer == operation:
                continue
            peer_receipt = peer / "receipt.json"
            if (peer / "native/receipt.json").exists() or (peer_receipt.exists()
                    and "native_production_attempt" in _read(peer_receipt)):
                raise ValueError("An original native producer attempt exists in another operation; no mode-based retry")
        if operation.exists() and not plan_file.exists():
            raise ValueError("An unresolved native production operation cannot be replaced")
        saved_plan = _read(plan_file) if plan_file.exists() else {
            "schema": "system_daily_native_production_plan_v1", "day": day,
            "execution_day": execution_day, "source_only": source_only,
            "registration": head["registration"], "source": source,
            "parent_profile": deepcopy(head["native_profile"]), **FLAGS}
        parent = saved_plan["parent_profile"]
        if (saved_plan.get("schema") != "system_daily_native_production_plan_v1"
                or saved_plan.get("day") != day or saved_plan.get("source_only") is not source_only
                or saved_plan.get("execution_day") != execution_day
                or saved_plan.get("registration") != head["registration"] or saved_plan.get("source") != source
                or parent["source_day"] != previous[day]
                or any(saved_plan.get(k) is not v for k, v in FLAGS.items())):
            raise ValueError("Native production cannot replace or skip its original source/profile/mode")
        mutable = {"source_day", "checkpoint", "checkpoint_ref", "full_closes", "last_operation",
            "latest_plans", "forward_templates", "score_publication", "chain_has_missing_plan", "missing_plan_days"}
        initial = registration["initial_native_profile"]
        if (any(parent.get(k) != v for k, v in initial.items() if k not in mutable)
                or parent["full_closes"][:len(initial["full_closes"])] != initial["full_closes"]):
            raise ValueError("Native production must retain its registered model, scope and source prefix")
        validation_head = {**head, "native_profile": parent}
        _validate_native_command_plan({"schema": "system_daily_native_command_plan_v1", "day": day,
            "registration": head["registration"], "source": source, "parent_profile": parent, "profile": parent,
            "steps": [_step_spec(_native_forward_step(parent, key, day, package, operation))
                for key in parent["forward_templates"]], **FLAGS}, validation_head, registration, operation, day, source)
        _separate(operation, package, *_input_directories(head["profile"], parent,
            registration["original_routes"], head["lowvol"]))
        receipt = _read(receipt_file) if receipt_file.exists() else {
            "schema": "system_daily_native_production_result_v1", "day": day, "source": source,
            "source_only": source_only, "plan_ref": None, "started_at": _now(),
            "system_source_cursor_advanced": False, "engine_advanced": False,
            "whole_system_daily_complete": False, "automatic_retry": False, **FLAGS}
        if (receipt.get("schema") != "system_daily_native_production_result_v1"
                or receipt.get("day") != day or receipt.get("source") != source
                or receipt.get("source_only") is not source_only
                or any(receipt.get(k) is not v for k, v in FLAGS.items())):
            raise ValueError("Saved native producer result belongs to another exact operation")
        if not plan_file.exists():
            _atomic(plan_file, saved_plan)
        if receipt["plan_ref"] not in (None, _ref(plan_file)):
            raise ValueError("Saved native producer plan changed")
        receipt["plan_ref"] = _ref(plan_file)
        before = deepcopy(head)
        if ("native_production_attempt" in receipt
                and receipt["native_production_attempt"].get("parent_profile") != parent):
            raise ValueError("The original producer attempt belongs to another native parent profile")

        def continuation():
            value = _native_continuation(receipt, operation, day, package)
            for stage in receipt["native_production"].get("stages", {}).values():
                if stage.get("status") != "completed" or any(
                        _ref(ref["file"]) != ref for ref in stage.get("artifact", {}).get("refs", [])):
                    raise ValueError("Saved native stage artifacts changed or are incomplete")
            if source_only:
                result = receipt["native_production"]
                expected = {**deepcopy(parent), "source_day": day,
                    "full_closes": [*parent["full_closes"], str(package)],
                    "checkpoint": result["latest_checkpoint"],
                    "checkpoint_ref": _ref(result["latest_checkpoint"]),
                    "last_operation": result["operation"], "chain_has_missing_plan": True,
                    "missing_plan_days": [*parent["missing_plan_days"], day]}
                if (value != expected or set(result.get("stages", {})) != {"features"}
                        or result.get("source_only") is not True
                        or result.get("signals_published") is not False
                        or result.get("missing_plan_day") != day
                        or result.get("status") != "saved_native_source_context_without_publication"):
                    raise ValueError("Source-only continuation must preserve plans and declare its missing publication")
            return value

        if "native_production_continuation" in receipt:
            restored = continuation()
            if head["native_profile"] == restored:
                return {**receipt, "status": "already_completed_native_only_production", "producer_called": False}
            if head["native_profile"] != parent:
                raise ValueError("Native head moved beyond this saved continuation; no rollback is allowed")
        elif head["native_profile"] != parent:
            raise ValueError("Native source profile changed before exact single-day production")
        elif "native_production_attempt" in receipt:
            if receipt["native_production_attempt"].get("status") != "started":
                return {**receipt, "status": "partial_native_only_production_no_retry", "producer_called": False}
            try:
                recovered = recover_saved_profile(parent, day, package, operation / "native", source_only=source_only)
            except (ValueError, KeyError, TypeError, OSError) as error:
                return {**receipt, "status": "partial_native_only_production_no_retry", "producer_called": False,
                    "saved_producer_recovery_error": f"{type(error).__name__}: {error}"}
            saved = recovered["result"]
            producer_file = operation / "native/receipt.json"
            if (Path(saved["operation"]).resolve() != producer_file
                    or _read(producer_file) != {k:v for k,v in saved.items() if k != "operation"}):
                raise ValueError("Recovery requires this original native producer result")
            receipt.update(native_production=saved,
                native_production_continuation={"next_profile": recovered["next_profile"],
                    "producer_receipt": _ref(producer_file)}, native_saved_result_adopted_without_production=True)
            receipt["native_production_attempt"].update(status="completed", recovered_at=_now(), producer_repeated=False)
            continuation()
            _atomic(receipt_file, receipt)
        called = "native_production_attempt" not in receipt
        _native_production_refresh(head, receipt, operation, day, package, previous[day],
            lambda profile, source_day, folder, out: run_profile(profile, source_day, folder, out, source_only=source_only))
        complete = "native_production_continuation" in receipt
        if complete:
            next_profile = continuation()
            head["native_profile"] = deepcopy(next_profile)
            assert {k:v for k,v in head.items() if k != "native_profile"} == {
                k:v for k,v in before.items() if k != "native_profile"}
            _atomic(directory / "head.json", head)
        receipt.update(status="completed_native_only_production" if complete else "partial_native_only_production_no_retry",
            completed_at=_now(), native_profile_adopted=complete,
            signals_published=complete and receipt["native_production"].get("status") ==
                "completed_saved_native_daily_features_scores_two_policies",
            missing_plan_days=head["native_profile"].get("missing_plan_days", []))
        _atomic(receipt_file, receipt)
        return {**receipt, "producer_called": called}


def _saved_native_forward_step(head, registration, directory, key, progress):
    """Resolve a prior exact command, including after other routes moved ahead."""
    day = progress["day"]
    operation = directory / "operations" / day.replace("-", "")
    path = operation / "native_command_plan.json"
    if progress.get("plan_ref") != _ref(path):
        raise ValueError("Prior native forward lost its exact original command plan")
    saved = _read(operation / "receipt.json")
    plan = _read(path)
    validation_head = {**head, "native_profile": plan["profile"]}
    _validate_native_command_plan(plan, validation_head, registration, operation, day, saved["source"])
    matches = [step for step in plan["steps"] if step["id"] == "native_forward_" + key]
    if len(matches) != 1:
        raise ValueError("Prior native policy command is not unique")
    step = {**matches[0], "binding": {"kind": "native", "registration": head["registration"],
        "source": saved["source"], "plan": _ref(path)}}
    return operation, step


def _native_forward_refresh(head, registration, directory, operation, day, package, source, previous_day,
        parent_profile, key):
    progress = head.setdefault("native_forward_progress", {}).get(key)
    if progress is None:
        # Existing registrations predate this per-policy cursor. Retain the
        # most recent saved attempt rather than silently retrying an old fail.
        for receipt_path in reversed(head["operations"]):
            saved = _read(receipt_path)
            result = saved.get("native_forward_" + key)
            if saved["day"] >= day or result is None:
                continue
            plan_file = Path(receipt_path).parent / "native_command_plan.json"
            progress = {"day": saved["day"], "status": result["status"], "result": deepcopy(result),
                "plan_ref": _ref(plan_file) if plan_file.is_file() else None}
            head["native_forward_progress"][key] = progress
            break
    if progress and progress["day"] < day and progress["status"] not in {
            "completed", "exact_execution_day_already_committed"}:
        # Only an existing exact supervisor receipt can clear an older failure.
        # _run_command never starts a child when that original receipt exists.
        prior_operation, prior_step = _saved_native_forward_step(head, registration, directory, key, progress)
        attempt = prior_operation / "commands" / (prior_step["id"] + ".json")
        recovered = _run_command(prior_step, prior_operation / "commands", progress["day"]) if attempt.is_file() else None
        if recovered is None or recovered["status"] != "completed":
            return {"status": "blocked_by_unresolved_prior_native_forward", "blocking_day": progress["day"],
                "automatic_retry": False, "consumer_called": False}
        progress = {**progress, "status": "completed", "result": recovered}
        head["native_forward_progress"][key] = progress
    profile = head["native_profile"]
    if profile["source_day"] < previous_day or any(missing < day for missing in profile.get("missing_plan_days", [])):
        return {"status": "blocked_by_missing_original_source_plan", "last_native_source_day": profile["source_day"],
            "required_previous_day": previous_day, "missing_plan_days": profile.get("missing_plan_days", []),
            "consumer_called": False}
    if progress and progress["day"] == day and progress["status"] not in {
            "completed", "exact_execution_day_already_committed"}:
        attempt = operation / "commands" / ("native_forward_" + key + ".json")
        if not attempt.is_file():
            return deepcopy(progress["result"])
    template = profile["forward_templates"][key]
    account_file = Path(template["run_dir"]) / "head.json"
    account = _read(account_file) if account_file.exists() else None
    command_receipt = operation / "commands" / ("native_forward_" + key + ".json")
    if (not command_receipt.exists() and account
            and all(case["state"].get("day") == day for case in account["cases"].values())):
        result = {"status": "exact_execution_day_already_committed"}
        head["native_forward_progress"][key] = {"day": day, "status": result["status"], "result": result}
        return result
    plan_file, _ = _native_command_plan(head, registration, parent_profile, operation, day, source)
    step = _native_forward_step(profile, key, day, package, operation)
    step["binding"] = {"kind": "native", "registration": head["registration"], "source": source, "plan": _ref(plan_file)}
    try:
        result = _run_command(step, operation / "commands", day)
    except Exception as error:
        result = {"status": "failed_native_forward_no_automatic_retry",
            "error": f"{type(error).__name__}: {error}", "automatic_retry": False}
    head["native_forward_progress"][key] = {"day": day, "status": result["status"],
        "plan_ref": _ref(plan_file), "result": deepcopy(result)}
    return result


def _recovery_step(directory, day, step_id, head, registration, *, allow_historical_attached=False):
    """Resolve only a step already saved by this registered daily operation."""
    from .daily_refresh_recipe import PLAN_SCHEMA

    _exact_day(day)
    _step_spec({"id": step_id, "command": ["saved"], "expected_files": [directory / "head.json"]})
    operation = directory / "operations" / day.replace("-", "")
    receipt = _read(operation / "receipt.json")
    source = receipt["source"]
    if (receipt.get("schema") != SCHEMA or receipt.get("day") != day or source.get("day") != day
            or any(receipt.get(key) is not FLAGS[key] for key in ("research_only", "candidate_panel_only",
                "execution_admitted", "selection_enabled", "strategy_validated", "user_account_mutated"))):
        raise ValueError("Recovery requires the exact original daily operation")
    calendar_ref = registration["calendar"]
    if _ref(calendar_ref["file"]) != calendar_ref:
        raise ValueError("Registered exchange calendar changed")
    calendar = _read(calendar_ref["file"])
    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01", f"{calendar['year']}-12-31", [])
    native_key = step_id.removeprefix("native_forward_") if step_id in {
        "native_forward_v1", "native_forward_qualified"} else None
    native_progress = head.get("native_forward_progress", {}).get(native_key, {})
    historical_native = (native_key is not None and native_progress.get("day") == day
        and native_progress.get("status") not in {"completed", "exact_execution_day_already_committed"})
    if (day not in sessions or status != "pinned_static_exchange_calendar"
            or (day < head["last_source_day"] and not allow_historical_attached and not historical_native)):
        raise ValueError("Recovery cannot adopt another or superseded source session")
    if step_id in {"native_forward_v1", "native_forward_qualified"}:
        plan_file = operation / "native_command_plan.json"
        if historical_native:
            _saved_native_forward_step(head, registration, directory, native_key, native_progress)
            plan = _read(plan_file)
        else:
            plan = _validate_native_command_plan(_read(plan_file), head, registration, operation, day, source)
        kind = "native"
    elif step_id.startswith("native_paper_close_"):
        from .native_paper_daily_close import validate_plan
        plan_file = operation / "native_paper_closing_plan.json"
        plan = validate_plan(_read(plan_file), day, source)
        kind = "native_paper_closing"
    elif step_id.startswith("catchup_"):
        from . import leader_dated_catchup_daily as dated
        if head.get("dated_catchup_profile") is None:
            raise ValueError("Dated catchup recovery requires its original attachment")
        plan_file = operation / "dated_catchup/plan.json"
        plan = _read(plan_file)
        if (plan.get("day") != day and plan.get("source_day") != day):
            raise ValueError("Dated catchup recovery belongs to another exact day")
        refs = plan["source_refs"]
        if not isinstance(refs, list) or len(refs) != 2:
            raise ValueError("Dated catchup recovery requires its exact panel and analysis references")
        panel, analysis = (reference["file"] for reference in refs)
        dated.validate_plan(plan, head["dated_catchup_profile"], day, panel, analysis,
            operation / "dated_catchup")
        kind = "dated_catchup"
    elif step_id.startswith("residual_"):
        from . import leader_eight_type_residual_daily as residual
        if head.get("residual_exit_profile") is None:
            raise ValueError("Residual-exit recovery requires its original attachment")
        plan_file = operation / "residual_exit/plan.json"
        plan = _read(plan_file)
        if plan.get("source_day") != day:
            raise ValueError("Residual-exit recovery belongs to another exact day")
        refs = plan["source_refs"]
        if not isinstance(refs, list) or len(refs) != 2:
            raise ValueError("Residual-exit recovery requires its exact panel and base publication references")
        panel, base_publication = (reference["file"] for reference in refs)
        residual.validate_plan(plan, head["residual_exit_profile"], day, panel, base_publication,
            operation / "residual_exit")
        kind = "residual_exit"
    elif step_id.startswith("known_identity_"):
        from . import leader_known_identity_daily as known
        if head.get("known_identity_profile") is None:
            raise ValueError("Known-identity recovery requires its original attachment")
        plan_file = operation / "known_identity/plan.json"
        plan = _read(plan_file)
        if plan.get("source_day") != day:
            raise ValueError("Known-identity recovery belongs to another exact day")
        refs = plan["source_refs"]
        if not isinstance(refs, list) or len(refs) != 2:
            raise ValueError("Known-identity recovery requires its exact panel and base publication references")
        panel, base_publication = (reference["file"] for reference in refs)
        known.validate_plan(plan, head["known_identity_profile"], day, panel, base_publication,
            operation / "known_identity")
        kind = "known_identity"
    elif step_id.startswith(("entry_", "leader_type_router_", "router_repair_")):
        kind = ("router_repair" if step_id.startswith("router_repair_") else
            "leader_type_router" if step_id.startswith("leader_type_router_") else "entry_comparison")
        recipe = _attached_recipe(kind)
        if head.get(kind + "_profile") is None:
            raise ValueError("Entry-comparison recovery requires its original attachment")
        plan_file = operation / kind / "plan.json"
        plan = _read(plan_file)
        refs = plan.get("source_refs")
        if plan.get("source_day") != day or not isinstance(refs, list) or len(refs) != 2:
            raise ValueError("Entry-comparison recovery requires the exact own bundle and dated extras")
        recipe.validate_plan(plan, head[kind + "_profile"], day, refs[0]["file"], refs[1]["file"],
            operation / kind)
    elif step_id.startswith("seat_disclosure_"):
        from . import seat_disclosure_daily as disclosures
        kind = "seat_disclosures"
        if head.get("public_seat_profile") is None:
            raise ValueError("Disclosure recovery requires its original public-seat attachment")
        plan_file = operation / kind / "plan.json"
        plan = _read(plan_file)
        disclosures.validate_plan(plan, day, plan["prior_catalog"]["file"], operation / kind,
            calendar=plan["calendar"]["file"], source=plan["source"], batch=plan["batch"],
            saved_catalog=plan["saved_catalog"], completion_receipt=plan["completion_receipt"])
    elif step_id.startswith(("old_seat_", "append_seat_")):
        kind = "public_seat"
        recipe = _attached_recipe(kind)
        if head.get(kind + "_profile") is None:
            raise ValueError("Public-seat recovery requires its original attachment")
        plan_file = operation / kind / "plan.json"
        plan = _read(plan_file)
        if plan.get("source_day") != day or Path(plan["package"]).resolve() != Path(source["source_folder"]).resolve():
            raise ValueError("Public-seat recovery requires the exact original close package")
        recipe.validate_plan(plan, head[kind + "_profile"], day, plan["package"], operation / kind,
            catalog=plan["catalog"])
    else:
        plan_file = operation / "plan.json"
        plan = _read(plan_file)
        if (plan.get("schema") != PLAN_SCHEMA or plan.get("source_day") != day
                or Path(plan.get("package", "")).resolve() != Path(source["source_folder"]).resolve()
                or Path(plan.get("out", "")).resolve() != operation
                or plan.get("next_profile", {}).get("last_source_day") != day):
            raise ValueError("Recovery requires this exact saved core daily plan")
        kind = "core"
    if (day < head["last_source_day"] and not historical_native and kind not in {
            "dated_catchup", "residual_exit", "known_identity", "entry_comparison", "public_seat", "leader_type_router", "router_repair"}):
        raise ValueError("Historical recovery is limited to the original attached research routes")
    matches = [step for step in plan["steps"] if step["id"] == step_id]
    if len(matches) != 1:
        raise ValueError("The requested step is not unique in the saved daily plan")
    step = _step_spec(matches[0])
    _command(step["command"])
    step["binding"] = {"kind": kind, "registration": head["registration"],
        "source": source, "plan": _ref(plan_file)}
    if kind == "dated_catchup":
        step["binding"]["attachment"] = head["dated_catchup_attachment"]
    elif kind == "residual_exit":
        step["binding"]["attachment"] = head["residual_exit_attachment"]
    elif kind == "known_identity":
        step["binding"]["attachment"] = head["known_identity_attachment"]
    elif kind in {"entry_comparison", "public_seat", "leader_type_router", "router_repair"}:
        step["binding"]["attachment"] = head[kind + "_attachment"]
    elif kind == "seat_disclosures":
        step["binding"]["attachment"] = head["public_seat_attachment"]
    return operation, step


def recover_step(run_dir, day, step_id):
    """Adopt one successful child receipt, writing only a separate recovery file.

    No producer, account reader/runner, source collector, or opening product is
    invoked. The next ordinary ``run`` may consume this exact saved recovery.
    """
    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        operation, step = _recovery_step(directory, day, step_id, head, registration)
        commands = operation / "commands"
        receipt_file = commands / (step_id + ".json")
        receipt = _read(receipt_file)
        if receipt.get("command") != step["command"]:
            raise ValueError("The original command receipt differs from its saved daily plan")
        files = command_child.paths(commands, step_id)
        base = {"day": day, "step": step_id, "producer_repeated": False,
            "system_head_mutated": False, "ledger_mutated": False, **FLAGS}
        if not receipt.get("child_request_ref") or not files["terminal"].exists():
            return {**base, "status": "unknown_no_successful_child_terminal",
                "execution_progress_unknown": True}
        recovered = _recovered_command(step, commands, day, receipt)
        if recovered is not None:
            return {**base, "status": "already_recovered", "recovery_ref": recovered["recovery_ref"]}
        request_ref, terminal_ref, terminal, outputs = _terminal(step, commands, day, receipt)
        if receipt.get("status") == "completed":
            return {**base, "status": "already_completed", "producer_repeated": False}
        recovery = {"schema": "system_daily_command_recovery_v1", "day": day,
            "step": _step_spec(step), "binding": step["binding"],
            "original_receipt": _ref(receipt_file), "request_ref": request_ref,
            "terminal_ref": terminal_ref, "output_refs": outputs, "exit_code": terminal["exit_code"],
            "supervisor_identity_basis": ("parent_recorded_pid_and_exclusive_child_claim"
                if receipt.get("child_pid") is not None else "exclusive_child_claim_parent_pid_not_recorded"),
            "recovered_at": _now(), "producer_repeated": False,
            "system_head_mutated": False, "ledger_mutated": False, **FLAGS}
        _atomic(files["recovery"], recovery)
        return {**base, "status": "recovered_saved_command", "recovery_ref": _ref(files["recovery"]),
            "execution_progress_unknown": False}


def register(run_dir, opening_run_dir, lowvol_publication, lowvol_run_dir,
        lowvol_source_dir, *, action_event_view, action_reference_view):
    from .daily_refresh_recipe import bootstrap_profile
    from .native_shortline_daily import bootstrap_profile as native_bootstrap
    from .native_lowvol_publication import load_publication
    directory = opening_daily._destination(run_dir)
    if directory.exists():
        raise FileExistsError("Daily refresh needs a new independent registration; existing accounts are not re-registered")
    coordinator = Path(opening_run_dir).resolve()
    _, routes = opening_daily._load(coordinator)
    profile, native = bootstrap_profile(), native_bootstrap()
    publication = load_publication(lowvol_publication)
    day = profile["last_source_day"]
    if native["source_day"] != day or publication["source_day"] != day:
        raise ValueError("Initial registered routes and the explicit low-volatility publication must share source D")
    calendar = profile["calendar"]
    calendar_file = calendar.get("file", calendar.get("path")) if isinstance(calendar, dict) else calendar
    registration = {"schema": SCHEMA, "registered_at": _now(), "run_directory": str(directory),
        "opening_run_directory": str(coordinator), "original_routes": deepcopy(routes["routes"]),
        "calendar": _ref(calendar_file), "initial_profile": profile, "initial_native_profile": native,
        "lowvol": {"run_directory": str(Path(lowvol_run_dir).resolve()),
            "source_directory": str(Path(lowvol_source_dir).resolve()),
            "publications": [str(Path(lowvol_publication).resolve())],
            "raw_closes": list(native["full_closes"]),
            "action_event_view": str(Path(action_event_view).resolve()),
            "action_reference_view": str(Path(action_reference_view).resolve())}, **FLAGS}
    for source in [coordinator, Path(lowvol_publication).parent,
            *_input_directories(profile, native, routes["routes"], registration["lowvol"])]:
        _separate(directory, source)
    directory.mkdir(parents=True, exist_ok=False)
    _atomic(directory / "registration.json", registration)
    head = {"schema": SCHEMA, "registration": _ref(directory / "registration.json"),
        "profile": profile, "native_profile": native, "lowvol": deepcopy(registration["lowvol"]),
        "last_source_day": day, "operations": [], **FLAGS}
    _atomic(directory / "head.json", head)
    return head


def _load(directory):
    head = _read(directory / "head.json")
    registration = _read(directory / "registration.json")
    if (head.get("schema") != SCHEMA or head["registration"] != _ref(directory / "registration.json")
            or registration.get("run_directory") != str(directory)
            or any(record.get(key) is not value for record in (head, registration) for key, value in FLAGS.items())):
        raise ValueError("Daily registration or research boundary changed")
    lowvol_generation = head["lowvol"].get("generation_ref")
    if lowvol_generation is not None:
        if _ref(lowvol_generation["file"]) != lowvol_generation:
            raise ValueError("Explicit low-volatility generation addendum changed")
        saved = _read(lowvol_generation["file"])
        initial, active = saved["lowvol"], head["lowvol"]
        if (saved.get("schema") != "system_daily_lowvol_bootstrap_attachment_v1"
                or saved.get("system_registration") != head["registration"]
                or any(active.get(k) != initial.get(k) for k in ("run_directory", "source_directory", "route_id"))
                or active["publications"][:len(initial["publications"])] != initial["publications"]
                or active["raw_closes"][:len(initial["raw_closes"])] != initial["raw_closes"]
                or any(saved.get(k) is not v for k, v in FLAGS.items())):
            raise ValueError("New low-volatility generation lost its original independent inception/source prefix")
    attachment = head.get("dated_catchup_attachment")
    profile = head.get("dated_catchup_profile")
    if (attachment is None) != (profile is None):
        raise ValueError("Dated catchup profile requires its explicit saved attachment")
    if attachment is not None:
        if _ref(attachment["file"]) != attachment:
            raise ValueError("Dated catchup daily attachment changed")
        saved = _read(attachment["file"])
        mutable = {"last_source_day", "catalog", "candidate_inputs", "publications", "operations"}
        initial = saved.get("initial_profile", {})
        if (saved.get("schema") != "system_daily_dated_catchup_attachment_v1"
                or saved.get("system_registration") != head["registration"]
                or any(profile.get(key) != value for key, value in initial.items() if key not in mutable)
                or any(saved.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("Dated catchup daily attachment belongs to another system or method")
    attachment = head.get("residual_exit_attachment")
    profile = head.get("residual_exit_profile")
    if (attachment is None) != (profile is None):
        raise ValueError("Residual-exit profile requires its explicit saved attachment")
    if attachment is not None:
        if _ref(attachment["file"]) != attachment:
            raise ValueError("Residual-exit daily attachment changed")
        saved = _read(attachment["file"])
        mutable = {"last_source_day", "panels", "base_publications", "publications", "operations"}
        initial = saved.get("initial_profile", {})
        if (saved.get("schema") != "system_daily_residual_exit_attachment_v1"
                or saved.get("system_registration") != head["registration"]
                or any(profile.get(key) != value for key, value in initial.items() if key not in mutable)
                or any(saved.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("Residual-exit daily attachment belongs to another system or method")
    attachment = head.get("known_identity_attachment")
    profile = head.get("known_identity_profile")
    if (attachment is None) != (profile is None):
        raise ValueError("Known-identity profile requires its explicit saved attachment")
    if attachment is not None:
        if _ref(attachment["file"]) != attachment:
            raise ValueError("Known-identity daily attachment changed")
        saved = _read(attachment["file"])
        mutable = {"last_source_day", "panels", "base_publications", "publications", "operations"}
        initial = saved.get("initial_profile", {})
        if (saved.get("schema") != "system_daily_known_identity_attachment_v1"
                or saved.get("system_registration") != head["registration"]
                or any(profile.get(key) != value for key, value in initial.items() if key not in mutable)
                or any(saved.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("Known-identity daily attachment belongs to another system or method")
    for kind in ("entry_comparison", "public_seat", "leader_type_router", "router_repair"):
        attachment, profile = head.get(kind + "_attachment"), head.get(kind + "_profile")
        if (attachment is None) != (profile is None):
            raise ValueError(f"{kind} requires its explicit saved attachment")
        if attachment is not None:
            if _ref(attachment["file"]) != attachment:
                raise ValueError(f"{kind} daily attachment changed")
            recipe, saved = _attached_recipe(kind), _read(attachment["file"])
            initial = saved.get("initial_profile", {})
            if (saved.get("schema") != f"system_daily_{kind}_attachment_v1"
                    or saved.get("system_registration") != head["registration"]
                    or any(profile.get(key) != value for key, value in initial.items()
                           if key not in recipe.MUTABLE_PROFILE_KEYS)
                    or any(saved.get(key) is not value for key, value in FLAGS.items())):
                raise ValueError(f"{kind} daily attachment belongs to another system or method")
    return head, registration


def _attach_existing_route(system_run_dir, kind, **bootstrap_options):
    """Bind an already registered/published route; do not run any producer."""
    recipe = _attached_recipe(kind)
    directory = opening_daily._destination(system_run_dir)
    with _lock(directory / "runner.lock"):
        head, _ = _load(directory)
        if head.get(kind + "_profile") is not None:
            return {"status": f"existing_{kind}_attachment_reused",
                "attachment": head[kind + "_attachment"], "profile": head[kind + "_profile"],
                "commands_executed": False, **FLAGS}
        cursor = head["last_source_day"]
        if cursor != head["profile"]["last_source_day"] or cursor != head["native_profile"]["source_day"]:
            raise ValueError("Attach only at the exact existing source cursor shared by the original routes")
        profile = recipe.bootstrap_profile(current_source_day=cursor, **bootstrap_options)
        if profile["last_source_day"] != cursor:
            raise ValueError("Attach only at the exact existing source cursor; never skip an original publication")
        path = directory / f"addenda/{kind}.json"
        saved = {"schema": f"system_daily_{kind}_attachment_v1", "attached_at": _now(),
            "system_registration": head["registration"], "initial_profile": profile,
            "existing_route_accounts_reregistered": False, "commands_executed": False, **FLAGS}
        if path.exists():
            prior = _read(path)
            if any(prior.get(key) != value for key, value in saved.items() if key != "attached_at"):
                raise ValueError("An existing attachment cannot change its original profile")
        else:
            _atomic(path, saved)
        head.update({kind + "_attachment": _ref(path), kind + "_profile": profile})
        _atomic(directory / "head.json", head)
        return {"status": f"{kind}_attached_without_running", "attachment": head[kind + "_attachment"],
            "profile": profile, "commands_executed": False, **FLAGS}


def attach_entry_comparison(run_dir):
    return _attach_existing_route(run_dir, "entry_comparison")


def attach_public_seat(run_dir):
    return _attach_existing_route(run_dir, "public_seat")


def attach_leader_type_router(run_dir, *, registration=None, forward_run_dir=None):
    options = {}
    if registration is not None:
        options["registration"] = registration
    if forward_run_dir is not None:
        options["run_dir"] = forward_run_dir
    return _attach_existing_route(run_dir, "leader_type_router", **options)


def adopt_router_repair(run_dir, *, registration, publication, panel, consumer_observation):
    """Explicitly attach a new, already consumed runtime generation; keep the old failure."""
    recipe = _attached_recipe("router_repair")
    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, configured = _load(directory)
        if head.get("router_repair_profile") is not None:
            profile = head["router_repair_profile"]
            if (profile["registration"] != _ref(registration) or profile["publications"][0] != str(Path(publication).resolve())
                    or profile["panels"][0] != str(Path(panel).resolve())):
                raise ValueError("The existing runtime-repair attachment cannot change its first identity/source")
            return {"status": "existing_router_repair_attachment_reused", "attachment": head["router_repair_attachment"],
                "commands_executed": False, **FLAGS}
        if head["last_source_day"] != head["profile"]["last_source_day"]:
            raise ValueError("Repair adoption requires the completed exact core source cursor")
        profile = recipe.bootstrap_profile(registration=registration, publication=publication, panel=panel,
            consumer_observation=consumer_observation, current_source_day=head["last_source_day"])
        original = head.get("leader_type_router_profile")
        if original is None or {"path": original["registration"]["file"],
                "sha256": original["registration"]["sha256"]} != profile["original_registration"]:
            raise ValueError("New repair must refer to this system's unchanged original failed router registration")
        saved = {"schema": "system_daily_router_repair_attachment_v1", "attached_at": _now(),
            "system_registration": head["registration"], "initial_profile": profile,
            "original_router_attachment": deepcopy(head["leader_type_router_attachment"]),
            "original_router_profile_at_adoption": deepcopy(original),
            "original_failed_route_rewritten": False, "commands_executed": False, **FLAGS}
        path = directory / "addenda/router_repair.json"
        if path.exists():
            prior = _read(path)
            if any(prior.get(k) != v for k, v in saved.items() if k != "attached_at"):
                raise ValueError("An existing repair addendum cannot change its exact initial generation")
        else:
            _atomic(path, saved)
        head.update(router_repair_attachment=_ref(path), router_repair_profile=profile)
        _atomic(directory / "head.json", head)
        issues = _registered_opening(head, configured, directory / "operations" / profile["last_source_day"].replace("-", ""))
        return {"status": "new_router_repair_generation_attached", "attachment": head["router_repair_attachment"],
            "opening_source_issues": issues, "original_failed_route_rewritten": False, "commands_executed": False, **FLAGS}


def adopt_lowvol_bootstrap(run_dir, *, publication, forward_run_dir, source_directory,
        recent_action_extension, route_id="native_lowvol_bootstrap_20261019"):
    from .native_lowvol_bootstrap_adoption import prepare
    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        existing = head["lowvol"].get("generation_ref")
        if existing is not None:
            saved = _read(existing["file"])
            if (head["lowvol"]["route_id"] != route_id
                    or head["lowvol"]["run_directory"] != str(Path(forward_run_dir).resolve())
                    or head["lowvol"]["source_directory"] != str(Path(source_directory).resolve())
                    or saved["generation"]["publication"]["file"] != str(Path(publication).resolve())
                    or _ref(publication)["sha256"] != saved["generation"]["publication"]["sha256"]
                    or _ref(recent_action_extension) != saved["generation"]["recent_action_extension"]):
                raise ValueError("An existing bootstrap generation cannot switch its original identity/source")
            return {"status": "existing_lowvol_bootstrap_generation_reused", "generation": existing,
                "commands_executed": False, **FLAGS}
        prepared = prepare(head, publication, forward_run_dir, source_directory,
            recent_action_extension, route_id)
        path = directory / "addenda" / (route_id + ".json")
        saved = {"schema": "system_daily_lowvol_bootstrap_attachment_v1",
            "system_registration": head["registration"], **prepared, **FLAGS}
        if path.exists():
            prior = _read(path)
            for key in ("adopted_at",):
                saved["generation"][key] = prior["generation"][key]
            if saved != prior:
                raise ValueError("A saved bootstrap addendum cannot change its exact new/old generation")
        else:
            _atomic(path, saved)
        head["lowvol"] = {**prepared["lowvol"], "generation_ref": _ref(path)}
        _atomic(directory / "head.json", head)
        from .native_lowvol_saved_opening import build_source, build_opening
        issues = []
        try:
            build_source(forward_run_dir, publication, source_directory)
            coordinator = Path(registration["opening_run_directory"])
            _, configured = opening_daily._load(coordinator)
            if not any(row["id"] == route_id for row in configured["routes"]):
                destination = directory / "operations" / prepared["generation"]["source_day"].replace("-", "") / "artifacts" / (route_id + "_opening")
                if not (destination / "result.json").exists():
                    build_opening(Path(source_directory) / "head.json", destination)
                result = _read(destination / "result.json")
                artifacts = {"result": str(destination / "result.json")}
                if result.get("opening_pack"):
                    artifacts["opening_pack"] = result["opening_pack"]
                opening_daily.extend(coordinator, [{"id": route_id, "family": "native_lowvol",
                    "source": str(Path(source_directory).resolve()), "adopt": artifacts}])
        except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
            issues.append({"route": route_id, "error": str(error)})
        return {"status": "new_lowvol_bootstrap_generation_attached", "generation": head["lowvol"]["generation_ref"],
            "route_id": route_id, "opening_source_issues": issues,
            "old_account_migrated": False, "engine_advanced": False, "commands_executed": False, **FLAGS}


def attach_dated_catchup(run_dir):
    """Attach the already registered four-case signal, without running it."""
    from . import leader_dated_catchup_daily as dated

    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        if head.get("dated_catchup_profile") is not None:
            return {"status": "existing_dated_catchup_attachment_reused",
                "attachment": head["dated_catchup_attachment"],
                "profile": head["dated_catchup_profile"], "commands_executed": False, **FLAGS}
        profile = dated.bootstrap_profile()
        if profile["last_source_day"] != head["profile"]["last_source_day"]:
            raise ValueError("Attach only at the exact existing source cursor; no skipped or backdated publication")
        if datetime.now(MARKET_TZ) >= datetime.fromisoformat(profile["first_decision_day"] + "T15:00:00+08:00"):
            raise ValueError("Attach before the original first decision close; never roll the registration")
        path = directory / "addenda/dated_catchup.json"
        saved = {"schema": "system_daily_dated_catchup_attachment_v1", "attached_at": _now(),
            "system_registration": head["registration"], "initial_profile": profile,
            "existing_route_accounts_reregistered": False, "commands_executed": False, **FLAGS}
        if path.exists():
            # A saved attachment may be adopted after an interrupted head write;
            # its original date/profile must still be exactly the same.
            prior = _read(path)
            if any(prior.get(key) != saved.get(key) for key in saved if key != "attached_at"):
                raise ValueError("An existing attachment cannot change its original profile")
        else:
            _atomic(path, saved)
        head.update(dated_catchup_attachment=_ref(path), dated_catchup_profile=profile)
        _atomic(directory / "head.json", head)
        return {"status": "dated_catchup_attached_without_running",
            "attachment": head["dated_catchup_attachment"], "profile": profile,
            "commands_executed": False, **FLAGS}


def attach_residual_exit(run_dir):
    """Attach the registered residual-exit method without running any route."""
    from . import leader_eight_type_residual_daily as residual

    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        if head.get("residual_exit_profile") is not None:
            return {"status": "existing_residual_exit_attachment_reused",
                "attachment": head["residual_exit_attachment"],
                "profile": head["residual_exit_profile"], "commands_executed": False, **FLAGS}
        profile = residual.bootstrap_profile()
        if profile["last_source_day"] != head["profile"]["last_source_day"]:
            raise ValueError("Attach only at the exact existing source cursor; no skipped or backdated publication")
        if datetime.now(MARKET_TZ) >= datetime.fromisoformat(profile["first_decision_day"] + "T15:00:00+08:00"):
            raise ValueError("Attach before the original first decision close; never roll the registration")
        path = directory / "addenda/residual_exit.json"
        saved = {"schema": "system_daily_residual_exit_attachment_v1", "attached_at": _now(),
            "system_registration": head["registration"], "initial_profile": profile,
            "existing_route_accounts_reregistered": False, "commands_executed": False, **FLAGS}
        if path.exists():
            prior = _read(path)
            if any(prior.get(key) != saved.get(key) for key in saved if key != "attached_at"):
                raise ValueError("An existing attachment cannot change its original profile")
        else:
            _atomic(path, saved)
        head.update(residual_exit_attachment=_ref(path), residual_exit_profile=profile)
        _atomic(directory / "head.json", head)
        return {"status": "residual_exit_attached_without_running",
            "attachment": head["residual_exit_attachment"], "profile": profile,
            "commands_executed": False, **FLAGS}


def attach_known_identity(run_dir):
    """Attach the four registered crossing books without running any route."""
    from . import leader_known_identity_daily as known

    directory = opening_daily._destination(run_dir)
    with _lock(directory / "runner.lock"):
        head, registration = _load(directory)
        if head.get("known_identity_profile") is not None:
            return {"status": "existing_known_identity_attachment_reused",
                "attachment": head["known_identity_attachment"],
                "profile": head["known_identity_profile"], "commands_executed": False, **FLAGS}
        profile = known.bootstrap_profile()
        if profile["last_source_day"] != head["profile"]["last_source_day"]:
            raise ValueError("Attach only at the exact existing source cursor; no skipped or backdated publication")
        if datetime.now(MARKET_TZ) >= datetime.fromisoformat(profile["first_decision_day"] + "T15:00:00+08:00"):
            raise ValueError("Attach before the original first decision close; never roll the registration")
        path = directory / "addenda/known_identity.json"
        saved = {"schema": "system_daily_known_identity_attachment_v1", "attached_at": _now(),
            "system_registration": head["registration"], "initial_profile": profile,
            "existing_route_accounts_reregistered": False, "commands_executed": False, **FLAGS}
        if path.exists():
            prior = _read(path)
            if any(prior.get(key) != saved.get(key) for key in saved if key != "attached_at"):
                raise ValueError("An existing attachment cannot change its original profile")
        else:
            _atomic(path, saved)
        head.update(known_identity_attachment=_ref(path), known_identity_profile=profile)
        _atomic(directory / "head.json", head)
        return {"status": "known_identity_attached_without_running",
            "attachment": head["known_identity_attachment"], "profile": profile,
            "commands_executed": False, **FLAGS}


def _checked_action_result(head, day, package, result_file, source_proof):
    """Bind one saved producer result to this day's exact low-volatility scope."""
    from .recent_action_extension import extension_readiness
    from .recent_action_sources import _head_scope, _positive
    from .native_lowvol_publication import load_publication
    result = _read(result_file)
    if (result.get("schema") != "scoped_recent_action_refresh_result_v1" or result.get("day") != day
            or any(result.get(key) is not value for key, value in
                   {"execution_admitted": False, "engine_advanced": False, "ledger_mutated": False}.items())):
        raise ValueError("An exact-day, unconsumed research action-refresh result is required")
    request = _read(result["request_file"])
    lowvol = head["lowvol"]
    calendar = head["native_profile"]["calendar"]
    for key, expected in (("package", package), ("calendar", calendar),
                          ("base_event_view", lowvol["action_event_view"]),
                          ("base_reference_view", lowvol["action_reference_view"])):
        if Path(request[key]).resolve() != Path(expected).resolve():
            raise ValueError("Action-refresh result belongs to different daily inputs")
    prepared = result["preparation"]
    if prepared["source_close_proof"] != source_proof:
        raise ValueError("Action-refresh result changed the exact complete-close proof")
    manifest = prepared["manifest_ref"]
    if _ref(manifest["path"])["sha256"] != manifest["sha256"]:
        raise ValueError("Saved action-refresh preparation changed")
    publication = Path(lowvol["publications"][-1]).resolve()
    plan = load_publication(publication)
    scope = {symbol for symbol, weight in (plan.get("weights") or {}).items() if _positive(weight)}
    expected_sources = {("saved_native_lowvol_publication", str(publication), _ref(publication)["sha256"])}
    account = Path(lowvol["run_directory"]) / "head.json"
    if account.exists():
        held, evidence = _head_scope(account, day)
        scope.update(held)
        expected_sources.add((evidence["kind"], evidence["path"], evidence["sha256"]))
    actual_sources = {(row["kind"], str(Path(row["path"]).resolve()), row["sha256"])
                      for row in prepared["scope_sources"]}
    if actual_sources != expected_sources or set(prepared["scope_symbols"]) != scope:
        raise ValueError("Action-refresh scope is not the current saved plan and research account")
    readiness = None
    if result.get("status") == "ready":
        readiness = extension_readiness(result["bundle"], lowvol["action_event_view"],
            lowvol["action_reference_view"], calendar, through=day)
        if not readiness["ready_for_day"] or set(readiness["scope_symbols"]) != scope:
            raise ValueError("Prepared action extension does not cover the exact day and exposure scope")
    return result, readiness


def _recent_actions(head, day, package, operation, *, options=None, saved_result=None, source_proof=None):
    """Explicit producer/adoption stage; a saved attempt is never relaunched."""
    from .recent_action_refresh import run as refresh
    options = dict(options or {})
    if set(options) - {"reuse_roots", "reviews", "prior_bundle", "wheel"}:
        raise ValueError("Unsupported scoped action-refresh option")
    request = {"options": {key: [str(Path(p).resolve()) for p in value]
        if key in {"reuse_roots", "reviews"} else str(Path(value).resolve()) if value is not None else None
        for key, value in options.items()},
        "saved_result": str(Path(saved_result).resolve()) if saved_result is not None else None,
        "source_proof": source_proof}
    request["evidence_refs"] = [_ref(p) for p in
        [*options.get("reviews", []), *([options["prior_bundle"]] if options.get("prior_bundle") else []),
         *([saved_result] if saved_result is not None else [])]]
    identity = sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    base = operation / "action_refresh"
    directory = base / identity
    stage_file = directory / "stage.json"
    if stage_file.exists():
        stage = _read(stage_file)
        if stage["request"] != request:
            raise ValueError("An existing daily action stage cannot switch its original request")
        for ref in stage.get("output_refs", []):
            if _ref(ref["file"]) != ref:
                raise ValueError("Saved daily action-refresh output changed")
        return stage, stage.get("bundle") if stage["status"] == "ready" else None
    prior_provider_roots = []
    for started in base.glob("*/started.json"):
        prior_stage = started.parent / "stage.json"
        if (not prior_stage.exists() or _read(prior_stage).get("execution_progress_unknown") is True):
            return {"status": "action_refresh_progress_unknown_no_automatic_retry", "bundle": None,
                "operation": str(started.parent), "producer_called": False, "network_accessed": False}, None
        prepared = started.parent / "refresh/source/manifest.json"
        if prepared.exists():
            prior_provider_roots.append(_read(prepared)["provider_root"])
    if directory.exists():
        return {"status": "action_refresh_progress_unknown_no_automatic_retry", "bundle": None,
            "operation": str(directory), "producer_called": False, "network_accessed": False}, None
    lowvol = head["lowvol"]
    prior = options.get("prior_bundle") or lowvol.get("recent_action_extension")
    # A later explicit review amendment reuses the previous attempt's source,
    # including its failed-query evidence; it must not query that day again.
    roots = [*options.get("reuse_roots", []), *prior_provider_roots]
    if prior is not None:
        roots.append(_read(prior)["provider_root"])
    roots = list(dict.fromkeys(str(Path(p).resolve()) for p in roots))
    protected = [package, *roots, *[Path(p).parent for p in options.get("reviews", [])]]
    if saved_result is not None:
        protected.append(Path(saved_result).parent)
    if prior is not None:
        protected.append(Path(prior).parent)
    _separate(directory, *protected)
    directory.mkdir(parents=True)
    _atomic(directory / "started.json", {"request": request, "started_at": _now(), **FLAGS})
    producer_called = saved_result is None
    result_file = Path(saved_result).resolve() if saved_result is not None else directory / "refresh/result.json"
    try:
        if producer_called:
            account = Path(lowvol["run_directory"]) / "head.json"
            refresh(day, package, head["native_profile"]["calendar"], result_file.parent,
                publications=[lowvol["publications"][-1]], account_heads=[account] if account.exists() else [],
                reuse_roots=roots, base_event_view=lowvol["action_event_view"],
                base_reference_view=lowvol["action_reference_view"], prior_bundle=prior,
                reviews=options.get("reviews", []), wheel=options.get("wheel"))
        value, readiness = _checked_action_result(head, day, package, result_file, source_proof)
        refs = [_ref(result_file), _ref(value["request_file"]), _ref(value["preparation"]["manifest_ref"]["path"])]
        refs.extend({"file": ref["path"], "sha256": ref["sha256"]}
                    for ref in value.get("source", {}).get("output_refs", []))
        if readiness is not None:
            refs.extend({"file": ref["path"], "sha256": ref["sha256"]} for ref in readiness["refs"])
        stage = {"status": value["status"], "bundle": value.get("bundle") if readiness is not None else None,
            "request": request, "producer_result": str(result_file), "producer_called": producer_called,
            "pending_review": value.get("pending_review", []), "output_refs": refs,
            "prepared_scope_count": len(value["preparation"]["scope_symbols"]),
            "network_accessed": value.get("source", {}).get("network_accessed", False),
            "execution_progress_unknown": "progress_unknown" in value.get("source", {}).get("status", ""),
            "prepared_not_account_consumed": True, "completed_at": _now()}
    except Exception as error:
        stage = {"status": "failed_action_refresh_no_automatic_retry", "bundle": None,
            "request": request, "producer_called": producer_called,
            "producer_result": str(result_file), "execution_progress_unknown": False,
            "error": f"{type(error).__name__}: {error}",
            "network_accessed": None if producer_called else False, "completed_at": _now()}
    stage.update({key: value for key, value in FLAGS.items() if key != "network_accessed"})
    _atomic(stage_file, stage)
    return stage, stage["bundle"]


def _lowvol(head, day, package, operation, *, saved_lowvol_run=None, recent_action_extension=None):
    from .native_lowvol_forward import run as consume
    from .native_lowvol_publication import load_publication, publish
    lowvol = deepcopy(head["lowvol"])
    current = load_publication(lowvol["publications"][-1])
    if current["source_day"] == day:
        return {"status": "saved_exact_source_plan_reused", "publication": current["publication_ref"]}, lowvol
    run_dir = Path(lowvol["run_directory"])
    account_file = run_dir / "head.json"
    if current["execution_day"] != day:
        return {"status": "missing_exact_preceding_plan_no_skip", "source_day": current["source_day"]}, lowvol
    # A known old action window is a specific absent input, not an empty list
    # of dividends or a reason to recreate the source/query batch.
    action = _read(Path(lowvol["action_event_view"]) / "result.json")
    extension = recent_action_extension or lowvol.get("recent_action_extension")
    extension_status = None
    if extension is not None:
        from .recent_action_extension import extension_readiness
        extension_status = extension_readiness(extension, lowvol["action_event_view"],
            lowvol["action_reference_view"], current["calendar_ref"]["file"], through=day)
        if not extension_status["ready_for_day"]:
            return {"status": "waiting_for_action_coverage", "required_day": day,
                "action_source_through": extension_status["window_end"],
                "missing_action_sessions": extension_status["missing_action_sessions"],
                "recent_action_extension": _ref(extension), "producer_called": False}, lowvol
    elif action["end"] < day:
        return {"status": "waiting_for_action_coverage", "required_day": day,
            "action_source_through": action["end"], "producer_called": False}, lowvol
    closes = list(dict.fromkeys([*lowvol["raw_closes"], str(Path(package).resolve())]))
    saved_run = saved_lowvol_run or next(ref["file"] for ref in current["source_refs"] if ref.get("role") == "saved_run")
    inputs = {"publication": current["publication_ref"], "saved_run": _ref(saved_run),
        "event": _ref(Path(lowvol["action_event_view"]) / "result.json"),
        "reference": _ref(Path(lowvol["action_reference_view"]) / "result.json"),
        "close": _ref(Path(package) / "receipt.json")}
    if extension_status is not None:
        inputs["recent_action_extension"] = _ref(extension)
        inputs["recent_action_extension_inputs"] = extension_status["refs"]
    identity = sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    job = operation / "lowvol" / identity
    result_file = job / "consumer.json"
    # Changing a bundle hash cannot turn an unresolved earlier consumer into a
    # new attempt. A completed no-commit evidence failure may use genuinely new
    # evidence; an unknown process outcome needs explicit recovery first.
    for prior in (operation / "lowvol").glob("*/consumer_started.json"):
        prior_result = prior.parent / "consumer.json"
        if (not prior_result.exists()
                or _read(prior_result).get("execution_progress_unknown") is True):
            return {"status": "unresolved_lowvol_consumer_progress_no_automatic_retry",
                "operation": str(prior.parent)}, lowvol
    if result_file.exists():
        result = _read(result_file)
    else:
        started = job / "consumer_started.json"
        if started.exists():
            return {"status": "unresolved_lowvol_consumer_progress_no_automatic_retry", "operation": str(job)}, lowvol
        _atomic(started, {"inputs": inputs, "started_at": _now(), **FLAGS})
        try:
            options = {"through": day}
            if extension_status is not None:
                options["recent_action_extension"] = extension
            result = consume(lowvol["publications"], closes,
                head["native_profile"]["ipo_evidence"], head["native_profile"]["master_run"],
                lowvol["action_event_view"], lowvol["action_reference_view"], run_dir, **options)
        except Exception as error:
            result = {"status": "failed_lowvol_consumer_no_automatic_retry", "last_execution_day": None,
                "error": f"{type(error).__name__}: {error}", "execution_progress_unknown": True, **FLAGS}
        _atomic(result_file, result)
    if result["last_execution_day"] != day:
        return result, lowvol
    if extension_status is not None:
        lowvol["recent_action_extension"] = str(Path(extension).resolve())
    destination = job / "publication"
    if (destination / "publication.json").exists():
        plan = load_publication(destination / "publication.json")
    elif destination.exists() or (job / "publication_started.json").exists():
        return {"status": "unresolved_publication_progress_no_retry"}, lowvol
    else:
        _atomic(job / "publication_started.json", {"inputs": inputs, "started_at": _now(), **FLAGS})
        try:
            plan = publish(saved_run, current["calendar_ref"]["file"], package, destination,
                inception=current["inception"], account_path=account_file,
                previous_publication=lowvol["publications"][-1])
        except Exception as error:
            failure = {"status": "failed_lowvol_publication_no_automatic_retry", "error": str(error), **FLAGS}
            _atomic(job / "publication_failed.json", failure)
            return failure, lowvol
    lowvol["publications"].append(plan["publication_ref"]["file"])
    lowvol["raw_closes"] = closes
    return {"status": "advanced_and_published_next_native_lowvol_plan", "consumer": result,
        "publication": plan["publication_ref"]}, lowvol


def _opening(head, registration, operation):
    from .native_saved_opening import build_source as native_source, source_dependencies_current as native_current
    from .native_lowvol_saved_opening import build_source as lowvol_source, build_opening, source_dependencies_current
    coordinator = Path(registration["opening_run_directory"])
    _, configured = opening_daily._load(coordinator)
    native = head["native_profile"]
    issues = []
    for key, route_id in (("v1", "native_preopen"), ("qualified", "native_ipoqualified")):
        route = next(row for row in configured["routes"] if row["id"] == route_id)
        template = native["forward_templates"][key]
        if not native_current(route["source"]):
            try:
                result = native_source(template["run_dir"], native["latest_plans"][key], route["source"])
                if result.get("status") != "saved_native_account_and_next_plan":
                    issues.append({"route": route_id, "status": result.get("status"), "reason": result.get("reason")})
            except (ValueError, KeyError) as error:
                issues.append({"route": route_id, "error": str(error)})
    lowvol = head["lowvol"]
    source_dir = Path(lowvol["source_directory"])
    lowvol_route = lowvol.get("route_id", "native_lowvol")
    if not source_dependencies_current(source_dir):
        try:
            lowvol_source(lowvol["run_directory"], lowvol["publications"][-1], source_dir)
        except (ValueError, KeyError) as error:
            issues.append({"route": lowvol_route, "error": str(error)})
    if not any(row["id"] == lowvol_route for row in configured["routes"]):
        destination = operation / "artifacts" / (lowvol_route + "_opening")
        if not (destination / "result.json").exists():
            build_opening(source_dir / "head.json", destination)
        artifacts = {"result": str(destination / "result.json")}
        result = _read(destination / "result.json")
        if result.get("opening_pack"):
            artifacts["opening_pack"] = result["opening_pack"]
        opening_daily.extend(coordinator, [{"id": lowvol_route, "family": "native_lowvol",
            "source": str(source_dir), "adopt": artifacts}])
    pending_routes = []
    dated = head.get("dated_catchup_profile")
    if dated is not None:
        account = Path(dated["run_directory"])
        if (account / "head.json").exists():
            if not any(row["id"] == "dated_catchup" for row in configured["routes"]):
                from .leader_eight_type_opening import build as published_opening
                account_head = _read(account / "head.json")
                processed = {case["state"].get("day") for case in account_head["cases"].values()}
                if len(processed) != 1 or None in processed:
                    issues.append({"route": "dated_catchup", "reason": "four_case_processed_days_do_not_agree"})
                else:
                    calendar = account_head["binding"]["calendar"]
                    sessions, _, _ = _calendar_view(calendar, f"{calendar['year']}-01-01",
                        f"{calendar['year']}-12-31", [])
                    last = next(iter(processed))
                    execution = next((value for value in sessions if value > last), None)
                    destination = operation / "artifacts/dated_catchup_opening"
                    if not (destination / "result.json").exists():
                        published_opening(account, execution, destination, source_family="dated_catchup")
                    result = _read(destination / "result.json")
                    artifacts = {"result": str(destination / "result.json")}
                    if result.get("opening_pack"):
                        artifacts["opening_pack"] = result["opening_pack"]
                    opening_daily.extend(coordinator, [{"id": "dated_catchup", "family": "dated_catchup",
                        "source": str(account), "adopt": artifacts}])
        else:
            # A saved publication before first E is a plan, not an account.
            pending_routes.append({"id": "dated_catchup", "case_count": 4,
                "status": "registered_without_saved_account", "account_count": None,
                "first_decision_day": dated["first_decision_day"],
                "first_execution_day": dated["first_execution_day"], "metrics": None})
    residual = head.get("residual_exit_profile")
    if residual is not None:
        account = Path(residual["run_directory"])
        if not (account / "head.json").exists():
            pending_routes.append({"id": "eight_type_residual", "case_count": 32,
                "status": "registered_without_saved_account", "account_count": None,
                "first_decision_day": residual["first_decision_day"],
                "first_execution_day": residual["first_execution_day"], "metrics": None})
        elif not any(row["id"] == "eight_type_residual" for row in configured["routes"]):
            from .leader_eight_type_opening import build as published_opening
            try:
                account_head = _read(account / "head.json")
                cases = account_head["cases"]
                processed = {case["state"].get("day") for case in cases.values()}
                if (len(cases) != 32 or set(cases) != set(residual["case_ids"])
                        or len(processed) != 1 or None in processed
                        or next(iter(processed)) != residual["last_source_day"]):
                    issues.append({"route": "eight_type_residual",
                        "reason": "all_32_cases_must_commit_the_exact_latest_source_day"})
                else:
                    calendar = account_head["binding"]["calendar"]
                    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01",
                        f"{calendar['year']}-12-31", [])
                    last = next(iter(processed))
                    if status != "pinned_static_exchange_calendar" or last not in sessions[:-1]:
                        raise ValueError("The committed residual books need an exact next exchange session")
                    execution = sessions[sessions.index(last) + 1]
                    destination = operation / "artifacts/eight_type_residual_opening"
                    if not (destination / "result.json").exists():
                        published_opening(account, execution, destination, source_family="eight_type_residual")
                    result = _read(destination / "result.json")
                    artifacts = {"result": str(destination / "result.json")}
                    if result.get("opening_pack"):
                        artifacts["opening_pack"] = result["opening_pack"]
                    opening_daily.extend(coordinator, [{"id": "eight_type_residual", "family": "eight_type_residual",
                        "source": str(account), "adopt": artifacts}])
            except (ValueError, KeyError, FileNotFoundError) as error:
                issues.append({"route": "eight_type_residual", "error": str(error)})
    known = head.get("known_identity_profile")
    if known is not None:
        account = Path(known["run_directory"])
        if not (account / "head.json").exists():
            pending_routes.append({"id": "known_identity", "case_count": 4,
                "status": "registered_without_saved_account", "account_count": None,
                "first_decision_day": known["first_decision_day"],
                "first_execution_day": known["first_execution_day"], "metrics": None})
        elif not any(row["id"] == "known_identity" for row in configured["routes"]):
            from .leader_eight_type_opening import build as published_opening
            try:
                account_head = _read(account / "head.json")
                cases = account_head["cases"]
                processed = {case["state"].get("day") for case in cases.values()}
                if (len(cases) != 4 or set(cases) != set(known["case_ids"])
                        or len(processed) != 1 or None in processed
                        or next(iter(processed)) != known["last_source_day"]):
                    issues.append({"route": "known_identity",
                        "reason": "all_four_cases_must_commit_the_exact_latest_source_day"})
                else:
                    calendar = account_head["binding"]["calendar"]
                    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01",
                        f"{calendar['year']}-12-31", [])
                    last = next(iter(processed))
                    if status != "pinned_static_exchange_calendar" or last not in sessions[:-1]:
                        raise ValueError("The committed crossing books need an exact next exchange session")
                    execution = sessions[sessions.index(last) + 1]
                    destination = operation / "artifacts/known_identity_opening"
                    if not (destination / "result.json").exists():
                        published_opening(account, execution, destination, source_family="known_identity")
                    result = _read(destination / "result.json")
                    artifacts = {"result": str(destination / "result.json")}
                    if result.get("opening_pack"):
                        artifacts["opening_pack"] = result["opening_pack"]
                    opening_daily.extend(coordinator, [{"id": "known_identity", "family": "known_identity",
                        "source": str(account), "adopt": artifacts}])
            except (ValueError, KeyError, FileNotFoundError) as error:
                issues.append({"route": "known_identity", "error": str(error)})
    issues.extend(_registered_opening(head, registration, operation))
    answer = opening_daily.run(coordinator)
    saved = opening_daily.status(coordinator)
    return {"result": answer, "status": saved["status"], "source_issues": issues,
        "routes": [{key: row.get(key) for key in ("id", "family", "status", "saved_product")} for row in saved["routes"]],
        "pending_registered_routes": pending_routes,
        "legacy_fixed_engineering_advanced": False, **FLAGS}


def _registered_opening(head, registration, operation):
    """Publish saved inputs/accounts for the three attached research routes."""
    from .registered_research_saved_opening import build_source, build_opening
    coordinator = Path(registration["opening_run_directory"])
    _, configured = opening_daily._load(coordinator)
    profiles, issues = [], []
    entry = head.get("entry_comparison_profile")
    if entry is not None:
        entry_observation = operation / "entry_comparison/forward_result.json"
        if not entry_observation.exists():
            for receipt_file in reversed(entry["operations"]):
                candidate = Path(receipt_file).parent / "forward_result.json"
                if candidate.exists():
                    entry_observation = candidate
                    break
        profiles.append(("entry_comparison", entry["registration"], entry["publications"],
            entry["run_directory"], entry_observation))
    router = head.get("leader_type_router_profile")
    if router is not None:
        observation = operation / "leader_type_router/forward_result.json"
        if not observation.exists():
            for receipt_file in reversed(router["operations"]):
                candidate = Path(receipt_file).parent / "forward_result.json"
                if candidate.exists():
                    observation = candidate
                    break
        profiles.append(("leader_type_router", router["registration"], router["publications"],
            router["run_directory"], observation))
    repaired = head.get("router_repair_profile")
    if repaired is not None:
        observation = operation / "router_repair/forward_result.json"
        if not observation.exists() and repaired.get("consumer_result"):
            reference = repaired["consumer_result"]
            if _ref(reference["file"]) != reference:
                issues.append({"route": "leader_type_router_runtime_repair", "error": "saved_consumer_observation_changed"})
            else:
                observation = Path(reference["file"])
        profiles.append(("leader_type_router_runtime_repair", repaired["registration"], repaired["publications"],
            repaired["run_directory"], observation))
    seats = head.get("public_seat_profile")
    if seats is not None:
        for branch, route in (("old_seat", "public_seat_static"), ("append_seat", "public_seat_append")):
            saved = seats[branch]
            observation = operation / f"public_seat/{branch}_forward_result.json"
            if not observation.exists() and saved.get("consumer_result"):
                reference = saved["consumer_result"]
                if _ref(reference["file"]) != reference:
                    issues.append({"route": route, "error": "saved_consumer_observation_changed"})
                    continue
                observation = Path(reference["file"])
            profiles.append((route, saved["registration"], saved["publications"], saved["run_directory"],
                observation))
    for kind, registration_ref, publications, account, observation in profiles:
        route = next((row for row in configured["routes"] if row["id"] == kind), None)
        source_dir = (Path(route["source"]) if route is not None else
            DATA_ROOT / "research/registered_saved_opening" / Path(registration["run_directory"]).name / kind)
        try:
            # This explicit producer refreshes only small saved dependencies.
            # GET/status never calls it, even after a new publication appears.
            build_source(kind, registration_ref["file"], publications, account, source_dir,
                consumer_observation=observation if observation.exists() else None)
            if route is None:
                destination = operation / "artifacts" / (kind + "_opening")
                if not (destination / "result.json").exists():
                    build_opening(source_dir / "head.json", destination)
                result = _read(destination / "result.json")
                artifacts = {"result": str(destination / "result.json")}
                if result.get("opening_pack"):
                    artifacts["opening_pack"] = result["opening_pack"]
                opening_daily.extend(coordinator, [{"id": kind, "family": "registered_research",
                    "source": str(source_dir), "adopt": artifacts}])
        except (ValueError, KeyError, FileNotFoundError, FileExistsError) as error:
            issues.append({"route": kind, "error": str(error)})
    return issues


def _dated_refresh(head, registration, operation, day, source, core_profile):
    """Advance only the attached signal module using saved core outputs."""
    from . import leader_dated_catchup_daily as dated

    profile = head["dated_catchup_profile"]
    if day < profile["first_decision_day"]:
        return {"status": "not_due_before_first_decision", "commands_executed": False,
            "producer_complete": False, "consumer_required": False, "consumer_complete": False,
            "first_decision_day": profile["first_decision_day"],
            "first_execution_day": profile["first_execution_day"], "case_count": 4, **FLAGS}
    if core_profile is None:
        return {"status": "blocked_by_current_eight_type_inputs", "commands_executed": False,
            "producer_complete": False, "consumer_required": day >= profile["first_execution_day"],
            "consumer_complete": False, "case_count": 4, **FLAGS}
    out = operation / "dated_catchup"
    plan_file = out / "plan.json"

    def execute(step):
        bound = {**step, "binding": {"kind": "dated_catchup", "registration": head["registration"],
            "attachment": head["dated_catchup_attachment"], "source": source, "plan": _ref(plan_file)}}
        return _run_command(bound, operation / "commands", day)

    paths = core_profile["output_paths"]
    answer = dated.run_profile(profile, day, paths["eight_inputs"]["manifest"],
        paths["environment"]["result"], out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["dated_catchup_profile"] = answer["next_profile"]
    return answer["result"]


def _residual_refresh(head, registration, operation, day, source, core_profile):
    """Wrap the saved original eight-type publication; do not rerun producers."""
    from . import leader_eight_type_residual_daily as residual

    profile = head["residual_exit_profile"]
    if day < profile["first_decision_day"]:
        return {"status": "not_due_before_first_decision", "commands_executed": False,
            "producer_complete": False, "consumer_required": False, "consumer_complete": False,
            "first_decision_day": profile["first_decision_day"],
            "first_execution_day": profile["first_execution_day"], "case_count": len(profile["case_ids"]), **FLAGS}
    if core_profile is None:
        return {"status": "blocked_by_current_eight_type_publication", "commands_executed": False,
            "producer_complete": False, "consumer_required": day >= profile["first_execution_day"],
            "consumer_complete": False, "case_count": len(profile["case_ids"]), **FLAGS}
    out = operation / "residual_exit"
    plan_file = out / "plan.json"

    def execute(step):
        bound = {**step, "binding": {"kind": "residual_exit", "registration": head["registration"],
            "attachment": head["residual_exit_attachment"], "source": source, "plan": _ref(plan_file)}}
        return _run_command(bound, operation / "commands", day)

    paths = core_profile["output_paths"]
    answer = residual.run_profile(profile, day, paths["eight_inputs"]["manifest"],
        paths["paper32"]["publication"], out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["residual_exit_profile"] = answer["next_profile"]
    return answer["result"]


def _known_identity_refresh(head, registration, operation, day, source, core_profile):
    """Reuse the saved qualified input and original publication for four books."""
    from . import leader_known_identity_daily as known

    profile = head["known_identity_profile"]
    if day < profile["first_decision_day"]:
        return {"status": "not_due_before_first_decision", "commands_executed": False,
            "producer_complete": False, "consumer_required": False, "consumer_complete": False,
            "first_decision_day": profile["first_decision_day"],
            "first_execution_day": profile["first_execution_day"], "case_count": len(profile["case_ids"]), **FLAGS}
    if core_profile is None:
        return {"status": "blocked_by_current_eight_type_publication", "commands_executed": False,
            "producer_complete": False, "consumer_required": day >= profile["first_execution_day"],
            "consumer_complete": False, "case_count": len(profile["case_ids"]), **FLAGS}
    out = operation / "known_identity"
    plan_file = out / "plan.json"

    def execute(step):
        bound = {**step, "binding": {"kind": "known_identity", "registration": head["registration"],
            "attachment": head["known_identity_attachment"], "source": source, "plan": _ref(plan_file)}}
        return _run_command(bound, operation / "commands", day)

    paths = core_profile["output_paths"]
    answer = known.run_profile(profile, day, paths["eight_inputs"]["manifest"],
        paths["paper32"]["publication"], out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["known_identity_profile"] = answer["next_profile"]
    return answer["result"]


def _entry_comparison_refresh(head, registration, operation, day, source, core_profile):
    entry = _attached_recipe("entry_comparison")
    profile = head["entry_comparison_profile"]
    if core_profile is None:
        return {"status": "blocked_by_current_own_bundle_or_dated_extras", "commands_executed": False,
            "producer_complete": False, "consumer_required": True, "consumer_complete": False, **FLAGS}
    out = operation / "entry_comparison"

    def execute(step):
        bound = {**step, "binding": {"kind": "entry_comparison", "registration": head["registration"],
            "attachment": head["entry_comparison_attachment"], "source": source,
            "plan": _ref(out / "plan.json")}}
        return _run_command(bound, operation / "commands", day)

    paths = core_profile["output_paths"]
    answer = entry.run_profile(profile, day, paths["pub20"]["bundle"], paths["extras"]["result"],
        out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["entry_comparison_profile"] = answer["next_profile"]
    return answer["result"]


def _latest_saved_seat_catalog(head):
    """Carry a successful disclosure append forward without changing a frozen D plan."""
    seat = _attached_recipe("public_seat")
    current = head["public_seat_profile"]["append_seat"]["catalog"]
    if _ref(current["file"]) != current:
        raise ValueError("Current saved public-seat catalog changed")
    current_facts = seat._catalog(current["file"])
    for receipt_path in reversed(head["operations"]):
        receipt = _read(receipt_path)
        claim = receipt.get("seat_disclosures")
        if not claim or claim.get("daily_complete") is not True:
            continue
        if (claim.get("schema") != "public_seat_disclosure_daily_result_v1"
                or claim.get("status") != "completed_saved_disclosure_catalog_research_only"
                or claim.get("day") != receipt.get("day")
                or receipt.get("source", {}).get("day") != receipt.get("day")
                or claim["day"] > head["last_source_day"]):
            raise ValueError("Saved completed disclosure claim has incompatible exact source/date identity")
        reference = claim["catalog"]
        if _ref(reference["file"]) != reference:
            raise ValueError("Latest completed disclosure catalog changed; do not fall back to an older catalog")
        facts = seat._catalog(reference["file"])
        if (not facts["event_days"] or facts["event_days"][-1] != claim["day"]
                or claim.get("event_days", facts["event_days"]) != facts["event_days"]):
            raise ValueError("Completed disclosure catalog does not contain its exact latest event day")
        if current in facts["refs"]:
            return reference
        if reference in current_facts["refs"]:
            return current  # An explicit newer valid profile already contains this prior success.
        raise ValueError("Completed disclosure catalog is not in the exact current append ancestry")
    return current


def _public_seat_refresh(head, registration, operation, day, source, package, *, catalog=None):
    seat = _attached_recipe("public_seat")
    out = operation / "public_seat"
    plan_file = out / "plan.json"
    if catalog is None and not plan_file.exists():
        catalog = _latest_saved_seat_catalog(head)["file"]
    if plan_file.is_file():
        # D's actual publication and commands retain D's original catalog.
        # A later same-D disclosure append is saved by seat_disclosures for
        # an explicit later source day, never inserted into this frozen plan.
        frozen = _read(plan_file)
        seat.validate_plan(frozen, head["public_seat_profile"], day, package, out,
            catalog=frozen["catalog"])
        if catalog is not None:
            seat._catalog(catalog, frozen["append_catalog"])
        catalog = frozen["catalog"]

    def execute(step):
        bound = {**step, "binding": {"kind": "public_seat", "registration": head["registration"],
            "attachment": head["public_seat_attachment"], "source": source,
            "plan": _ref(out / "plan.json")}}
        return _run_command(bound, operation / "commands", day)

    answer = seat.run_profile(head["public_seat_profile"], day, package, out,
        run_step=execute, catalog=catalog)
    if answer.get("next_profile") is not None:
        head["public_seat_profile"] = answer["next_profile"]
    return answer["result"]


def _leader_type_router_refresh(head, registration, operation, day, source, core_profile):
    recipe = _attached_recipe("leader_type_router")
    profile = head["leader_type_router_profile"]
    if day < profile["first_decision_day"]:
        return {"status": "not_due_before_first_decision", "daily_complete": True,
            "producer_complete": False, "consumer_required": False, "consumer_complete": False,
            "commands_executed": False, **FLAGS}
    if core_profile is None:
        return {"status": "blocked_by_current_eight_type_publication", "daily_complete": False,
            "producer_complete": False, "consumer_required": day >= profile["first_decision_day"],
            "consumer_complete": False, "commands_executed": False, **FLAGS}
    out = operation / "leader_type_router"
    def execute(step):
        bound = {**step, "binding": {"kind": "leader_type_router", "registration": head["registration"],
            "attachment": head["leader_type_router_attachment"], "source": source,
            "plan": _ref(out / "plan.json")}}
        return _run_command(bound, operation / "commands", day)
    paths = core_profile["output_paths"]
    answer = recipe.run_profile(profile, day, paths["eight_inputs"]["manifest"],
        paths["paper32"]["publication"], out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["leader_type_router_profile"] = answer["next_profile"]
    return answer["result"]


def _router_repair_refresh(head, registration, operation, day, source, core_profile):
    recipe = _attached_recipe("router_repair")
    if core_profile is None:
        return {"status": "blocked_by_current_eight_type_publication", "daily_complete": False, **FLAGS}
    profile = head["router_repair_profile"]
    if profile["last_source_day"] == day and not (operation / "router_repair/plan.json").exists():
        return {"status": "saved_actual_repair_inception_reused", "daily_complete": True,
            "producer_complete": True, "consumer_complete": True, "commands_executed": False, **FLAGS}
    out = operation / "router_repair"
    def execute(step):
        bound = {**step, "binding": {"kind": "router_repair", "registration": head["registration"],
            "attachment": head["router_repair_attachment"], "source": source, "plan": _ref(out / "plan.json")}}
        return _run_command(bound, operation / "commands", day)
    paths = core_profile["output_paths"]
    answer = recipe.run_profile(profile, day, paths["eight_inputs"]["manifest"], paths["paper32"]["publication"],
        out, run_step=execute)
    if answer.get("next_profile") is not None:
        head["router_repair_profile"] = answer["next_profile"]
    return answer["result"]


def _seat_disclosures_refresh(head, registration, operation, day, source):
    from . import seat_disclosure_daily as disclosures
    out = operation / "seat_disclosures"
    plan_file = out / "plan.json"
    prior = (_read(plan_file)["prior_catalog"] if plan_file.exists()
        else _latest_saved_seat_catalog(head))

    def bound_step(step):
        return {**step, "binding": {"kind": "seat_disclosures", "registration": head["registration"],
            "attachment": head["public_seat_attachment"], "source": source, "plan": _ref(plan_file)}}

    def execute(step):
        return _run_command(bound_step(step), operation / "commands", day)

    def recover(step):
        receipt_file = operation / "commands" / (step["id"] + ".json")
        if not receipt_file.exists():
            return None
        return _recovered_command(bound_step(step), operation / "commands", day, _read(receipt_file))

    resume = any((operation / "commands" / (step + ".recovery.json")).exists()
        for step in ("seat_disclosure_collect", "seat_disclosure_batch", "seat_disclosure_catalog"))
    return disclosures.run(day, prior["file"], out, calendar=registration["calendar"]["file"],
        run_step=execute, execute=True, resume_saved_recovery=resume, recover_step=recover)


def _merge_network(receipt, observation):
    value = observation.get("network_accessed")
    if receipt.get("network_accessed") is not True:
        receipt["network_accessed"] = value if value is not False else receipt.get("network_accessed", False)
    receipt["network_access_unknown"] = bool(receipt.get("network_access_unknown")
        or observation.get("network_access_unknown") or value is None)


def run(run_dir, day, package, *, action_event_view=None, action_reference_view=None, saved_lowvol_run=None,
        recent_action_extension=None, action_refresh=None, saved_action_refresh=None, seat_catalog=None,
        refresh_seat_disclosures=False, native_paper_closes=None):
    from .daily_refresh_recipe import next_plan
    from .native_shortline_daily import run_profile
    directory = opening_daily._destination(run_dir)
    head, registration = _load(directory)
    if refresh_seat_disclosures and (seat_catalog is not None or head.get("public_seat_profile") is None):
        raise ValueError("Choose a saved seat catalog or explicit refresh of the attached public-seat source")
    refresh_requested = action_refresh is not None or saved_action_refresh is not None
    if ((action_refresh is not None and saved_action_refresh is not None)
            or (refresh_requested and recent_action_extension is not None)):
        raise ValueError("Choose explicit action refresh, saved refresh result, or an existing extension")
    if (action_event_view is None) != (action_reference_view is None):
        raise ValueError("New action evidence requires an explicit event/reference pair")
    if action_event_view is not None:
        head["lowvol"].update(action_event_view=str(Path(action_event_view).resolve()),
            action_reference_view=str(Path(action_reference_view).resolve()))
    calendar_ref = registration["calendar"]
    if _ref(calendar_ref["file"]) != calendar_ref:
        raise ValueError("Registered exchange calendar changed")
    payload = _read(calendar_ref["file"])
    year = payload["year"]
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if day not in sessions or status != "pinned_static_exchange_calendar" or day < head["last_source_day"]:
        raise ValueError("Exact current/next source session required; no backwards replay")
    if day != head["last_source_day"] and previous[day] != head["last_source_day"]:
        raise ValueError("The daily producer chain cannot skip an intervening source session")
    source = _source(package, calendar_ref, sessions, datetime.now(MARKET_TZ))
    if source["proof"]["day"] != day:
        raise ValueError("The complete close package is not the exact requested D")
    operation = directory / "operations" / day.replace("-", "")
    action_inputs = []
    for paper_run, closing_quote in native_paper_closes or []:
        action_inputs.extend((Path(paper_run).resolve(), Path(closing_quote).resolve().parent))
    if saved_action_refresh is not None:
        action_inputs.append(Path(saved_action_refresh).parent)
    if action_refresh is not None:
        action_inputs.extend(action_refresh.get("reuse_roots", []))
        action_inputs.extend(Path(p).parent for p in action_refresh.get("reviews", []))
        if action_refresh.get("prior_bundle") is not None:
            action_inputs.append(Path(action_refresh["prior_bundle"]).parent)
    if head.get("dated_catchup_profile") is not None:
        from .leader_dated_catchup_daily import input_directories
        dated_profile = head["dated_catchup_profile"]
        dated_plan = operation / "dated_catchup/plan.json"
        if dated_plan.exists():
            # Resumption protects the original sources. Already saved outputs
            # in this operation must not become a false source/output overlap.
            dated_profile = _read(dated_plan)["before_profile"]
        action_inputs.extend(input_directories(dated_profile))
    if head.get("residual_exit_profile") is not None:
        from .leader_eight_type_residual_daily import input_directories as residual_input_directories
        residual_profile = head["residual_exit_profile"]
        residual_plan = operation / "residual_exit/plan.json"
        if residual_plan.exists():
            residual_profile = _read(residual_plan)["before_profile"]
        action_inputs.extend(residual_input_directories(residual_profile))
    if head.get("known_identity_profile") is not None:
        from .leader_known_identity_daily import input_directories as known_input_directories
        known_profile = head["known_identity_profile"]
        known_plan = operation / "known_identity/plan.json"
        if known_plan.exists():
            known_profile = _read(known_plan)["before_profile"]
        action_inputs.extend(known_input_directories(known_profile))
    for kind in ("entry_comparison", "public_seat", "leader_type_router", "router_repair"):
        if head.get(kind + "_profile") is not None:
            profile = head[kind + "_profile"]
            plan_file = operation / kind / "plan.json"
            if plan_file.exists():
                profile = _read(plan_file)["before_profile"]
            action_inputs.extend(_attached_recipe(kind).input_directories(profile))
    if seat_catalog is not None:
        if head.get("public_seat_profile") is None:
            raise ValueError("A newer seat catalog requires its explicit registered daily attachment")
        action_inputs.append(Path(seat_catalog).resolve().parent)
    native_inputs = _native_input_profile(head, registration, operation, day, source["proof"], package)
    _separate(operation, package, *_input_directories(head["profile"], native_inputs,
        registration["original_routes"], head["lowvol"], recent_action_extension=recent_action_extension), *action_inputs)
    operation.mkdir(parents=True, exist_ok=True)
    with _lock(directory / "runner.lock"):
        saved_head, _ = _load(directory)
        expected_head = deepcopy(saved_head)
        if action_event_view is not None:
            expected_head["lowvol"].update(action_event_view=str(Path(action_event_view).resolve()),
                action_reference_view=str(Path(action_reference_view).resolve()))
        if expected_head != head:
            raise ValueError("Daily state changed during input validation")
        receipt_file = operation / "receipt.json"
        receipt = _read(receipt_file) if receipt_file.exists() else {"schema": SCHEMA,
            "day": day, "source": source["proof"], "started_at": _now(), "steps": {}, **FLAGS}
        if receipt["source"] != source["proof"]:
            raise ValueError("An existing daily operation cannot switch its original complete close")
        if native_paper_closes or (operation / "native_paper_closing_plan.json").is_file():
            from .native_paper_daily_close import refresh as close_native_paper
            try:
                receipt["native_paper_closing"] = close_native_paper(native_paper_closes, day,
                    source["proof"], operation, head["registration"], execute=_run_command)
            except (OSError, ValueError, KeyError, TypeError) as error:
                receipt["native_paper_closing"] = {"status": "paper_closing_input_or_saved_result_unavailable",
                    "error": str(error), "daily_complete": False, "execution_admitted": False}
            _atomic(receipt_file, receipt)
        core_complete = day == head["profile"]["last_source_day"]
        dated_core = head["profile"] if core_complete else None
        residual_core = head["profile"] if core_complete else None
        known_core = head["profile"] if core_complete else None
        entry_core = head["profile"] if core_complete else None
        if not core_complete:
            plan_file = operation / "plan.json"
            if plan_file.exists():
                plan = _read(plan_file)
            else:
                plan = next_plan(head["profile"], day, package, operation)
                _atomic(plan_file, plan)
            for step in plan["steps"]:
                if any(receipt["steps"].get(parent, {}).get("status") != "completed" for parent in step["requires"]):
                    receipt["steps"][step["id"]] = {"status": "blocked_by_prior_stage"}
                else:
                    bound = {**step, "binding": {"kind": "core", "registration": head["registration"],
                        "source": source["proof"], "plan": _ref(plan_file)}}
                    receipt["steps"][step["id"]] = _run_command(bound, operation / "commands", day)
                _atomic(receipt_file, receipt)
            core_complete = all(row["status"] == "completed" for row in receipt["steps"].values())
            if core_complete:
                head["profile"] = plan["next_profile"]
            if all(receipt["steps"].get(key, {}).get("status") == "completed"
                    for key in ("environment", "observe", "eight_inputs")):
                dated_core = plan["next_profile"]
            if all(receipt["steps"].get(key, {}).get("status") == "completed"
                    for key in ("eight_inputs", "paper32")):
                residual_core = plan["next_profile"]
                known_core = plan["next_profile"]
            if all(receipt["steps"].get(key, {}).get("status") == "completed" for key in ("extras", "pub20")):
                entry_core = plan["next_profile"]
        else:
            receipt["core_producers"] = "saved_exact_source_day_reused_without_reexecution"
        # The shared core owns its source cursor. An unrelated native model
        # failure must not roll completed core publications back to yesterday.
        if core_complete:
            head["last_source_day"] = day
        _atomic(directory / "head.json", head)
        if head.get("dated_catchup_profile") is not None:
            try:
                receipt["dated_catchup"] = _dated_refresh(head, registration, operation, day,
                    source["proof"], dated_core)
            except (ValueError, KeyError, FileExistsError) as error:
                receipt["dated_catchup"] = {"status": "dated_catchup_input_or_terminal_unavailable",
                    "error": str(error), "producer_complete": False, "consumer_complete": False,
                    "consumer_required": day >= head["dated_catchup_profile"]["first_execution_day"]}
            _atomic(receipt_file, receipt)
        if head.get("residual_exit_profile") is not None:
            try:
                receipt["residual_exit"] = _residual_refresh(head, registration, operation, day,
                    source["proof"], residual_core)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["residual_exit"] = {"status": "residual_exit_input_or_terminal_unavailable",
                    "error": str(error), "producer_complete": False, "consumer_complete": False,
                    "consumer_required": day >= head["residual_exit_profile"]["first_execution_day"]}
            _atomic(receipt_file, receipt)
        if head.get("known_identity_profile") is not None:
            try:
                receipt["known_identity"] = _known_identity_refresh(head, registration, operation, day,
                    source["proof"], known_core)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["known_identity"] = {"status": "known_identity_input_or_terminal_unavailable",
                    "error": str(error), "producer_complete": False, "consumer_complete": False,
                    "consumer_required": day >= head["known_identity_profile"]["first_execution_day"]}
            _atomic(receipt_file, receipt)
        if head.get("entry_comparison_profile") is not None:
            try:
                receipt["entry_comparison"] = _entry_comparison_refresh(head, registration, operation, day,
                    source["proof"], entry_core)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["entry_comparison"] = {"status": "entry_comparison_input_or_terminal_unavailable",
                    "error": str(error), "producer_complete": False, "consumer_required": True,
                    "consumer_complete": False}
            _atomic(receipt_file, receipt)
        if head.get("leader_type_router_profile") is not None and head.get("router_repair_profile") is None:
            try:
                receipt["leader_type_router"] = _leader_type_router_refresh(head, registration, operation,
                    day, source["proof"], known_core)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["leader_type_router"] = {"status": "router_input_or_terminal_unavailable",
                    "error": str(error), "daily_complete": False, "producer_complete": False,
                    "consumer_required": day >= head["leader_type_router_profile"]["first_decision_day"],
                    "consumer_complete": False}
            _atomic(receipt_file, receipt)
        if head.get("router_repair_profile") is not None:
            try:
                receipt["router_repair"] = _router_repair_refresh(head, registration, operation,
                    day, source["proof"], known_core)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["router_repair"] = {"status": "repair_route_input_or_terminal_unavailable",
                    "error": str(error), "daily_complete": False, **FLAGS}
            receipt["original_router_superseded_by"] = head["router_repair_profile"]["repair_identity"]
            _atomic(receipt_file, receipt)
            _atomic(directory / "head.json", head)
        if head.get("public_seat_profile") is not None:
            selected_catalog = seat_catalog
            if refresh_seat_disclosures:
                try:
                    disclosure_result = _seat_disclosures_refresh(head, registration, operation, day, source["proof"])
                    receipt["seat_disclosures"] = disclosure_result
                    if disclosure_result.get("daily_complete") and disclosure_result.get("catalog"):
                        selected_catalog = disclosure_result["catalog"]["file"]
                except (ValueError, KeyError, FileNotFoundError, FileExistsError) as error:
                    receipt["seat_disclosures"] = {"status": "disclosure_source_or_terminal_unavailable",
                        "daily_complete": False, "error": str(error), "network_accessed": None,
                        "network_access_unknown": True}
                _merge_network(receipt, receipt["seat_disclosures"])
                _atomic(receipt_file, receipt)
            try:
                receipt["public_seat"] = _public_seat_refresh(head, registration, operation, day,
                    source["proof"], package, catalog=selected_catalog)
            except (ValueError, KeyError, FileExistsError, FileNotFoundError) as error:
                receipt["public_seat"] = {"status": "public_seat_input_or_terminal_unavailable",
                    "error": str(error), "daily_complete": False, "network_accessed": None,
                    "network_access_unknown": True}
            _merge_network(receipt, receipt["public_seat"])
            _atomic(receipt_file, receipt)
        try:
            _native_production_refresh(head, receipt, operation, day, package, previous[day], run_profile)
        except Exception as error:
            observation = {"status": "native_production_evidence_unavailable_no_retry",
                "error": f"{type(error).__name__}: {error}", "automatic_retry": False}
            receipt["native_production_observation_error"] = observation
            receipt.setdefault("native_production", observation)
        _atomic(receipt_file, receipt)
        _atomic(directory / "head.json", head)
        # Native opening execution consumes the previous saved plan; E's new
        # score is never used to decide an order retrospectively at E open.
        for key in head["native_profile"]["forward_templates"]:
            try:
                receipt["native_forward_" + key] = _native_forward_refresh(head, registration, directory,
                    operation, day, package, source["proof"], previous[day], saved_head["native_profile"], key)
            except Exception as error:
                receipt["native_forward_" + key] = {"status": "native_forward_evidence_unavailable_no_retry",
                    "error": f"{type(error).__name__}: {error}", "automatic_retry": False}
                progress = head.setdefault("native_forward_progress", {}).get(key)
                if progress is None or progress["day"] == day:
                    plan_file = operation / "native_command_plan.json"
                    head["native_forward_progress"][key] = {"day": day,
                        "status": receipt["native_forward_" + key]["status"],
                        "plan_ref": _ref(plan_file) if plan_file.is_file() else None,
                        "result": deepcopy(receipt["native_forward_" + key])}
            _atomic(receipt_file, receipt)
            _atomic(directory / "head.json", head)
        action_ready = True
        if refresh_requested:
            receipt["action_refresh"], recent_action_extension = _recent_actions(head, day, package, operation,
                options=action_refresh, saved_result=saved_action_refresh, source_proof=source["proof"])
            action_ready = receipt["action_refresh"]["status"] == "ready"
            if receipt["action_refresh"]["network_accessed"] is True:
                receipt["network_accessed"] = True
            _atomic(receipt_file, receipt)
        if action_ready:
            try:
                receipt["lowvol"], head["lowvol"] = _lowvol(head, day, package, operation,
                    saved_lowvol_run=saved_lowvol_run, recent_action_extension=recent_action_extension)
            except (ValueError, KeyError, FileExistsError) as error:
                receipt["lowvol"] = {"status": "required_lowvol_input_or_publication_unavailable", "error": str(error)}
        else:
            receipt["lowvol"] = {"status": "blocked_by_scoped_action_refresh",
                "action_refresh_status": receipt["action_refresh"]["status"], "consumer_called": False}
        _atomic(receipt_file, receipt)
        _atomic(directory / "head.json", head)
        try:
            receipt["opening"] = _opening(head, registration, operation)
        except Exception as error:
            receipt["opening"] = {"status": "opening_saved_source_unavailable", "routes": [],
                "source_issues": [{"error": f"{type(error).__name__}: {error}"}]}
        native_ready = (head["native_profile"]["source_day"] == day
            and not head["native_profile"].get("chain_has_missing_plan")
            and all(receipt.get("native_forward_" + key, {}).get("status") in
                {"completed", "exact_execution_day_already_committed"} for key in ("v1", "qualified")))
        lowvol_ready = receipt["lowvol"].get("status") in {
            "saved_exact_source_plan_reused", "advanced_and_published_next_native_lowvol_plan"}
        inactive_opening = {"fixed_lowvol"} | ({"leader_type_router"} if head.get("router_repair_profile") else set())
        if head["lowvol"].get("generation_ref"):
            inactive_opening.add("native_lowvol")
        opening_ready = (not receipt["opening"]["source_issues"] and all(
            row["status"] == "current_saved_product" and row["saved_product"]["source_day"] == day
            for row in receipt["opening"]["routes"] if row["id"] not in inactive_opening))
        dated_result = receipt.get("dated_catchup")
        dated_ready = (dated_result is None or dated_result.get("status") == "not_due_before_first_decision"
            or (dated_result.get("producer_complete") is True and
                (dated_result.get("consumer_required") is False or dated_result.get("consumer_complete") is True)))
        residual_result = receipt.get("residual_exit")
        residual_ready = (residual_result is None or residual_result.get("status") == "not_due_before_first_decision"
            or (residual_result.get("producer_complete") is True and
                (residual_result.get("consumer_required") is False or residual_result.get("consumer_complete") is True)))
        known_result = receipt.get("known_identity")
        known_ready = (known_result is None or known_result.get("status") == "not_due_before_first_decision"
            or (known_result.get("producer_complete") is True and
                (known_result.get("consumer_required") is False or known_result.get("consumer_complete") is True)))
        entry_result = receipt.get("entry_comparison")
        entry_ready = (entry_result is None or (entry_result.get("producer_complete") is True
            and (entry_result.get("consumer_required") is False or entry_result.get("consumer_complete") is True)))
        seat_ready = receipt.get("public_seat") is None or receipt["public_seat"].get("daily_complete") is True
        router_ready = (head.get("router_repair_profile") is not None or receipt.get("leader_type_router") is None
            or receipt["leader_type_router"].get("daily_complete") is True)
        repair_ready = receipt.get("router_repair") is None or receipt["router_repair"].get("daily_complete") is True
        disclosures_ready = receipt.get("seat_disclosures") is None or receipt["seat_disclosures"].get("daily_complete") is True
        paper_closing_ready = receipt.get("native_paper_closing") is None or receipt["native_paper_closing"].get("daily_complete") is True
        receipt.update(completed_at=_now(), status="completed_saved_research_refresh" if
            core_complete and native_ready and lowvol_ready and opening_ready and dated_ready and residual_ready and known_ready
            and entry_ready and seat_ready and router_ready and repair_ready and disclosures_ready and paper_closing_ready else "partial_research_routes",
            no_real_orders=True, native_lowvol_account_created=Path(head["lowvol"]["run_directory"], "head.json").exists())
        _atomic(receipt_file, receipt)
        if str(receipt_file) not in head["operations"]:
            head["operations"].append(str(receipt_file))
        _atomic(directory / "head.json", head)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    initialize = commands.add_parser("register")
    for name in ("run-dir", "opening-run-dir", "lowvol-publication", "lowvol-run-dir", "lowvol-source-dir",
                 "action-event-view", "action-reference-view"):
        initialize.add_argument("--" + name, type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--run-dir", type=Path, required=True)
    execute.add_argument("--day", required=True)
    execute.add_argument("--package", type=Path, required=True)
    execute.add_argument("--action-event-view", type=Path)
    execute.add_argument("--action-reference-view", type=Path)
    execute.add_argument("--saved-lowvol-run", type=Path)
    execute.add_argument("--recent-action-extension", type=Path)
    execute.add_argument("--refresh-recent-actions", action="store_true")
    execute.add_argument("--saved-action-refresh", type=Path)
    execute.add_argument("--action-reuse-root", type=Path, action="append", default=[])
    execute.add_argument("--action-review", type=Path, action="append", default=[])
    execute.add_argument("--action-prior-bundle", type=Path)
    execute.add_argument("--action-wheel", type=Path)
    execute.add_argument("--seat-catalog", type=Path,
        help="An actually saved append catalog; missing disclosure days stay unknown")
    execute.add_argument("--refresh-seat-disclosures", action="store_true",
        help="After the real D supplier window, collect only the new disclosure day and append its saved catalog")
    execute.add_argument("--native-paper-close", nargs=2, action="append", metavar=("RUN_DIR", "CLOSING_QUOTE"),
        help="Close only an existing intraday PAPER opening with its explicit saved same-provider closing quote")
    settlement = commands.add_parser("settle-native",
        help="Settle saved original D plans at exact E raw close, without producing E signals")
    settlement.add_argument("--run-dir", type=Path, required=True)
    settlement.add_argument("--day", required=True)
    settlement.add_argument("--package", type=Path, required=True)
    settlement.add_argument("--policy", choices=("v1", "qualified"))
    native_only = commands.add_parser("advance-native",
        help="Continue native source context or a current publication without advancing any account or core cursor")
    native_only.add_argument("--run-dir", type=Path, required=True)
    native_only.add_argument("--day", required=True)
    native_only.add_argument("--package", type=Path, required=True)
    mode = native_only.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source-only", action="store_true")
    mode.add_argument("--publish-current", action="store_true")
    leader_only = commands.add_parser("advance-leader20",
        help="Continue fixed leader source context or current publication without consuming original accounts")
    leader_only.add_argument("--run-dir", type=Path, required=True)
    leader_only.add_argument("--day", required=True)
    leader_only.add_argument("--package", type=Path, required=True)
    leader_mode = leader_only.add_mutually_exclusive_group(required=True)
    leader_mode.add_argument("--source-only", action="store_true")
    leader_mode.add_argument("--publish-current", action="store_true")
    eight_only = commands.add_parser("advance-eight32",
        help="Continue original eight-type inputs/current publication without consuming accounts or the core cursor")
    eight_only.add_argument("--run-dir", type=Path, required=True)
    eight_only.add_argument("--day", required=True)
    eight_only.add_argument("--package", type=Path, required=True)
    eight_mode = eight_only.add_mutually_exclusive_group(required=True)
    eight_mode.add_argument("--source-only", action="store_true")
    eight_mode.add_argument("--publish-current", action="store_true")
    generation_start = commands.add_parser("start-eight32-generation",
        help="Register one explicitly independent generation from the latest successful real eight-type publication")
    generation_start.add_argument("--run-dir", type=Path, required=True)
    generation_start.add_argument("--generation-id", required=True)
    generation_advance = commands.add_parser("advance-eight32-generation",
        help="Consume only this generation's contiguous actual-publication prefix with the existing Engine")
    generation_advance.add_argument("--run-dir", type=Path, required=True)
    generation_advance.add_argument("--day", required=True)
    recover = commands.add_parser("recover-step")
    recover.add_argument("--run-dir", type=Path, required=True)
    recover.add_argument("--day", required=True)
    recover.add_argument("--step", required=True)
    recover_route = commands.add_parser("recover-route")
    recover_route.add_argument("--run-dir", type=Path, required=True)
    recover_route.add_argument("--day", required=True)
    recover_route.add_argument("--route", required=True, choices=(
        "dated_catchup", "residual_exit", "known_identity", "entry_comparison", "public_seat", "leader_type_router", "router_repair"))
    attach = commands.add_parser("attach-dated-catchup")
    attach.add_argument("--run-dir", type=Path, required=True)
    residual_attach = commands.add_parser("attach-residual-exit")
    residual_attach.add_argument("--run-dir", type=Path, required=True)
    known_attach = commands.add_parser("attach-known-identity")
    known_attach.add_argument("--run-dir", type=Path, required=True)
    entry_attach = commands.add_parser("attach-entry-comparison")
    entry_attach.add_argument("--run-dir", type=Path, required=True)
    seat_attach = commands.add_parser("attach-public-seat")
    seat_attach.add_argument("--run-dir", type=Path, required=True)
    router_attach = commands.add_parser("attach-leader-type-router")
    router_attach.add_argument("--run-dir", type=Path, required=True)
    router_attach.add_argument("--registration", type=Path)
    router_attach.add_argument("--forward-run-dir", type=Path)
    repair_adopt = commands.add_parser("adopt-router-repair")
    for name in ("run-dir", "registration", "publication", "panel", "consumer-observation"):
        repair_adopt.add_argument("--" + name, type=Path, required=True)
    lowvol_adopt = commands.add_parser("adopt-lowvol-bootstrap")
    for name in ("run-dir", "publication", "forward-run-dir", "source-dir", "recent-action-extension"):
        lowvol_adopt.add_argument("--" + name, type=Path, required=True)
    lowvol_adopt.add_argument("--route-id", default="native_lowvol_bootstrap_20261019")
    args = parser.parse_args(argv)
    if args.action == "register":
        result = register(args.run_dir, args.opening_run_dir, args.lowvol_publication,
            args.lowvol_run_dir, args.lowvol_source_dir, action_event_view=args.action_event_view,
            action_reference_view=args.action_reference_view)
    elif args.action == "recover-step":
        result = recover_step(args.run_dir, args.day, args.step)
    elif args.action == "settle-native":
        result = settle_native(args.run_dir, args.day, args.package, policy=args.policy)
    elif args.action == "advance-native":
        result = advance_native(args.run_dir, args.day, args.package, source_only=args.source_only)
    elif args.action == "advance-leader20":
        from .leader_close_daily import advance
        result = advance(args.run_dir, args.day, args.package, source_only=args.source_only)
    elif args.action == "advance-eight32":
        from .leader_eight_type_daily import advance
        result = advance(args.run_dir, args.day, args.package, source_only=args.source_only)
    elif args.action == "start-eight32-generation":
        from .eight32_paper_generation import start
        result = start(args.run_dir, args.generation_id)
    elif args.action == "advance-eight32-generation":
        from .eight32_paper_generation import advance
        result = advance(args.run_dir, args.day)
    elif args.action == "recover-route":
        from .system_daily_route_recovery import recover_route
        result = recover_route(args.run_dir, args.day, args.route)
    elif args.action == "attach-dated-catchup":
        result = attach_dated_catchup(args.run_dir)
    elif args.action == "attach-residual-exit":
        result = attach_residual_exit(args.run_dir)
    elif args.action == "attach-known-identity":
        result = attach_known_identity(args.run_dir)
    elif args.action == "attach-entry-comparison":
        result = attach_entry_comparison(args.run_dir)
    elif args.action == "attach-public-seat":
        result = attach_public_seat(args.run_dir)
    elif args.action == "attach-leader-type-router":
        result = attach_leader_type_router(args.run_dir, registration=args.registration,
            forward_run_dir=args.forward_run_dir)
    elif args.action == "adopt-router-repair":
        result = adopt_router_repair(args.run_dir, registration=args.registration, publication=args.publication,
            panel=args.panel, consumer_observation=args.consumer_observation)
    elif args.action == "adopt-lowvol-bootstrap":
        result = adopt_lowvol_bootstrap(args.run_dir, publication=args.publication, forward_run_dir=args.forward_run_dir,
            source_directory=args.source_dir, recent_action_extension=args.recent_action_extension, route_id=args.route_id)
    else:
        if (args.action_reuse_root or args.action_review or args.action_prior_bundle or args.action_wheel
                ) and not args.refresh_recent_actions:
            parser.error("Action producer options require --refresh-recent-actions")
        refresh_options = {"reuse_roots": args.action_reuse_root, "reviews": args.action_review,
            "prior_bundle": args.action_prior_bundle, "wheel": args.action_wheel} if args.refresh_recent_actions else None
        result = run(args.run_dir, args.day, args.package, action_event_view=args.action_event_view,
            action_reference_view=args.action_reference_view, saved_lowvol_run=args.saved_lowvol_run,
            recent_action_extension=args.recent_action_extension, action_refresh=refresh_options,
            saved_action_refresh=args.saved_action_refresh, seat_catalog=args.seat_catalog,
            refresh_seat_disclosures=args.refresh_seat_disclosures, native_paper_closes=args.native_paper_close)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") in {
        "partial_research_routes", "partial_native_settlement_no_automatic_retry", "partial_native_only_production_no_retry", "partial_leader20_production_no_retry", "unknown_no_successful_child_terminal",
        "recovered_saved_route_partial", "unknown_saved_route_publication_incomplete",
        "unknown_interrupted_route_projection_no_retry"} else 0
