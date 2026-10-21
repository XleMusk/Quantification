"""Explicit eight-type residual-exit method using unchanged saved candidates.

Publish a new method-bound envelope over the original close publication. No
candidate producer, ranking, price acquisition or account writer is added.
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

from ..contracts import MARKET_TZ, PortfolioIntent
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from . import leader_eight_type_paper as original
from . import leader_eight_type_forward_run as shared
from .leader_daily_signals import _calendar_view
from .leader_eight_type_residual_exit import (
    TARGET_POLICY_ID, RISK_POLICY_ID, RULE_SPEC, risk_policy, target_policy,
)


METHOD_ID = "leader_eight_type_residual_full_exit_paper_v1"
SCHEMA = "leader_eight_type_residual_close_publication_v1"
REGISTRATION_SCHEMA = "leader_eight_type_residual_method_registration_v1"
CHECKPOINT_SCHEMA = "leader_eight_type_residual_shared_engine_checkpoint_v1"
RESULT_SCHEMA = "leader_eight_type_residual_shared_engine_result_v1"
ACCOUNT_PREFIX = "leader-eight-type-residual-forward:"
CASE_IDS = shared.CASE_IDS
FLAGS = deepcopy(original.FLAGS)
PROTOCOL = {**deepcopy(original.PROTOCOL), "id": METHOD_ID,
    "combined_risk_policy_id": RISK_POLICY_ID, "target_policy_id": TARGET_POLICY_ID,
    "structural_risk_rules": deepcopy(RULE_SPEC), "execution_lifecycle_changed": True,
    "original_32_method_changed": False, "candidates_and_ranking_changed": False,
    "signal_only_comparison_to_original_accounts": False,
    "account_start_differs_from_original_32": True}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": digest(path)}


def _verify(reference):
    if _ref(reference["file"]) != reference:
        raise ValueError("Registered residual-exit evidence changed")
    return _read(reference["file"])


def _sessions(calendar):
    value = _verify(calendar)
    year = value["year"]
    days, _, status = _calendar_view(value, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("A pinned official exchange calendar is required")
    return days


def register(base_publication, calendar, first_day, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use one new explicit method registration directory")
    now = _now()
    from .leader_eight_type_paper_view import read_saved_plan
    saved = read_saved_plan(base_publication, now=now)
    base = _read(base_publication)
    cal = _ref(calendar)
    days = _sessions(cal)
    anchor_day = saved["source_close_day"]
    if (saved["status"] != "saved_paper_plan" or anchor_day not in days[:-2]
            or first_day != days[days.index(anchor_day) + 1]
            or now >= original._stamp(first_day + "T15:00:00+08:00")):
        raise ValueError("Register before the exact first new decision close; never roll a missed date")
    panel = _ref(base["source"]["input_manifest"])
    if panel["sha256"] != base["source"]["input_manifest_sha256"]:
        raise ValueError("Original candidate input changed")
    record = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID,
        "registered_at": now.isoformat(), "calendar": cal,
        "first_decision_day": first_day, "first_execution_day": days[days.index(first_day) + 1],
        "anchor_source_day": anchor_day, "anchor_base_publication": _ref(base_publication),
        "anchor_input": panel, "case_ids": list(CASE_IDS),
        "config": shared._config(100000, 5).to_dict(), "protocol": deepcopy(PROTOCOL),
        "new_shared_runner_accounts_only": True, "original_accounts_migrated": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", record)
    return record


def _registered(path, now):
    record = _read(path)
    days = _sessions(record["calendar"])
    _verify(record["anchor_base_publication"])
    _verify(record["anchor_input"])
    first, anchor = record["first_decision_day"], record["anchor_source_day"]
    clock = original._stamp(record["registered_at"])
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("protocol") != PROTOCOL or record.get("case_ids") != list(CASE_IDS)
            or record.get("config") != shared._config(100000, 5).to_dict()
            or anchor not in days[:-2] or first != days[days.index(anchor) + 1]
            or record["first_execution_day"] != days[days.index(first) + 1]
            or not clock <= now or clock >= original._stamp(first + "T15:00:00+08:00")
            or record.get("new_shared_runner_accounts_only") is not True
            or record.get("original_accounts_migrated") is not False
            or any(record.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Original explicit eight-type residual method registration changed")
    return record, _ref(path)


def _envelope(base_publication, panel, registration_file, stamp):
    from .leader_eight_type_paper_view import read_saved_plan
    reg, reg_ref = _registered(registration_file, stamp)
    base_ref, base = _ref(base_publication), _read(base_publication)
    saved = read_saved_plan(base_publication, now=stamp)
    day, execution = saved["source_close_day"], saved["proposed_execution_day"]
    if (saved["status"] != "saved_paper_plan" or day < reg["first_decision_day"]
            or stamp.date().isoformat() != day
            or stamp <= original._stamp(saved["recorded_at"])
            or stamp >= original._stamp(execution + "T09:30:00+08:00")
            or Path(panel).resolve() != Path(base["source"]["input_manifest"]).resolve()
            or digest(panel) != base["source"]["input_manifest_sha256"]):
        raise ValueError("A same-day real original publication and its exact input are required")
    return {"schema": SCHEMA, "method_id": METHOD_ID, "source_close_day": day,
        "proposed_execution_day": execution, "recorded_at": stamp.isoformat(),
        "publication_at": stamp.isoformat(), "base_publication": base_ref,
        "paper_registration": reg_ref, "input_manifest": _ref(panel),
        "source": deepcopy(base["source"]), "cases": _project_cases(base["cases"], stamp),
        "case_count": len(CASE_IDS), "execution_day_bound": execution,
        "next_open_deadline": execution + "T09:30:00+08:00",
        "risk_policy_id": RISK_POLICY_ID, "risk_policy_spec": deepcopy(RULE_SPEC),
        "protocol": deepcopy(PROTOCOL), "case_ids": list(CASE_IDS),
        "candidates_recomputed": False, "publication_time_backfilled": False, **FLAGS}


def _project_cases(cases, stamp):
    projected = deepcopy(cases)
    for row in projected:
        intent = PortfolioIntent.from_dict(row["portfolio_intent"])
        row["portfolio_intent"] = replace(intent, decided_at=stamp.isoformat(),
            source="research:leader_eight_type_residual:" + row["case_id"]).to_dict()
    return projected


def publish(base_publication, panel, registration_file, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Publish into a new output directory")
    stamp = _now()
    record = _envelope(base_publication, panel, registration_file, stamp)
    finished = _now()
    if finished < stamp or finished.date() != stamp.date():
        raise ValueError("Actual publication clock crossed its own close day")
    record.update(recorded_at=finished.isoformat(), publication_at=finished.isoformat())
    record["cases"] = _project_cases(record["cases"], finished)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "paper_plan.json", record)
    return record


def load_publication(path, ipo, master, now, cache):
    record, proof = _read(path), _ref(path)
    if record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID:
        raise ValueError("A saved residual-method close envelope is required")
    _verify(record["base_publication"])
    stamp = original._stamp(record["recorded_at"])
    if stamp > now or record != _envelope(record["base_publication"]["file"],
            record["input_manifest"]["file"], record["paper_registration"]["file"], stamp):
        raise ValueError("Residual publication clocks, original signal or method changed")
    loaded = shared._publication(record["base_publication"]["file"], ipo, master, now, cache)
    if loaded.get("unbindable"):
        raise ValueError("A late original signal cannot become a new executable publication")
    reg, _ = _registered(record["paper_registration"]["file"], now)
    anchor_path = str(Path(reg["anchor_input"]["file"]).resolve())
    if anchor_path not in cache:
        cache[anchor_path] = shared.load_inputs(anchor_path, ipo, master)
    _anchor_prefix(cache[anchor_path], loaded["loaded"])
    virtual = deepcopy(loaded["record"])
    virtual.update(schema=SCHEMA, protocol=deepcopy(PROTOCOL), recorded_at=record["recorded_at"],
        publication_at=record["recorded_at"], paper_registration=record["paper_registration"],
        risk_policy_id=RISK_POLICY_ID, risk_policy_spec=deepcopy(RULE_SPEC))
    intents = {key: replace(intent, decided_at=record["recorded_at"],
        source="research:leader_eight_type_residual:" + key) for key, intent in loaded["intents"].items()}
    for row in virtual["cases"]:
        row["portfolio_intent"] = intents[row["case_id"]].to_dict()
    return {**loaded, "record": virtual, "intents": intents,
        "reference": {**loaded["reference"], **proof, "recorded_at": record["recorded_at"]}}


def _anchor_prefix(anchor, latest):
    """The original fixed-capacity history must survive the new method's start."""
    shared._same_lineage(anchor, latest)
    for day, panel in anchor["panel_by_day"].items():
        if (day not in latest["panel_by_day"]
                or shared._facts(panel) != shared._facts(latest["panel_by_day"][day])):
            raise ValueError("Registered original candidate/risk/identity prefix changed")


