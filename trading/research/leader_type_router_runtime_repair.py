"""Explicit new runtime generation for the failed 19 October router route.

This is a clean-process CLI adapter, not an exception to the original loader.
The old registration and exit-one evidence remain unchanged. Only the already
identified engine-file drift is admitted, under a new registration/run identity
and the actual after-close software-registration clock. The frozen selector,
publisher and shared Engine/Ledger consumer are reused without editing them.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from ..contracts import MARKET_TZ
from ..settings import DATA_ROOT
from . import leader_type_router_paper as paper
from . import system_daily_command_child as child


SCHEMA = "leader_type_router_after_close_runtime_repair_registration_v1"
REPAIR_METHOD = "original_frozen_router_explicit_after_close_runtime_generation_v1"
TARGETS = {
    "registration": {"path": str(DATA_ROOT / "research/leader_type_router_paper/20261019_v1_registration/registration.json"),
        "sha256": "5738e70436fb670f5a746c0346e179136749ca7f209eaab172274271e4884022"},
    "terminal": {"path": str(DATA_ROOT / "research/system_daily/20261017_v1/operations/20261019/commands/leader_type_router_publication.child_terminal.json"),
        "sha256": "820abdf5ce6e5202660fb169eb5a4fe247ebc0f32ff37b55dceaa35f388398de"},
    "old_engine_sha256": "ec73354177c1f33c9dd7e5639a1b5438689d5f6de70f2f1178fb6ce4a0bf3e2a",
    "new_engine_sha256": "eb93767a80d9621c06dd7880d6415b281b43e14145027f5d49ea3aa3eb74526a",
}
POLICY = {"method": REPAIR_METHOD, "strategy_parameters_changed": False,
    "old_registration_relaxed": False, "old_failure_recovered": False,
    "registration_timing": "actual_same_D_after_close_before_new_publication_and_first_E_open",
    "initial_signal": "same_original_D_inputs_and_base_publication_new_identity_no_backfill",
    "execution": "unchanged_shared_Engine_Ledger_no_future_E_bar_required_for_D_queue"}


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Repair clocks require an explicit timezone")
    return stamp


def _verify(reference):
    if paper._pin(reference["path"]) != reference:
        raise ValueError("Exact original repair evidence changed: " + reference["path"])
    return paper._read(reference["path"])


def _normal(reference):
    return {"path": str(Path(reference.get("path") or reference["file"]).resolve()),
        "sha256": reference["sha256"]}


def _separate(out, roots):
    out = Path(out).resolve()
    for root in roots:
        root = Path(root).resolve()
        if out == root or out in root.parents or root in out.parents:
            raise ValueError("New repair output/run must remain separate from original evidence and accounts")


def _origin(original_registration, failure_terminal, *, verify_sources=False):
    """Validate this one observed failure, never a generic stale-code escape."""
    original_ref, terminal_ref = paper._pin(original_registration), paper._pin(failure_terminal)
    if original_ref != TARGETS["registration"] or terminal_ref != TARGETS["terminal"]:
        raise ValueError("Only the exact observed original registration and exit-one terminal are supported")
    original, terminal = _verify(original_ref), _verify(terminal_ref)
    if (original.get("schema") != paper.REGISTRATION_SCHEMA or original.get("method_id") != paper.METHOD_ID
            or original.get("spec") != paper._spec()
            or original.get("case_ids") != list(paper.CASE_IDS)
            or original.get("config") != paper._config(100000, 5).to_dict()
            or any(original.get(k) is not v for k, v in paper.FLAGS.items())):
        raise ValueError("The original frozen strategy definition/configuration cannot change")
    day, execution = original["first_decision_day"], original["first_execution_day"]
    if (day != "2026-10-19" or execution != "2026-10-20"
            or not _stamp(original["registered_at"]) < _stamp(day + "T15:00:00+08:00")):
        raise ValueError("This repair cannot roll the original strategy's registered D/E")
    runtime = paper._runtime()
    old = {r["path"]: r for r in original["code"]}
    new = {r["path"]: r for r in runtime}
    drift = [path for path in old if old[path] != new.get(path)]
    if (set(old) != set(new) or len(drift) != 1 or Path(drift[0]).name != "engine.py"
            or old[drift[0]]["sha256"] != TARGETS["old_engine_sha256"]
            or new[drift[0]]["sha256"] != TARGETS["new_engine_sha256"]):
        raise ValueError("Only the one previously observed engine-file drift is supported")
    request = _verify(_normal(terminal["request_ref"]))
    started = _verify(_normal(terminal["started_ref"]))
    terminal_path = Path(failure_terminal).resolve()
    directory = terminal_path.parent
    parent_file = directory / "leader_type_router_publication.json"
    parent = paper._read(parent_file)
    plan_file = directory.parent / "leader_type_router/plan.json"
    plan = paper._read(plan_file)
    steps = [s for s in plan["steps"] if s["id"] == "leader_type_router_publication"]
    if len(steps) != 1:
        raise ValueError("Original failed publication step must be unique")
    from .system_daily_refresh import _command, _step_spec, FLAGS as DAILY_FLAGS
    step = _step_spec(steps[0])
    expected_binding = {"kind": "leader_type_router", "registration": request.get("binding", {}).get("registration"),
        "attachment": request.get("binding", {}).get("attachment"),
        "source": request.get("binding", {}).get("source"),
        "plan": {"file": str(plan_file), "sha256": paper.digest(plan_file)}}
    if (plan.get("source_day") != day or plan.get("execution_day") != execution
            or _normal(plan["before_profile"]["registration"]) != original_ref
            or request.get("schema") != child.REQUEST_SCHEMA or request.get("day") != day
            or request.get("step") != step or request.get("argv") != _command(step["command"])
            or request.get("binding") != expected_binding
            or expected_binding["source"].get("day") != day
            or terminal.get("schema") != child.TERMINAL_SCHEMA or terminal.get("day") != day
            or terminal.get("step") != step or terminal.get("argv") != request["argv"]
            or terminal.get("status") != "failed_command_no_automatic_retry" or terminal.get("exit_code") != 1
            or terminal.get("execution_progress_unknown") is not False or terminal.get("output_refs") is not None
            or started.get("schema") != child.START_SCHEMA or started.get("request_ref") != terminal["request_ref"]
            or started.get("supervisor_pid") != terminal.get("supervisor_pid")
            or parent.get("status") != "failed_no_automatic_retry" or parent.get("exit_code") != 1
            or parent.get("child_terminal_ref") != {"file": terminal_ref["path"], "sha256": terminal_ref["sha256"]}
            or parent.get("child_request_ref") != terminal["request_ref"]
            or any(terminal.get(k) is not v for k, v in DAILY_FLAGS.items())):
        raise ValueError("Original failure must retain its exact request, claim, terminal and unchanged plan")
    source = {"inputs": _normal(plan["source_refs"][0]), "base_publication": _normal(plan["source_refs"][1])}
    command = step["command"]
    for flag, expected in (("--registration", original_ref["path"]),
            ("--inputs", source["inputs"]["path"]), ("--base-publication", source["base_publication"]["path"])):
        values = [command[i + 1] for i, token in enumerate(command[:-1]) if token == flag]
        if values != [expected]:
            raise ValueError("Original failed command source identity changed")
    stderr = directory / "leader_type_router_publication.stderr.txt"
    if "Original daily router registration, method/code and actual clock required" not in stderr.read_text(encoding="utf-8"):
        raise ValueError("The saved failure does not identify the observed runtime-registration rejection")
    failed_out = str(Path(step["expected_files"][0]).parent)
    original_run = plan["before_profile"]["run_directory"]
    if Path(step["expected_files"][0]).exists() or (Path(original_run) / "head.json").exists():
        raise ValueError("A route with an original publication/account cannot adopt this never-started repair")
    if verify_sources:
        for reference in original["references"].values():
            paper._checked(reference)
    return {"original": original, "runtime": runtime, "source": source,
        "evidence": {"original_registration": original_ref, "failure_terminal": terminal_ref,
            "failure_parent": paper._pin(parent_file), "failure_request": _normal(terminal["request_ref"]),
            "failure_started": _normal(terminal["started_ref"]), "failure_plan": paper._pin(plan_file),
            "failure_stderr": paper._pin(stderr)},
        "failed_out": failed_out, "original_run": original_run,
        "terminal_at": terminal["recorded_at"]}


def _identity(value):
    return REPAIR_METHOD + ":" + paper._hash({k: value[k] for k in
        ("registered_at", "repair_evidence", "code", "repair_run_directory", "repair_adapter")})[:20]


def register(original_registration, failure_terminal, out, run_dir):
    facts = _origin(original_registration, failure_terminal)
    original, now = facts["original"], _now()
    day, execution = original["first_decision_day"], original["first_execution_day"]
    if (now.date().isoformat() != day or not _stamp(day + "T15:05:00+08:00") <= now
            or not _stamp(facts["terminal_at"]) <= now < _stamp(execution + "T09:30:00+08:00")):
        raise ValueError("Repair registration requires its actual original D evening before first E open")
    out, run_dir = Path(out).resolve(), Path(run_dir).resolve()
    roots = [Path(r["path"]).parent for r in facts["evidence"].values()]
    roots += [facts["failed_out"], facts["original_run"], *[Path(r["path"]).parent for r in facts["source"].values()]]
    _separate(out, roots + [run_dir])
    _separate(run_dir, roots + [out])
    if out.exists() or run_dir.exists():
        raise FileExistsError("A repair needs new independent registration and research-run directories")
    result = {**deepcopy(original), "schema": SCHEMA, "registered_at": now.isoformat(),
        "strategy_definition_registered_at": original["registered_at"], "after_close_registration": True,
        "not_original_preregistered_route_success": True, "repair_policy": deepcopy(POLICY),
        "repair_evidence": facts["evidence"], "repair_source": facts["source"],
        "code": facts["runtime"], "repair_adapter": paper._pin(__file__),
        "repair_run_directory": str(run_dir), "original_run_directory": facts["original_run"],
        "original_failed_publication_directory": facts["failed_out"],
        "accounts_created": False, "engine_advanced": False, "orders_generated": False}
    result["repair_identity"] = _identity(result)
    out.mkdir(parents=True, exist_ok=False)
    paper.write_json(out / "registration.json", result)
    return result


def registration(path, *, now=None):
    value = paper._read(path)
    facts = _origin(value["repair_evidence"]["original_registration"]["path"],
        value["repair_evidence"]["failure_terminal"]["path"], verify_sources=True)
    original, now = facts["original"], now or _now()
    stamp = _stamp(value["registered_at"])
    if (value.get("schema") != SCHEMA or value.get("repair_policy") != POLICY
            or value.get("repair_evidence") != facts["evidence"] or value.get("repair_source") != facts["source"]
            or value.get("strategy_definition_registered_at") != original["registered_at"]
            or value.get("after_close_registration") is not True
            or value.get("not_original_preregistered_route_success") is not True
            or value.get("code") != facts["runtime"] or value.get("repair_adapter") != paper._pin(__file__)
            or value.get("repair_identity") != _identity(value)
            or value.get("original_run_directory") != facts["original_run"]
            or value.get("original_failed_publication_directory") != facts["failed_out"]
            or stamp.date().isoformat() != original["first_decision_day"]
            or not _stamp(facts["terminal_at"]) <= stamp <= now
            or not _stamp(original["first_decision_day"] + "T15:05:00+08:00") <= stamp
                < _stamp(original["first_execution_day"] + "T09:30:00+08:00")):
        raise ValueError("New repair identity, actual clock or exact runtime/evidence changed")
    replaced = {"schema", "registered_at", "code"}
    if any(value.get(k) != v for k, v in original.items() if k not in replaced):
        raise ValueError("The repair cannot change any original frozen strategy field")
    _separate(Path(value["repair_run_directory"]), [facts["original_run"], facts["failed_out"], Path(path).parent])
    return value


@contextmanager
def _bound_loader(path, record):
    # Used only inside this explicit clean CLI call; the legacy loader is
    # restored even when publication/Engine validation raises.
    original_loader, expected = paper.registration, Path(path).resolve()
    def load(candidate, *, now=None):
        if Path(candidate).resolve() != expected:
            raise ValueError("A repair call cannot load another registration")
        return deepcopy(record)
    paper.registration = load
    try:
        yield
    finally:
        paper.registration = original_loader


def _marker(record, reference):
    return {"identity": record["repair_identity"], "registration": reference,
        "after_close_registration": True, "registered_at": record["registered_at"],
        "strategy_definition_registered_at": record["strategy_definition_registered_at"],
        "original_D_route_succeeded": False, "original_failure_recovered": False}


def publish(registration_path, inputs, base_publication, out):
    record = registration(registration_path)
    base = paper._read(base_publication)
    if base.get("source_close_day") == record["first_decision_day"]:
        if (paper._pin(inputs) != record["repair_source"]["inputs"]
                or paper._pin(base_publication) != record["repair_source"]["base_publication"]):
            raise ValueError("The new first-D repair must use the exact failed attempt's original source pair")
    _separate(out, [record["original_failed_publication_directory"], record["original_run_directory"],
        record["repair_run_directory"]])
    with _bound_loader(registration_path, record):
        result = paper.publish(registration_path, inputs, base_publication, out)
    result["runtime_repair"] = _marker(record, paper._pin(registration_path))
    recorded = _now()
    if (recorded.date().isoformat() != result["source_close_day"]
            or recorded < _stamp(result["recorded_at"])
            or not _stamp(record["registered_at"]) <= recorded
                < _stamp(result["proposed_execution_day"] + "T09:30:00+08:00")):
        raise ValueError("The new publication cannot backdate or shift its actual repaired generation")
    result.update(recorded_at=recorded.isoformat(), publication_at=recorded.isoformat())
    paper.write_json(Path(out) / "publication.json", result)
    return result


def run(publications, panel, run_dir, *, registration, through=None):
    record = globals()["registration"](registration)
    if Path(run_dir).resolve() != Path(record["repair_run_directory"]).resolve():
        raise ValueError("New repair consumer must use only its separately registered research account")
    marker = _marker(record, paper._pin(registration))
    if not publications or any(paper._read(p).get("runtime_repair") != marker for p in publications):
        raise ValueError("Only this new repaired generation's actual publications may enter its research account")
    from . import leader_type_router_forward as forward
    with _bound_loader(registration, record):
        result = forward.run(publications, panel, run_dir, registration=registration, through=through)
    return {**result, "runtime_repair": marker}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    initial = commands.add_parser("register")
    for name in ("original-registration", "failure-terminal", "out", "run-dir"):
        initial.add_argument("--" + name, type=Path, required=True)
    publication = commands.add_parser("publish")
    for name in ("registration", "inputs", "base-publication", "out"):
        publication.add_argument("--" + name, type=Path, required=True)
    execute = commands.add_parser("forward")
    for name in ("registration", "panel", "run-dir"):
        execute.add_argument("--" + name, type=Path, required=True)
    execute.add_argument("--publication", type=Path, required=True, action="append")
    execute.add_argument("--through")
    execute.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    if args.action == "register":
        result = register(args.original_registration, args.failure_terminal, args.out, args.run_dir)
    elif args.action == "publish":
        result = publish(args.registration, args.inputs, args.base_publication, args.out)
    else:
        if args.result_json:
            target = args.result_json.resolve()
            if target.exists():
                raise FileExistsError("Do not overwrite a saved repair command observation")
            roots = [args.run_dir, args.registration.parent, args.panel.parent, *[p.parent for p in args.publication]]
            _separate(target, roots)
        result = run(args.publication, args.panel, args.run_dir, registration=args.registration, through=args.through)
        if args.result_json:
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            with args.result_json.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
