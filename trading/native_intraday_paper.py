"""Persist two native-model PAPER books through the shared opening/closing path.

Only explicit observed quote inputs are consumed.  A whole-phase journal is
published after both trials; recovery installs that saved result without fills.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

from . import intraday_session
from .contracts import MARKET_TZ, RunContext, TradingMode
from .ledger import new_state
from .market_dataset import write_json
from .research.shortline_native_preopen import MODEL_KINDS, bind_preopen_plan
from .research.shortline_native_preopen_forward import _lock


METHOD = "native_two_model_verified_daily_projection_intraday_paper_v1"
SCHEMA = METHOD + "_head"
JOURNAL_SCHEMA = METHOD + "_phase_commit"
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, returns_reportable=False,
    user_account_mutated=False, broker_interacted=False, network_accessed=False,
    model_refit=False, scores_recomputed=False, pit_verified=False,
    corporate_actions_verified=False, ths_payload_generated=False)


def prepare_inputs(*args, **kwargs):
    from .native_intraday_feed import prepare_inputs as prepare
    return prepare(*args, **kwargs)


def _now(value=None):
    value = datetime.now(MARKET_TZ) if value is None else value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Actual phase commit time must carry a timezone")
    return value.astimezone(MARKET_TZ)


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Saved native paper object must be a JSON object")
    return value


def _ref(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    return dict(file=str(path), sha256=sha256(data).hexdigest(), byte_count=len(data))


def _context(directory, kind):
    suffix = sha256(str(directory).encode()).hexdigest()[:16]
    return RunContext("native-intraday-paper:" + suffix + ":" + kind, TradingMode.PAPER)


def _protect(directory, paths):
    for item in paths:
        path = Path(item).resolve()
        package = path if path.is_dir() else path.parent
        if directory == package or package in directory.parents or directory in package.parents:
            raise ValueError("Paper run must remain outside all input files and source packages")


def _binding(prepared):
    proof = prepared.proof
    core = Path(__file__).resolve().parent
    return dict(method_id=METHOD, mode="paper",
        daily_source_policy="native_verified_daily_projection_v1",
        policy_id=prepared.loaded["record"]["policy_id"], config=prepared.config.to_dict(),
        calendar_ref=deepcopy(proof["calendar_ref"]),
        model_manifest=deepcopy(proof["model_manifest"]),
        code={name: _ref(core / name) for name in
            ("engine.py", "intraday_session.py", "native_intraday_feed.py", "native_intraday_paper.py")})


def _head_body(head):
    value = deepcopy(head)
    value.pop("last_journal_ref", None)
    return value


def _validate_shape(head, directory):
    if (head.get("schema") != SCHEMA or head.get("method_id") != METHOD
            or head.get("binding", {}).get("mode") != "paper"
            or head.get("phase") not in {"opening", "closing"}
            or set(head.get("cases", {})) != set(MODEL_KINDS)
            or not isinstance(head.get("sequence"), int) or head["sequence"] < 1
            or any(head.get(key) != value for key, value in FLAGS.items())):
        raise ValueError("Existing EOD, broker or incompatible head cannot become this PAPER method")
    for kind in MODEL_KINDS:
        case = head["cases"][kind]
        state = case["state"]
        context = _context(directory, kind).to_dict()
        if case.get("context") != context or state.get("broker_binding"):
            raise ValueError("Saved independent model context or broker binding changed")
        marker = state.get("intraday_open_session")
        if head["phase"] == "opening":
            if not isinstance(marker, dict) or marker.get("day") != head["execution_day"]:
                raise ValueError("Saved opening phase lacks its exact durable session marker")
        elif marker is not None or state.get("day") != head["execution_day"]:
            raise ValueError("Saved close does not finish the exact opening session")


def _verify_last_journal(head, directory):
    reference = head.get("last_journal_ref")
    if not isinstance(reference, dict):
        raise ValueError("Saved phase head has no complete journal reference")
    path = Path(reference["file"]).resolve()
    if path.parent != directory / "journal" or _ref(path) != reference:
        raise ValueError("Saved phase journal changed or escaped this run")
    journal = _read(path)
    if (journal.get("schema") != JOURNAL_SCHEMA
            or journal.get("after_head") != _head_body(head)
            or journal.get("after_sha256") != _hash(_head_body(head))):
        raise ValueError("Saved whole-phase journal differs from the head")


def _recover(directory, head, *, expected_binding, source_day, execution_day, inputs, phase):
    """Install only already successful whole-phase commits, never run Engine."""
    recovered = 0
    while True:
        sequence = head["sequence"] + 1 if head else 1
        files = list((directory / "journal").glob(f"{sequence:08d}_*.json"))
        if not files:
            return head, recovered
        if len(files) != 1:
            raise ValueError("Multiple journals claim the same next phase")
        path = files[0]
        journal = _read(path)
        after = journal.get("after_head")
        if (journal.get("schema") != JOURNAL_SCHEMA or journal.get("sequence") != sequence
                or journal.get("before_sha256") != (_hash(head) if head else None)
                or not isinstance(after, dict) or journal.get("after_sha256") != _hash(after)
                or after.get("sequence") != sequence or "last_journal_ref" in after):
            raise ValueError("Interrupted phase journal has an invalid predecessor or payload")
        _validate_shape(after, directory)
        if after["binding"] != expected_binding:
            raise ValueError("Interrupted phase differs from the current permanent method/input code binding")
        exact_session = (after["source_day"] == source_day and after["execution_day"] == execution_day
            and after["inputs"] == inputs)
        following_open = (after["phase"] == "closing" and phase == "opening"
            and source_day == after["execution_day"])
        if not (exact_session or following_open):
            raise ValueError("Unrelated or changed plan inputs cannot recover a different saved phase")
        if head is not None and after["binding"] != head["binding"]:
            raise ValueError("Interrupted phase cannot change the permanent account binding")
        restored = {**after, "last_journal_ref": _ref(path)}
        write_json(directory / "head.json", restored)
        head, recovered = restored, recovered + 1


def _summary(directory, head, *, result_status=None, committed=False, recovered=0):
    cases = []
    if head is not None:
        for kind in MODEL_KINDS:
            case = head["cases"][kind]
            state = case["state"]
            daily = case.get("daily")
            opening = case.get("opening_receipt") or {}
            cases.append(dict(model_kind=kind, context=case["context"], cash_cents=state["cash"],
                opening_marked_equity_cents=opening.get("opening_marked_equity_cents"),
                closing_equity_rmb=daily.get("equity") if daily else None,
                order_count=len(state["orders"]), fill_count=len(state["fills"]),
                quantities={symbol: sum(lot["qty"] for lot in lots)
                    for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)},
                financial_performance=None))
    return dict(schema=METHOD+"_status", method_id=METHOD, mode="paper",
        status=result_status or ("saved_"+head["phase"] if head else "not_created"),
        run_dir=str(directory), head_file=str(directory/"head.json") if head else None,
        account_count=len(cases), phase=head["phase"] if head else None,
        source_day=head["source_day"] if head else None,
        execution_day=head["execution_day"] if head else None,
        sequence=head["sequence"] if head else 0,
        actual_committed_at=head["committed_at"] if head else None,
        cases=cases, phase_committed=committed, recovered_commits=recovered,
        financial_performance=None, **FLAGS)


def status(run_dir):
    """Read only the saved head; no feed, quote, strategy, recovery or writes."""
    directory = Path(run_dir).resolve()
    path = directory / "head.json"
    head = _read(path) if path.is_file() else None
    if head is not None:
        _validate_shape(head, directory)
    return _summary(directory, head)


def run(run_dir, plan=None, prior_package=None, quote=None, *, phase="status",
        ipo_proof=None, master_run=None, now=None):
    if phase == "status":
        return status(run_dir)
    if phase not in {"opening", "closing"} or any(value is None for value in (plan, prior_package, quote)):
        raise ValueError("Opening/closing needs an exact plan, D package and explicit quote")
    moment = _now(now)
    prepared = prepare_inputs(plan, prior_package, ipo_proof=ipo_proof, master_run=master_run, now=moment)
    return _run_prepared(run_dir, plan, prior_package, quote, phase=phase,
        prepared=prepared, moment=moment, fixed_now=now)


def _run_prepared(run_dir, plan, prior_package, quote, *, phase, prepared, moment, fixed_now):
    directory = Path(run_dir).resolve()
    loaded, config = prepared.loaded, prepared.config
    d, e = loaded["source_day"], loaded["execution_day"]
    calendar = list(prepared.calendar)
    if d not in calendar or e not in calendar or calendar.index(e) != calendar.index(d) + 1:
        raise ValueError("Plan D and E must be adjacent sessions in the fixed calendar")
    inputs = deepcopy(prepared.proof)
    if inputs.get("plan_ref") != loaded["plan_ref"] or inputs.get("source_day") != d or inputs.get("execution_day") != e:
        raise ValueError("Prepared phase inputs differ from the original saved plan")
    protected = [plan, prior_package, quote, *prepared.input_paths]
    _protect(directory, protected)
    expected_binding = _binding(prepared)
    quote_ref = _ref(quote)
    if directory.exists() and any(path.name not in {"head.json", "journal", "runner.lock"}
            for path in directory.iterdir()):
        raise ValueError("An existing unrelated directory cannot be reused as native PAPER accounts")
    directory.mkdir(parents=True, exist_ok=True)
    with _lock(directory / "runner.lock"):
        head_path = directory / "head.json"
        head = _read(head_path) if head_path.is_file() else None
        if head is not None:
            _validate_shape(head, directory)
            _verify_last_journal(head, directory)
        head, recovered = _recover(directory, head, expected_binding=expected_binding,
            source_day=d, execution_day=e, inputs=inputs, phase=phase)
        if head is not None:
            if head["binding"] != expected_binding:
                raise ValueError("Permanent policy/config/calendar/models/code binding changed")
            if head["source_day"] == d and head["execution_day"] == e:
                if head["inputs"] != inputs:
                    raise ValueError("Same-session plan, D source or qualification evidence changed")
                if head["phase"] == phase:
                    if head["phase_sources"][phase]["quote_file"] != quote_ref:
                        raise ValueError("Committed phase cannot be rebound to another quote")
                    return _summary(directory, head, result_status="already_committed_saved_phase", recovered=recovered)
                if phase == "opening":
                    raise ValueError("The closed session cannot be opened again")
            elif head["phase"] == "opening":
                raise ValueError("Pending opening must be closed before a later plan is consumed")
            elif phase != "opening" or d != head["execution_day"]:
                raise ValueError("Daily plans must continue exactly from the last closed execution day")
        elif phase != "opening":
            raise ValueError("Closing requires a committed two-model opening checkpoint")
        if phase == "closing" and (head["phase"] != "opening" or head["source_day"] != d or head["execution_day"] != e):
            raise ValueError("Closing must use the original opening plan and D source")
        feeds = {kind: prepared.feed(quote, phase, _context(directory, kind)) for kind in MODEL_KINDS}
        for feed in feeds.values():
            _protect(directory, [*protected, *getattr(feed, "input_paths", [])])
            declared = getattr(feed, "intraday_phase_binding", {})
            if declared.get("daily_source_policy") != "native_verified_daily_projection_v1":
                raise ValueError("Feed must explicitly opt into the verified daily projection method")
        cases = {}
        phase_sources = deepcopy(head["phase_sources"]) if phase == "closing" else {}
        model_sources = {}
        for kind in MODEL_KINDS:
            context, feed = _context(directory, kind), feeds[kind]
            previous = head["cases"][kind] if head else None
            state = deepcopy(previous["state"]) if previous else new_state(config.initial_cash)
            if phase == "opening":
                if state["day"] is not None and state["day"] != d:
                    raise ValueError("Original account must close on this exact plan source day")
                envelope = bind_preopen_plan(plan, kind, context)
                receipt = intraday_session.opening(state, feed, e, config, envelope, context=context,
                    opened_at=moment.isoformat(), source_ref=feed.intraday_opening_source_ref)
                case = dict(context=context.to_dict(), state=state, opening_receipt=receipt,
                    daily=None, completed_sessions=previous["completed_sessions"] if previous else 0)
            else:
                before_fills = deepcopy(state["fills"])
                daily = intraday_session.closing(state, feed, e, config, context=context,
                    closed_at=moment.isoformat(), source_ref=feed.intraday_closing_source_ref)
                if state["fills"] != before_fills:
                    raise ValueError("Closing changed the opening fills")
                case = dict(context=context.to_dict(), state=state,
                    opening_receipt=deepcopy(previous["opening_receipt"]), daily=daily,
                    completed_sessions=previous["completed_sessions"] + 1)
            cases[kind] = case
            model_sources[kind] = dict(binding=deepcopy(feed.intraday_phase_binding),
                source_ref=getattr(feed, f"intraday_{phase}_source_ref"),
                observed_at=getattr(feed, f"intraday_{phase}_observed_at"),
                source_available_at=getattr(feed, f"intraday_{phase}_source_available_at", None))
        # No account was published while either model trial was incomplete.
        phase_sources[phase] = dict(quote_file=quote_ref, models=model_sources)
        committed_at = _now(fixed_now).isoformat()
        after = dict(schema=SCHEMA, method_id=METHOD, binding=expected_binding,
            sequence=head["sequence"]+1 if head else 1, phase=phase,
            source_day=d, execution_day=e, inputs=inputs, phase_sources=phase_sources,
            registered_at=head["registered_at"] if head else moment.isoformat(),
            phase_started_at=moment.isoformat(), committed_at=committed_at,
            process=dict(pid=os.getpid(), parent_pid=os.getppid()),
            cases=cases, **FLAGS)
        _validate_shape(after, directory)
        journal = dict(schema=JOURNAL_SCHEMA, sequence=after["sequence"], phase=phase,
            source_day=d, execution_day=e, before_sha256=_hash(head) if head else None,
            after_sha256=_hash(after), after_head=after, committed_at=committed_at)
        journal_path = directory / "journal" / f"{after['sequence']:08d}_{e}_{phase}.json"
        if journal_path.exists():
            raise FileExistsError("A whole-phase journal is immutable; recover it without replay")
        write_json(journal_path, journal)
        committed = {**after, "last_journal_ref": _ref(journal_path)}
        write_json(head_path, committed)
        return _summary(directory, committed, result_status="committed_two_model_"+phase,
            committed=True, recovered=recovered)


def run_from_csv(run_dir, plan, prior_package, csv, *, quote_out, source,
        source_kind, quote_asof, phase, ipo_proof=None, master_run=None,
        risk_warning_source=None, now=None):
    """Import one explicit export then immediately use the original PAPER path.

    Preparing the dated plan before saving the quote avoids a second source
    load inside the 90-second window. An already saved identical quote may be
    reused for recovery; neither a new source nor a fresh timestamp replaces it.
    """
    from .native_intraday_feed import import_quote, load_quote

    if phase not in {"opening", "closing"} or not source or not quote_asof:
        raise ValueError("CSV execution requires an explicit opening/closing observation")
    prepared = prepare_inputs(plan, prior_package, ipo_proof=ipo_proof,
        master_run=master_run, now=_now(now))
    moment = _now(now)
    prepared.now = moment
    day = prepared.loaded["execution_day"]
    if moment.date().isoformat() != day:
        raise ValueError("CSV execution requires the exact current plan execution day")
    directory, output, csv = map(lambda p: Path(p).resolve(), (run_dir, quote_out, csv))
    protected = [plan, prior_package, csv, *prepared.input_paths]
    if risk_warning_source is not None:
        protected.append(risk_warning_source)
    _protect(directory, [*protected, output])
    # Quote artifacts live separately from both original sources and accounts.
    _protect(output.parent, protected)
    if output.exists():
        record = _read(output)
        observed = _now(record["observed_at"])
        if observed > moment:
            raise ValueError("Saved quote observation is later than the actual invocation")
        # Check the original CSV and bound official evidence at the saved clock.
        # This permits idempotent recovery without pretending an old quote is
        # fresh: an uncommitted Engine phase still checks the current clock.
        record, _ = load_quote(output, phase, day, observed)
        expected = dict(source=str(source), source_kind=source_kind, phase=phase,
            trading_day=day, quote_asof=_now(quote_asof).isoformat(), source_csv=_ref(csv))
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError("Saved quote cannot be rebound to changed CSV, source, phase or clock")
        old_risk = record.get("official_positive_risk_warning_check")
        old_source = old_risk.get("source_directory") if old_risk else None
        wanted_source = str(Path(risk_warning_source).resolve()) if risk_warning_source else None
        if old_source != wanted_source:
            raise ValueError("Saved quote official source selection changed")
    else:
        import_quote(csv, source=source, source_kind=source_kind,
            quote_asof=quote_asof, trading_day=day, phase=phase, out=output,
            risk_warning_source=risk_warning_source, now=moment)
    # A slow import must not freeze the phase clock at its earlier start.
    moment = _now(now)
    prepared.now = moment
    return _run_prepared(directory, plan, prior_package, output, phase=phase,
        prepared=prepared, moment=moment, fixed_now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("status", "opening", "closing"), default="status")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--prior-package", type=Path)
    quotes = parser.add_mutually_exclusive_group()
    quotes.add_argument("--quote", type=Path)
    quotes.add_argument("--quote-csv", type=Path)
    parser.add_argument("--quote-out", type=Path)
    parser.add_argument("--quote-source")
    parser.add_argument("--quote-source-kind", choices=("public_export", "readonly_terminal_export"))
    parser.add_argument("--quote-asof")
    parser.add_argument("--risk-warning-source", type=Path)
    parser.add_argument("--ipo-proof", type=Path)
    parser.add_argument("--master-run", type=Path)
    args = parser.parse_args(argv)
    csv_options = (args.quote_out, args.quote_source, args.quote_source_kind,
        args.quote_asof, args.risk_warning_source)
    if args.quote_csv is not None:
        if args.phase == "status" or any(value is None for value in
                (args.plan, args.prior_package, *csv_options[:4])):
            parser.error("CSV mode needs opening/closing, plan, prior-package, quote-out/source/source-kind/asof")
        result = run_from_csv(args.run_dir, args.plan, args.prior_package, args.quote_csv,
            quote_out=args.quote_out, source=args.quote_source, source_kind=args.quote_source_kind,
            quote_asof=args.quote_asof, phase=args.phase, ipo_proof=args.ipo_proof,
            master_run=args.master_run, risk_warning_source=args.risk_warning_source)
    else:
        if any(value is not None for value in csv_options):
            parser.error("CSV import options require --quote-csv")
        if args.phase == "status" and args.quote is not None:
            parser.error("Status does not import or consume quotes")
        result = run(args.run_dir, args.plan, args.prior_package, args.quote, phase=args.phase,
            ipo_proof=args.ipo_proof, master_run=args.master_run)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
