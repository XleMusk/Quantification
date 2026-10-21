"""Replay saved fixed low-volatility signals with independent raw execution quotes.

Saved scores are reused; target weights, orders, fills and account balances are
always produced anew by the shared Engine/Ledger. No old account is upgraded.
"""
import argparse
import csv
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..report import metrics
from ..symbols import normalize_symbol
from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from .exchange_calendar import _closed_days, load_calendar
from .native_execution_quotes import NUMERIC_FIELDS, load_native_execution_quotes
from .stockdb_action_accounting import load_action_accounting
from .stockdb_controlled_signal_comparison import ControlledFeed
from .stockdb_rolling_low_volatility import _DayRows
from .monthly_timing import month_ends


METHOD = "saved_lowvol_native_whole_quotes_reported_actions_full_exit_replay_v1"
MULTIYEAR_METHOD = "saved_lowvol_native_multiyear_whole_quotes_reported_actions_full_exit_replay_v1"
MULTIYEAR_CALENDAR_METHOD = "continuous_explicit_year_exchange_calendars_v1"


def _sha(path):
    return sha256(Path(path).read_bytes()).hexdigest()


class NativeLowVolFeed:
    def __init__(self, saved, raw, calendar, instruments, accounting, start, end, *, method_id=METHOD):
        self.saved, self.calendar = saved, calendar
        self.symbols, self.instruments = saved.symbols, instruments
        self.action_accounting, self.actions = accounting, accounting.actions
        self.instrument_profiles = {}
        self.manifest = {"replay_only": True, "scope": method_id, "start": start, "end": end,
                         "historical_price_adjustments": accounting.special_references}
        self.rows = _DayRows({day: group.set_index("symbol") for day, group in raw.groupby("date", sort=True)})
        self.volumes = raw.pivot(index="date", columns="symbol", values="volume").reindex(columns=self.symbols)

    def next_day(self, day):
        return self.calendar[self.calendar.index(day) + 1]

    def validate_session(self, state, day):
        ControlledFeed.validate_session(self, state, day)
        held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        pending = state.get("pending_targets")
        required = held | {s for s, weight in pending.items() if weight > 0} if pending is not None else set()
        required.update((state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {}))
        rows = self.rows.get(day, {})
        required.update(s for s in held if rows.get(s, {}).get("isST") == 1)
        previous = self.calendar[self.calendar.index(day) - 1]
        for symbol in sorted(required):
            # A pure valuation or a known current halt needs no tradable volume.
            # A possible opening trade must not turn native null volume into 0.
            if rows.get(symbol, {}).get("tradestatus") != 1:
                continue
            value = self.volumes.at[previous, symbol]
            if not isfinite(value) or value < 0:
                raise ValueError(f"Unknown exact prior execution capacity: {symbol} {day}; previous={previous}")

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.saved.config:
            raise ValueError("Saved low-volatility replay requires its exact config and REPLAY context")
        if day not in self.saved.decisions:
            raise ValueError("Missing exact saved low-volatility decision; no invented NO_CHANGE")
        if day not in self.saved.signals:
            return SignalUnavailable("saved_" + self.saved.decisions[day])
        return SignalFrame.from_legacy(self.saved.signals[day], day, "research:" + self.manifest["scope"], context)


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _reference(path):
    return {"path": str(Path(path).resolve()), "sha256": _sha(path)}


