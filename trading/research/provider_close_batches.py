"""Fixed leader candidates from strictly complete, exact-dated provider closes.

This is a new source contract, not a completion flag for StockDB partitions.
Provider status0 inventory can explain an unheld new candidate's null activity;
it cannot price owned shares, supply a successor bar or clear PIT/action gates.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import json
import re

import numpy as np
import pandas as pd

from . import baostock_full_close as close_source
from .baostock_daily_slice import FIELDS, digest
from .leader_close_preview import canonical
from .leader_daily_signals import (
    FAMILIES, _calendar_view, build_leader_daily_batches, read_physical_snapshot,
)


CONTRACT = "complete_dated_provider_close_candidate_v1"
SCOPE = "exact_dated_provider_ordinary_SH_SZ_inventory"
NUMERIC = ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]
PRICE = ["open", "high", "low", "close", "preclose"]


def _acquisition_times(metadata, folder):
    """Propagate recorded capture times; never derive a publication clock."""
    receipt = metadata["receipt"]
    timestamps = {key: receipt[key] for key in ("created_at", "run_started_at", "updated_at", "completed_at")
                  if receipt.get(key) is not None}
    # load_complete verifies this exact roster directory. Read only its saved
    # receipt for acquisition timestamps, not a historical publication archive.
    roster_path = Path(metadata["source_folder"]) / "roster" / "receipt.json"
    if metadata["plan"].get("schema") == "baostock_bulk_exact_dated_close_inventory_v1":
        roster_path = Path(metadata["plan"]["roster_folder"]) / "receipt.json"
    roster_receipt = json.loads(roster_path.read_text(encoding="utf-8"))
    roster_times = {key: roster_receipt[key] for key in ("created_at", "completed_at")
                    if roster_receipt.get(key) is not None}
    for value in [*timestamps.values(), *roster_times.values()]:
        if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Recorded source acquisition timestamps require explicit timezone")
    completed = timestamps.get("completed_at")
    basis = "explicit_source_receipt_completed_at" if completed else "terminal_inventory_receipt_updated_at_after_full_assembly"
    if completed is None:
        completed = timestamps.get("updated_at")
    if completed is None:
        basis = "terminal_completion_time_not_recorded"
    return {"acquisition_timestamps": timestamps,
            "terminal_completion_observed_at": completed, "terminal_completion_time_basis": basis,
            "roster_receipt": str(roster_path.resolve()), "roster_receipt_sha256": digest(roster_path),
            "roster_acquisition_timestamps": roster_times,
            "roster_receipt_completed_at": roster_times.get("completed_at"),
            "timestamp_semantics": "local_source_retrieval_not_historical_publication_or_signal_release",
            "captured_timestamp_not_signal_publication": True,
            "actual_signal_publication_verified": False, "historical_publication_time_known": False,
            "pit_verified": False}


def _inventory(raw, metadata, calendar_status):
    """Recheck current inventory; no name/ST filter narrows the dated roster."""
    plan, receipt = metadata["plan"], metadata["receipt"]
    day = plan["day"]
    roster = pd.DataFrame(metadata["ordinary_roster"])
    if (metadata.get("read_only_verified") is not True
            or receipt.get("status") != "complete_full_dated_close_inventory_not_admitted"
            or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("pending_symbols") or receipt.get("preserved_failed_batches")):
        raise ValueError("Only the strict full-close loader's complete source may enter candidates")
    if (list(raw.columns) != FIELDS.split(",") or raw.date.tolist() != [day] * len(raw)
            or raw.code.duplicated().any() or roster.symbol.duplicated().any()
            or raw.code.tolist() != roster.code.tolist()
            or roster.symbol.tolist() != plan["symbols"]
            or len(raw) != plan["ordinary_symbols"]
            or raw.tradestatus.tolist() != roster.tradeStatus.tolist()):
        raise ValueError("Current raw identities/statuses differ from the entire exact-dated roster")
    if not raw.tradestatus.isin(["0", "1"]).all() or not raw.isST.isin(["0", "1"]).all() or not raw.adjustflag.eq("3").all():
        raise ValueError("Unknown provider status/ST or non-raw source prices")
    values = raw[NUMERIC].apply(pd.to_numeric, errors="coerce")
    finite = pd.Series(np.isfinite(values.to_numpy()).all(axis=1), index=raw.index)
    shape = (values.high.ge(values[["open", "close", "low"]].max(axis=1))
             & values.low.le(values[["open", "close", "high"]].min(axis=1)))
    price_valid = (pd.Series(np.isfinite(values[PRICE].to_numpy()).all(axis=1), index=raw.index)
                   & values[PRICE].gt(0).all(axis=1) & shape)
    raw_valid = finite & price_valid & values[["volume", "amount"]].ge(0).all(axis=1)
    active = raw.tradestatus.eq("1")
    if not raw_valid.loc[active].all():
        raise ValueError("Every active status1 roster member requires a real finite valid raw bar")
    halted = ~active
    nullable = raw[["volume", "amount", "pctChg"]].eq("").any(axis=1)
    halt_nullable_ok = raw.loc[halted, ["volume", "amount", "pctChg"]].apply(
        lambda col: col.eq("") | pd.to_numeric(col, errors="coerce").eq(0)).all(axis=1)
    if (not price_valid.loc[halted].all() or not halt_nullable_ok.all()
            or values.loc[halted, ["open", "high", "low", "close"]]
            .sub(values.loc[halted, "preclose"], axis=0).abs().gt(.0001).any().any()):
        raise ValueError("Status0 inventory has contradictory raw prices/activity")
    symbols = roster.symbol.tolist()
    return {"schema": CONTRACT, "day": day, "scope": SCOPE,
            "source_folder": metadata["source_folder"], "roster_raw_sha256": plan["roster_raw_sha256"],
            "raw_daily_sha256": receipt["raw_daily_sha256"], "ordinary_inventory_rows": len(raw),
            "status_by_symbol": dict(zip(symbols, raw.tradestatus)),
            "active_valid_raw_symbols": [symbol for symbol, okay in zip(symbols, active & raw_valid) if okay],
            "raw_valuation_usable_symbols": [symbol for symbol, okay in zip(symbols, raw_valid) if okay],
            "status0_symbols": [symbol for symbol, okay in zip(symbols, halted) if okay],
            "nullable_status0_symbols": [symbol for symbol, okay in zip(symbols, halted & nullable) if okay],
            "candidate_inventory_complete": calendar_status == "pinned_static_exchange_calendar",
            "calendar_status": calendar_status, "raw_valuation_coverage_complete": bool(raw_valid.all()),
            "nullable_status0_is_candidate_explanation_only": True,
            "exchange_universe_verified": False, "pit_verified": False,
            "corporate_actions_verified": False, "paper_admitted": False, "execution_admitted": False}


def build_provider_close_bundle(packages, history_snapshot, calendar, *, ipo_qualification=None,
                                recent_source_view=None):
    """Produce all five unchanged rule families after strict full-source load.

    All source days append to the original historical snapshot. Source-specific
    candidate completeness never overwrites the old producer's coverage facts.
    No account or replay is created. A partial package fails before history work.
    """
    if isinstance(packages, (str, Path)):
        packages = [packages]
    if not packages:
        raise ValueError("At least one complete full-close package is required")
    loaded, loaded_cache = [], {}
    for folder in packages:
        folder = Path(folder).resolve()
        if folder not in loaded_cache:
            loaded_cache[folder] = close_source.load_complete(folder)
        raw, metadata = loaded_cache[folder]
        loaded.append((raw, metadata, folder))
    days = [metadata["plan"]["day"] for _, metadata, _ in loaded]
    if len(days) != len(set(days)):
        raise ValueError("Duplicate full-close source day")
    loaded.sort(key=lambda item: item[1]["plan"]["day"])
    days = sorted(days)
    calendar = Path(calendar).resolve()
    calendar_payload = json.loads(calendar.read_text(encoding="utf-8"))
    if (calendar_payload.get("schema") != "cn_a_share_exchange_calendar_v1"
            or any(date.fromisoformat(day).year != int(calendar_payload["year"]) for day in days)):
        raise ValueError("Provider source days need the same pinned exchange-calendar year")
    year = int(calendar_payload["year"])
    sessions, _, calendar_status = _calendar_view(calendar_payload, f"{year}-01-01", f"{year}-12-31", days)
    if calendar_status != "pinned_static_exchange_calendar" or any(day not in sessions for day in days):
        raise ValueError("Provider close is outside the declared pinned exchange sessions")
    recent_history = None
    actual_loaded = loaded
    recent_proofs = []
    if recent_source_view is None:
        historical, history_metadata = read_physical_snapshot(Path(history_snapshot), end=days[-1])
        if historical.empty or historical.date.max() >= days[0]:
            raise ValueError("Provider closes must append later dates without replacing historical observations")
        combined = pd.concat([historical, *[canonical(raw) for raw, _, _ in loaded]], ignore_index=True)
    else:
        from .leader_recent_source_history import compose_recent_sources, read_recent_source_view, source_proof
        recent_history = read_recent_source_view(recent_source_view)
        if (Path(history_snapshot).resolve() != Path(recent_history["original_history_snapshot"])
                or calendar != Path(recent_history["calendar"])
                or digest(calendar) != recent_history["calendar_sha256"]):
            raise ValueError("Recent-source view and requested original history/calendar differ")
        if days[0] < recent_history["view_sessions"][-1]:
            raise ValueError("Current signal days cannot precede the fixed recent-history prefix end")
        by_day = {}
        for expected in recent_history["source_proofs"]:
            folder = Path(expected["source_folder"]).resolve()
            if folder not in loaded_cache:
                loaded_cache[folder] = close_source.load_complete(folder)
            raw, metadata = loaded_cache[folder]
            proof = source_proof(raw, metadata, recent_history["calendar_sha256"])
            if proof != expected:
                raise ValueError("Recent-source saved proof differs from its strict source package")
            by_day[proof["day"]] = (raw, metadata, folder)
        for raw, metadata, folder in loaded:
            day = metadata["plan"]["day"]
            if day in by_day:
                prior_raw, prior_metadata, prior_folder = by_day[day]
                if (folder != prior_folder or metadata["receipt"]["raw_daily_sha256"] != prior_metadata["receipt"]["raw_daily_sha256"]
                        or metadata["plan"]["roster_raw_sha256"] != prior_metadata["plan"]["roster_raw_sha256"]):
                    raise ValueError("Overlapping current/prefix day requires the exact same source folder and hashes")
            else:
                by_day[day] = (raw, metadata, folder)
        actual_loaded = [by_day[day] for day in sorted(by_day)]
        composed = compose_recent_sources([(raw, metadata) for raw, metadata, _ in actual_loaded],
            history_snapshot, calendar, base_cutoff=recent_history["base_history_end"], history_reader=read_physical_snapshot)
        combined, history_metadata = composed["combined"], composed["history_metadata"]
        recent_proofs = composed["sources"]
    inventory = {metadata["plan"]["day"]: _inventory(raw, metadata, calendar_status)
                 for raw, metadata, _ in loaded}
    options = {"ipo_qualification": ipo_qualification} if ipo_qualification is not None else {}
    original = build_leader_daily_batches(combined, history_metadata, start=days[0], end=days[-1],
                                         calendar_payload=calendar_payload, **options)
    raw_bars = original["raw_bars"].copy()
    # Actual provider execution fields remain separate from the fixed producer's
    # physical-pct feature reference. Null suspension activity remains NaN.
    actual = pd.concat([raw for raw, _, _ in actual_loaded], ignore_index=True).copy()
    actual["symbol"] = actual.code.str[3:] + np.where(actual.code.str.startswith("sh."), ".SSE", ".SZSE")
    for field in NUMERIC:
        actual[field] = pd.to_numeric(actual[field], errors="coerce")
    actual["tradestatus"] = actual.tradestatus.astype(int)
    actual["isST"] = actual.isST.astype(int)
    actual["provider_raw_observation"] = True
    actual_fields = actual[["date", "symbol", "preclose", "tradestatus", "isST", "adjustflag", "provider_raw_observation"]]
    raw_bars = raw_bars.merge(actual_fields, how="left", on=["date", "symbol"], validate="one_to_one")
    source_packages = []
    for raw, metadata, folder in actual_loaded:
        item = {"day": metadata["plan"]["day"], "folder": str(folder),
            "plan_sha256": digest(folder / "plan.json"), "receipt_sha256": digest(folder / "receipt.json"),
            "roster_raw_sha256": metadata["plan"]["roster_raw_sha256"],
            "raw_daily_sha256": metadata["receipt"]["raw_daily_sha256"],
            **_acquisition_times(metadata, folder)}
        if recent_history is not None:
            source_day = metadata["plan"]["day"]
            item["roles"] = (["recent_history_prefix"] if source_day in recent_history["view_sessions"] else []) + (["requested_current"] if source_day in days else [])
        source_packages.append(item)
    batches = {family: {} for family in FAMILIES}
    for family in FAMILIES:
        for day in days:
            old = original["batches"][family][day]
            batch = deepcopy(old)
            view = inventory[day]
            active_valid = set(view["active_valid_raw_symbols"])
            batch["candidates"] = [row for row in batch["candidates"] if row["symbol"] in active_valid]
            batch.update(status="complete" if view["candidate_inventory_complete"] else "incomplete",
                source=f"research:leader_provider_dated_close:{family}", source_coverage_scope=SCOPE,
                candidate_scope="all_exact_dated_provider_current_active_valid_candidates_no_future_survival_filter",
                input_source_contract=CONTRACT, provider_inventory_complete=view["candidate_inventory_complete"],
                original_producer_batch_status=old["status"], original_producer_coverage=deepcopy(old["coverage"]),
                coverage={key: deepcopy(value) for key, value in view.items() if not key.endswith("symbols") and key != "status_by_symbol"})
            # This remains a provider breadth proxy. It does not pass the
            # V2 verified exchange-wide defense gate.
            batch["market_breadth"].update(scope=SCOPE, complete=view["candidate_inventory_complete"],
                raw_counts_verified=view["candidate_inventory_complete"], verified=False, source_verified=False)
            if ipo_qualification is not None:
                qualification = batch["feature_qualification"]
                qualification["source_coverage_status_before_qualification"] = (
                    "complete" if view["candidate_inventory_complete"] else "incomplete")
                if not qualification["qualification_complete"]:
                    batch.update(status="incomplete", incomplete_reason="listing_feature_qualification_unknown")
            if recent_history is not None:
                binding_fields = ("method_id", "view_file", "view_sha256", "shared_view_id", "base_history_end",
                                  "available_at", "original_history_manifest_sha256")
                batch["recent_source_history"] = {key: recent_history[key] for key in binding_fields}
                proofs_used = [proof for proof in recent_proofs if proof["day"] <= day]
                batch["recent_source_history"].update(source_days_used=[proof["day"] for proof in proofs_used],
                                                     source_proofs=deepcopy(proofs_used))
            batches[family][day] = batch
    result = {"schema": "provider_close_leader_signal_bundle_v1", "input_source_contract": CONTRACT,
            "raw_bars": raw_bars, "calendar": sessions, "calendar_path": str(calendar),
            "calendar_payload": calendar_payload, "calendar_sha256": digest(calendar),
            "batches": batches, "families": list(FAMILIES), "candidate_table": original["candidate_table"],
            "source_days_complete": days, "provider_inventory_by_day": inventory,
            "source_packages": source_packages, "first_observed_date": original["first_observed_date"],
            "original_producer_coverage": original["coverage_facts"],
            "source_metadata": {"input_source_contract": CONTRACT, "scope": SCOPE,
                "history_metadata": deepcopy(history_metadata), "source_packages": source_packages,
                "calendar_path": str(calendar), "calendar_sha256": digest(calendar)},
            "protocol": {**original["protocol"], "source_inventory_basis": SCOPE,
                "history_coverage_promoted": False, "history_prices_imputed": False,
                "fixed_historical_cohort_preserved_by_scope_consumer": True,
                "held_pending_and_successor_raw_required_independently": True,
                "calendar_is_session_schedule_not_raw_bars": True},
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "signal_admitted": False, "paper_admitted": False, "execution_admitted": False,
            "source_verified": False, "exchange_universe_verified": False, "pit_verified": False,
            "actual_signal_publication_verified": False,
            "corporate_actions_verified": False, "orders_generated": False, "persistent_ledger_mutated": False,
            "model_refit": False, "future_labels_used_for_score": False}
    if ipo_qualification is not None:
        result["feature_qualification"] = deepcopy(original["feature_qualification"])
    if recent_history is not None:
        result["recent_source_history"] = deepcopy(recent_history)
        result["source_metadata"]["recent_source_history"] = deepcopy(recent_history)
    return result


def scope_provider_batches(bundle, symbols, *, decision_end=None, held_symbols=(), pending_symbols=()):
    """Scope new-source candidates to a fixed cohort without survivor pruning.

    A cohort member absent from today's roster is unknown. A status0 member may
    explain candidate non-participation, but a held/pending nullable raw bar still
    blocks the account-dependent price requirement. The shared feed must repeat
    that current/next required-symbol check against each actual Ledger state.
    """
    if bundle.get("input_source_contract") != CONTRACT:
        raise ValueError("Explicit provider candidate source contract is required")
    if (not symbols or len(symbols) != len(set(symbols))
            or any(not re.fullmatch(r"\d{6}\.(?:SSE|SZSE)", symbol) for symbol in symbols)):
        raise ValueError("Use one fixed distinct normalized cohort; do not remove missing survivors")
    members = set(symbols)
    required = set(held_symbols) | set(pending_symbols)
    output, coverage = {family: {} for family in FAMILIES}, {}
    for day in bundle["source_days_complete"]:
        view = bundle["provider_inventory_by_day"][day]
        if view.get("schema") != CONTRACT or view.get("day") != day or view.get("candidate_inventory_complete") is not True:
            raise ValueError("Source-specific candidate inventory is not complete for its exact day")
        statuses = view["status_by_symbol"]
        absent = sorted(members - set(statuses))
        active_valid = set(view["active_valid_raw_symbols"])
        active_invalid = sorted(symbol for symbol in members if statuses.get(symbol) == "1" and symbol not in active_valid)
        halted = sorted(symbol for symbol in members if statuses.get(symbol) == "0")
        unpriceable = sorted(members - set(view["raw_valuation_usable_symbols"]))
        required_missing = sorted(required - set(view["raw_valuation_usable_symbols"]))
        complete = not absent and not active_invalid and not required_missing
        facts = {"schema": CONTRACT, "day": day, "status": "complete" if complete else "incomplete",
            "completion_semantics": "fixed_cohort_current_candidate_inventory_only_not_account_or_execution_prices",
            "expected_cohort_rows": len(symbols), "provider_roster_observed_cohort_rows": len(members) - len(absent),
            "historical_cohort_missing_current_roster": absent, "active_invalid_current_raw": active_invalid,
            "candidate_status0_explained_symbols": [symbol for symbol in halted if symbol not in required],
            "nullable_status0_symbols": sorted(members.intersection(view["nullable_status0_symbols"])),
            "raw_valuation_unusable_symbols": unpriceable, "required_held_or_pending_unpriceable_symbols": required_missing,
            "whole_cohort_membership_preserved": True, "held_pending_inventory_modified": False,
            "raw_marks_or_successor_coverage_promoted": False, "history_coverage_promoted": False,
            "pit_verified": False, "corporate_actions_verified": False, "paper_admitted": False,
            "execution_admitted": False}
        coverage[day] = facts
        for family in FAMILIES:
            family_batches = bundle["batches"].get(family, {})
            if day not in family_batches:
                # Preserve the missing decision batch.  Its caller must retain
                # NO_CHANGE rather than inventing a complete empty target.
                continue
            batch = deepcopy(family_batches[day])
            candidates = [row for row in batch["candidates"] if row["symbol"] in members and row["symbol"] in active_valid]
            batch.update(candidates=candidates[:5], cohort_candidate_coverage=deepcopy(facts),
                         decision_scope="fixed_pre_window_cohort_source_specific_candidate_coverage")
            qualification = batch.get("feature_qualification")
            if qualification is not None:
                qualification["unknown_symbols"] = sorted(members.intersection(qualification["unknown_symbols"]))
                qualification["known_excluded_symbols"] = sorted(members.intersection(qualification["known_excluded_symbols"]))
                qualification["unknown_reasons_by_symbol"] = {symbol: reason for symbol, reason in
                    qualification.get("unknown_reasons_by_symbol", {}).items() if symbol in members}
                qualification["qualification_complete"] = not qualification["unknown_symbols"]
                qualification["scope"] = "fixed_pre_window_cohort_keeps_unknown_members"
                # Restore only this explicit qualification status after narrowing
                # to the fixed cohort. Raw inventory/account checks still apply.
                batch["status"] = ("complete" if complete and qualification["qualification_complete"]
                    and qualification["source_coverage_status_before_qualification"] == "complete" else "incomplete")
                if not qualification["qualification_complete"]:
                    batch["incomplete_reason"] = "fixed_cohort_listing_feature_qualification_unknown"
                elif batch["status"] == "complete":
                    batch.pop("incomplete_reason", None)
            if decision_end is not None and day > decision_end:
                batch.update(status="incomplete", candidates=[], incomplete_reason="execution_tail_no_new_decision")
            elif not complete or batch.get("status") != "complete":
                batch.update(status="incomplete", incomplete_reason=("fixed_cohort_listing_feature_qualification_unknown"
                    if qualification is not None and not qualification["qualification_complete"]
                    else "provider_fixed_cohort_candidate_inventory_incomplete"))
            output[family][day] = batch
    return output, coverage
