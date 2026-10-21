"""Observed native history -> unchanged eight-type rules -> original 32 Ledgers.

This separate historical contract never fabricates recent complete-preview or
dated-roster proofs.  Provider listing dates are an explicit posthoc research
assumption; their actual availability clocks are retained rather than backdated.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ
from ..rules import stock_board
from .baostock_daily_slice import digest
from .instrument_master import load_master
from .ipo_qualification import IpoQualification, _time
from .leader_daily_signals import _calendar_view, _features
from .leader_eight_type_inputs import _old_type, OLD_TYPES
from .leader_eight_type_panel_replay import TYPE_IDS, PROTOCOL, replay_eight_types
from .leader_environment import _qualified, _rebound_paths, _archetypes, _clean
from .leader_missing_type_candidates import _eligibility, _turn, RULE_SPEC
from .native_history_source import NativeHistorySource, _day
from .leader_historical_turnover_inputs import UNIT_EVIDENCE
from .stockdb_action_accounting import load_action_accounting
from .leader_historical_execution_policy import HistoricalExecutionFactory


INPUT_SCHEMA = "leader_eight_type_observed_native_historical_input_v1"
METHOD_ID = "fixed_eight_types_observed_native_posthoc_listing_metadata_research_v1"
LISTING_METHOD = "posthoc_listing_metadata_preserved_actual_clock_v1"
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "model_refit": False,
    "future_labels_used_for_score": False, "historical_signal_publication_verified": False,
    "historical_dated_roster_verified": False, "strict_full_close_admitted": False,
    "pit_verified": False, "corporate_actions_verified": False, "returns_reportable": False,
    "provider_contacted": False, "old_publications_modified": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Historical research metadata must be a JSON object")
    return value


def _save(path, value):
    Path(path).write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class PosthocListingQualification(IpoQualification):
    """Different explicit method, leaving the original qualification unchanged."""
    assessment_calendar: tuple = ()

    def _assess(self, symbol, day, *, decision_as_of, historical_feature):
        requested = decision_as_of or day + "T15:05:00+08:00"
        # Invoke the existing first-five/calendar rules using the actual late
        # observation clock.  No record.available_at is changed to historical D.
        assessment = IpoQualification(self.records, self.assessment_calendar or self.calendar, self.verification)
        result = assessment._assess(symbol, day,
            decision_as_of=self.verification["posthoc_observed_at"], historical_feature=True)
        record = self.records.get(symbol, {})
        prior = record.get("native_prior_listing_sessions", [])
        if (result["engine_block_reason"] == "listing_calendar_incomplete"
                and record.get("ipo_date", "9999") < assessment.calendar[0]
                and len([date for date in prior if date < day]) >= 5):
            # Five distinct actually observed traded bars after the reported
            # IPO prove that this later session is beyond its first five.
            # This is a lower bound, not an invented prior-year calendar/ordinal.
            result.update(engine_block_reason=None, ipo_window_feature_support=True,
                ipo_feature_exclusion_reason=None, listing_session_ordinal_lower_bound=6,
                lower_bound_basis="five_explicit_prior_native_traded_sessions_after_reported_IPO",
                native_prior_listing_sessions=prior)
        out = record.get("reported_out_date")
        if out and day >= out:
            result.update(engine_block_reason="posthoc_reported_outside_listing_period", ipo_window_feature_support=False)
        result.update(qualification_method=LISTING_METHOD,
            requested_historical_cutoff=requested, historical_cutoff_knowledge_verified=False,
            posthoc_listing_metadata_used=True, original_available_at=record.get("available_at"),
            historical_publication_archive_verified=False, pit_verified=False)
        return result


def _calendar_prefix(current, prior_paths, *, observed_at):
    """Explicit official prior years; never infer trading days from price rows."""
    paths = [Path(path).resolve() for path in prior_paths]
    if len(paths) != len(set(paths)):
        raise ValueError("Prior calendar paths must be distinct")
    years, prior_sessions, proofs = [], [], []
    for path in paths:
        payload = _read(path)
        year = int(payload["year"])
        sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
        host = urlparse(payload.get("source_url", "")).hostname or ""
        if (status != "pinned_static_exchange_calendar" or not sessions
                or payload.get("source_status") != "official_static_schedule_snapshot"
                or not (host == "sse.com.cn" or host.endswith(".sse.com.cn"))
                or not payload.get("source_published") or year >= int(current[0][:4])):
            raise ValueError("Explicit completed official SSE prior-year schedules required")
        for clock in ("retrieved_at", "created_at", "actual_retrieved_at"):
            if payload.get(clock) is not None and _time(payload[clock]) > _time(observed_at):
                raise ValueError("Prior calendar source clock is after actual input recording")
        notice, receipt = Path(payload["source_file"]).resolve(), Path(payload["source_receipt"]).resolve()
        if digest(notice) != payload["source_sha256"]:
            raise ValueError("Prior official calendar notice bytes changed")
        years.append(year)
        prior_sessions.extend(sessions)
        proofs.append({"path": str(path), "sha256": digest(path), "year": year,
            "source_url": payload["source_url"], "source_published": payload["source_published"],
            "source_status": payload["source_status"], "actual_read_at": observed_at,
            "source_file": str(notice), "source_sha256": payload["source_sha256"],
            "source_receipt": str(receipt), "source_receipt_sha256": digest(receipt),
            "source_retrieved_at": payload.get("retrieved_at", payload.get("actual_retrieved_at")),
            "source_created_at": payload.get("created_at"), "old_availability_clock_modified": False})
    if paths and (years != list(range(years[0], int(current[0][:4])))
            or prior_sessions != sorted(set(prior_sessions))):
        raise ValueError("Prior calendars must be contiguous ascending complete years ending immediately before current year")
    return tuple(prior_sessions) + tuple(current), proofs


def load_posthoc_listing(master_run, calendar_path, *, observed_at, prior_calendars=()):
    path, calendar_path = Path(master_run).resolve(), Path(calendar_path).resolve()
    saved = _read(path)
    master = path.parent / saved["master_file"]
    if (saved.get("schema") != "instrument_master_v1" or saved.get("execution_admitted") is not False
            or digest(master) != saved.get("master_sha256")
            or _time(saved["created_at"]) > _time(observed_at)):
        raise ValueError("Unchanged actual provider research master required")
    rows = load_master(master)
    if len(rows) != saved["instrument_count"]:
        raise ValueError("Original provider master count changed")
    payload = _read(calendar_path)
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Pinned full-year research calendar required")
    records = {}
    for row in rows:
        symbol = row["canonical_symbol"]
        try:
            board = stock_board(symbol)
        except ValueError:
            continue
        ipo, out = row["provider_ipo_date"], row["provider_out_date"]
        if not ipo:
            continue  # Missing IPO is unknown, never first-bar/2000 substitution.
        _day(ipo)
        if out:
            _day(out)
        records[symbol] = {"ipo_date": ipo, "reported_out_date": out,
            "available_at": saved["created_at"], "board": board,
            "source_basis": "saved_retrieval_time_provider_listing_dates_posthoc_not_historical_PIT"}
    proof = {"method_id": LISTING_METHOD, "master_run": str(path), "master_run_sha256": digest(path),
        "master_file": str(master), "master_sha256": saved["master_sha256"],
        "original_available_at": saved["created_at"], "posthoc_observed_at": observed_at,
        "calendar": str(calendar_path), "calendar_sha256": digest(calendar_path),
        "original_availability_clocks_backdated": False, **FLAGS}
    if prior_calendars:
        full, prior = _calendar_prefix(sessions, prior_calendars, observed_at=observed_at)
        proof.update(prior_calendars=prior,
            assessment_calendar_scope="explicit_prior_years_plus_unchanged_current_exchange_calendar",
            execution_calendar_contract_unchanged=True)
        return PosthocListingQualification(records, tuple(sessions), proof, full)
    return PosthocListingQualification(records, tuple(sessions), proof)


def with_prior_listing_witness(qualification, witness, start):
    records = deepcopy(qualification.records)
    for symbol, days in witness.items():
        if (symbol not in records or days != sorted(set(days)) or len(days) != 5
                or any(_day(day) >= start or day < records[symbol]["ipo_date"] for day in days)):
            raise ValueError("Listing lower bounds require exactly five explicit earlier native sessions")
        records[symbol]["native_prior_listing_sessions"] = list(days)
    encoded = json.dumps(witness, sort_keys=True, separators=(",", ":")).encode()
    verification = {**qualification.verification,
        "native_prior_listing_witness_sha256": sha256(encoded).hexdigest(),
        "native_prior_listing_witness_basis": "five_observed_status1_positive_close_bars_not_synthetic_calendar",
        "native_prior_listing_witness_before": start}
    return PosthocListingQualification(records, qualification.calendar, verification, qualification.assessment_calendar)


def _physical_native(raw):
    """Original raw reference/status retained; reuse only fixed feature formulas."""
    frame = raw.copy()
    frame["symbol"] = frame.code.str[3:] + np.where(frame.code.str.startswith("sh."), ".SSE", ".SZSE")
    frame["board"] = frame.symbol.map(stock_board)
    for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"):
        frame[field] = pd.to_numeric(frame[field].replace("", None), errors="raise")
    frame["pct_chg"] = frame.pctChg
    frame["preclose_for_limits"] = frame.preclose  # Exact native field, not a prior-close repair.
    frame["is_st_unknown"] = ~frame.isST.isin(["0", "1"])
    frame["is_st"] = frame.isST.eq("1")
    numeric = ["open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg"]
    valid = np.isfinite(frame[numeric]).all(axis=1) & ~frame.is_st_unknown
    valid &= frame[["open", "high", "low", "close", "preclose"]].gt(0).all(axis=1)
    valid &= frame.volume.ge(0) & frame.amount.ge(0) & frame.tradestatus.isin(["0", "1"])
    valid &= frame.high.ge(frame[["open", "low", "close"]].max(axis=1))
    valid &= frame.low.le(frame[["open", "high", "close"]].min(axis=1))
    frame["source_row_valid"] = valid
    frame["active_non_st"] = valid & frame.tradestatus.eq("1") & ~frame.is_st & frame.volume.gt(0)
    return frame.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def _three_types(day, scope, current, views, previous, batches, observed_at):
    items = []
    for identity in ("cross_regime_candidate", "sector_catchup_leader", "turnover_leader"):
        candidates, unknown, exclusions, missing = [], [], {}, Counter()
        for symbol in scope:
            state, reason, row = _eligibility(current, symbol)
            evidence, clocks = {}, []
            if state is None:
                if identity == "turnover_leader":
                    state, reason, evidence, clocks = _turn(symbol, row, day, observed_at, views, previous, batches)
                else:
                    state, reason = "unknown", ("pre_episode_identity_and_dated_sector_evidence_unavailable"
                        if identity == "cross_regime_candidate" else "prior_sector_leader_identity_and_dated_membership_unavailable")
            if state == "unknown":
                unknown.append(symbol); missing[reason] += 1
            elif state == "not_matched":
                exclusions[symbol] = reason
            else:
                candidates.append({"date": day, "source_day": day, "symbol": symbol,
                    "amount_cny": float(row["amount"]), "score": float(row["amount"]),
                    "available_at": observed_at, "setup_low": row["low"], "structure_high": row["high"],
                    "evidence": evidence, "invalidation": ["reference_chain_unknown_or_incompatible",
                        "original_structure_broken", "required_sector_or_turnover_evidence_lost"], "confirmed_leader": False})
        candidates.sort(key=lambda row: (-row["amount_cny"], row["symbol"]))
        items.append({"id": identity, "status": "matched" if candidates else "unknown" if unknown else "not_matched",
            "candidates": candidates, "candidate_count": len(candidates), "unresolved_symbols": unknown,
            "scope_complete": not unknown, "known_exclusions": exclusions, "missing_evidence": dict(missing),
            "scope_member_count": len(scope), "source_method": RULE_SPEC["id"], **FLAGS})
    return items


def derive_panels(raw_history, source_days, scope, calendar_payload, qualification, *, observed_at,
                  feature_qualification=None, rolling_observation_cache=False):
    """Pure causal producer for observed rows; no complete-preview disguise."""
    sessions, previous, status = _calendar_view(calendar_payload,
        f"{calendar_payload['year']}-01-01", f"{calendar_payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar" or list(qualification.calendar) != sessions:
        raise ValueError("Historical features and listing method require the same exact calendar")
    feature_sessions = sessions
    if feature_qualification is not None:
        feature_sessions = list(feature_qualification.calendar)
        if (tuple(feature_sessions) != qualification.assessment_calendar
                or feature_qualification.records != qualification.records
                or [day for day in feature_sessions if day >= sessions[0]] != sessions
                or feature_sessions != sorted(set(feature_sessions))):
            raise ValueError("Feature listing facts must use the explicit fixed calendar prefix and same original records")
        previous = {day: feature_sessions[index - 1] if index else None for index, day in enumerate(feature_sessions)}
    feature_qualification = feature_qualification or qualification
    physical = _physical_native(raw_history)
    features = _features(physical, previous)
    raw_by_day = {day: rows.copy() for day, rows in raw_history.groupby("date", sort=False)}
    qualified, views, batches, panels = {}, {}, {}, {}
    for day in sorted(raw_by_day):
        if day not in feature_sessions:
            continue  # Earlier-year price warmup is not a fabricated 2026 panel.
        current_features = features.loc[features.date.eq(day)].copy()
        rows = _qualified(current_features, feature_qualification, feature_qualification_verified=False)
        for row in rows:
            proof = row["ipo_qualification"]
            row["listing_qualification_status"] = ("supported" if row["ipo_feature_support"] else
                "known_listing_window_excluded" if proof["engine_block_reason"] in {
                    "unsupported_first_five_listing_sessions", "not_yet_listed", "posthoc_reported_outside_listing_period"}
                else "unknown")
        prior = {date: values for date, values in qualified.items() if date in {previous.get(day), previous.get(previous.get(day))}}
        _rebound_paths(rows, prior, previous)
        # The existing risk consumer accepts explicit qualified fields; identify
        # this late listing method instead of pretending the strict method ran.
        for row in rows:
            row.update(listing_feature_policy_verified=True, listing_feature_policy_method=LISTING_METHOD,
                qualification_historical_clock_verified=False)
        qualified[day] = rows
        feature_map = {row["symbol"]: row for row in rows}
        ordinary = raw_by_day[day].copy()
        ordinary["symbol"] = ordinary.code.str[3:] + np.where(ordinary.code.str.startswith("sh."), ".SSE", ".SZSE")
        view = {"features": feature_map, "ordinary": ordinary.set_index("symbol").to_dict("index"),
            "available_at": observed_at}
        views[day] = view
        batches[day] = {"indexed": {row["symbol"]: {"turn_percent": row["turn"]}
            for row in ordinary.to_dict("records")}, "available_at": observed_at}
        if rolling_observation_cache:
            keep = {day, previous.get(day), previous.get(previous.get(day))}
            for cache in (qualified, views, batches):
                for old_day in list(cache):
                    if old_day not in keep:
                        del cache[old_day]
        if day not in source_days:
            continue
        if not rows:
            raise ValueError(f"No trustworthy active native denominator: {day}; do not invent zero breadth")
        archetypes, _ = _archetypes(rows)
        feature_frame = pd.DataFrame(rows)
        old = {item["id"]: _old_type(item, feature_frame, raw_by_day[day], scope)
            for item in archetypes if item["id"] in OLD_TYPES}
        new = {item["id"]: item for item in _three_types(day, scope, view, views, previous, batches, observed_at)}
        types = old | new
        for item in types.values():
            item.update(**FLAGS, historical_listing_method=LISTING_METHOD)
            item.pop("scope_members", None)  # One unchanged scope is stored at the manifest level.
        pct = pd.Series([row["pct_chg"] for row in rows], dtype=float)
        risk_keys = ("date", "board", "close", "open", "sma20", "sma60", "amount_rank_pct",
            "qualified_stage", "qualified_limit_up", "listing_feature_policy_verified", "ipo_feature_support")
        panels[day] = {"day": day, "available_at": observed_at,
            "environment": {"source_complete": True,
                "breadth": float((pct.gt(0).sum() - pct.lt(0).sum()) / len(pct)),
                "median_return_pct": float(pct.median()),
                "denominator_scope": "all_observed_native_active_nonST_rows_not_exchange_roster",
                "historical_dated_roster_verified": False},
            "types": [types[identity] for identity in TYPE_IDS],
            "risk_features": {row["symbol"]: {key: row.get(key) for key in risk_keys} for row in rows},
            "risk_feature_scope_complete": True,
            "source_days_used": [value for value in raw_by_day if value <= day],
            "historical_signal_publication_verified": False, "historical_input_schema": INPUT_SCHEMA}
    if sorted(panels) != source_days:
        raise ValueError("Every exact requested native source day needs its own eight-type panel")
    return _clean(panels)


def _warmup_reuse_contract(value):
    """Pin identities/bytes, preserving rather than equating new read clocks."""
    q = value["IPO_qualification"]
    prior = [{key: item for key, item in proof.items() if key != "actual_read_at"}
        for proof in value.get("prior_calendars", q.get("prior_calendars", []))]
    return {"first_day": value["start"], "scope": value["scope_members"],
        "warmup_days": value["warmup_source_days"],
        "sources_in_original_order": [{key: item for key, item in proof.items() if key != "opened_at"}
            for proof in value["source_proofs"]],
        "master_run": value["master_run"], "calendar": value["calendar"],
        "calendar_sha256": value["calendar_sha256"], "prior_calendars": prior,
        "listing_source": {key: q.get(key) for key in ("method_id", "master_run", "master_run_sha256",
            "master_file", "master_sha256", "original_available_at", "calendar", "calendar_sha256",
            "assessment_calendar_scope", "execution_calendar_contract_unchanged")},
        "turnover_unit_evidence": value["turnover_unit_evidence"]}


def _restoration_source_matches(saved, sources, *, reused):
    if not reused:
        return saved in sources
    identity = lambda proof: {key: item for key, item in proof.items() if key != "opened_at"}
    return any(identity(saved) == identity(source) for source in sources)


def _saved_warmup_for_reuse(path, expected):
    path = Path(path).resolve()
    loaded, old = load_inputs(path)  # Existing saved-payload checks; no NativeHistorySource/Engine.
    del loaded
    restore = old.get("warmup_restoration")
    if (restore is None or _warmup_reuse_contract(old) != _warmup_reuse_contract(expected)
            or not old.get("prior_calendars")):
        raise ValueError("Restoration reuse requires the exact original first-day/scope/source/listing/calendar/unit contract")
    prefix, diagnostic = pd.read_parquet(restore["raw_file"]), _read(restore["diagnostic_file"])
    reused = deepcopy(restore)
    reused["reuse_from"] = {"manifest": str(path), "manifest_sha256": digest(path),
        "manifest_created_at": old["created_at"], "restoration_created_at": diagnostic["created_at"]}
    return prefix, diagnostic, reused


def build_inputs(views, calendar, master_run, start, end, through, out, *, prior_calendars=(),
                 restore_valid_warmup=False, restore_valid_warmup_from=None):
    out, calendar = Path(out).resolve(), Path(calendar).resolve()
    if out.exists():
        raise FileExistsError("Historical inputs need a new separate output")
    payload = _read(calendar)
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar" or any(day not in sessions for day in (start, end, through)) or not start <= end <= through:
        raise ValueError("Exact ordered decision/source bounds in the pinned calendar required")
    source_days = [day for day in sessions if start <= day <= through]
    paths = [Path(path).resolve() for path in views]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Distinct explicit native source views required")
    if any(path == out or path in out.parents or out in path.parents for path in paths):
        raise ValueError("Historical output must be outside original source trees")
    observed = _now()
    if restore_valid_warmup and restore_valid_warmup_from is not None:
        raise ValueError("Fresh warmup recovery and saved warmup reuse are mutually exclusive")
    units = _read(UNIT_EVIDENCE)
    if (units.get("status") != "official_provider_document_read" or units.get("turn_unit") != "percent"
            or _time(units["retrieved_at"]) > _time(observed)):
        raise ValueError("Original saved official reported-turnover percentage evidence required")
    qualification = load_posthoc_listing(master_run, calendar, observed_at=observed, prior_calendars=prior_calendars)
    use_restoration = restore_valid_warmup or restore_valid_warmup_from is not None
    if use_restoration and not qualification.assessment_calendar:
        raise ValueError("Valid-observation recovery requires the explicit fixed prior-calendar prefix")
    restored_prefix, restoration, reused_restoration = None, None, None
    with ExitStack() as stack:
        sources = [stack.enter_context(NativeHistorySource(path)) for path in paths]
        owners = {}
        for index, source in enumerate(sources):
            for day in source.calendar:
                if day in owners:
                    raise ValueError("Ambiguous overlapping native source history")
                owners[day] = index
        all_days = sorted(owners)
        if any(day not in owners for day in source_days):
            raise ValueError("Missing exact native session, never use the next available date")
        at = all_days.index(start)
        if at < 61:
            raise ValueError("Historical eight-type inputs require 61 original source sessions of price warmup")
        context_days = all_days[at-61:at]
        requested = context_days + source_days
        frames, day_proofs = [], []
        for day in requested:
            source = sources[owners[day]]
            frame = source.frame(day)
            proof = deepcopy(frame.attrs["provenance"])
            frame.attrs.clear()
            supported = frame.symbol.map(lambda symbol: symbol.startswith(("60", "688", "00", "30"))) & ~frame.symbol.eq("689009.SSE")
            frame = frame.loc[supported].copy()
            frames.append(frame)
            day_proofs.append({"day": day, "source_index": owners[day], "row_group": proof["row_group"],
                "source_task": deepcopy(source._tasks[day]), "provenance": proof,
                "ordinary_observed_rows": len(frame)})
        history = pd.concat(frames, ignore_index=True)
        initial = history.loc[history.date.eq(start)]
        scope = sorted(initial.symbol.unique())
        if not scope:
            raise ValueError("First observed native day must have a fixed nonempty ordinary scope")
        if restore_valid_warmup:
            from .leader_valid_warmup import restore_valid_warmup as recover_prefix
            restored_prefix, restoration = recover_prefix(history, first_day=start, warmup_days=context_days,
                scope_members=scope, qualification=qualification,
                fixed_sessions=qualification.assessment_calendar,
                source_by_day={day: sources[index] for day, index in owners.items()}, physical_native=_physical_native)
        elif restore_valid_warmup_from is not None:
            expected = {"start": start, "scope_members": scope, "warmup_source_days": context_days,
                "source_proofs": [source.provenance for source in sources], "master_run": str(Path(master_run).resolve()),
                "calendar": str(calendar), "calendar_sha256": digest(calendar),
                "IPO_qualification": qualification.verification,
                "prior_calendars": qualification.verification["prior_calendars"],
                "turnover_unit_evidence": {"path": str(UNIT_EVIDENCE.resolve()), "sha256": digest(UNIT_EVIDENCE)}}
            restored_prefix, restoration, reused_restoration = _saved_warmup_for_reuse(restore_valid_warmup_from, expected)
        if restored_prefix is not None and not restored_prefix.empty:
            history = pd.concat([restored_prefix, history], ignore_index=True)
        witness = {}
        prior = history.loc[history.date.lt(start) & history.tradestatus.eq("1")
            & pd.to_numeric(history.close, errors="coerce").gt(0)]
        for symbol, values in prior.groupby("symbol", sort=False):
            record = qualification.records.get(symbol)
            if record:
                dates = sorted(set(values.loc[values.date.ge(record["ipo_date"]), "date"]))[:5]
                if len(dates) == 5:
                    witness[symbol] = dates
        qualification = with_prior_listing_witness(qualification, witness, start)
        feature_qualification = (PosthocListingQualification(qualification.records, qualification.assessment_calendar,
            qualification.verification, qualification.assessment_calendar) if prior_calendars else None)
        panels = derive_panels(history, source_days, scope, payload, qualification, observed_at=_now(),
            feature_qualification=feature_qualification, rolling_observation_cache=use_restoration)
        source_proofs = [source.provenance for source in sources]
    prefix = history.loc[history.date.eq(context_days[-1])]
    positive = pd.to_numeric(prefix.close, errors="coerce")
    anchor = {"day": context_days[-1], "closes": dict(zip(prefix.loc[positive.gt(0), "symbol"], positive.loc[positive.gt(0)]))}
    raw = history.loc[history.date.isin(source_days)].drop(columns=["tradestatus_value", "isST_value"], errors="ignore")
    out.mkdir(parents=True, exist_ok=False)
    raw_path, panels_path = out / "raw_daily.parquet", out / "panels.json"
    context_path = out / "preceding_native_rows.parquet"
    raw.to_parquet(raw_path, index=False)
    prefix.drop(columns=["tradestatus_value", "isST_value"], errors="ignore").to_parquet(context_path, index=False)
    _save(panels_path, panels)
    manifest = {"schema": INPUT_SCHEMA, "method_id": METHOD_ID, "created_at": _now(),
        "start": start, "end": end, "through": through, "source_days": source_days,
        "calendar": str(calendar), "calendar_sha256": digest(calendar),
        "master_run": str(Path(master_run).resolve()), "IPO_qualification": qualification.verification,
        "native_prior_listing_witness": witness,
        "raw_file": str(raw_path), "raw_sha256": digest(raw_path), "raw_rows": len(raw),
        "panels_file": str(panels_path), "panels_sha256": digest(panels_path),
        "context_raw_file": str(context_path), "context_raw_sha256": digest(context_path),
        "scope_members": scope, "scope_basis": "first_decision_day_native_observed_ordinary_rows_including_ST_halts_not_PIT_roster",
        "scope_frozen_at": observed, "source_proofs": source_proofs, "day_sources": day_proofs,
        "warmup_source_days": context_days, "prior_year_calendar_independently_verified": False,
        "context_anchor": anchor, "fixed_protocol": PROTOCOL,
        "turnover_unit_evidence": {"path": str(UNIT_EVIDENCE.resolve()), "sha256": digest(UNIT_EVIDENCE)},
        "source_contract": "observed_native_rows_without_historical_complete_dated_roster",
        "feature_formula_source": "unchanged_leader_daily_signals._features_exact_native_preclose",
        "old_strict_input_schema_used": False, "future_path_survivors_used_for_scope": False,
        "identity_or_historical_sector_membership_backfilled": False,
        "initial_previous_year_positions_consumed": False, **FLAGS}
    if prior_calendars:
        manifest.update(prior_calendars=qualification.verification["prior_calendars"],
            feature_calendar_scope="explicit_complete_prior_years_plus_current_year",
            feature_previous_day_from_fixed_calendar_prefix=True,
            prior_year_calendar_independently_verified=False,
            prior_calendar_official_schedule_supplied=True)
    if reused_restoration is not None:
        manifest["warmup_restoration"] = reused_restoration
    elif restoration is not None:
        restored_path, diagnostic_path = out / "restore_history.parquet", out / "restore_diagnostic.json"
        restored_prefix.to_parquet(restored_path, index=False)
        _save(diagnostic_path, restoration)
        manifest["warmup_restoration"] = {"enabled": True, "method_id": restoration["method_id"],
            "required_valid_observations": 61, "first_decision_day": start,
            "raw_file": str(restored_path), "raw_sha256": digest(restored_path),
            "diagnostic_file": str(diagnostic_path), "diagnostic_sha256": digest(diagnostic_path),
            "original_full_market_warmup_unchanged": True,
            "scope": "explicit_past_symbol_projections_not_full_market",
            "selection_uses_first_day_or_earlier_only": True,
            "rolling_observation_cache": "exact_current_and_two_previous_fixed_sessions"}
    _save(out / "manifest.json", manifest)
    return manifest


def load_inputs(path):
    path = Path(path).resolve()
    value = _read(path)
    if (value.get("schema") != INPUT_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("fixed_protocol") != PROTOCOL
            or any(value.get(key) is not expected for key, expected in FLAGS.items())
            or _time(value["created_at"]) > _time(_now())):
        raise ValueError("Separate unchanged historical eight-type input required")
    for file_key, hash_key in (("raw_file", "raw_sha256"), ("panels_file", "panels_sha256"), ("calendar", "calendar_sha256")):
        if digest(value[file_key]) != value[hash_key]:
            raise ValueError(f"Historical input changed: {file_key}")
    if digest(value["context_raw_file"]) != value["context_raw_sha256"]:
        raise ValueError("Exact preceding native context rows changed")
    units = value["turnover_unit_evidence"]
    if digest(units["path"]) != units["sha256"]:
        raise ValueError("Original percentage-unit evidence changed")
    for source in value["source_proofs"]:
        for key in ("result", "plan", "receipt"):
            if digest(source[key + "_file"]) != source[key + "_sha256"]:
                raise ValueError("Historical native source metadata changed")
    restore = value.get("warmup_restoration")
    if restore is not None:
        from .leader_valid_warmup import METHOD_ID as RESTORE_METHOD
        if (restore.get("enabled") is not True or restore.get("method_id") != RESTORE_METHOD
                or restore.get("required_valid_observations") != 61 or restore.get("first_decision_day") != value["start"]
                or digest(restore["raw_file"]) != restore["raw_sha256"]
                or digest(restore["diagnostic_file"]) != restore["diagnostic_sha256"]):
            raise ValueError("Explicit past-only restoration payload changed")
        diagnostic = _read(restore["diagnostic_file"])
        projected = pd.read_parquet(restore["raw_file"])
        reuse = restore.get("reuse_from")
        if reuse is not None:
            anchor = _read(reuse["manifest"])
            if (digest(reuse["manifest"]) != reuse["manifest_sha256"]
                    or anchor["created_at"] != reuse["manifest_created_at"]
                    or diagnostic["created_at"] != reuse["restoration_created_at"]
                    or _warmup_reuse_contract(anchor) != _warmup_reuse_contract(value)
                    or {key: item for key, item in restore.items() if key != "reuse_from"}
                       != {key: item for key, item in anchor.get("warmup_restoration", {}).items() if key != "reuse_from"}):
                raise ValueError("Saved warmup reuse anchor, byte pins or original created clocks changed")
        if (diagnostic.get("method_id") != RESTORE_METHOD
                or diagnostic.get("first_day_scope_members") != value["scope_members"]
                or diagnostic.get("original_full_market_warmup_days") != value["warmup_source_days"]
                or diagnostic.get("future_rows_used_for_selection_or_counting") is not False
                or diagnostic.get("projected_prefix_rows") != len(projected)
                or projected.duplicated(["date", "code"]).any()
                or (not projected.empty and projected.date.max() >= value["warmup_source_days"][0])):
            raise ValueError("Restored native prefix differs from its first-day scope or exact original warmup")
        for read in diagnostic["projection_day_sources"]:
            rows = projected.loc[projected.date.eq(read["day"])]
            proof = read["row_group_provenance"]
            source_matches = _restoration_source_matches(read["original_source_provenance"],
                value["source_proofs"], reused=reuse is not None)
            if (not source_matches
                    or proof.get("day") != read["day"] or proof.get("symbols") != read["symbols_requested"]
                    or not set(read["symbols_requested"]) <= set(value["scope_members"])
                    or len(rows) != read["projected_rows_included"]
                    or not set(rows.symbol) <= set(read["symbols_requested"])):
                raise ValueError("Restoration projection and original pinned native source metadata differ")
    raw = pd.read_parquet(value["raw_file"])
    panels = _read(value["panels_file"])
    if (len(raw) != value["raw_rows"] or sorted(raw.date.unique()) != value["source_days"]
            or raw.duplicated(["date", "code"]).any() or sorted(panels) != value["source_days"]):
        raise ValueError("Historical raw/panel exact dates and row identities differ")
    if value.get("identity_research") is not None:
        from .leader_historical_identity_inputs import validate_overlay
        validate_overlay(value, panels)
    prior = value.get("prior_calendars", [])
    if prior != value["IPO_qualification"].get("prior_calendars", []):
        raise ValueError("Input and qualification prior-calendar proofs differ")
    qualification = load_posthoc_listing(value["master_run"], value["calendar"],
        observed_at=value["IPO_qualification"]["posthoc_observed_at"], prior_calendars=[item["path"] for item in prior])
    qualification = with_prior_listing_witness(qualification, value["native_prior_listing_witness"], value["start"])
    if qualification.verification != value["IPO_qualification"]:
        raise ValueError("Historical listing evidence changed")
    if value.get("historical_listing_classification") is not None:
        from .leader_historical_listing_classification import validate
        validate(value, panels, raw, qualification)
    return {"panel_by_day": panels, "raw": raw, "source_days": value["source_days"],
        "calendar_payload": _read(value["calendar"]), "scope_members": value["scope_members"],
        "qualification": qualification, "context_anchor": value["context_anchor"],
        "provenance": {"method_id": METHOD_ID, "historical_input_schema": INPUT_SCHEMA,
            "manifest": str(path), "manifest_sha256": digest(path), "input_record": value, **FLAGS}}, value


def run(inputs, out, *, action_event_view, action_reference_view):
    loaded, manifest = load_inputs(inputs)
    context_raw = pd.read_parquet(manifest["context_raw_file"])
    context_days = sorted(context_raw.date.unique())
    if context_days != [manifest["context_anchor"]["day"]]:
        raise ValueError("One exact preceding full native row-group context required")
    accounting = load_action_accounting(action_event_view, action_reference_view,
        symbols=manifest["scope_members"], raw_days=context_days + manifest["source_days"],
        replay_days=manifest["source_days"], price_source="independent_baostock_raw")
    result = replay_eight_types(**loaded, start=manifest["start"], end=manifest["end"], out=out,
        context_raw=context_raw, execution_policy_factory=HistoricalExecutionFactory(accounting))
    _save(Path(out) / "historical_method.json", {"schema": "leader_eight_type_historical_native_result_v1",
        "method_id": METHOD_ID, "shared_engine_result": str(Path(out).resolve() / "result.json"),
        "original_32_protocol_unchanged": True, "cases": result["case_count"],
        "IPO_qualification": manifest["IPO_qualification"],
        "action_accounting": accounting.provenance,
        "historical_dated_roster_or_publication_claimed": False, **FLAGS})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("inputs")
    build.add_argument("--native-view", type=Path, action="append", required=True)
    build.add_argument("--prior-calendar", type=Path, action="append", default=[])
    recovery = build.add_mutually_exclusive_group()
    recovery.add_argument("--restore-valid-warmup", action="store_true")
    recovery.add_argument("--restore-valid-warmup-from", type=Path)
    for name in ("calendar", "research-master-run", "out"):
        build.add_argument("--" + name, type=Path, required=True)
    for name in ("start", "end", "through"):
        build.add_argument("--" + name, required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--inputs", type=Path, required=True)
    replay.add_argument("--out", type=Path, required=True)
    replay.add_argument("--action-event-view", type=Path, required=True)
    replay.add_argument("--action-reference-view", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "inputs":
        result = build_inputs(args.native_view, args.calendar, args.research_master_run,
            args.start, args.end, args.through, args.out, prior_calendars=args.prior_calendar,
            restore_valid_warmup=args.restore_valid_warmup, restore_valid_warmup_from=args.restore_valid_warmup_from)
        print(json.dumps({"manifest": str(args.out.resolve() / "manifest.json"),
            "days": len(result["source_days"]), "scope": len(result["scope_members"]), **FLAGS}, ensure_ascii=False))
    else:
        result = run(args.inputs, args.out, action_event_view=args.action_event_view,
            action_reference_view=args.action_reference_view)
        print(json.dumps({"case_count": result["case_count"], "source_days": len(result["source_days"]),
            "cases_with_fills": sum(bool(case["fills"]) for case in result["cases"]),
            "all_cases_complete_PnL": result["all_cases_complete_PnL"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