def _multiyear_calendar(calendar_path, prior_calendars, start, end):
    """Join explicit whole-year calendars, without any inferred missing year."""
    if (date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end
            or start >= end or start[:4] == end[:4]):
        raise ValueError("Explicit ordered multi-year replay bounds required")
    paths = [Path(path).resolve() for path in (*prior_calendars, calendar_path)]
    if not prior_calendars or len(paths) != len(set(paths)):
        raise ValueError("Multi-year calendar paths must be explicit and unique")
    records, years = [], []
    for path in paths:
        payload = load_calendar(path)
        if payload is None or type(payload.get("year")) is not int:
            raise ValueError("Exact fixed exchange calendar and year required: " + str(path))
        years.append(payload["year"])
        records.append((path, payload))
    first_year, last_year = int(start[:4]), int(end[:4])
    if (years[-1] != last_year or any(year >= last_year for year in years[:-1])
            or len(years) != len(set(years)) or first_year not in years
            or sorted(years) != list(range(min(years), last_year + 1))):
        raise ValueError("Multi-year calendars require unique continuous years and current calendar matching end year")
    fixed, pins = [], []
    for path, payload in sorted(records, key=lambda item: item[1]["year"]):
        year = payload["year"]
        closed = _closed_days(payload)
        opened = {date.fromisoformat(day) for day in payload.get("open_sessions", [])}
        if any(day.year != year for day in closed | opened):
            raise ValueError("Calendar closure/open-session dates must match their declared year")
        fixed.extend(day.strftime("%Y-%m-%d") for day in pd.date_range(f"{year}-01-01", f"{year}-12-31")
            if day.date() in opened or day.weekday() < 5 and day.date() not in closed)
        pins.append({**_reference(path), "year": year})
    if (start not in fixed or end not in fixed or fixed.index(start) == 0
            or fixed.index(end) + 1 >= len(fixed)):
        raise ValueError("Multi-year replay requires exact bounds, predecessor and calendar successor")
    return fixed, {"method_id": MULTIYEAR_CALENDAR_METHOD, "files": pins,
        "years": sorted(years), "missing_years_inferred": False,
        "session_rule": "explicit_open_sessions_or_weekday_outside_closed_ranges"}


def _unchanged(references):
    if any(_sha(ref["path"]) != ref["sha256"] for ref in references):
        raise ValueError("A pinned native low-volatility continuation input changed")


def _stable_native_proof(value):
    if isinstance(value, dict):
        return {key: _stable_native_proof(item) for key, item in value.items() if key not in {"opened_at", "read_at"}}
    if isinstance(value, list):
        return [_stable_native_proof(item) for item in value]
    return value


def _resume_signals(saved_run, registration, days, fixed):
    """Reuse the old full scope and small cache pins, never scan its chunks."""
    from .native_lowvol_publication import _cache, _rank
    from .saved_lowvol_signals import SavedLowvolSignals
    source = registration["signal_source"]
    if Path(saved_run).resolve() != Path(source["run"]).resolve():
        raise ValueError("Continuation cannot replace the original saved signal run")
    _unchanged(source["pins"])
    cache = _cache(saved_run, days[-1], fixed)
    actual = [{"path": ref["file"], "sha256": ref["sha256"]} for ref in cache["refs"]]
    if (actual != source["pins"] or cache["config"].to_dict() != registration["config"]
            or days[-1] > cache["decision_cache_through"]):
        raise ValueError("Continuation requires the original exact saved decisions, ranks and config")
    symbols = registration["execution_source"]["requested_symbols"]
    if not symbols or symbols != sorted(set(symbols)) or len(symbols) != source["source_symbols"]:
        raise ValueError("Original full execution scope cannot change or become a rank union")
    signals, decisions = {}, {}
    monthly = set(month_ends(fixed, days[-1]))
    for day in days:
        row = cache["decisions"].loc[cache["decisions"].date.eq(day)]
        if len(row) != 1:
            raise ValueError("Missing exact saved continuation decision: " + day)
        reason = row.decision.item()
        if (reason not in {"monthly_ranking", "not_month_end", "252_session_warmup"}
                or (reason != "not_month_end") != (day in monthly)):
            raise ValueError("Saved continuation decision conflicts with its monthly calendar")
        decisions[day] = reason
        if reason == "monthly_ranking":
            ranked = _rank(cache, day)
            rows = cache["ranks"].loc[cache["ranks"].date.eq(day)].set_index("symbol")
            if any(row["symbol"] not in symbols for row in ranked):
                raise ValueError("Saved continuation ranking left the original full scope")
            if ranked:
                signals[day] = [{**row, "volatility": float(rows.loc[row["symbol"], "volatility"]),
                    "momentum": float(rows.loc[row["symbol"], "momentum"])} for row in ranked]
        elif cache["ranks"].date.eq(day).any():
            raise ValueError("A non-ranking continuation decision contains rank rows")
    provenance = {**deepcopy(source), "requested_start": days[0], "requested_end": days[-1],
        "preceding_session": fixed[fixed.index(days[0]) - 1],
        "scope_reused_from_original_registration": True, "snapshot_chunks_rescanned_this_call": False,
        "continuation_read_at": datetime.now(MARKET_TZ).isoformat()}
    return SavedLowvolSignals(cache["config"], list(symbols), signals, decisions,
        provenance["preceding_session"], provenance)


