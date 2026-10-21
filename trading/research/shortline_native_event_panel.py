"""Causal nine-feature events from the existing native observed-history view.

Only one dated cross-section and 21 valid observed closes per original code are
kept.  The event population is a new board-limit proxy research method, not the
old StockDB population, an IPO-qualified universe, or historically published
signals.  Future observations can complete labels, never change D features.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import date, datetime
import json
from pathlib import Path
import re
import tempfile

import numpy as np
import pandas as pd

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .native_history_source import NativeHistorySource

SCHEMA = "native_shortline_event_panel_v1"
METHOD_ID = "native_board_limit_proxy_stage123_causal_features_exact_witness_v1"
FEATURE_CONTRACT_ID = "native_active_known_nonST_observed_nine_features_v1"
FEATURES = ("amount_rank_pct", "volume_rank_pct", "market_limitup_rate", "market_down_rate",
    "range_pct", "close_location", "ret_5_observed_rows", "ret_10_observed_rows", "ret_20_observed_rows")
FEATURE_SEMANTICS = {
    "contract_id": FEATURE_CONTRACT_ID,
    "scope": "own_dated_ordinary_SH_SZ_active_known_nonST_finite_positive_OHLCV_amount_rows",
    "ordinary_codes": "sh.60xxxx,sz.00xxxx,sz.30xxxx,sh.688xxx; original codes are never merged",
    "rank": "same_day_average_tie_percentile_rank_in_the_complete_valid_observed_scope",
    "market_limitup_rate": "board_limit_proxy_count_divided_by_valid_observed_scope_count",
    "market_down_rate": "count(close/preclose-1<0)_divided_by_valid_observed_scope_count",
    "range_pct": "(high-low)/native_preclose_fraction_not_percentage_points",
    "close_location": "(close-low)/(high-low); zero_range_is_null_not_zero",
    "observed_returns": "native_close/current_original_code_close_k_valid_observed_rows_ago-1; k=5,10,20; no_fill",
    "history_scope": "same_active_known_nonST_valid_observed_scope; not_exact_session_returns_or_adjusted_returns",
    "board_limit": "main_10pct_chinext_star_20pct; cents_floor(price*100+0.5); no_IPO_qualification_claim",
    "decision_reference": "exact_previous_session_valid_observation_and_abs(native_preclose/previous_close-1)<=0.01",
    "initial_or_interrupted_limit_stage": "unknown_until_an_explicit_valid_nonlimit_reset; never_reset_unknown_to_stage1",
    "unknown_interruptions": "missing_row,ST,suspension,unknown_status,invalid_bar,or_reference_gap_censor_open_run",
    "label": "exact_successor_nonlimit_witness_required_even_if_stage5_already_observed",
    "training_label_end_column": "label_witness_day",
    "latest_membership_scope": "valid_observed_scope_only; source_unknown_and_known_excluded_are_reported_separately",
    "reference_check_proves_absence_of_actions": False,
    "full_market_or_PIT_coverage_claimed": False,
    "old_feature_contract_reused": False,
}
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "current_signal_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "network_accessed": False, "account_accessed": False,
    "orders_generated": False, "ledger_mutated": False, "engine_advanced": False,
    "historical_publication_verified": False, "pit_verified": False,
    "corporate_actions_verified": False, "IPO_qualified": False,
    "old_panels_modified": False, "raw_prices_imputed": False, "status_imputed": False}
_ORDINARY = re.compile(r"(?:sh\.60[0-9]{4}|sz\.00[0-9]{4}|sz\.30[0-9]{4}|sh\.688[0-9]{3})")
_NUMERIC = ("open", "high", "low", "close", "preclose", "volume", "amount", "pctChg")
_LABEL_FIELDS = ("run_max_stage_observed", "run_end_date", "label_status", "label_witness_day",
    "label_witness_source_available_at", "label_available_at", "hit_4", "hit_5")


def _now():
    return datetime.now().astimezone()


def _time(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Native feature/label clocks must retain a timezone")
    return stamp


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Exact canonical source sessions are required")
    return value


def _censor(state, reason):
    for event in state["pending"]:
        event["label_status"] = reason
    state["pending"] = []
    state["stage"] = None


def _new_state():
    return {"last_index": None, "last_close": None, "stage": None,
        "closes": deque(maxlen=21), "pending": []}


def _cross_section(original, day):
    required = {"date", "code", "symbol", "adjustflag", "tradestatus", "isST", *_NUMERIC}
    if not required <= set(original):
        raise ValueError("The original native price, identity and status fields are required")
    if (not original.date.eq(day).all() or original.code.duplicated().any()
            or original.symbol.duplicated().any() or not original.adjustflag.eq("3").all()):
        raise ValueError("Native source date, original identity or unadjusted basis changed")
    ordinary = original.loc[original.code.map(lambda value: isinstance(value, str) and bool(_ORDINARY.fullmatch(value)))].copy()
    expected = ordinary.code.str[3:] + ordinary.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    if not ordinary.symbol.eq(expected).all():
        raise ValueError("Native original code/symbol identities cannot be projected or merged")
    unknown_trade = ordinary.loc[~ordinary.tradestatus.isin(["0", "1"]), "symbol"].tolist()
    unknown_st = ordinary.loc[~ordinary.isST.isin(["0", "1"]), "symbol"].tolist()
    known_halt = ordinary.loc[ordinary.tradestatus.eq("0"), "symbol"].tolist()
    known_st = ordinary.loc[ordinary.isST.eq("1"), "symbol"].tolist()
    candidate = ordinary.loc[ordinary.tradestatus.eq("1") & ordinary.isST.eq("0")].copy()
    for key in _NUMERIC:
        candidate[key] = pd.to_numeric(candidate[key], errors="coerce")
    prices = candidate[["open", "high", "low", "close", "preclose"]]
    valid = (np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
        & np.isfinite(candidate[["volume", "amount"]]).all(axis=1)
        & candidate.volume.gt(0) & candidate.amount.gt(0)
        & candidate.high.ge(prices[["open", "close", "low"]].max(axis=1))
        & candidate.low.le(prices[["open", "close", "high"]].min(axis=1)))
    invalid = candidate.loc[~valid, "symbol"].tolist()
    frame = candidate.loc[valid].copy().sort_values("symbol", kind="stable")
    frame["board"] = frame.code.map(lambda value: "main" if value.startswith(("sh.60", "sz.00")) else "star" if value.startswith("sh.688") else "chinext")
    rate = np.where(frame.board.eq("main"), .10, .20)
    frame["limit_up_proxy"] = np.floor(frame.close * 100 + .5).ge(np.floor(frame.preclose * (1 + rate) * 100 + .5))
    frame["amount_rank_pct"] = frame.amount.rank(method="average", pct=True)
    frame["volume_rank_pct"] = frame.volume.rank(method="average", pct=True)
    n = len(frame)
    derived = frame.close / frame.preclose - 1
    limit_rate = float(frame.limit_up_proxy.sum() / n) if n else None
    down_rate = float(derived.lt(0).sum() / n) if n else None
    frame["market_limitup_rate"], frame["market_down_rate"] = limit_rate, down_rate
    frame["range_pct"] = (frame.high - frame.low) / frame.preclose
    frame["close_location"] = (frame.close - frame.low) / (frame.high - frame.low).replace(0, np.nan)
    scope = {"source_rows": len(original), "ordinary_rows": len(ordinary), "valid_scope_rows": n,
        "unsupported_source_rows": len(original) - len(ordinary),
        "unknown_trade_status_symbols": sorted(unknown_trade), "unknown_ST_symbols": sorted(unknown_st),
        "known_suspended_symbols": sorted(known_halt), "known_ST_symbols": sorted(known_st),
        "invalid_active_bar_symbols": sorted(invalid),
        "market_limitup_rate": limit_rate, "market_down_rate": down_rate}
    return frame, set(ordinary.symbol), scope


def produce(source, *, through=None, observed_at=None):
    """One causal pass over a validated NativeHistorySource-compatible reader."""
    started = _time(observed_at or _now())
    provenance = source.provenance
    source_available = _time(provenance["source_available_at"])
    if source_available > started:
        raise ValueError("The native source was not available at the actual producer read")
    calendar = source.calendar
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("A unique ascending exact source calendar is required")
    for day in calendar:
        _day(day)
    through = calendar[-1] if through is None else _day(through)
    if through not in calendar:
        raise ValueError("Through must be an exact source session, not a next-available date")
    days = calendar[:calendar.index(through) + 1]
    states, events, event_features, daily, unresolved, proofs = {}, [], [], [], [], []
    latest, latest_scope = [], None
    for index, day in enumerate(days):
        original = source.frame(day)
        day_refs = provenance.get("day_sources")
        own_proof = (day_refs.get(day) if isinstance(day_refs, dict) else None)
        if own_proof is None:
            own_proof = original.attrs.get("provenance", {})
        day_available = _time(own_proof.get("source_available_at", source_available.isoformat()))
        if not _time(day + "T15:00:00+08:00") <= day_available <= started:
            raise ValueError("A source component is not a completed close available at the actual read")
        frame, observed, scope = _cross_section(original, day)
        valid_symbols = set(frame.symbol)
        for symbol, state in states.items():
            if symbol not in valid_symbols:
                reason = "right_censored_missing_exact_source_row" if symbol not in observed else "right_censored_inactive_ST_unknown_status_or_invalid_bar"
                _censor(state, reason)
                state["last_index"], state["last_close"] = index, None
        current, excluded, unknown_stage = [], set(observed - valid_symbols), []
        for row in frame.to_dict("records"):
            symbol = row["symbol"]
            state = states.setdefault(symbol, _new_state())
            exact = state["last_index"] == index - 1 and state["last_close"] is not None
            reference = bool(exact and abs(row["preclose"] / state["last_close"] - 1) <= .01)
            prior_stage = state["stage"]
            if not reference:
                _censor(state, "right_censored_exact_reference_unknown_or_incompatible")
            flag = bool(row["limit_up_proxy"])
            if not flag:
                if reference and prior_stage is not None and prior_stage > 0:
                    for event in state["pending"]:
                        event.update(label_status="complete", label_witness_day=day,
                            label_witness_source_available_at=day_available.isoformat(),
                            hit_4=int(event["run_max_stage_observed"] >= 4),
                            hit_5=int(event["run_max_stage_observed"] >= 5))
                state["pending"], state["stage"] = [], 0
                stage, stage_known = 0, True
            elif reference and prior_stage is not None:
                stage, stage_known = prior_stage + 1, True
                state["stage"] = stage
                for event in state["pending"]:
                    event.update(run_max_stage_observed=stage, run_end_date=day)
            else:
                stage, stage_known = None, False
                state["stage"] = None
                unknown_stage.append(symbol)
                unresolved.append({"date": day, "symbol": symbol,
                    "reason": "limit_proxy_observed_but_exact_prior_stage_or_reference_unknown",
                    "stage": None, "not_a_negative_example": True})
            history = state["closes"]
            feature = {"event_id": f"{symbol}|{day}|s{stage}" if stage in (1, 2, 3) else None,
                "symbol": symbol, "date": day, "source_day": day, "board": row["board"],
                "stage": stage, "stage_reference_known": stage_known,
                "limit_up_proxy": flag, "decision_eligible": reference and stage_known,
                "decision_reference_known": reference,
                "decision_exclusion_reason": None if reference and stage_known else "exact_prior_reference_or_stage_unknown",
                **{key: float(row[key]) for key in ("open", "high", "low", "close", "preclose", "volume", "amount")},
                "observed_history_close_count": len(history),
                **{key: float(row[key]) if pd.notna(row[key]) else None for key in FEATURES[:6]},
                **{f"ret_{lag}_observed_rows": row["close"] / history[-lag] - 1 if len(history) >= lag else None for lag in (5, 10, 20)}}
            if not feature["decision_eligible"]:
                excluded.add(symbol)
            current.append(feature)
            if stage in (1, 2, 3):
                frozen = dict(feature)
                event_features.append(frozen)
                event = {**feature, "run_max_stage_observed": stage, "run_end_date": day,
                    "label_status": "right_censored_snapshot_end", "label_witness_day": None,
                    "label_witness_source_available_at": None, "label_available_at": None,
                    "hit_4": None, "hit_5": None}
                events.append(event)
                state["pending"].append(event)
            history.append(float(row["close"]))
            state["last_index"], state["last_close"] = index, float(row["close"])
        latest_scope = {**scope, "stage_membership_scope": FEATURE_SEMANTICS["scope"],
            "stage_membership_complete": not unknown_stage,
            "full_market_stage_membership_complete": False,
            "unknown_stage_symbols": sorted(unknown_stage), "unknown_stage_count": len(unknown_stage),
            "decision_excluded_symbols": sorted(excluded), "decision_excluded_count": len(excluded),
            "previously_observed_missing_symbols": sorted(set(states) - observed),
            "known_stage_eligible_count": sum(row["decision_eligible"] for row in current),
            "stage123_event_count": sum(row["stage"] in (1, 2, 3) for row in current)}
        daily.append({"date": day, **{key: value for key, value in latest_scope.items() if not isinstance(value, list)},
            "unknown_trade_status_count": len(scope["unknown_trade_status_symbols"]),
            "unknown_ST_count": len(scope["unknown_ST_symbols"]),
            "known_suspended_count": len(scope["known_suspended_symbols"]),
            "known_ST_count": len(scope["known_ST_symbols"]),
            "invalid_active_bar_count": len(scope["invalid_active_bar_symbols"])})
        proofs.append({"day": day, "source_session_index": index,
            "row_group": own_proof.get("row_group"),
            "component_id": own_proof.get("component_id"),
            "component_kind": own_proof.get("component_kind", "native_base_view"),
            "source_view": own_proof.get("source_view"),
            "raw_file": own_proof.get("raw_file"), "close_file": own_proof.get("close_file"),
            "source_rows": len(original),
            "source_available_at": own_proof.get("source_available_at", source_available.isoformat())})
        latest = current
    computed = _time(_now())
    if computed < started:
        raise ValueError("The actual native feature completion clock moved backwards")
    for row in event_features + latest:
        row["feature_available_at"] = computed.isoformat()
    for event in events:
        event["feature_available_at"] = computed.isoformat()
        if event["label_status"] == "complete":
            event["label_available_at"] = computed.isoformat()
    return {"events": events, "event_features": event_features, "daily": daily,
        "latest_features": latest, "unresolved_membership": unresolved,
        "latest_scope": latest_scope, "source_proof": {**provenance, "used_source_days": days,
            "day_sources": proofs, "future_row_groups_read_by_producer": False},
        "computed_at": computed.isoformat(), "available_at": computed.isoformat(),
        "started_at": started.isoformat(), "source_day": through, "last_source_day": through}


def build(view, out, *, through=None):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new native shortline panel; old panels are never overwritten")
    with NativeHistorySource(view) as source:
        tables = produce(source, through=through)
    return _write_tables(tables, out)


def build_from_source(source, out, *, through=None):
    """Use an explicitly validated source chain without changing the feature method."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new native shortline panel; old panels are never overwritten")
    return _write_tables(produce(source, through=through), out)


