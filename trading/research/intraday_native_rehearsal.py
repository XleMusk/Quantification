"""One posthoc native-plan session, split at its original opening and close.

Historical open timestamps below are REPLAY assumptions. The strict source's
actual after-close acquisition clocks remain separately pinned and displayed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from .. import engine, intraday_session
from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..ledger import Ledger, new_state
from .exchange_calendar import load_calendar
from .leader_daily_signals import _calendar_view
from .leader_forward_run import ForwardRawFeed
from .shortline_decision_v3 import _known_raw
from .shortline_native_preopen import bind_preopen_plan, load_preopen_plan, MODEL_KINDS
from .shortline_native_preopen_forward import _source, _panel_link, _qualification_link, _read
from .ipo_qualification import load_ipo_qualification
from .shortline_panel_replay import _config


DATA = Path("D:/QuantificationData/trading/research")
ROOT = Path(__file__).resolve().parents[2]
PLAN = DATA/"shortline_native_preopen/20261016_source1015_ipoqualified_v1/plan.json"
SOURCES = {day: DATA/f"bulk_close_cycles/{day.replace('-', '')}_v1/full_close"
    for day in ("2026-10-15", "2026-10-16")}
METHOD = "saved_native_qualified_plan_single_session_two_phase_rehearsal_v1"
ECONOMIC_FIELDS = ("cash", "positions", "orders", "fills", "events", "pending_targets", "targets",
    "candidates", "prices", "entitlements", "receivables", "day", "sequence",
    "preopen_execution_binding", "last_preopen_intent", "price_units",
    "research_corporate_action_binding", "full_exit_lifecycle", "prior_volume_execution_policy",
    "pending_risk_exits")
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, pit_verified=False,
    corporate_actions_verified=False, returns_reportable=False,
    user_account_mutated=False, broker_interacted=False, network_accessed=False,
    model_refit=False, scores_recomputed=False, current_intraday_evidence=False)


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if hasattr(value, "item"):
        return _clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _save(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(_clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish complete bytes atomically while retaining the old exclusive-x
        # contract.  link refuses an existing destination; replace would not.
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ref(path):
    data = Path(path).read_bytes()
    return dict(file=str(Path(path).resolve()), sha256=sha256(data).hexdigest(), byte_count=len(data))


class _OpenOnlyRow(dict):
    FORBIDDEN = {"close", "high", "low", "volume", "amount"}

    def __getitem__(self, key):
        if key in self.FORBIDDEN:
            raise AssertionError("Opening accessed a future/current-session field: " + key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self.FORBIDDEN:
            raise AssertionError("Opening accessed a future/current-session field: " + key)
        return super().get(key, default)


class _PhaseFeed:
    """Explicit read surfaces; no generic delegation to the complete E feed."""
    def __init__(self, full, loaded, sources, qualification, phase, model_kind):
        self.calendar, self.symbols = list(full.calendar), list(full.symbols)
        self.instruments, self.manifest = deepcopy(full.instruments), deepcopy(full.manifest)
        self.actions = deepcopy(full.actions)
        self.instrument_profiles = deepcopy(getattr(full, "instrument_profiles", {}))
        self.next_day = full.next_day
        self.research_listing_limit_reason = full.research_listing_limit_reason
        self.validate_preopen_targets = full.validate_preopen_targets
        d, e = loaded["source_day"], loaded["execution_day"]
        self.volumes = full.volumes.reindex([d]).copy()  # Never expose E's final volume.
        original = full.rows.get(e, {})
        safe = (lambda row: _OpenOnlyRow({field: row[field] for field in
            ("open", "preclose", "tradestatus", "isST", "adjustflag")}))
        self.rows = {e: {symbol: safe(row) if phase == "opening" else
            {field: row[field] for field in ("close", "tradestatus")}
            for symbol, row in original.items()}}
        source_id = sha256(json.dumps({"plan": loaded["plan_ref"],
            "source_proofs": {day: source["proof"] for day, source in sources.items()}},
            sort_keys=True).encode()).hexdigest()
        self.intraday_phase_binding = dict(schema_version=1, source_id=source_id,
            quote_source_kind="replay_raw_open", historical_phase_time_is_assumed=True,
            actual_source_available_at=sources[e]["proof"]["source_available_at"],
            phase_input_is_not_current_BrokerQuote=True)
        self.intraday_opening_source_ref = source_id + ":opening_projection"
        self.intraday_closing_source_ref = source_id + ":closing_projection"
        self.intraday_opening_observed_at = e + "T09:30:00+08:00"
        self.intraday_closing_observed_at = e + "T15:00:00+08:00"
        self.intraday_opening_source_available_at = sources[e]["proof"]["source_available_at"]
        self.intraday_closing_source_available_at = sources[e]["proof"]["source_available_at"]
        self._source_day, self._execution_day = d, e
        self._previous = full.rows.get(d, {})
        # The opening guard owns only D facts and E open/reference/status.
        self._original_previous = {s: r["close"] for s, r in sources[d]["original_rows"].items()}
        self._original_preclose = {s: r["preclose"] for s, r in sources[e]["original_rows"].items()}
        self._qualification, self._decision = qualification, loaded["decisions"][model_kind]
        if phase == "closing":
            self._complete_close_rows = original

    def _guard(self, state, day, *, closing=False):
        if day != self._execution_day:
            raise ValueError("The rehearsal cannot change its exact execution day")
        required = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        required.update(s for s, weight in (self._decision["weights"] or {}).items() if weight > 0)
        for symbol in sorted(required):
            previous = self._previous.get(symbol)
            if not _known_raw(previous):
                raise ValueError("Original D raw status/activity is unknown: " + symbol)
            if closing:
                if not _known_raw(self._complete_close_rows.get(symbol)):
                    raise ValueError("Complete E raw quality is unknown at closing: " + symbol)
                continue
            current = self.rows[day].get(symbol)
            if (current is None or str(current.get("adjustflag")) != "3"
                    or current.get("tradestatus") not in {0, 1} or current.get("isST") not in {0, 1}
                    or any(not isinstance(current.get(key), (int, float)) or isinstance(current[key], bool)
                        or not math.isfinite(current[key]) or current[key] <= 0 for key in ("open", "preclose"))):
                raise ValueError("Exact E opening raw price/status is unknown: " + symbol)
            if Decimal(self._original_previous[symbol]) != Decimal(self._original_preclose[symbol]):
                raise ValueError("Original Dclose/Epreclose differs; no action/reference imputation: " + symbol)
            assessment = self._qualification.assess(symbol, day, decision_as_of=day+"T09:30:00+08:00")
            if assessment["engine_block_reason"] is not None:
                raise ValueError("Original execution-open IPO qualification blocks: " + symbol)

    def validate_opening_session(self, state, day):
        self._guard(state, day)

    def validate_closing_session(self, state, day):
        self._guard(state, day, closing=True)


def _verified_json(path, reference):
    path = Path(path)
    if _ref(path) != reference:
        raise ValueError("Saved rehearsal file changed: " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _process(phase, started_at):
    return dict(phase=phase, pid=os.getpid(), parent_pid=os.getppid(),
        actual_started_at=started_at)


def run(out, delivery, phase="all"):
    if phase not in {"all", "opening", "closing"}:
        raise ValueError("Unknown rehearsal phase")
    if phase == "all":
        opening = run(out, delivery, phase="opening")
        if opening["status"] != "completed_two_model_opening_rehearsal":
            return opening
        return run(out, delivery, phase="closing")
    started_at = _now()
    out, delivery = Path(out).resolve(), Path(delivery).resolve()
    if out == delivery or out in delivery.parents or delivery in out.parents:
        raise ValueError("Data and delivery directories must be separate")
    saved_opening = saved_registration = None
    if phase == "opening":
        if out.exists() or delivery.exists():
            raise FileExistsError("Use the exclusive rehearsal output and delivery directories once")
    else:
        if not out.is_dir() or not delivery.is_dir():
            raise ValueError("Closing requires the saved opening and delivery directories")
        if ((out/"result.json").exists() or (out/"closing_started.json").exists()
                or (delivery/"actual_readback.json").exists()
                or any((out/kind/"closed_account.json").exists() for kind in MODEL_KINDS)):
            raise FileExistsError("Closing was already attempted; do not repeat this rehearsal")
        saved_opening = json.loads((out/"opening_phase_result.json").read_text(encoding="utf-8"))
        if (saved_opening.get("schema") != METHOD+"_opening_result"
                or saved_opening.get("status") != "completed_two_model_opening_rehearsal"
                or set(saved_opening.get("cases", {})) != set(MODEL_KINDS)):
            raise ValueError("A complete committed two-model opening is required")
        saved_registration = _verified_json(out/"registration.json", saved_opening["registration"])
        for kind in MODEL_KINDS:
            case = saved_opening["cases"][kind]
            if case.get("status") != "opened_waiting_for_closing":
                raise ValueError("An original model opening did not complete")
            _verified_json(out/kind/"open_account.json", case["opening_checkpoint"])
            _verified_json(out/kind/"opening_receipt.json", case["opening_receipt"])
    loaded = load_preopen_plan(PLAN)
    d, e = loaded["source_day"], loaded["execution_day"]
    if (d, e) != ("2026-10-15", "2026-10-16"):
        raise ValueError("Only the original saved 15→16 plan is this rehearsal")
    now = datetime.now(MARKET_TZ)
    calendar = load_calendar(Path(loaded["calendar"]["file"]))
    sessions, _, status = _calendar_view(calendar, "2026-01-01", "2026-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Original pinned calendar is unavailable")
    qualification_inputs = loaded["registration"]["qualification_inputs"]
    evidence = qualification_inputs["ipo_evidence"]["file"]
    master = qualification_inputs["research_master_run"]["file"]
    qualification = load_ipo_qualification(evidence, loaded["calendar"]["file"], master_run=master)
    _qualification_link(loaded, qualification,
        {"ipo_evidence": _read(evidence)[1], "research_master_run": _read(master)[1]})
    source_link = _panel_link(loaded, now)
    sources = {day: _source(folder, loaded["calendar"], sessions, now) for day, folder in SOURCES.items()}
    if any(sources[d]["proof"][key] != value for key, value in source_link["source_close_proof"].items()):
        raise ValueError("D execution source differs from the original saved feature source")
    members = sorted({s for decision in loaded["decisions"].values()
        for s, w in (decision["weights"] or {}).items() if w > 0})
    raw = pd.concat([source["canonical"].loc[source["canonical"].symbol.isin(members)]
        for source in sources.values()], ignore_index=True)
    full = ForwardRawFeed(raw, sessions, members, ipo_qualification=qualification)
    config = _config(100000, 5)
    if config.to_dict() != loaded["record"]["spec"]["config"]:
        raise ValueError("Native saved configuration changed")
    protected = [PLAN.parent, *SOURCES.values(), Path(evidence).parent, Path(master).parent]
    for target in (out, delivery):
        if any(target == p.resolve() or p.resolve() in target.parents or target in p.resolve().parents for p in protected):
            raise ValueError("Rehearsal output cannot overlap a source or old account")
    fixed = dict(plan=loaded["plan_ref"], source_day=d, execution_day=e,
        source_proofs={day: value["proof"] for day, value in sources.items()},
        configuration=config.to_dict(), qualification_inputs=qualification_inputs,
        code={name: _ref(Path(__file__).parents[1]/name) for name in ("engine.py", "intraday_session.py")},
        rehearsal_code=_ref(__file__), output_directory=str(out), delivery_directory=str(delivery),
        contexts={kind: RunContext("native-intraday-rehearsal:"+sha256(str(out).encode()).hexdigest()[:12]+":"+kind,
            TradingMode.REPLAY).to_dict() for kind in MODEL_KINDS})
    if phase == "opening":
        out.mkdir(parents=True, exist_ok=False)
        delivery.mkdir(parents=True, exist_ok=False)
        registration = dict(schema=METHOD+"_registration", registered_at=_now(), mode="replay", **fixed,
        replay_phase_assumption=e+"T09:30:00+08:00",
        actual_execution_raw_available_at=sources[e]["proof"]["source_available_at"],
        historical_open_observation_claimed=False, original_published_plan_unmodified=True,
        economic_fields=list(ECONOMIC_FIELDS), same_day_legacy_control_not_new_strategy_comparison=True,
        opening_process=_process(phase, started_at), **FLAGS)
        _save(out/"registration.json", registration)
    else:
        if (saved_registration.get("schema") != METHOD+"_registration"
                or any(saved_registration.get(key) != value for key, value in fixed.items())
                or any(saved_registration.get(key) != value for key, value in FLAGS.items())):
            raise ValueError("Original plan/source/configuration/context/code registration changed")
        registration = saved_registration
        _save(out/"closing_started.json", dict(schema=METHOD+"_phase_started", phase="closing",
            registration=_ref(out/"registration.json"), opening_result=_ref(out/"opening_phase_result.json"),
            process=_process(phase, started_at), **FLAGS))
    cases = {}
    for kind in MODEL_KINDS:
        directory = out/kind
        if phase == "opening":
            directory.mkdir()
        context = RunContext.from_dict(registration["contexts"][kind])
        envelope = bind_preopen_plan(PLAN, kind, context)
        active_phase = phase
        try:
            if phase == "opening":
                state = new_state(config.initial_cash)
                opening_feed = _PhaseFeed(full, loaded, sources, qualification, "opening", kind)
                open_receipt = intraday_session.opening(state, opening_feed, e, config, envelope,
                    context=context, opened_at=e+"T09:31:00+08:00", source_ref=opening_feed.intraday_opening_source_ref)
                _save(directory/"open_account.json", state)
                _save(directory/"opening_receipt.json", open_receipt)
                cases[kind] = dict(status="opened_waiting_for_closing", context=context.to_dict(),
                    opening_checkpoint=_ref(directory/"open_account.json"),
                    opening_receipt=_ref(directory/"opening_receipt.json"),
                    orders=len(state["orders"]), fill_callbacks=len(state["fills"]),
                    cash_cents=state["cash"], financial_performance=None,
                    opening_used_execution_close=False, opening_used_execution_volume=False,
                    closing_executed=False)
                continue
            case = saved_opening["cases"][kind]
            if case["context"] != context.to_dict():
                raise ValueError("Saved opening context differs from registration")
            restored = _verified_json(directory/"open_account.json", case["opening_checkpoint"])
            closing_feed = _PhaseFeed(full, loaded, sources, qualification, "closing", kind)
            open_fills = deepcopy(restored["fills"])
            daily = intraday_session.closing(restored, closing_feed, e, config, context=context,
                closed_at=e+"T15:05:00+08:00", source_ref=closing_feed.intraday_closing_source_ref)
            if restored["fills"] != open_fills:
                raise ValueError("Closing must not create or change opening fills")
            _save(directory/"closed_account.json", restored)
            _save(directory/"closed_daily.json", daily)
            active_phase = "legacy_same_day_control"
            legacy_state = new_state(config.initial_cash)
            legacy = engine.advance(legacy_state, full, e, config, 0, context=context, preopen_intent=envelope)
            _save(directory/"legacy_same_day_account.json", legacy_state)
            _save(directory/"legacy_same_day_daily.json", legacy)
            equality = {field: restored.get(field) == legacy_state.get(field) for field in ECONOMIC_FIELDS}
            daily_equal = daily == legacy
            book = Ledger(restored, config, context)
            following = full.next_day(e)
            cases[kind] = dict(status="closed_and_economically_matched" if all(equality.values()) and daily_equal else "comparison_mismatch",
                context=context.to_dict(), equality=equality, exact_daily_equal=daily_equal,
                opening_checkpoint=_ref(directory/"open_account.json"), closed_account=_ref(directory/"closed_account.json"),
                opening_receipt=_ref(directory/"opening_receipt.json"), closed_daily=_ref(directory/"closed_daily.json"),
                orders=len(restored["orders"]), fill_callbacks=len(restored["fills"]), cash_cents=restored["cash"],
                fees_cents=sum(value["fees"] for value in restored["fills"].values()),
                positions={symbol: dict(quantity=book.quantity(symbol), sellable_on_E=book.available(symbol, e),
                    sellable_next_session=book.available(symbol, following)) for symbol in members if book.quantity(symbol)},
                financial_performance=None, opening_used_execution_close=False, opening_used_execution_volume=False,
                restored_from_saved_json=True, opening_replayed_in_closing=False)
        except (ValueError, KeyError, AssertionError, TypeError) as error:
            cases[kind] = dict(status="blocked_rehearsal_preserving_saved_phase", failed_phase=active_phase,
                error=type(error).__name__+": "+str(error), context=context.to_dict(), financial_performance=None,
                open_checkpoint_preserved=(directory/"open_account.json").is_file())
            _save(directory/"failure.json", cases[kind])
    if phase == "opening":
        complete = all(case["status"] == "opened_waiting_for_closing" for case in cases.values())
        result = dict(schema=METHOD+"_opening_result", phase="opening",
            status="completed_two_model_opening_rehearsal" if complete else "partial_opening_rehearsal",
            completed_at=_now(), process=_process(phase, started_at), registration=_ref(out/"registration.json"),
            source_day=d, execution_day=e, cases=cases, account_count=len(cases), closing_executed=False,
            actual_intraday_quotes_observed=False, source_acquired_after_execution_open=True,
            original_accounts_modified=False, **FLAGS)
        # This is the final opening commit: closing never trusts an uncommitted
        # account file or fills in an absent model receipt.
        _save(out/"opening_phase_result.json", result)
        _save(delivery/"opening_actual_readback.json", result)
        return result
    complete = all(case["status"] == "closed_and_economically_matched" for case in cases.values())
    result = dict(schema=METHOD+"_result", status="completed_two_model_two_phase_rehearsal" if complete else "partial_two_phase_rehearsal",
        completed_at=_now(), registration=_ref(out/"registration.json"), source_day=d, execution_day=e,
        phase="closing", process=_process(phase, started_at), opening_result=_ref(out/"opening_phase_result.json"),
        opening_process=saved_opening["process"],
        separate_process_ids_observed=saved_opening["process"]["pid"] != os.getpid(),
        restored_from_saved_json=True, opening_replayed_in_closing=False,
        cases=cases, account_count=len(cases), engine_advanced=any(value.get("orders", 0)>0 for value in cases.values()),
        original_accounts_modified=False, actual_intraday_quotes_observed=False,
        source_acquired_after_execution_open=True, **FLAGS)
    _save(out/"result.json", result)
    _save(delivery/"actual_readback.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DATA/"native_intraday_session/20261019_source1015_v1")
    parser.add_argument("--delivery", type=Path, default=ROOT/"outputs/native_intraday_session/20261019_v1")
    parser.add_argument("--phase", choices=("opening", "closing", "all"), default="all")
    args = parser.parse_args(argv)
    result = run(args.out, args.delivery, phase=args.phase)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"] in {"completed_two_model_two_phase_rehearsal",
        "completed_two_model_opening_rehearsal"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