def _strict_source(folder, calendar_ref, fixed):
    from .shortline_native_preopen_forward import _source
    return _source(folder, {"file": calendar_ref["path"], "sha256": calendar_ref["sha256"]},
        fixed, datetime.now(MARKET_TZ))


def _strict_frame(source, symbols):
    frame = source["raw"].loc[source["raw"].symbol.isin(symbols)].copy()
    if not frame.adjustflag.eq("3").all():
        raise ValueError("Strict continuation quotes require unchanged original adjustflag 3")
    for field in NUMERIC_FIELDS:
        frame[field] = pd.to_numeric(frame[field].replace("", None), errors="raise")
    for field in ("tradestatus", "isST"):
        frame[field + "_raw"] = frame[field]
        frame[field] = pd.array([int(value) if value in {"0", "1"} else pd.NA
            for value in frame[field]], dtype="Int8")
    return frame


def _resume_quotes(registration, views, folders, symbols, raw_days, fixed):
    original = registration["execution_source"]
    if [str(Path(p).resolve()) for p in views] != [item["source_view"] for item in original["source_views"]]:
        raise ValueError("Original native view ownership cannot change during continuation")
    prior = [row for row in original["day_sources"] if row["day"] == raw_days[0]]
    if len(prior) != 1:
        raise ValueError("One exact original preceding execution source is required")
    prior = prior[0]
    if prior.get("source_kind") == "strict_dated_close":
        source = _strict_source(prior["source_folder"], registration["calendar"], fixed)
        if source["proof"] != prior["proof"]:
            raise ValueError("The previous strict execution source changed")
        context = _strict_frame(source, symbols)
    else:
        context, proof = load_native_execution_quotes([prior["source_view"]], symbols, [raw_days[0]])
        current = proof["day_sources"][0]
        if any(_stable_native_proof(current[k]) != _stable_native_proof(prior[k])
               for k in ("source_view", "row_group", "source_task", "provenance")):
            raise ValueError("The exact original preceding native execution source changed")
    sources = [_strict_source(folder, registration["calendar"], fixed) for folder in folders]
    if [item["proof"]["day"] for item in sources] != raw_days[1:]:
        raise ValueError("Strict closes must be the complete exact next-session prefix in order")
    frames, dated = [context], []
    for source in sources:
        frame = _strict_frame(source, symbols)
        frames.append(frame)
        dated.append({"day": source["proof"]["day"], "source_kind": "strict_dated_close",
            "source_folder": source["proof"]["source_folder"], "proof": source["proof"],
            "requested_symbols": len(symbols), "observed_rows": len(frame),
            "missing_symbol_rows": len(symbols) - len(frame)})
    raw = pd.concat(frames, ignore_index=True)
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Continuation raw sources overlap exact stock/session ownership")
    provenance = deepcopy(original)
    provenance.update(requested_days=original["requested_days"] + raw_days[1:],
        day_sources=original["day_sources"] + dated,
        observed_rows=original["observed_rows"] + sum(row["observed_rows"] for row in dated),
        missing_stock_day_rows=original["missing_stock_day_rows"] + sum(row["missing_symbol_rows"] for row in dated),
        old_execution_sessions_replayed=False, original_preceding_context_only=raw_days[0],
        strict_recent_original_fields_preserved=True)
    return raw, provenance


def _recent_accounting(event_view, reference_view, extension, **kwargs):
    if extension is None:
        return load_action_accounting(event_view, reference_view, **kwargs)
    from .recent_action_extension import load_extension
    return load_extension(event_view, reference_view, extension, **kwargs)


