"""Coordinate one explicitly new eight-type research-paper generation.

The existing registrar and published-close consumer own registration and account
semantics. This wrapper owns only their exact saved command prefix and receipts.
It cannot repair older missing publications or reuse their account cash.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from ..contracts import MARKET_TZ
from . import system_daily_refresh as daily
from . import leader_eight_type_daily as producer
from .leader_eight_type_forward_registration import (GENERATION_METHOD_ID, SPLIT_GENERATION_METHOD_ID,
    SPLIT_SESSION_METHOD_ID, SPLIT_SESSION_OPTION)
from .leader_close_daily import _saved_command

SCHEMA = "eight32_explicit_paper_generation_coordinator_v1"
KEY = "eight32_paper_generation"


def _calendar(registration):
    reference = registration["calendar"]
    if daily._ref(reference["file"]) != reference:
        raise ValueError("The original system calendar changed")
    value = daily._read(reference["file"])
    sessions, previous, status = daily._calendar_view(value, f"{value['year']}-01-01",
        f"{value['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("A fixed exchange calendar is required")
    return sessions, previous


def _checked_ref(reference):
    if daily._ref(reference["file"]) != reference:
        raise ValueError("A saved generation registration/publication/input changed")
    return daily._read(reference["file"])


def _current(head, registration, sessions, now, *, allow_context=False):
    profile = head.get(producer.PROFILE_KEY)
    if profile is None:
        raise ValueError("A successful independent eight-type publication is required first")
    producer._validate(profile, head, registration, sessions, now)
    if not allow_context and profile["source_day"] != profile["publication_source_day"]:
        raise ValueError("Latest source is context only; it cannot start/advance a new publication generation")
    return profile


def _validate_generation(value, head, registration):
    if (value.get("schema") != SCHEMA or value.get("system_registration") != head["registration"]
            or any(value.get(k) is not v for k,v in daily.FLAGS.items())):
        raise ValueError("Generation belongs to another exact system registration")
    registered = _checked_ref(value["registration"])
    split = registered.get("generation_method_id") == SPLIT_GENERATION_METHOD_ID
    expected_method = SPLIT_GENERATION_METHOD_ID if split else GENERATION_METHOD_ID
    if split and (registered.get("session_method_id") != SPLIT_SESSION_METHOD_ID
            or value.get("session_method_id") != SPLIT_SESSION_METHOD_ID):
        raise ValueError("Split generation must retain its explicit session method")
    run = Path(value["run_directory"]).resolve()
    if (Path(registered["generation"]["intended_run_directory"]).resolve() != run
            or registered.get("generation_method_id") != expected_method
            or registered.get("initial_quality_scope") != "per_case"
            or Path(registered.get("registered_run_dir", "")).resolve() != run
            or registered["generation"] != value["generation_binding"]
            or registered["method"] != value["consumer_method"]
            or registered["first_decision_day"] != value["first_decision_day"]
            or registered["first_execution_day"] != value["first_execution_day"]
            or registered["generation"].get("original_generations_unchanged") is not True
            or registered["generation"].get("original_cash_reused") is not False
            or registered["generation"].get("missing_old_publications_repaired") is not False
            or registered["qualification_inputs"] != {"ipo_evidence":value["ipo_evidence"],
                "research_master_run":value["research_master_run"]}):
        raise ValueError("The explicit new generation cannot replace its original method or account directory")
    for ref in [value["ipo_evidence"], value["research_master_run"], value["latest_manifest"],
            *[ref for row in value["publications"] for ref in (row["publication"],row["manifest"])]]:
        _checked_ref(ref)
    for row in value["publications"]:
        published=_checked_ref(row["publication"])
        if (published["source_close_day"] != row["day"]
                or Path(published["source"]["input_manifest"]).resolve() != Path(row["manifest"]["file"]).resolve()
                or published["source"]["input_manifest_sha256"] != row["manifest"]["sha256"]):
            raise ValueError("A generation publication lost its own unchanged input manifest")
    original = registered["original_publication"]
    if ({k:original[k] for k in ("file", "sha256")} != value["publications"][0]["publication"]
            or registered["own_input_manifest"] != value["publications"][0]["manifest"]):
        raise ValueError("The first generation publication/input cannot change")
    sessions, _ = _calendar(registration)
    days = [row["day"] for row in value["publications"]]
    expected = [d for d in sessions if value["first_decision_day"] <= d <= days[-1]]
    if (not days or days != sorted(set(days)) or days[0] != value["first_decision_day"]
            or any(d not in expected for d in days) or (not split and days != expected)):
        raise ValueError("The generation requires its original actual-publication prefix; gaps are explicit only for split sessions")
    return registered


def _operation(directory, generation_id, kind, day=None):
    root = directory / "eight32_generations" / generation_id
    return root / kind if day is None else root / kind / day.replace("-", "")


def _execute(operation, plan, step, day, *, allow_start=True):
    plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
    if plan_file.exists():
        if daily._read(plan_file) != plan:
            raise ValueError("A saved generation operation cannot change its exact command/input prefix")
    elif operation.exists():
        raise ValueError("An unresolved generation operation cannot be replaced")
    else:
        daily._atomic(plan_file, plan)
    receipt = daily._read(receipt_file) if receipt_file.exists() else {
        "schema": SCHEMA, "kind": plan["kind"], "day": day, "plan_ref": daily._ref(plan_file),
        "started_at": daily._now(), "automatic_retry":False, **daily.FLAGS}
    if (receipt.get("plan_ref") != daily._ref(plan_file) or receipt.get("kind") != plan["kind"]
            or any(receipt.get(k) is not v for k,v in daily.FLAGS.items())):
        raise ValueError("An original generation operation receipt changed")
    bound = {**step, "binding":{"kind":"eight32_explicit_new_paper_generation",
        "registration":plan["system_registration"], "plan":daily._ref(plan_file)}}
    allow_start = allow_start and (plan["kind"] != "start" or datetime.now(MARKET_TZ)
        < datetime.fromisoformat(plan["execution_day"] + "T09:30:00+08:00"))
    command = _saved_command(bound, operation, day, allow_start=allow_start)
    if command.get("status") != "completed":
        receipt.update(status="partial_generation_command_no_retry", command_result=command,
            account_state_unknown=True)
        daily._atomic(receipt_file, receipt)
        return receipt
    if receipt.get("status") == "completed_generation_command":
        return receipt
    # The command child owns actual execution evidence. Saving this parent
    # result can be repeated after interruption without invoking the child.
    receipt.update(status="completed_generation_command", command_result=command,
        completed_at=daily._now())
    daily._atomic(receipt_file, receipt)
    return receipt


def start(run_dir, generation_id, *, session_method=None):
    if session_method not in (None, SPLIT_SESSION_OPTION):
        raise ValueError("Only the explicit split-settlement-v1 session method is supported")
    split = session_method == SPLIT_SESSION_OPTION
    if not isinstance(generation_id,str) or not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}",generation_id):
        raise ValueError("Use one explicit simple generation identifier")
    directory = daily.opening_daily._destination(run_dir)
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        sessions, _ = _calendar(registration)
        existing = head.get(KEY)
        if existing is not None:
            existing_split = existing.get("session_method_id") == SPLIT_SESSION_METHOD_ID
            if existing["generation_id"] != generation_id or existing_split != split:
                raise ValueError("An existing explicit generation cannot be replaced or reset")
            _validate_generation(existing, head, registration)
            return {"status":"already_registered_eight32_paper_generation", "generation":existing,
                "command_called":False, "account_created":False, **daily.FLAGS}
        operation = _operation(directory,generation_id,"start")
        plan_file = operation / "plan.json"
        if plan_file.exists():
            plan = daily._read(plan_file)
            if (plan.get("schema") != SCHEMA or plan.get("kind") != "start"
                    or plan.get("generation_id") != generation_id or plan.get("system_registration") != head["registration"]
                    or plan.get("session_method") != session_method
                    or any(plan.get(k) is not v for k,v in daily.FLAGS.items())):
                raise ValueError("Saved start belongs to another exact generation/system")
        else:
            profile = _current(head,registration,sessions,datetime.now(MARKET_TZ))
            day = profile["publication_source_day"]
            publication = profile["latest_publication"]
            manifest = profile["publication_manifest"]
            execution = sessions[sessions.index(day)+1]
            now = datetime.now(MARKET_TZ)
            stamp = datetime.fromisoformat(_checked_ref(publication)["recorded_at"])
            if not stamp <= now < datetime.fromisoformat(execution+"T09:30:00+08:00"):
                raise ValueError("A new generation must register after actual publication and before its exact first open")
            root = daily.DATA_ROOT / "research/leader_eight_type_forward_generations" / generation_id
            out, account = root / "registration.json", root / "books"
            if out.exists() or account.exists():
                raise FileExistsError("The explicit new generation registration/account path already exists")
            plan = {"schema":SCHEMA, "kind":"start", "generation_id":generation_id,
                "system_registration":head["registration"], "day":day, "execution_day":execution,
                "publication":publication, "manifest":manifest,
                "ipo_evidence":daily._ref(producer._one(profile["commands"]["paper32"],"--ipo-evidence")),
                "research_master_run":daily._ref(producer._one(profile["commands"]["paper32"],"--research-master-run")),
                "registration_file":str(out), "run_directory":str(account), **daily.FLAGS}
            if split:
                plan["session_method"] = SPLIT_SESSION_OPTION
        for name in ("publication","manifest","ipo_evidence","research_master_run"):
            _checked_ref(plan[name])
        published=_checked_ref(plan["publication"])
        if (published["source_close_day"] != plan["day"] or plan["day"] not in sessions
                or sessions[sessions.index(plan["day"])+1] != plan["execution_day"]
                or published["source"]["input_manifest_sha256"] != plan["manifest"]["sha256"]
                or Path(published["source"]["input_manifest"]).resolve() != Path(plan["manifest"]["file"]).resolve()):
            raise ValueError("Saved start changed its original own input or exact decision/execution day")
        command = ["leader-eight-type-forward-register"]
        for flag, value in (("publication",plan["publication"]["file"]),("input-manifest",plan["manifest"]["file"]),
                ("ipo-evidence",plan["ipo_evidence"]["file"]),("research-master-run",plan["research_master_run"]["file"]),
                ("out",plan["registration_file"]),("run-dir",plan["run_directory"])):
            command.extend(["--"+flag,value])
        if split:
            command.extend(["--session-method", SPLIT_SESSION_OPTION])
        step = {"id":"register_generation", "command":command,"requires":[],
            "expected_files":[plan["registration_file"]]}
        result = _execute(operation,plan,step,plan["day"])
        if result["status"] != "completed_generation_command":
            return result
        record = daily._read(plan["registration_file"])
        value = {"schema":SCHEMA,"generation_id":generation_id,"system_registration":head["registration"],
            "registration":daily._ref(plan["registration_file"]),"run_directory":plan["run_directory"],
            "generation_binding":record["generation"],"consumer_method":record["method"],
            "first_decision_day":plan["day"],"first_execution_day":plan["execution_day"],
            "ipo_evidence":plan["ipo_evidence"],"research_master_run":plan["research_master_run"],
            "publications":[{"day":plan["day"],"publication":plan["publication"],"manifest":plan["manifest"]}],
            "latest_manifest":plan["manifest"],"last_consumer_day":None,"account_count":0,
            "start_operation":str(operation / "receipt.json"),**daily.FLAGS}
        if split:
            value["session_method_id"] = SPLIT_SESSION_METHOD_ID
        _validate_generation(value,head,registration)
        if Path(value["run_directory"]).exists():
            raise ValueError("Registration must not create an account")
        before = deepcopy(head)
        head[KEY]=value
        assert {k:v for k,v in before.items() if k != KEY} == {k:v for k,v in head.items() if k != KEY}
        daily._atomic(directory / "head.json",head)
        return {"status":"registered_eight32_paper_generation", "generation":value,
            "account_created":False,"old_accounts_advanced":False,**daily.FLAGS}


def advance(run_dir, day):
    daily._exact_day(day)
    directory = daily.opening_daily._destination(run_dir)
    with daily._lock(directory / "runner.lock"):
        head, registration = daily._load(directory)
        value = head.get(KEY)
        if value is None:
            raise ValueError("An explicitly registered new eight-type generation is required")
        _validate_generation(value,head,registration)
        if value.get("session_method_id") == SPLIT_SESSION_METHOD_ID:
            return _advance_split(directory, head, registration, value, day)
        sessions, previous = _calendar(registration)
        if day not in sessions or day < value["first_decision_day"]:
            raise ValueError("An exact current generation source day is required")
        if value["last_consumer_day"] == day:
            _checked_ref(value["account_head"])
            return {"status":"already_saved_eight32_generation_day","command_called":False,**daily.FLAGS}
        operation = _operation(directory,value["generation_id"],"advance",day)
        saved_plan=operation/"plan.json"
        recover_only=saved_plan.exists()
        if recover_only:
            plan=daily._read(saved_plan)
            if (plan.get("schema") != SCHEMA or plan.get("kind") != "advance" or plan.get("day") != day
                    or plan.get("generation_id") != value["generation_id"]
                    or plan.get("system_registration") != head["registration"]
                    or plan.get("parent_generation") != value
                    or any(plan.get(k) is not v for k,v in daily.FLAGS.items())):
                raise ValueError("Saved advance belongs to another original generation/input prefix")
            rows,manifest=plan["publications"],plan["manifest"]
            candidate={**deepcopy(value),"publications":rows,"latest_manifest":manifest}
            _validate_generation(candidate,head,registration)
            if (rows[-1]["day"] != day or rows[:-1] != value["publications"]
                    or rows[-1]["manifest"] != manifest):
                raise ValueError("Saved advance changed its original continuous publication/input prefix")
        else:
            profile = _current(head,registration,sessions,datetime.now(MARKET_TZ))
            if profile["source_day"] != day:
                raise ValueError("Use the latest exact complete source/publication day")
            if day < value["first_execution_day"]:
                return {"status":"awaiting_exact_first_execution_close","required_day":value["first_execution_day"],
                    "command_called":False,"account_created":False,**daily.FLAGS}
            rows = deepcopy(value["publications"])
            if rows[-1]["day"] != previous[day]:
                raise ValueError("Missing generation publication cannot be filled, skipped or rolled")
            rows.append({"day":day,"publication":profile["latest_publication"],"manifest":profile["publication_manifest"]})
            manifest=profile["latest_manifest"]
        command=["leader-eight-type-forward"]
        for row in rows:
            command.extend(["--publication",row["publication"]["file"]])
        for flag,item in (("panel",manifest["file"]),("ipo-evidence",value["ipo_evidence"]["file"]),
                ("research-master-run",value["research_master_run"]["file"]),("run-dir",value["run_directory"]),
                ("registration",value["registration"]["file"]),("through",day)):
            command.extend(["--"+flag,item])
        expected_plan={"schema":SCHEMA,"kind":"advance","day":day,"generation_id":value["generation_id"],
            "system_registration":head["registration"],"parent_generation":deepcopy(value),"publications":rows,
            "manifest":manifest,"command":command,**daily.FLAGS}
        if recover_only and plan != expected_plan:
            raise ValueError("Saved advance changed its original registered command")
        plan=expected_plan
        step={"id":"consume_generation","command":command,"requires":[],
            "expected_files":[str(Path(value["run_directory"])/"head.json")]}
        result=_execute(operation,plan,step,day,allow_start=not recover_only)
        if result["status"] != "completed_generation_command":
            return result
        account_file=Path(value["run_directory"])/"head.json"
        account=daily._read(account_file)
        stdout_file=operation/"commands/consume_generation.stdout.txt"
        observed=daily._read(stdout_file)
        if (observed.get("case_count") != 32 or Path(observed.get("checkpoint", "")).resolve() != account_file
                or observed.get("execution_admitted") is not False):
            raise ValueError("Saved consumer stdout does not describe this exact independent generation")
        case_status=[]
        for key,case in account.get("cases",{}).items():
            current=(case.get("state") or {}).get("day")
            blocked=case.get("blocking_evidence")
            error=case.get("error")
            status="error" if error else "blocked" if blocked else "current" if current == day else "not_current"
            case_status.append({"case_id":key,"state_day":current,"status":status,
                "blocking_evidence":blocked,"error":error,"metrics":None})
        updated={**deepcopy(value),"publications":rows,"latest_manifest":manifest,
            "last_consumer_day":day,"account_count":len(account.get("cases",{})),
            "account_head":daily._ref(account_file),"last_operation":str(operation/"receipt.json"),
            "consumer_result":daily._ref(stdout_file),"consumer_status":observed["status"],
            "cases":case_status,"current_case_count":sum(c["state_day"] == day for c in case_status),
            "blocked_case_count":sum(c["status"] == "blocked" for c in case_status),
            "error_case_count":sum(c["status"] == "error" for c in case_status),"metrics":None}
        _validate_generation(updated,head,registration)
        head[KEY]=updated
        daily._atomic(directory/"head.json",head)
        return {"status":"saved_eight32_generation_consumer_result","generation":updated,
            "account_count":updated["account_count"],"old_accounts_advanced":False,**daily.FLAGS}


def _split_candidate(value, profile, day):
    rows = deepcopy(value["publications"])
    published_day = profile["publication_source_day"]
    if published_day > day:
        raise ValueError("A future publication cannot be consumed by an earlier exact session")
    if published_day >= value["first_decision_day"]:
        observed = {"day": published_day, "publication": profile["latest_publication"],
            "manifest": profile["publication_manifest"]}
        if published_day > rows[-1]["day"]:
            rows.append(observed)
        elif published_day == rows[-1]["day"] and observed != rows[-1]:
            raise ValueError("The original actual publication cannot be replaced")
    return rows, profile["latest_manifest"]


def _split_source_profile(directory, head, registration, sessions, now, day):
    """Read successful inputs from the original failed-publication operation.

    The producer's cursor advances only after paper publication. A successful
    own-input stage can nevertheless settle known orders; its original terminal
    evidence is required, and neither the producer nor its head is changed.
    """
    profile = _current(head, registration, sessions, now, allow_context=True)
    if profile["source_day"] == day:
        return profile
    if sessions.index(day) == 0 or profile["source_day"] != sessions[sessions.index(day)-1]:
        raise ValueError("Split settlement needs the current source or one exact successful saved input successor")
    operation = directory / "eight32_production" / day.replace("-", "")
    plan_file, receipt_file = operation / "plan.json", operation / "receipt.json"
    if not plan_file.is_file() or not receipt_file.is_file():
        raise ValueError("No original completed eight-type input stage exists for this exact settlement day")
    plan, receipt = daily._read(plan_file), daily._read(receipt_file)
    execution = sessions[sessions.index(day)+1]
    if (plan.get("schema") != producer.SCHEMA or plan.get("day") != day
            or plan.get("execution_day") != execution or plan.get("source_only") is not False
            or plan.get("parent_profile") != profile or plan.get("system_registration") != head["registration"]
            or receipt.get("schema") != producer.SCHEMA or receipt.get("day") != day
            or receipt.get("source_only") is not False or receipt.get("plan_ref") != daily._ref(plan_file)
            or any(plan.get(k) is not v or receipt.get(k) is not v for k,v in daily.FLAGS.items())):
        raise ValueError("Saved input context changed its exact original producer parent, source day or method")
    source = plan["source"]
    package = Path(source["source_folder"]).resolve()
    checked = daily._source(package, registration["calendar"], sessions, now)["proof"]
    if source != checked or source["day"] != day:
        raise ValueError("Saved input context requires the exact completed source-D package")
    _, steps = producer._steps(profile, day, package, operation, False)
    for step in steps[:-1]:
        saved = receipt.get("steps", {}).get(step["id"])
        if not saved or saved.get("status") != "completed":
            raise ValueError("Saved pre-publication input stages require successful terminal evidence")
        bound = {**step, "binding":{"kind":"eight32_independent_production",
            "registration":head["registration"], "source":source, "plan":daily._ref(plan_file)}}
        daily._terminal(bound, operation/"commands", day, saved)
    manifest = daily._ref(operation/"artifacts/eight_inputs/manifest.json")
    producer._manifest(manifest, [*profile["source_days"],day], [*profile["packages"],str(package)])
    # This temporary projection is only a source resolver result. It is never
    # saved as a producer profile or treated as an E publication.
    return {**profile, "source_day":day, "latest_manifest":manifest}


def _advance_split(directory, head, registration, value, day):
    """Settle exact raw days and attach only genuinely saved publications.

    An operation is pinned to its manifest and full publication prefix. A later
    same-day publication gets another operation; recovery only adopts a saved
    successful child, never repeats an interrupted or failed execution.
    """
    sessions, _ = _calendar(registration)
    if day not in sessions or day < value["first_decision_day"]:
        raise ValueError("An exact split generation source day is required")
    if value["last_consumer_day"] and day < value["last_consumer_day"]:
        raise ValueError("A split generation cannot roll its execution cursor backwards")
    root = _operation(directory, value["generation_id"], "advance_split", day)
    # A completed child can exist before the coordination head adopted it.
    # Recover that original plan before looking at a newer producer profile.
    pending = []
    if root.exists():
        for file in sorted(root.glob("*/plan.json")):
            saved = daily._read(file)
            if saved.get("parent_generation") == value:
                pending.append((file.parent, saved))
    if len(pending) > 1:
        raise ValueError("Multiple unresolved split operations cannot be silently selected")
    recover_only = bool(pending)
    if recover_only:
        operation, plan = pending[0]
        rows, manifest = plan["publications"], plan["manifest"]
        input_binding = plan["input_binding"]
    else:
        profile = _split_source_profile(directory, head, registration, sessions, datetime.now(MARKET_TZ), day)
        if profile["source_day"] != day:
            raise ValueError("Use the latest exact complete source day for a new split operation")
        rows, manifest = _split_candidate(value, profile, day)
        input_binding = {"day":day, "manifest":manifest, "publications":rows}
        if value.get("last_split_input") == input_binding:
            _checked_ref(value["account_head"])
            return {"status":"already_saved_eight32_generation_day", "command_called":False, **daily.FLAGS}
        if value.get("account_head"):
            _checked_ref(value["account_head"])
        key = sha256(json.dumps(input_binding, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
        operation = root / key
    candidate = {**deepcopy(value), "publications":rows, "latest_manifest":manifest}
    _validate_generation(candidate, head, registration)
    if (not rows or rows[:len(value["publications"])] != value["publications"]
            or any(row["day"] > day for row in rows)):
        raise ValueError("Split settlement cannot replace or backfill its saved publication prefix")
    command = ["leader-eight-type-split-forward"]
    for row in rows:
        command.extend(["--publication", row["publication"]["file"]])
    for flag, item in (("panel",manifest["file"]),("ipo-evidence",value["ipo_evidence"]["file"]),
            ("research-master-run",value["research_master_run"]["file"]),("run-dir",value["run_directory"]),
            ("registration",value["registration"]["file"]),("through",day)):
        command.extend(["--"+flag,item])
    expected = {"schema":SCHEMA,"kind":"advance_split","day":day,"generation_id":value["generation_id"],
        "session_method_id":SPLIT_SESSION_METHOD_ID,"system_registration":head["registration"],
        "parent_generation":deepcopy(value),"publications":rows,"manifest":manifest,
        "input_binding":input_binding,"command":command,**daily.FLAGS}
    if recover_only and plan != expected:
        raise ValueError("Saved split operation changed its original method, source or publication prefix")
    plan = expected
    step={"id":"consume_generation","command":command,"requires":[],
        "expected_files":[str(Path(value["run_directory"])/"head.json")]}
    result = _execute(operation,plan,step,day,allow_start=not recover_only)
    if result["status"] != "completed_generation_command":
        return result
    account_file = Path(value["run_directory"]) / "head.json"
    account = daily._read(account_file)
    # The saved child's output validation is also applied during recovery.
    daily._outputs(step, day)
    stdout_file = operation / "commands/consume_generation.stdout.txt"
    observed = daily._read(stdout_file)
    if (observed.get("schema") != "leader_eight_type_split_forward_result_v1"
            or observed.get("case_count") != 32
            or Path(observed.get("checkpoint", "")).resolve() != account_file
            or any(observed.get(k) is not v for k,v in daily.FLAGS.items() if k in observed)):
        raise ValueError("Saved split consumer stdout must describe this exact independent generation")
    cases=[]
    for key, case in account["cases"].items():
        current=(case.get("state") or {}).get("day")
        blocked,error=case.get("blocking_evidence"),case.get("error")
        status="error" if error else "blocked" if blocked else "current" if current == day else "not_current"
        cases.append({"case_id":key,"state_day":current,"status":status,
            "last_publication_day":case.get("last_publication_day"),
            "unattached_publication_days":deepcopy(case.get("missing_publication_days",[])),
            "missing_publication_days":[d for d in case.get("missing_publication_days",[])
                if d not in {r["day"] for r in rows}],
            "blocking_evidence":blocked,"error":error,"metrics":None})
    updated={**candidate,"last_consumer_day":day,"last_split_input":input_binding,
        "account_count":len(cases),"account_head":daily._ref(account_file),
        "last_operation":str(operation/"receipt.json"),"consumer_result":daily._ref(stdout_file),
        "consumer_status":observed["status"],"cases":cases,
        "current_case_count":sum(c["state_day"] == day for c in cases),
        "blocked_case_count":sum(c["status"] == "blocked" for c in cases),
        "error_case_count":sum(c["status"] == "error" for c in cases),
        "missing_publication_days":[d for d in sessions if value["first_decision_day"] <= d <= day
            and d not in {row["day"] for row in rows}], "metrics":None}
    _validate_generation(updated,head,registration)
    head[KEY]=updated
    daily._atomic(directory/"head.json",head)
    return {"status":"saved_eight32_generation_consumer_result","generation":updated,
        "account_count":len(cases),"old_accounts_advanced":False,**daily.FLAGS}