def _write_tables(tables, out):
    events = pd.DataFrame(tables["events"])
    features = pd.DataFrame(tables["event_features"])
    latest = pd.DataFrame(tables["latest_features"])
    if events.empty:
        events = pd.DataFrame(columns=["event_id", "symbol", "date", "stage", *FEATURES, *_LABEL_FIELDS])
        features = pd.DataFrame(columns=["event_id", "symbol", "date", "stage", *FEATURES])
    if latest.empty:
        latest = pd.DataFrame(columns=list(features.columns))
    for field in ("hit_4", "hit_5"):
        events[field] = pd.array(events[field], dtype="Int8")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".panel-", dir=out.parent) as temporary:
        stage = Path(temporary)
        events.to_parquet(stage / "event_panel.parquet", index=False)
        events.to_csv(stage / "event_panel.csv", index=False)
        features.to_csv(stage / "event_features.csv", index=False)
        latest.to_csv(stage / "latest_features.csv", index=False)
        latest.loc[latest.stage.isin([1, 2, 3])].to_csv(stage / "latest_event_features.csv", index=False) if not latest.empty else features.iloc[:0].to_csv(stage / "latest_event_features.csv", index=False)
        pd.DataFrame(tables["daily"]).to_csv(stage / "daily_market_context.csv", index=False)
        pd.DataFrame(tables["unresolved_membership"], columns=["date", "symbol", "reason", "stage", "not_a_negative_example"]).to_csv(stage / "unresolved_membership.csv", index=False)
        counts = Counter(row["label_status"] for row in tables["events"])
        files = {file.name: digest(file) for file in stage.iterdir() if file.is_file()}
        available = _time(_now())
        if available < _time(tables["computed_at"]):
            raise ValueError("Actual output availability precedes the native feature computation")
        result = {"schema": SCHEMA, "method_id": METHOD_ID,
            "status": "completed_native_causal_shortline_research_panel",
            "feature_contract_id": FEATURE_CONTRACT_ID, "feature_semantics": FEATURE_SEMANTICS,
            "features": list(FEATURES), "computed_at": tables["computed_at"], "available_at": available.isoformat(),
            "started_at": tables["started_at"], "source_day": tables["source_day"], "last_source_day": tables["last_source_day"],
            "source_proof": tables["source_proof"], "source_days": tables["source_proof"]["used_source_days"],
            "event_rows": len(events), "event_stage_counts": {str(stage_id): sum(row["stage"] == stage_id for row in tables["events"]) for stage_id in (1, 2, 3)},
            "label_status_counts": dict(counts), "complete_label_rows": counts["complete"],
            "latest_scope": tables["latest_scope"],
            "stage_membership_complete": tables["latest_scope"]["stage_membership_complete"],
            "unknown_stage_symbols": tables["latest_scope"]["unknown_stage_symbols"],
            "latest_stageunknown_count": tables["latest_scope"]["unknown_stage_count"],
            "latest_decisionexcluded_count": tables["latest_scope"]["decision_excluded_count"],
            "full_market_stage_membership_complete": False,
            "training_label_end_column": "label_witness_day", "future_labels_in_feature_files": False,
            "events_path": str(out / "event_panel.parquet"), "event_features_path": str(out / "event_features.csv"),
            "events_sha256": digest(stage / "event_panel.parquet"),
            "latest_features_path": str(out / "latest_features.csv"), "latest_event_features_path": str(out / "latest_event_features.csv"),
            "latest_features_sha256": digest(stage / "latest_features.csv"),
            "unknown_membership_path": str(out / "unresolved_membership.csv"), "daily_context_path": str(out / "daily_market_context.csv"),
            "files": files, **FLAGS}
        write_json(stage / "result.json", result)
        stage.rename(out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    result = build(args.view, args.out, through=args.through)
    print(json.dumps({key: result[key] for key in ("schema", "status", "source_day", "event_rows", "complete_label_rows", "execution_admitted")}
        | {"result": str(args.out.resolve() / "result.json")}, ensure_ascii=False))
    return 0


def chain_main(argv=None):
    from .native_history_chain_source import NativeHistoryChainSource
    parser = argparse.ArgumentParser(description="Causal native features from explicit existing source components")
    parser.add_argument("--base-view", type=Path, required=True)
    parser.add_argument("--extension-view", type=Path, action="append", default=[])
    parser.add_argument("--full-close", type=Path, action="append", default=[])
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--through")
    args = parser.parse_args(argv)
    if args.out.exists():
        raise FileExistsError("Use a new native chain panel directory")
    with NativeHistoryChainSource(args.base_view, args.extension_view, args.full_close,
            calendar=args.calendar) as source:
        result = build_from_source(source, args.out, through=args.through)
    print(json.dumps({key: result[key] for key in
        ("schema", "status", "source_day", "event_rows", "complete_label_rows", "execution_admitted")}
        | {"result": str(args.out.resolve() / "result.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