def _resume(saved_run, views, event_view, reference_view, calendar_path, listing_master,
            start, end, out, resume_from, raw_closes, recent_action_extension):
    from .stockdb_controlled_signal_comparison import _saved_daily
    original, out = Path(resume_from).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Continuation requires a new output directory")
    protected = [original, Path(saved_run).resolve().parent, *[Path(p).resolve() for p in raw_closes],
        *[Path(p).resolve() for p in views]]
    if any(out == p or out in p.parents or p in out.parents for p in protected):
        raise ValueError("Continuation output must not overlap its old account or sources")
    references = [_reference(original / name) for name in ("registration.json", "result.json", "account.json", "daily.csv")]
    registration, result, state = [_json(original / name) for name in ("registration.json", "result.json", "account.json")]
    if registration.get("method_id") == MULTIYEAR_METHOD:
        raise ValueError("Multi-year saved low-volatility continuation is not implemented; no account was resumed")
    if (result.get("schema") != METHOD or result.get("status") != "completed" or result.get("error") is not None
            or result.get("registration") != registration or registration.get("method_id") != METHOD
            or registration.get("execution_admitted") is not False or state.get("broker_binding")
            or result.get("completed_sessions") != result.get("requested_sessions")):
        raise ValueError("Only the exact completed independent native account can be continued")
    calendar_ref, master_ref = _reference(calendar_path), _reference(listing_master)
    if calendar_ref != registration["calendar"] or master_ref != registration["listing_master"]:
        raise ValueError("Continuation cannot replace its calendar or listing metadata")
    calendar = load_calendar(Path(calendar_path))
    closed = _closed_days(calendar)
    fixed = [d.strftime("%Y-%m-%d") for d in pd.date_range(str(calendar["year"]) + "-01-01", str(calendar["year"]) + "-12-31")
        if d.weekday() < 5 and d.date() not in closed]
    last = registration["end"]
    if (start not in fixed or end not in fixed or start > end or fixed[fixed.index(last) + 1] != start
            or fixed.index(end) + 1 >= len(fixed) or state["day"] != last):
        raise ValueError("Continuation must start at the exact next unprocessed session")
    old_days = [day for day in fixed if registration["start"] <= day <= last]
    daily = _saved_daily(original / "daily.csv", result["completed_sessions"])
    context = RunContext(METHOD, TradingMode.REPLAY)
    if ([row["date"] for row in daily] != old_days or len(daily) != result["completed_sessions"]
            or round(daily[-1]["equity"] * 100) != Ledger(state, StrategyConfig(**registration["config"]), context).equity()
            or state.get("full_exit_lifecycle", {}).get("method_id") != FULL_EXIT_LIFECYCLE):
        raise ValueError("Saved continuation account/daily prefix or lifecycle changed")
    days = [day for day in fixed if start <= day <= end]
    saved = _resume_signals(saved_run, registration, days, fixed)
    raw_days = [last] + days
    raw, quote_proof = _resume_quotes(registration, views, raw_closes, saved.symbols, raw_days, fixed)
    accounting = _recent_accounting(event_view, reference_view, recent_action_extension,
        symbols=saved.symbols,
        raw_days=registration["execution_source"]["requested_days"] + days,
        replay_days=old_days + days, price_source="independent_baostock_raw")
    master = pd.read_csv(listing_master, dtype=str, keep_default_na=False)
    instruments = {}
    for row in master.to_dict("records"):
        if row.get("type") == "1":
            symbol = normalize_symbol(row["code"]).vt_symbol
            if symbol in saved.symbols:
                value = {"ipoDate": row.get("ipoDate", ""), "outDate": row.get("outDate", "")}
                if symbol in instruments and instruments[symbol] != value:
                    raise ValueError("Conflicting saved listing metadata")
                instruments[symbol] = value
    if set(instruments) != set(saved.symbols):
        raise ValueError("Original listing metadata no longer covers the frozen full scope")
    feed = NativeLowVolFeed(saved, raw, raw_days + [fixed[fixed.index(end) + 1]],
        instruments, accounting, registration["start"], end)
    _unchanged(references + registration["signal_source"]["pins"] + [calendar_ref, master_ref])
    out.mkdir(parents=True, exist_ok=False)
    count, new_daily, error = len(daily), [], None
    continued = {**deepcopy(registration), "created_at": datetime.now(MARKET_TZ).isoformat(), "end": end,
        "signal_source": saved.provenance, "execution_source": quote_proof, "action_accounting": accounting.provenance,
        "continuation": {"from_directory": str(original), "input_refs": references,
            "new_start": start, "new_end": end, "reused_sessions": count,
            "global_session_index_start": count, "context": context.to_dict(), "new_state_created": False}}
    (out / "registration.json").write_text(json.dumps(continued, ensure_ascii=False, indent=2), encoding="utf-8")
    for offset, day in enumerate(days):
        trial = deepcopy(state)
        try:
            row = advance(trial, feed, day, saved.config, count + offset, context=context,
                full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
        except Exception as exc:
            error = {"day": day, "type": type(exc).__name__, "message": str(exc)}
            (out / "failed_trial.json").write_text(json.dumps(trial, ensure_ascii=False, indent=2), encoding="utf-8")
            break
        state, new_daily = trial, new_daily + [row]
    _unchanged(references + registration["signal_source"]["pins"] + [calendar_ref, master_ref])
    daily += new_daily
    for name, value in (("account.json", state), ("orders.csv", list(state["orders"].values())),
            ("fills.csv", list(state["fills"].values())), ("action_reference_checks.csv", accounting.checks)):
        if name.endswith(".json"):
            (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            pd.DataFrame(value).to_csv(out / name, index=False)
    # Keep all original CSV bytes as the exact prefix; append only new sessions.
    old_csv = (original / "daily.csv").read_bytes()
    (out / "daily.csv").write_bytes(old_csv + (b"" if old_csv.endswith(b"\n") else b"\n"))
    with (original / "daily.csv").open(encoding="utf-8-sig", newline="") as handle:
        columns = next(csv.reader(handle))
    with (out / "daily.csv").open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=columns).writerows(new_daily)
    record = {"schema": METHOD, "status": "incomplete" if error else "completed", "error": error,
        "registration": continued, "requested_sessions": count + len(days), "completed_sessions": len(daily),
        "reused_sessions": count, "newly_completed_sessions": len(new_daily), "global_session_index_start": count,
        "resume_from": str(original), "orders": len(state["orders"]), "fills": len(state["fills"]),
        "metrics": metrics(daily, saved.config.initial_cash) if not error and state["fills"] else None,
        "pending_targets": state.get("pending_targets"),
        "remaining_full_exits": state["full_exit_lifecycle"]["remaining_full_exits"],
        "open_positions": {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)},
        "failed_trial_committed": False, "model_refit": False, "signal_features_recomputed": False,
        "metric_basis": result["metric_basis"], "total_return_verified": False, "returns_reportable": False,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False, "old_account_mutated": False, "new_cash_account_created": False}
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def run(saved_run, views, event_view, reference_view, calendar_path, listing_master, start, end, out,
        *, resume_from=None, raw_closes=(), recent_action_extension=None, prior_calendars=(),
        resume_stopped_multiyear=None, inspect_only=False, frozen_engine_source=None):
    if resume_stopped_multiyear is not None:
        if resume_from is not None or raw_closes or recent_action_extension is not None:
            raise ValueError("Stopped multi-year recovery cannot mix single-year continuation inputs")
        from .native_lowvol_multiyear_resume import resume
        return resume(saved_run, views, event_view, reference_view, calendar_path, listing_master,
            start, end, out, resume_from=resume_stopped_multiyear,
            prior_calendars=prior_calendars, inspect_only=inspect_only,
            frozen_engine_source=frozen_engine_source)
    if frozen_engine_source is not None:
        raise ValueError("Frozen Engine sources require explicit stopped multi-year recovery")
    if inspect_only:
        raise ValueError("Read-only stopped inspection requires --resume-stopped-multiyear")
    if resume_from is not None:
        if prior_calendars:
            raise ValueError("Multi-year saved low-volatility continuation is not implemented; no account was resumed")
        return _resume(saved_run, views, event_view, reference_view, calendar_path, listing_master,
            start, end, out, resume_from, raw_closes, recent_action_extension)
    if raw_closes or recent_action_extension is not None:
        raise ValueError("Strict recent closes/action extensions require explicit completed-account continuation")
    from .saved_lowvol_signals import MULTIYEAR_INCEPTION_CONTEXT, load_saved_lowvol_signals

    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Native low-volatility replay needs a new output; old accounts cannot be upgraded")
    calendar_path, listing_master = Path(calendar_path).resolve(), Path(listing_master).resolve()
    method_id, calendar_proof = METHOD, None
    if prior_calendars:
        fixed, calendar_proof = _multiyear_calendar(calendar_path, prior_calendars, start, end)
        method_id = MULTIYEAR_METHOD
    else:
        if start[:4] != end[:4] or start > end:
            raise ValueError("One explicit ordered replay year required")
        calendar = load_calendar(calendar_path)
        if calendar is None or str(calendar["year"]) != start[:4]:
            raise ValueError("Exact fixed exchange calendar required")
        closed = _closed_days(calendar)
        fixed = [d.strftime("%Y-%m-%d") for d in pd.date_range(start[:4] + "-01-01", start[:4] + "-12-31")
                 if d.weekday() < 5 and d.date() not in closed]
    if start not in fixed or end not in fixed or fixed.index(end) + 1 >= len(fixed):
        raise ValueError("Replay bounds and an exact calendar successor are required")
    days = [day for day in fixed if start <= day <= end]
    saved = (load_saved_lowvol_signals(saved_run, start, end, fixed,
        inception_context=MULTIYEAR_INCEPTION_CONTEXT) if calendar_proof else
        load_saved_lowvol_signals(saved_run, start, end, fixed))
    raw_days = [saved.preceding_session] + days
    raw, quote_proof = load_native_execution_quotes(views, saved.symbols, raw_days)
    if calendar_proof and not raw.date.eq(saved.preceding_session).any():
        raise ValueError("Multi-year inception requires real native rows for the exact calendar predecessor")
    accounting = load_action_accounting(event_view, reference_view, symbols=saved.symbols,
        raw_days=raw_days, replay_days=days, price_source="independent_baostock_raw")
    master = pd.read_csv(listing_master, dtype=str, keep_default_na=False)
    instruments = {}
    for row in master.to_dict("records"):
        if row.get("type") != "1":
            continue
        symbol = normalize_symbol(row["code"]).vt_symbol
        if symbol in saved.symbols:
            value = {"ipoDate": row.get("ipoDate", ""), "outDate": row.get("outDate", "")}
            if symbol in instruments and instruments[symbol] != value:
                raise ValueError("Conflicting saved listing metadata")
            instruments[symbol] = value
    if set(instruments) != set(saved.symbols):
        raise ValueError("Pinned provider listing metadata must cover the full saved symbol scope")
    feed = NativeLowVolFeed(saved, raw, [saved.preceding_session] + days + [fixed[fixed.index(end) + 1]],
                            instruments, accounting, start, end, method_id=method_id)
    out.mkdir(parents=True, exist_ok=False)
    registration = {"method_id": method_id, "created_at": datetime.now().astimezone().isoformat(),
        "start": start, "end": end, "config": saved.config.to_dict(),
        "signal_source": saved.provenance, "execution_source": quote_proof,
        "action_accounting": accounting.provenance,
        "implementation": [{"path": str(path), "sha256": _sha(path)} for path in (
            Path(__file__).resolve(), Path(__file__).with_name("saved_lowvol_signals.py").resolve(),
            Path(__file__).with_name("native_execution_quotes.py").resolve(),
            Path(__file__).with_name("stockdb_action_accounting.py").resolve(),
            Path(__file__).with_name("action_events.py").resolve(),
            Path(__file__).with_name("bulk_feed.py").resolve(),
            Path(__file__).resolve().parents[1] / "engine.py",
            Path(__file__).resolve().parents[1] / "ledger.py")],
        "calendar": {"path": str(calendar_path), "sha256": _sha(calendar_path)},
        "listing_master": {"path": str(listing_master), "sha256": _sha(listing_master)},
        "listing_basis": "provider_metadata_posthoc_not_invented_listing_year",
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE,
        "missing_prior_execution_capacity": "unknown_stops_possible_trade_not_zero",
        "inception": "new_cash_account_no_implicit_previous_year_positions_or_signal",
        "model_refit": False, "signal_features_recomputed": False,
        "actual_historical_publication_verified": False, "research_only": True,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False}
    if calendar_proof:
        registration.update(calendar_method=calendar_proof,
            continuation_supported=False, account_year_boundary_reset=False)
    (out / "registration.json").write_text(json.dumps(registration, ensure_ascii=False, indent=2), encoding="utf-8")
    state, daily, error = new_state(saved.config.initial_cash), [], None
    context = RunContext(method_id, TradingMode.REPLAY)
    for index, day in enumerate(days):
        trial = deepcopy(state)
        try:
            record = advance(trial, feed, day, saved.config, index, context=context,
                             full_exit_lifecycle=FULL_EXIT_LIFECYCLE)
        except Exception as exc:
            error = {"day": day, "type": type(exc).__name__, "message": str(exc)}
            (out / "failed_trial.json").write_text(json.dumps(trial, ensure_ascii=False, indent=2), encoding="utf-8")
            break
        state, daily = trial, daily + [record]
    for name, value in (("account.json", state), ("daily.csv", daily),
                        ("orders.csv", list(state["orders"].values())), ("fills.csv", list(state["fills"].values())),
                        ("action_reference_checks.csv", accounting.checks)):
        if name.endswith(".json"):
            (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            pd.DataFrame(value).to_csv(out / name, index=False)
    record = {"schema": method_id, "status": "incomplete" if error else "completed", "error": error,
        "registration": registration, "requested_sessions": len(days), "completed_sessions": len(daily),
        "orders": len(state["orders"]), "fills": len(state["fills"]),
        "metrics": metrics(daily, saved.config.initial_cash) if not error and state["fills"] else None,
        "pending_targets": state.get("pending_targets"),
        "remaining_full_exits": (state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {}),
        "open_positions": {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items()
                           if sum(lot["qty"] for lot in lots)},
        "failed_trial_committed": False, "model_refit": False, "signal_features_recomputed": False,
        "metric_basis": "native_raw_close_with_reported_action_components_research_diagnostic",
        "total_return_verified": False, "returns_reportable": False,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False, "old_account_mutated": False}
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("saved-run", "action-event-view", "action-reference-view", "calendar", "listing-master", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execution-native-view", action="append", type=Path, required=True)
    parser.add_argument("--prior-calendar", action="append", type=Path, default=[])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--resume-stopped-multiyear", type=Path)
    parser.add_argument("--inspect-stopped-multiyear", action="store_true")
    parser.add_argument("--frozen-engine-source", type=Path)
    parser.add_argument("--raw-close", action="append", type=Path, default=[])
    parser.add_argument("--recent-action-extension", type=Path)
    args = parser.parse_args(argv)
    result = run(args.saved_run, args.execution_native_view, args.action_event_view, args.action_reference_view,
                 args.calendar, args.listing_master, args.start, args.end, args.out,
                 resume_from=args.resume_from, raw_closes=args.raw_close,
                 recent_action_extension=args.recent_action_extension, prior_calendars=args.prior_calendar,
                 resume_stopped_multiyear=args.resume_stopped_multiyear,
                 inspect_only=args.inspect_stopped_multiyear,
                 frozen_engine_source=args.frozen_engine_source)
    display = result if args.inspect_stopped_multiyear else {key: result.get(key) for key in (
        "status", "error", "completed_sessions", "orders", "fills", "metrics",
        "next_session", "first_session_readiness_only") if key in result}
    print(json.dumps(display, ensure_ascii=False))
    return 0 if result["status"] in {"completed", "ready_to_resume_stopped_multiyear"} else 1
