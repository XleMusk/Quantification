"""Supervise one saved daily command and publish its independent terminal receipt.

This one-shot child can record an actual CLI exit independently of its caller.
It never retries a command; a killed process tree or any other missing terminal
evidence remains unknown.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


REQUEST_SCHEMA = "system_daily_command_request_v1"
START_SCHEMA = "system_daily_command_child_started_v1"
TERMINAL_SCHEMA = "system_daily_command_child_terminal_v1"


def paths(directory, step_id):
    directory = Path(directory)
    return {name: directory / (step_id + suffix) for name, suffix in (
        ("request", ".child_request.json"), ("started", ".child_started.json"),
        ("terminal", ".child_terminal.json"), ("recovery", ".recovery.json"))}


def run_request(request_file, request_sha256):
    from . import system_daily_refresh as daily

    request_file = Path(request_file).resolve()
    reference = daily._ref(request_file)
    request = daily._read(request_file)
    step = daily._step_spec(request["step"])
    files = paths(request_file.parent, step["id"])
    if (reference["sha256"] != request_sha256 or request_file != files["request"]
            or request.get("schema") != REQUEST_SCHEMA or request.get("step") != step
            or request.get("argv") != daily._command(step["command"])
            or request.get("cwd") != str(daily.ROOT)
            or any(request.get(key) is not value for key, value in daily._command_flags(step).items())):
        raise ValueError("The child requires the exact saved daily command request")
    daily._exact_day(request["day"])
    if files["terminal"].exists():
        raise FileExistsError("A command with terminal evidence must never be launched again")
    started = {"schema": START_SCHEMA, "request_ref": reference,
        "supervisor_pid": os.getpid(), "started_at": daily._now(), **daily._command_flags(step)}
    # An exclusive claim prevents even an accidental second supervisor from
    # invoking the same saved CLI again. It is not a stale-process heuristic.
    with files["started"].open("x", encoding="utf-8") as handle:
        import json
        json.dump(started, handle, ensure_ascii=False, indent=2, allow_nan=False)
    terminal = {"schema": TERMINAL_SCHEMA, "request_ref": reference,
        "started_ref": daily._ref(files["started"]), "day": request["day"],
        "step": step, "argv": request["argv"], "supervisor_pid": os.getpid(),
        "started_at": started["started_at"], "execution_progress_unknown": True,
        "output_refs": None, **daily._command_flags(step)}
    try:
        process = subprocess.Popen(request["argv"], cwd=daily.ROOT)
        terminal["command_pid"] = process.pid
        terminal["exit_code"] = process.wait()
        terminal.update(terminal_observed_at=daily._now(), execution_progress_unknown=False)
        if terminal["exit_code"] != 0:
            terminal["status"] = "failed_command_no_automatic_retry"
        else:
            terminal["output_refs"] = daily._outputs(step, request["day"])
            terminal["status"] = "completed"
    except Exception as error:
        terminal.update(status="failed_or_interrupted_no_automatic_retry",
            error=f"{type(error).__name__}: {error}")
    terminal.update(daily._command_network_observation(step))
    terminal["recorded_at"] = daily._now()
    daily._atomic(files["terminal"], terminal)
    return terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args(argv)
    result = run_request(args.request, args.request_sha256)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