def registration(path, first, config, now):
    reg, proof = _registered(path, now)
    if (first["day"] != reg["first_decision_day"] or first["execution"] != reg["first_execution_day"]
            or first["record"]["paper_registration"] != proof or config.to_dict() != reg["config"]
            or first["loaded"]["calendar_payload"] != _verify(reg["calendar"])):
        raise ValueError("The first actual publication must bind the original registered D/E and capital")
    return {**proof, "method_id": METHOD_ID, "registered_at": reg["registered_at"],
        "first_decision_day": reg["first_decision_day"], "first_execution_day": reg["first_execution_day"]}


def forward(publications, panel, ipo, master, run_dir, *, registration_file, through=None):
    import sys
    reg, proof = _registered(registration_file, _now())
    result = shared.run(publications, panel, ipo, master, run_dir,
        registration=registration_file, through=through, method=sys.modules[__name__])
    return {**result, "signal_module": METHOD_ID, "paper_registration": proof,
        "first_decision_day": reg["first_decision_day"], "first_execution_day": reg["first_execution_day"],
        "original_accounts_migrated": False, "separate_trading_framework_created": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    for name in ("base-publication", "calendar", "out"):
        reg.add_argument("--" + name, type=Path, required=True)
    reg.add_argument("--first-day", required=True)
    pub = sub.add_parser("publish")
    for name in ("base-publication", "panel", "registration", "out"):
        pub.add_argument("--" + name, type=Path, required=True)
    pub.add_argument("--ipo-evidence", type=Path)
    pub.add_argument("--research-master-run", type=Path)
    run = sub.add_parser("forward")
    run.add_argument("--publication", type=Path, action="append", default=[])
    for name in ("panel", "ipo-evidence", "research-master-run", "registration", "run-dir"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--through")
    run.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    if args.command == "register":
        value = register(args.base_publication, args.calendar, args.first_day, args.out)
    elif args.command == "publish":
        value = publish(args.base_publication, args.panel, args.registration, args.out)
    else:
        from .leader_dated_catchup_forward import _result_export_path
        export = _result_export_path(args.result_json, publications=args.publication, panel=args.panel,
            ipo_evidence=args.ipo_evidence, research_master_run=args.research_master_run,
            registration=args.registration, run_dir=args.run_dir)
        value = forward(args.publication, args.panel, args.ipo_evidence, args.research_master_run,
            args.run_dir, registration_file=args.registration, through=args.through)
        if export is not None:
            export.parent.mkdir(parents=True, exist_ok=True)
            with export.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0
