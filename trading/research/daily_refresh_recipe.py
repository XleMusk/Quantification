"""Pure next-session recipes for the existing twenty/eight-type/Alpha paths.

This module reads successful command receipts and the fixed calendar only.
It never runs a command, creates an output, advances a ledger, or republishes.
The caller may adopt ``next_profile`` only after every planned step succeeds.
"""
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

from ..settings import ROOT
from .leader_daily_signals import _calendar_view


SCHEMA = "saved_daily_refresh_recipe_profile_v1"
PLAN_SCHEMA = "saved_daily_refresh_recipe_plan_v1"
RECEIPT_ROOT = ROOT / "outputs/daily_refresh/20261016_v1"
RECEIPTS = {
    "extras": ("leader/01_extras_receipt.json", "baostock-bulk-extras"),
    "pub20": ("leader/02_publication20_receipt.json", "leader-close-publication"),
    "forward20": ("leader/10_forward20_receipt.json", "leader-published-forward"),
    "preview": ("leader/03_preview_receipt.json", "leader-recent-source-preview"),
    "environment": ("leader/04_environment_receipt.json", "leader-environment"),
    "observe": ("leader/05_identity_observe_receipt.json", "leader-identity"),
    "episodes": ("leader/06_identity_episodes_receipt.json", "leader-identity-episodes"),
    "eight_inputs": ("leader/07_eight_inputs_receipt.json", "leader-eight-type-inputs"),
    "paper32": ("leader/08_eight_publication_receipt.json", "leader-eight-type-paper"),
    "forward32": ("leader/09_forward32_receipt.json", "leader-eight-type-forward"),
    "alpha_provider": ("alpha_lowvol/03_alpha_provider16.receipt.json", "recent-source-qlib-provider"),
    "alpha_score": ("alpha_lowvol/04_alpha_score16.receipt.json", "recent-source-alpha158"),
    "alpha_publication": ("alpha_lowvol/05_alpha_publication16.receipt.json", "recent-source-alpha158-target-publication"),
    "alpha_forward": ("alpha_lowvol/06_alpha_forward.receipt.json", "recent-source-alpha158-forward-run"),
}
REQUIRES = {"extras": [], "pub20": [], "forward20": ["pub20"], "preview": [],
    "environment": ["preview", "extras"], "observe": ["environment"], "episodes": ["observe"],
    "eight_inputs": ["observe", "preview", "extras"], "paper32": ["eight_inputs"],
    "forward32": ["paper32"], "alpha_provider": [], "alpha_score": ["alpha_provider"],
    "alpha_publication": ["alpha_score"], "alpha_forward": ["alpha_publication"]}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "commands_executed": False,
    "ledger_mutated": False, "model_refit": False, "source_acquired": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("An exact ISO session date is required")
    return value


def _values(command, flag):
    result = []
    for i, token in enumerate(command):
        if token == flag:
            if i + 1 >= len(command) or command[i + 1].startswith("--"):
                raise ValueError("Incomplete saved option: " + flag)
            result.append(command[i + 1])
    return result


def _one(command, flag):
    values = _values(command, flag)
    if len(values) != 1:
        raise ValueError("One saved option is required: " + flag)
    return values[0]


def _set(command, flag, values):
    """Replace only explicit options; never rewrite dates inside other paths."""
    if flag not in command:
        raise ValueError("Original command did not declare " + flag)
    values = [str(v) for v in values]
    rebuilt, inserted, index = [], False, 0
    while index < len(command):
        if command[index] == flag:
            if not inserted:
                rebuilt.extend(item for value in values for item in (flag, value))
                inserted = True
            index += 2
        else:
            rebuilt.append(command[index])
            index += 1
    return rebuilt


def _sessions(reference):
    if _ref(reference["path"]) != reference:
        raise ValueError("The recipe's fixed calendar changed")
    payload = _read(reference["path"])
    year = payload["year"]
    days, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("The original fixed exchange calendar is required")
    return days


