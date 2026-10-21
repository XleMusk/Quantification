"""One registered 2024 Donchian control/entry-2ATR pair on the same Engine."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..report import metrics
from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .bulk_feed import BulkResearchFeed
from .bulk_trend import BulkTrendFeed
from .bulk_seat_trend import ID as SOURCE_METHOD, portfolio
from .leader_daily_signals import _calendar_view
from . import trend_atr_stop_policy as stops


METHOD_ID = "donchian20_10_entry_fixed_2atr_stop_paired2024_v1"
ARMS = ("matched_control", "entry_2atr_stop")
START, END = "2024-01-02", "2024-12-31"
SOURCE_ID = "20261005_054954_83c0825b"
RULE_SOURCE = "https://github.com/vnpy/vnpy_ctastrategy/blob/6ef76981624bf55b2ea978f8587f74d633aafc72/vnpy_ctastrategy/strategies/turtle_signal_strategy.py"
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "returns_reportable": False,
    "independent_holdout_claimed": False, "historical_signal_publication_verified": False,
    "model_refit": False, "future_labels_used_for_score": False, "network_accessed": False,
    "user_account_mutated": False, "pit_verified": False, "corporate_actions_verified": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path)}


def _checked(reference):
    path = Path(reference["path"]).resolve()
    if _pin(path) != reference:
        raise ValueError("Registered source or implementation changed: " + str(path))
    return path


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(member) for member in value]
    if hasattr(value, "item"):
        return _clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(_clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def _spec():
    return {"id": METHOD_ID, "arms": list(ARMS), "start": START, "end": END,
        "decision_sessions": 242, "tail_sessions": 6, "config": portfolio().to_dict(),
        "source_method": SOURCE_METHOD, "source_run_id": SOURCE_ID, "source_rule": RULE_SOURCE,
        "fixed_signal": "original_BulkTrendFeed_prior_high20_low10_Wilder_ATR20_ranking_unchanged",
        "only_new_mechanism": deepcopy(stops.SPEC),
        "capacity": "original_five_holdings_retain100_daily_target_resizing_and_costs_no_ATR_sizing_no_pyramiding",
        "entry_anchor": "actual_continuous_holding_first_fill_and_exact_previous_order_session_N",
        "tail": "tail1_open_consumes_Dec31_tail1_4_close_NO_CHANGE_tail5_empty_FULL_tail6_open_then_NO_CHANGE",
        "study_end_boundary": "exact_tail5_empty_original_frame_is_independent_of_stop_anchor_unknowns_risk_and_execution_guards_unchanged",
        "metrics": "whole248_only_all_committed_has_fills_flat_no_positive_pending_no_error_otherwise_null",
        "main_boundary": "complete242_may_show_marked_NAV_not_closed_total_return",
        "source_boundary": "same_posthoc_adjusted_raw_action_pair_listing_alias_limits_no_unknown_repair",
        "adaptation": "long_only_close_confirmation_next_open_sticky_exit_and_same_regime_veto_not_exact_Turtle",
        "control": "fresh_same_current_Engine_account_not_old241_saved_returns", **FLAGS}


def _runtime():
    folder = Path(__file__).resolve().parent
    paths = [Path(__file__), Path(stops.__file__), folder / "bulk_feed.py", folder / "bulk_trend.py",
        folder / "bulk_seat_trend.py", folder / "seat_trend.py", folder / "leader_type_risk_exit.py"]
    paths += [folder.parent / (name + ".py") for name in
              ("engine", "ledger", "contracts", "rules", "simulated_broker", "config", "report")]
    return [_pin(path) for path in paths]


def _separate(out, refs):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new exclusive paired research directory")
    for reference in refs:
        parent = Path(reference["path"]).resolve().parent
        if out == parent or out in parent.parents or parent in out.parents:
            raise ValueError("Outputs must stay outside original sources, inputs and accounts")
    return out


def _metadata(source_run, calendar_paths):
    source_run = Path(source_run).resolve()
    source = _read(source_run)
    if (source.get("id") != SOURCE_ID or source.get("research_family") != SOURCE_METHOD
            or source.get("status") != "completed_seat_bulk_diagnostic"
            or source.get("execution_admitted") is not False or source["protocol"]["portfolio"] != portfolio().to_dict()):
        raise ValueError("Use the exact completed full-source Donchian run and original portfolio")
    inputs = {key: str(Path(source["protocol"]["stock_inputs"][key]).resolve())
              for key in ("price_view", "event_view", "reference_view", "listing_master")}
    price, event, reference = [_read(Path(inputs[key]) / "result.json")
                               for key in ("price_view", "event_view", "reference_view")]
    if (price.get("status") != "research_reconstruction_completed"
            or event.get("status") != "research_action_events_projected"
            or reference.get("status") != "research_reference_presence_diagnostic"
            or event.get("unqueried") != 0 or Path(event["raw_view"]).resolve() != Path(price["raw_view"]).resolve()
            or Path(reference["price_view"]).resolve() != Path(inputs["price_view"])
            or Path(reference["event_view"]).resolve() != Path(inputs["event_view"])):
        raise ValueError("The original aligned complete-query source pair is required; no newer pair substitution")
    refs = [_pin(source_run), *[_pin(Path(inputs[key]) / "result.json")
            for key in ("price_view", "event_view", "reference_view")], _pin(inputs["listing_master"])]
    event_folder = Path(inputs["event_view"])
    source_calendar = event_folder / "source_input_calendar.csv"
    source_days = pd.read_csv(source_calendar, dtype=str).date.tolist()
    if source_days != sorted(set(source_days)):
        raise ValueError("Original source calendar must be exact, distinct and ordered")
    refs += [_pin(source_calendar), _pin(event_folder / "source_input_universe.csv"),
             _pin(Path(inputs["reference_view"]) / "reviewed_special_references.json")]
    sessions = []
    if len(calendar_paths) != 2:
        raise ValueError("Exactly the saved official 2024 and 2025 calendars are required")
    for year, path in zip((2024, 2025), calendar_paths):
        payload = _read(path)
        dates, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
        if (payload.get("year") != year or status != "pinned_static_exchange_calendar"
                or payload.get("source_status") != "official_static_schedule_snapshot"):
            raise ValueError("Explicit saved official SSE calendars are required")
        refs.append(_pin(path))
        sessions.extend(dates)
    decisions = [day for day in sessions if START <= day <= END]
    tails = [day for day in sessions if day > END][:6]
    if len(decisions) != 242 or decisions[0] != START or decisions[-1] != END or len(tails) != 6:
        raise ValueError("Keep all 242 original 2024 decisions and six exact following exchange sessions")
    days = decisions + tails
    if ([day for day in source_days if START <= day <= tails[-1]] != days
            or source_days[-1] < tails[-1] or source_days.index(START) == 0):
        raise ValueError("Exact source/exchange coverage or the pre-start quote context differs")
    raw_folder = Path(price["raw_view"])
    raw_meta = _read(raw_folder / "result.json")
    refs.append(_pin(raw_folder / "result.json"))
    payloads = [{"path": str(Path(inputs["price_view"]) / price["canonical_panel_file"]),
                 "sha256": price["canonical_panel_sha256"]},
                {"path": str(raw_folder / raw_meta["panel_file"]), "sha256": raw_meta["panel_sha256"]}]
    for folder, metadata, name in ((event_folder, event, "canonical_actions.parquet"),
            (Path(inputs["reference_view"]), reference, "raw_reference_changes.parquet")):
        payloads.append({"path": str(folder / name), "sha256": metadata["files"][name]})
    if any(not Path(pin["path"]).is_file() for pin in payloads):
        raise ValueError("A declared immutable original source payload is absent")
    return {"stock_inputs": inputs, "source_refs": refs, "payload_refs": payloads,
        "decision_days": decisions, "tail_days": tails, "days": days,
        "source_start": source_days[0], "source_cutoff": source_days[-1],
        "source_calendar": str(source_calendar), "payload_hashes_verified_at_registration": False}


def register(source_run, out, *, calendars=None):
    paths = calendars or [DATA_ROOT / "research/calendar" / f"cn_a_share_{year}_sse_v1.json" for year in (2024, 2025)]
    metadata = _metadata(source_run, list(paths))
    refs = metadata["source_refs"] + metadata["payload_refs"]
    folder = _separate(out, refs)
    record = {"schema": METHOD_ID + "_registration", "registered_at": _now(), "spec": _spec(),
        "source": metadata, "code_refs": _runtime(), "engine_advanced": False,
        "accounts_created": False, "prices_or_ATR_loaded": False, **FLAGS}
    folder.mkdir(parents=True, exist_ok=False)
    _save(folder / "registration.json", record)
    return record


def _registration(path):
    record = _read(path)
    if record.get("schema") != METHOD_ID + "_registration" or record.get("spec") != _spec() or record["code_refs"] != _runtime():
        raise ValueError("Registered pair rules or current implementation changed")
    for reference in record["source"]["source_refs"]:
        _checked(reference)
    return record


class TrendSnapshotProvider:
    def __init__(self, trend, price_basis_id):
        self.trend, self.price_basis_id = trend, price_basis_id
        self.columns = {symbol: index for index, symbol in enumerate(trend.symbols)}

    def snapshot(self, day, symbol):
        base = self.trend.base
        if day not in base.day_index or symbol not in self.columns:
            return None
        i, j = base.day_index[day], self.columns[symbol]
        close = float(base.closes.iloc[i, j])
        raw_close = (base.rows.get(day, {}).get(symbol) or {}).get("close")
        factor = (close / raw_close if isinstance(raw_close, (int, float)) and math.isfinite(raw_close)
                  and raw_close > 0 and math.isfinite(close) and close > 0 else None)
        return {"date": day, "price_basis_id": self.price_basis_id, "price_unit": "CNY_per_share",
            "close": close if math.isfinite(close) else None,
            "atr": float(self.trend.trend["atr"][i, j]), "raw_to_signal_factor": factor,
            "regime": int(self.trend.trend["regime"][i, j]),
            "trend_active": bool(self.trend.trend["trend_active"][i, j]),
            "source": "same_saved_BulkTrendFeed_arrays_and_exact_D_raw_close:" + day}


class _WindowFeed:
    def __init__(self, trend, tail_days):
        self.base, self.tail_days = trend, list(tail_days)

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        if day <= END:
            return self.base.signal_frame(day, config, context)
        if day == self.tail_days[4]:
            return SignalFrame.from_legacy([], day, "research:" + METHOD_ID + ":study_end", context)
        return SignalUnavailable("research_tail_no_new_entries")


class _StudyStopFeed(stops.AtrStopFeed):
    """The shared study-end liquidation is independent of the stop assessment."""
    def close_signal_frame(self, day, config, context, original, book):
        if (day == self.base.tail_days[4] and isinstance(original, SignalFrame)
                and original.source == "research:" + METHOD_ID + ":study_end"
                and not original.signals):
            return original
        return super().close_signal_frame(day, config, context, original, book)


def _fresh_state(base, config, start):
    state = new_state(config.initial_cash)
    # Same cash-only inception fact as experiment.replay, not old entitlements.
    for reference in base.manifest.get("historical_price_adjustments", []):
        registration = reference.get("registration_date")
        if (reference["kind"] in {"rights_issue", "restructuring_holder_distribution"}
                and registration and registration < start):
            prefix = "rights:" if reference["kind"] == "rights_issue" else ""
            state["entitlements"][f"{prefix}{reference['symbol']}:{reference['date']}"] = 0
    return state


def _case(arm, trend, provider, registration, out):
    config, source = portfolio(), registration["source"]
    state, daily, failure = _fresh_state(trend, config, source["days"][0]), [], None
    context = RunContext(METHOD_ID + ":" + arm + ":" + provider.price_basis_id, TradingMode.REPLAY)
    window = _WindowFeed(trend, source["tail_days"])
    policy = stops.AtrEntryStopPolicy(provider) if arm == "entry_2atr_stop" else None
    feed = _StudyStopFeed(window, policy) if policy else window
    destination = out / arm
    destination.mkdir(exist_ok=False)
    main = None
    for index, day in enumerate(source["days"]):
        trial = deepcopy(state)
        try:
            row = advance(trial, feed, day, config, index, context=context, replay_risk_exit_policy=policy)
        except (ValueError, KeyError) as error:
            failure = {"day": day, "error": type(error).__name__ + ": " + str(error), "session_not_committed": True}
            break
        state = trial
        daily.append(row)
        if day == END:
            main = {"sessions_committed": len(daily), "marked_equity": row["equity"], "cash": row["cash"],
                "residual_positions": {s: sum(l["qty"] for l in lots) for s, lots in state["positions"].items() if sum(l["qty"] for l in lots)},
                "closed_total_return": None, "marked_NAV_not_closed_total_return": True}
    residual = {s: sum(l["qty"] for l in lots) for s, lots in state["positions"].items() if sum(l["qty"] for l in lots)}
    positive_pending = {s: w for s, w in (state.get("pending_targets") or {}).items() if w > 0}
    complete = bool(len(daily) == len(source["days"]) and state["fills"] and not failure and not residual
                    and not positive_pending and state.get("pending_risk_exits") is None)
    blocked = {}
    for event in state["events"]:
        if event["kind"] in {"order_blocked", "rejected", "rebalance_deferred"}:
            reason = event.get("reason", "unknown")
            blocked[reason] = blocked.get(reason, 0) + 1
    _save(destination / "account.json", state)
    pd.DataFrame([{key: value for key, value in row.items() if key != "events"} for row in daily]).to_csv(destination / "daily.csv", index=False)
    pd.DataFrame(state["orders"].values()).to_csv(destination / "orders.csv", index=False)
    pd.DataFrame(state["fills"].values()).to_csv(destination / "fills.csv", index=False)
    result = {"arm": arm, "context": context.to_dict(), "status": "completed_flat_account" if complete else "partial_or_unclosed_account",
        "complete_PnL_available": complete, "completed_sessions": len(daily), "expected_sessions": len(source["days"]),
        "last_committed_day": state["day"], "blocking_evidence": failure, "metrics": metrics(daily, config.initial_cash) if complete else None,
        "parent_orders": len(state["orders"]), "fill_callbacks": len(state["fills"]),
        "fees": sum(row["fees"] for row in daily) if daily else None, "blocked_reasons": blocked,
        "residual_positions": residual, "pending_targets": state.get("pending_targets"),
        "pending_risk_exits": state.get("pending_risk_exits"), "main_window": main,
        "account": _pin(destination / "account.json"), "daily": _pin(destination / "daily.csv"),
        "engine_advanced": bool(daily), "accounts_created": True, "research_ledger_mutated": bool(daily),
        "paper_orders_generated": bool(state["orders"]), **FLAGS}
    _save(destination / "result.json", result)
    return result


def run(registration, out):
    path = Path(registration).resolve()
    record = _registration(path)
    folder = _separate(out, record["source"]["source_refs"] + record["source"]["payload_refs"] + [_pin(path)])
    # Each actual immutable price/action payload is verified once, not per arm.
    for reference in record["source"]["payload_refs"]:
        _checked(reference)
    folder.mkdir(parents=True, exist_ok=False)
    result = {"schema": METHOD_ID + "_result", "method_id": METHOD_ID, "started_at": _now(),
        "registration": _pin(path), "spec": deepcopy(record["spec"]), "results": [], **FLAGS}
    base = None
    try:
        base = BulkResearchFeed(**record["source"]["stock_inputs"])
        if [day for day in base.calendar if START <= day <= record["source"]["tail_days"][-1]] != record["source"]["days"]:
            raise ValueError("Loaded source no longer matches the registered exact paired calendar")
        trend = BulkTrendFeed(base)
        provider = TrendSnapshotProvider(trend, "registered_adjusted_source:" + result["registration"]["sha256"])
        result["source_manifest"] = deepcopy(base.manifest)
        result["trend_checks"] = deepcopy(trend.data_checks)
        for arm in ARMS:
            value = _case(arm, trend, provider, record, folder)
            result["results"].append(value)
            print(json.dumps({key: value[key] for key in ("arm", "status", "completed_sessions")}), flush=True)
        result["status"] = ("completed_two_flat_research_accounts" if all(case["complete_PnL_available"] for case in result["results"])
                            else "partial_paired_research_accounts")
    except (ValueError, KeyError) as error:
        result.update(status="failed_preparation_or_saved_output", error=type(error).__name__ + ": " + str(error))
    finally:
        if base is not None:
            base.close()
        result.update(completed_at=_now(), engine_advanced=any(case["engine_advanced"] for case in result["results"]),
            accounts_created=any(case["accounts_created"] for case in result["results"]),
            all_cases_retained=len(result["results"]) == 2)
        _save(folder / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--source-run", required=True)
    register_parser.add_argument("--calendar", action="append")
    register_parser.add_argument("--out", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--registration", required=True)
    run_parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = (register(args.source_run, args.out, calendars=args.calendar) if args.command == "register"
              else run(args.registration, args.out))
    print(json.dumps({key: result.get(key) for key in ("schema", "status", "registered_at", "completed_at")}, ensure_ascii=False))
    return 1 if str(result.get("status", "")).startswith("failed") else 0
