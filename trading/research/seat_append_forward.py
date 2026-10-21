"""Append-only public-seat publications through the existing Engine/Ledger.

Independent registration and books; no migration of the single-snapshot method.
The four-trial journal protocol follows the existing registered consumer while
selection, quality, qualification and all account mutations reuse its helpers
and the shared Engine.  This module never collects disclosures or prices.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import pandas as pd
from ..contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from . import seat_hypothesis_dated_inputs as inputs
from . import seat_hypothesis_dated_forward as legacy
from . import seat_append_dated_inputs as dated
from . import seat_disclosure_catalog as catalog
from .ipo_qualification import load_ipo_qualification
from .leader_forward_run import ForwardRawFeed
from .shortline_native_preopen_forward import _lock, _source

METHOD_ID = "public_seat_append_four_arm_preopen_shared_engine_v1"
REGISTRATION_SCHEMA = "public_seat_append_forward_method_registration_v1"
SCHEMA = "public_seat_append_four_arm_forward_checkpoint_v1"
RESULT_SCHEMA = "public_seat_append_four_arm_forward_result_v1"
JOURNAL_SCHEMA = "public_seat_append_four_arm_committed_session_v1"
RULE_SPEC = {**legacy.RULE_SPEC, "method_id": METHOD_ID,
    "source": "independent_registered_append_catalog_prefix_each_actual_D",
    "catalog_prefix": "initial_catalog_then_true_parent_ancestry_no_rollback_or_branch",
    "event_schedule": "dated_input_fixed_first_maturity_latest_event_day_collision_rule",
    "account_migration": "forbidden_new_independent_four_books_only"}
FLAGS = {**legacy.FLAGS, "new_disclosure_append_supported": True}
_now, _hash = legacy._now, legacy._hash
_qualification_files = legacy._qualification_files
_targets, _quality, _summary = legacy._targets, legacy._quality, legacy._summary

def _context(directory, arm):
    return RunContext("seat-append:" + _hash(str(directory))[:20] + ":" + arm, TradingMode.REPLAY)

def _registration(path, now):
    record = inputs._read(path)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("rule_spec") != RULE_SPEC or record.get("arm_ids") != list(inputs.ARMS)
            or any(record.get(key) is not value for key,value in FLAGS.items())):
        raise ValueError("Exact independent append four-book registration is required")
    actual = inputs._stamp(record["registered_at"], now)
    if inputs._verify(record["implementation"]) != inputs._ref(__file__):
        raise ValueError("Registered append forward core changed")
    helper_refs = [inputs._ref(module.__file__) for module in (legacy, inputs)]
    if record.get("shared_helper_implementations") != helper_refs:
        raise ValueError("Original shared account helpers changed")
    for reference in record["shared_helper_implementations"]: inputs._verify(reference)
    method_ref = inputs._verify(record["dated_input_method_registration"])
    method = dated.load_registration(method_ref["file"], now=now)
    if (method["reference"] != method_ref
            or record.get("initial_catalog") != method["record"]["initial_catalog"]
            or record.get("calendar") != method["calendar"]
            or record.get("config") != method["config"]
            or record.get("first_decision_day") != method["record"]["first_decision_day"]
            or not inputs._stamp(method["record"]["registered_at"], now) <= actual):
        raise ValueError("Append dated method/catalog/config/first-D ancestry changed")
    first = record["first_decision_day"]
    if first < actual.date().isoformat():
        raise ValueError("First D cannot predate actual forward method registration")
    ipo = inputs._verify(record["ipo_evidence"])
    master = inputs._verify(record["research_master_run"]) if record.get("research_master_run") else None
    qualification_files, clocks = _qualification_files(ipo["file"], master["file"] if master else None)
    for value in clocks: inputs._stamp(value, now)
    files = list(method["files"]) + [Path(path), Path(ipo["file"]), Path(__file__)]
    files += [Path(r["file"]) for r in helper_refs] + qualification_files
    if master: files.append(Path(master["file"]))
    return {"record":record,"reference":inputs._ref(path),"dated":method,
        "sessions":method["sessions"],"calendar":method["calendar"],
        "config":inputs.original._config(100000,5),"files":files,"ipo":ipo,"master":master}

def _publication(path, registered, now):
    entry = dated.load_publication(path, registered["dated"], now=now)
    if inputs._stamp(entry["reference"]["published_at"], now) < inputs._stamp(registered["record"]["registered_at"], now):
        raise ValueError("Actual publication predates append forward registration")
    return entry


def _result(status, directory, head, config, *, committed=0, created=False, new_orders=0, **facts):
    return {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "status": status,
        "read_at": _now().isoformat(), "run_directory": str(directory),
        "checkpoint": str(directory / "head.json") if head else None,
        "research_accounts_created": 4 if created else 0, "account_count": 4 if head else 0,
        "research_ledger_mutated": bool(committed), "newly_committed_execution_sessions": committed,
        "new_order_count": new_orders, "orders_generated": new_orders > 0,
        "cases": [_summary(arm, head["cases"][arm], config) for arm in inputs.ARMS] if head else [],
        "metrics": None, "account_reset": False, **facts, **FLAGS}

def _head(head, binding, entries, sources, directory):
    if (head.get("schema") != SCHEMA or head.get("binding") != binding
            or list(head.get("cases", {})) != list(inputs.ARMS)
            or any(head.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Append four-account method/config/source/context binding changed")
    count = len(head["consumed_publications"])
    if count < 1 or not head.get("journal_files"):
        raise ValueError("A persisted head requires at least one committed publication and sealed journal")
    if head["consumed_publications"] != [entry["reference"] for entry in entries[:count]]:
        raise ValueError("Previously consumed publication prefix changed or disappeared")
    if head.get("consumed_catalog_prefixes") != [entry["bundle"]["catalog"] for entry in entries[:count]]:
        raise ValueError("Consumed catalog prefix records changed")
    end = entries[count - 1]["reference"]["execution_day"] if count else None
    prefix = [sources[d]["proof"] for d in sorted(sources) if end and d <= end]
    if head["raw_sources"] != prefix or len(head["journal_files"]) != count:
        raise ValueError("Previously consumed exact raw source or session journal prefix changed")
    previous, journal = None, None
    for index, reference in enumerate(head["journal_files"]):
        path = Path(reference["file"]).resolve()
        if path.parent != directory / "journal" or inputs._verify(reference) != inputs._ref(path):
            raise ValueError("Session journal source or bytes changed")
        journal = inputs._read(path)
        ending = entries[index]["reference"]["execution_day"]
        if (journal.get("schema") != JOURNAL_SCHEMA or journal.get("previous_journal_sha256") != previous
                or journal.get("publication") != entries[index]["reference"]
                or journal.get("binding_sha256") != _hash(binding)
                or journal.get("raw_sources_sha256") != _hash([sources[d]["proof"] for d in sorted(sources) if d <= ending])):
            raise ValueError("Exact journal publication/context/raw-source chain changed")
        previous = reference["sha256"]
    for arm in inputs.ARMS:
        case = head["cases"][arm]
        days = [entry["reference"]["execution_day"] for entry in entries[:count]]
        if (case["context"] != _context(directory, arm).to_dict() or case["state"].get("broker_binding")
                or case["state"]["day"] != end or [row["date"] for row in case["daily"]] != days
                or (journal and journal["post_case_sha256"][arm] != _hash(case))):
            raise ValueError("Full four-book state/daily trace differs from the sealed session journal")

def run(publications, raw_closes, registration, run_dir, *, through=None, mode=TradingMode.REPLAY):
    if mode != TradingMode.REPLAY:
        raise ValueError("This consumer is REPLAY only; no broker or PAPER worker")
    now, directory = _now(), Path(run_dir).resolve()
    registered = _registration(registration, now)
    config, sessions = registered["config"], registered["sessions"]
    packages = [path.parent for path in registered["files"]] + [Path(p).resolve() for p in raw_closes]
    packages += [Path(p).resolve().parent for p in publications]
    for package in packages:
        if directory == package or directory in package.parents or package in directory.parents:
            raise ValueError("Research run cannot cover or sit inside a source or registration package")
    head_path = directory / "head.json"
    head = inputs._read(head_path) if head_path.is_file() else None
    if not publications:
        if head:
            raise ValueError("Existing research account requires its original complete publication prefix")
        if directory.exists():
            raise FileExistsError("Existing directory without a checkpoint cannot become a reset account")
        return _result("waiting_for_first_actual_dated_publication", directory, None, config,
            first_decision_day=registered["record"]["first_decision_day"])
    entries = [_publication(path, registered, now) for path in publications]
    previous_catalog = registered["dated"]["initial_catalog"]
    for entry in entries:
        catalog.assert_catalog_prefix(entry["catalog_loaded"], previous_catalog)
        previous_catalog = entry["catalog_loaded"]
    # Publication validation reads these indirect sources even when they were
    # not explicit CLI parameters. A run must stay outside their packages.
    indirect_files, indirect_packages = [], []
    for entry in entries:
        bundle = entry["bundle"]
        primary, audit = bundle["D_primary"], bundle["source_audit"]
        indirect_files.append(Path(entry["reference"]["bundle"]["file"]))
        indirect_files.extend(Path(reference["file"]) for reference in bundle["input_references"])
        indirect_files.extend(Path(primary[key]["file"]) for key in ("raw", "plan", "receipt", "roster"))
        indirect_files.append(Path(primary["acquisition"]["roster_receipt"]))
        indirect_files.append(Path(audit["reference"]["file"]))
        indirect_files.extend(Path(audit["verification"]["comparison"][key]["file"])
            for key in ("raw", "metadata"))
        indirect_packages.append(Path(primary["source_folder"]).resolve())
    indirect_packages += [file.resolve().parent for file in indirect_files]
    for package in indirect_packages:
        if directory == package or directory in package.parents or package in directory.parents:
            raise ValueError("Research run cannot cover or sit inside an indirect publication/source package")
    days = [entry["reference"]["decision_day"] for entry in entries]
    first = registered["record"]["first_decision_day"]
    start = sessions.index(first)
    if days != sessions[start:start + len(days)]:
        raise ValueError("Original first D and complete publication prefix cannot be skipped, reordered or rolled")
    sources = {}
    for folder in raw_closes:
        source = _source(folder, registered["calendar"], sessions, now)
        day = source["proof"]["day"]
        if day in sources: raise ValueError("Strict raw day cannot be supplied twice")
        if day >= first: sources[day] = source
    for entry in entries:
        d = entry["reference"]["decision_day"]
        if d in sources:
            primary = entry["bundle"]["D_primary"]
            proof = sources[d]["proof"]
            if (str(Path(proof["raw_file"]).resolve()) != primary["raw"]["file"]
                    or proof["raw_sha256"] != primary["raw"]["sha256"]
                    or proof["plan_sha256"] != primary["plan"]["sha256"]
                    or proof["receipt_sha256"] != primary["receipt"]["sha256"]):
                raise ValueError("D execution source differs from the exact published original D source")
    qualification = load_ipo_qualification(registered["ipo"]["file"], registered["calendar"]["file"],
        master_run=registered["master"]["file"] if registered["master"] else None)
    if list(qualification.calendar) != sessions:
        raise ValueError("IPO qualification calendar differs from the fixed execution calendar")
    qualification_files, qualification_clocks = _qualification_files(registered["ipo"]["file"],
        registered["master"]["file"] if registered["master"] else None)
    knowledge = max(inputs._stamp(t, now) for t in qualification_clocks)
    for file in qualification_files:
        package = file.parent
        if directory == package or package in directory.parents or directory in package.parents:
            raise ValueError("Research output cannot cover or sit inside fixed IPO/master source packages")
    binding = {"method_id": METHOD_ID, "rule_spec": RULE_SPEC, "registration": registered["reference"],
        "calendar": registered["calendar"], "config": config.to_dict(),
        "dated_input_registration": registered["dated"]["reference"],
        "initial_catalog": registered["dated"]["record"]["initial_catalog"],
        "qualification": qualification.verification, "qualification_files": [inputs._ref(p) for p in qualification_files],
        "run_directory": str(directory), "first_decision_day": first,
        "contexts": {arm: _context(directory, arm).to_dict() for arm in inputs.ARMS}}
    if head:
        _head(head, binding, entries, sources, directory)
    elif directory.exists():
        raise FileExistsError("Existing directory without a valid head cannot be reset")
    cutoff = through or entries[-1]["reference"]["execution_day"]
    if cutoff not in sessions or (head and cutoff < head["cases"][next(iter(inputs.ARMS))]["state"]["day"]):
        raise ValueError("Through must be a fixed session without moving an existing account backwards")
    cursor = len(head["consumed_publications"]) if head else 0
    work = [entry for entry in entries[cursor:] if entry["reference"]["execution_day"] <= cutoff]
    if not work:
        return _result("waiting_for_next_actual_publication_or_execution_session", directory, head, config)
    required = [work[0]["reference"][key] for key in ("decision_day", "execution_day")]
    missing = [day for day in required if day not in sources]
    if missing:
        return _result("waiting_for_exact_D_and_E_complete_raw", directory, head, config, missing_source_days=missing)
    # The lock is outside the run: a failed first trial must not create a fake
    # account or poison an otherwise empty run directory.
    directory.parent.mkdir(parents=True, exist_ok=True)
    with _lock(directory.parent / (directory.name + ".runner.lock")):
        if (head_path.exists() and inputs._read(head_path) != head) or (head and not head_path.exists()):
            raise ValueError("Persistent checkpoint changed during source validation")
        committed, created, new_orders = 0, False, 0
        for entry in work:
            d, e = (entry["reference"][key] for key in ("decision_day", "execution_day"))
            missing = [day for day in (d, e) if day not in sources]
            if missing:
                return _result("waiting_for_exact_D_and_E_complete_raw", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders, missing_source_days=missing)
            cases = head["cases"] if head else {arm: {"context": _context(directory, arm).to_dict(),
                "state": new_state(config.initial_cash), "daily": []} for arm in inputs.ARMS}
            if head and any(case["state"]["day"] != d for case in cases.values()):
                raise ValueError("A research account cannot skip an intervening published source session")
            raw = pd.concat([sources[day]["canonical"] for day in sorted(sources) if day <= e], ignore_index=True)
            feed = ForwardRawFeed(raw, sessions, sorted(raw.symbol.unique()), ipo_qualification=qualification)
            arms = {arm["arm_id"]: arm for arm in entry["bundle"]["arms"]}
            targets = {arm: _targets(arms[arm], cases[arm]["state"], config) for arm in inputs.ARMS}
            quality = {arm: _quality(cases[arm], targets[arm], d, e, feed, sources, qualification, knowledge) for arm in inputs.ARMS}
            if any(row["issues"] for row in quality.values()):
                return _result("blocked_exact_reference_raw_or_actual_IPO_knowledge", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders, blocking_evidence=quality,
                    session_not_committed=True)
            trials = deepcopy(cases)
            try:
                for arm in inputs.ARMS:
                    case, context = trials[arm], RunContext.from_dict(trials[arm]["context"])
                    metadata = {"context": context, "reason": arms[arm]["reason"],
                        "source": "research:seat-append:" + entry["reference"]["publication"]["sha256"] + ":" + arm}
                    intent = PortfolioIntent.no_change(**metadata) if targets[arm] is None else PortfolioIntent.full_target(targets[arm], **metadata)
                    envelope = PreopenPortfolioIntent(d, e, entry["reference"]["published_at"], intent)
                    row = advance(case["state"], feed, e, config, len(case["daily"]), context=context, preopen_intent=envelope)
                    row.update(source_day=d, publication=entry["reference"], source_catalog=entry["bundle"]["catalog"], selected_targets=targets[arm],
                        retained_from_actual_D_book=True, E_close_used_for_selection=False,
                        reference_and_IPO_quality=quality[arm], original_published_decision=arms[arm]["decision_kind"])
                    case["daily"].append(row)
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                return _result("failed_four_arm_trial_no_commit", directory, head, config,
                    committed=committed, created=created, new_orders=new_orders,
                    failed_arm=arm, error=f"{type(error).__name__}: {error}",
                    session_not_committed=True, all_four_trials_discarded=True)
            current = deepcopy(head) if head else {"schema": SCHEMA, "binding": binding,
                "consumed_publications": [], "consumed_catalog_prefixes": [], "journal_files": [], "cases": {}, "raw_sources": [], **FLAGS}
            current["cases"] = trials
            current["consumed_publications"].append(entry["reference"])
            current["consumed_catalog_prefixes"].append(entry["bundle"]["catalog"])
            current["raw_sources"] = [sources[day]["proof"] for day in sorted(sources) if day <= e]
            journal = {"schema": JOURNAL_SCHEMA, "publication": entry["reference"],
                "binding_sha256": _hash(binding), "committed_at": _now().isoformat(),
                "previous_journal_sha256": head["journal_files"][-1]["sha256"] if head else None,
                "post_case_sha256": {arm: _hash(trials[arm]) for arm in inputs.ARMS},
                "raw_sources_sha256": _hash(current["raw_sources"]), **FLAGS}
            journal_file = directory / "journal" / (e + "_" + _hash(journal)[:24] + ".json")
            journal_file.parent.mkdir(parents=True, exist_ok=True)
            inputs._write(journal_file, journal)
            current["journal_files"].append(inputs._ref(journal_file))
            write_json(head_path, current)
            created, head = created or head is None, current
            committed += 1
            new_orders += sum(trials[arm]["daily"][-1]["new_orders"] for arm in inputs.ARMS)
        return _result("advanced_four_isolated_dated_research_books", directory, head, config,
            committed=committed, created=created, new_orders=new_orders,
            successor_publication_required_to_execute_D=False)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, action="append", default=[])
    parser.add_argument("--raw-close", type=Path, action="append", default=[])
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--through")
    parser.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    export = args.result_json.resolve() if args.result_json else None
    if export:
        registered = _registration(args.registration, _now())
        files = registered["files"] + [Path(p) for p in args.publication]
        qualification_files, _ = _qualification_files(registered["ipo"]["file"],
            registered["master"]["file"] if registered["master"] else None)
        files += qualification_files
        for publication in args.publication:
            pub = inputs._read(publication)
            bundle_path = Path(pub["bundle"]["file"])
            bundle = inputs._read(bundle_path)
            files.append(bundle_path)
            files += [Path(reference["file"]) for reference in bundle["input_references"]]
        packages = [p.resolve().parent for p in files] + [args.run_dir.resolve()] + [p.resolve() for p in args.raw_close]
        # The runner lock is a sibling of the run; protect its exact path,
        # without excluding every legitimate sibling observation directory.
        files.append(args.run_dir.resolve().parent / (args.run_dir.name + ".runner.lock"))
        inputs._protect(export, files, packages)
        export.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run(args.publication, args.raw_close, args.registration, args.run_dir, through=args.through)
    except Exception as error:
        if export:
            inputs._write(export, {"schema": "public_seat_append_forward_command_failure_v1",
                "failed_at": _now().isoformat(), "error": f"{type(error).__name__}: {error}",
                "execution_progress_unknown": True, **FLAGS})
        raise
    if export:
        export.parent.mkdir(parents=True, exist_ok=True)
        inputs._write(export, result)
    print(json.dumps({"schema": result["schema"], "status": result["status"], "checkpoint": result["checkpoint"],
        "account_count": result["account_count"], "newly_committed_execution_sessions": result["newly_committed_execution_sessions"],
        "new_order_count": result["new_order_count"], "metrics": None, **FLAGS}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