def _outputs(commands, source_days):
    paths = {}
    for step, command in commands.items():
        if step in ("forward20", "forward32", "alpha_forward"):
            paths[step] = {"head": str(Path(_one(command, "--run-dir")) / "head.json")}
        elif step == "observe":
            folder = Path(_one(command, "--registry")).parent / "observations" / source_days[-1].replace("-", "")
            paths[step] = {"observation": str(folder / "observation.json"),
                "receipt": str(folder / "observation_receipt.json")}
        elif step == "episodes":
            paths[step] = {"history": _one(command, "--out")}
        else:
            folder = Path(_one(command, "--out"))
            names = {"pub20": {"publication": "publication.json", "bundle": "input_bundle/bundle.json"},
                "eight_inputs": {"manifest": "manifest.json", "raw": "raw_daily.parquet"},
                "paper32": {"publication": "paper_plan.json"},
                "alpha_score": {"result": "result.json", "scores": "dated_scores.parquet"}}.get(step, {"result": "result.json"})
            paths[step] = {key: str(folder / value) for key, value in names.items()}
            if step == "preview":
                paths[step]["current_preview"] = str(folder / source_days[-1].replace("-", ""))
    return paths


def bootstrap_profile():
    """Read the one successful 16-Oct recipe; do not inspect or run its data."""
    commands, receipts = {}, {}
    for step, (relative, expected) in RECEIPTS.items():
        path = RECEIPT_ROOT / relative
        record = _read(path)
        terminal = record.get("exit_code", record.get("returncode"))
        ended = record.get("completed_at", record.get("finished_at"))
        if (type(terminal) is not int or terminal != 0 or not ended
                or datetime.fromisoformat(ended) < datetime.fromisoformat(record["started_at"])
                or record.get("execution_admitted") is not False):
            raise ValueError("Recipe bootstrap requires successful terminal receipts: " + step)
        argv = record["argv"]
        positions = [i for i, value in enumerate(argv) if Path(value).name == "run_committee.py"]
        if len(positions) != 1:
            raise ValueError("Receipt must name the existing project command entrypoint")
        command = argv[positions[0] + 1:]
        if not command or command[0] != expected or step == "observe" and command[1:2] != ["observe"]:
            raise ValueError("Unexpected saved recipe command: " + step)
        commands[step], receipts[step] = command, _ref(path)
    last = _day(_one(commands["alpha_score"], "--end"))
    if last != "2026-10-16" or _one(commands["alpha_score"], "--start") != last:
        raise ValueError("Bootstrap must retain the actual single-day 16-Oct generation")
    source_days = [datetime.strptime(Path(p).name, "%Y%m%d").date().isoformat()
        for p in _values(commands["environment"], "--preview")]
    calendar = _ref(_one(commands["pub20"], "--calendar"))
    sessions = _sessions(calendar)
    if source_days != [d for d in sessions if source_days[0] <= d <= last]:
        raise ValueError("Saved preview dates are not the original complete source prefix")
    for step in ("forward20", "forward32", "alpha_forward", "episodes"):
        if _one(commands[step], "--through") != last:
            raise ValueError("Successful source/account cursors must agree")
    return {"schema": SCHEMA, "last_source_day": last, "source_days": source_days,
        "calendar": calendar, "templates": commands, "output_paths": _outputs(commands, source_days),
        "bootstrap_receipts": receipts,
        "legacy_fixed_engineering": {"stopped": True, "route_id": "fixed_lowvol",
            "reason": "legacy_engineering_account_not_a_native_lowvol_migration"}, **FLAGS}


def next_plan(profile, day, package, out):
    """Return deterministic commands for one exact next session, without writes."""
    if (profile.get("schema") != SCHEMA or set(profile.get("templates", {})) != set(RECEIPTS)
            or any(profile.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Use an unchanged saved research-only recipe profile")
    day, last = _day(day), _day(profile["last_source_day"])
    sessions = _sessions(profile["calendar"])
    if (last not in sessions or sessions.index(last) + 1 >= len(sessions)
            or sessions[sessions.index(last) + 1] != day
            or sessions.index(day) + 1 >= len(sessions)):
        raise ValueError("Only the accurate next fixed-calendar session can be planned")
    commands = deepcopy(profile["templates"])
    for step, (_, expected) in RECEIPTS.items():
        if commands[step][0] != expected:
            raise ValueError("A saved recipe cannot change its strategy command")
    package, out = Path(package).resolve(), Path(out).resolve()
    if out == package or out in package.parents or package in out.parents:
        raise ValueError("Operation artifacts must remain separate from the source package")
    source_days = profile["source_days"] + [day]
    if profile["source_days"] != [d for d in sessions if profile["source_days"][0] <= d <= last]:
        raise ValueError("The original dated source prefix cannot skip or reset")
    folders = {step: out / "artifacts" / step for step in RECEIPTS}
    prior = profile["templates"]
    for step in ("pub20", "preview", "alpha_provider"):
        packages = _values(prior[step], "--package")
        if str(package) in packages:
            raise ValueError("The new exact-day package cannot already be in the saved prefix")
        commands[step] = _set(commands[step], "--package", packages + [str(package)])
    commands["extras"] = _set(commands["extras"], "--package", [package])
    for step in ("extras", "pub20", "preview", "environment", "eight_inputs", "paper32",
                 "alpha_provider", "alpha_score", "alpha_publication"):
        commands[step] = _set(commands[step], "--out", [folders[step]])
    commands["pub20"] = _set(commands["pub20"], "--decision-day", [day])
    current_preview = folders["preview"] / day.replace("-", "")
    commands["environment"] = _set(commands["environment"], "--preview",
        [folders["preview"] / d.replace("-", "") for d in source_days])
    commands["environment"] = _set(commands["environment"], "--bulk-extras", [folders["extras"] / "result.json"])
    commands["observe"] = _set(commands["observe"], "--analysis", [folders["environment"] / "result.json"])
    commands["episodes"] = _set(commands["episodes"], "--through", [day])
    commands["episodes"] = _set(commands["episodes"], "--out", [folders["episodes"] / "history.json"])
    for flag, values in (("--append-from", [Path(_one(prior["eight_inputs"], "--out")) / "manifest.json"]),
            ("--preview", [current_preview]), ("--turnover-source", [folders["extras"] / "result.json"])):
        commands["eight_inputs"] = _set(commands["eight_inputs"], flag, values)
    commands["paper32"] = _set(commands["paper32"], "--panel", [folders["eight_inputs"] / "manifest.json"])
    commands["forward32"] = _set(commands["forward32"], "--panel", [folders["eight_inputs"] / "manifest.json"])
    for step, pub_path in (("forward20", folders["pub20"] / "publication.json"),
            ("forward32", folders["paper32"] / "paper_plan.json"),
            ("alpha_forward", folders["alpha_publication"] / "result.json")):
        commands[step] = _set(commands[step], "--publication", _values(prior[step], "--publication") + [pub_path])
        commands[step] = _set(commands[step], "--through", [day])
    bundle = folders["pub20"] / "input_bundle/bundle.json"
    commands["forward20"] = _set(commands["forward20"], "--publication-bundle",
        _values(prior["forward20"], "--publication-bundle") + [bundle])
    commands["forward20"] = _set(commands["forward20"], "--bundle", [bundle])
    for step in ("alpha_score", "alpha_forward"):
        commands[step] = _set(commands[step], "--provider-view", [folders["alpha_provider"]])
    for flag in ("--start", "--end"):
        commands["alpha_score"] = _set(commands["alpha_score"], flag, [day])
    commands["alpha_publication"] = _set(commands["alpha_publication"], "--prefix-batch-run",
        _values(prior["alpha_publication"], "--prefix-batch-run") + [_one(prior["alpha_publication"], "--batch-run")])
    commands["alpha_publication"] = _set(commands["alpha_publication"], "--batch-run", [folders["alpha_score"] / "result.json"])
    paths = _outputs(commands, source_days)
    updated = {**deepcopy(profile), "last_source_day": day, "source_days": source_days,
        "templates": commands, "output_paths": paths}
    steps = [{"id": step, "command": command, "requires": REQUIRES[step],
        "expected_files": [value for key, value in paths[step].items() if key != "current_preview"],
        "writes_existing_contract": step in ("observe", "forward20", "forward32", "alpha_forward")}
        for step, command in commands.items()]
    return {"schema": PLAN_SCHEMA, "source_day": day,
        "execution_day": sessions[sessions.index(day) + 1], "package": str(package), "out": str(out),
        "steps": steps, "next_profile": updated,
        "next_profile_adoption": "only_after_all_steps_have_actual_successful_terminal_evidence", **FLAGS}
