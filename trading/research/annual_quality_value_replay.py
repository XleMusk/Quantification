"""Explicit retrospective annual-quality/value comparison through the shared ledger.

The annual retrieval clock is never backdated. This new research method uses
the reported fiscal-year and notice-date convention on a frozen, revised data
version; it does not change the existing prospective AnnualQualityFeed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalUnavailable
from ..settings import DATA_ROOT
from . import annual_quality_value_inputs as inputs
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result
from .low_risk import portfolio
from .monthly_timing import month_ends


METHOD_ID = "annual_quality_value_same_pool_retrospective_v1"
START, END, SESSIONS = "2025-01-02", "2026-09-30", 424
CASES = ("matched_momentum", "value_only", "annual_quality_only", "quality_value_half_half")
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "network_accessed": False,
    "returns_reportable": False,
    "real_account_accessed": False, "model_refit": False, "parameters_searched": False,
    "historical_publication_verified": False, "pit_verified": False,
    "independent_holdout_claimed": False, "source_snapshots_modified": False}
SPEC = {"method_id": METHOD_ID, "mode": "EXPLICIT_RETROSPECTIVE_FROZEN_FINANCIAL_VERSION",
    "start": START, "end": END, "sessions": SESSIONS, "cases": list(CASES),
    "universe": "saved_complete_original_value_monthly_pool_intersected_with_exact_annual_joint_quality",
    "schedule": "same_twenty_confirmed_month_ends_as_original_source_not_an_invented_June_midmonth",
    "fiscal_year": "D_is_actual_month_end_then_month_gte6_uses_year_minus1_otherwise_year_minus2",
    "notice": "all_required_balance_income_cashflow_notice_dates_strictly_before_D_unknown_or_late_excluded",
    "retrieval": "original_actual_retrieved_at_and_feature_asof_retained_not_backdated_to_historical_D",
    "quality_score": "(rank_z(revenue_cost_spread_assets)+rank_z(operating_cashflow_assets))/2_in_D_joint_pool",
    "value_score": "rank_z(original_saved_value_rank)_in_D_joint_pool_preserves_original_value_order",
    "matched_momentum": "original_saved_momentum_descending",
    "value_only": "value_score_descending",
    "annual_quality_only": "quality_score_descending",
    "quality_value_half_half": "(quality_score+value_score)/2_equal_economic_families_no_weight_fit",
    "ties": "score_descending_then_symbol_ascending_stable_top30",
    "unknown": "joint_pool_below30_is_shared_NO_CHANGE_not_empty_FULL_no_fill_or_backfill",
    "portfolio": "original140000_20names_rank30_gross0.8_equal_targets0.04_original_single_cap0.2",
    "execution": "unchanged_shared_Engine_Ledger_next_open_T1_lots_fees_slippage_prior_volume_capacity",
    "case_isolation": "four_new_independent_accounts_no_old_account_control_or_checkpoint_reuse",
    "actions": "original_exact_event_reference_pair_only_unknown_held_event_stops_affected_case_no_refresh",
    "manifest_compatibility": "all_original_keys_types_values_exact_only_five_explicit_unselected_scope_metadata_additions_allowed",
    "end_boundary": "original_finite_source_end_marked_account_equity_not_forced_liquidation_or_realized_only_return",
    "evidence": "all_cases_preserved_complete_window_metrics_only_if_completed_and_has_fills",
    "limitation": "provider_notice_dates_revisions_scope_and_units_not_independently_PIT_verified_not_QMJ_or_private_method_replication",
    **FLAGS}


def now():
    return datetime.now().astimezone().isoformat()


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(member) for key, member in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(member) for member in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if value is pd.NA:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def _refs(value):
    if isinstance(value, dict):
        if {"path", "sha256"} <= set(value):
            yield value
        else:
            for member in value.values():
                yield from _refs(member)
    elif isinstance(value, list):
        for member in value:
            yield from _refs(member)


def _directory(out, protected, *, external=False):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new exclusive annual quality/value directory; no retry/reset")
    if external:
        out.relative_to(DATA_ROOT.resolve())
    for value in protected:
        source = Path(value).resolve()
        if out == source or out in source.parents or source in out.parents:
            raise ValueError("New outputs must remain separate from frozen sources and registration")
    return out


def _code_files():
    root = Path(__file__).resolve().parents[1]
    return [Path(__file__).resolve(), Path(inputs.__file__).resolve(),
        *[root / name for name in ("research/capital_allocation_replay.py", "research/experiment.py",
            "research/bulk_feed.py", "research/low_risk.py", "research/monthly_timing.py",
            "research/profitability_portfolios.py", "engine.py", "ledger.py", "contracts.py",
            "config.py", "rules.py", "symbols.py")]]


def _validate_meta(meta):
    if (meta["replay_days"] != sorted(set(meta["replay_days"])) or len(meta["replay_days"]) != SESSIONS
            or meta["replay_days"][0] != START or meta["replay_days"][-1] != END
            or len(meta["decision_days"]) != 20 or meta["decision_days"] != sorted(set(meta["decision_days"]))):
        raise ValueError("Original424-session/twenty-month decision inventory required")
    config = StrategyConfig(**meta["config"]).validate()
    if config != portfolio():
        raise ValueError("The original140000/20/rank30/equal-weight portfolio and costs must remain unchanged")
    expected = [day for day in month_ends(meta["calendar"], END) if START <= day <= END]
    if meta["decision_days"] != expected:
        raise ValueError("All original decisions must be exact confirmed source-calendar month ends")
    return config


def register(source_run, quality_result, out, *, source_note=None):
    """Save source hashes/rules/code before any new score or account is built."""
    meta = inputs.inspect_saved_inputs(source_run, quality_result)
    _validate_meta(meta)
    refs = list(_refs(meta))
    note = pin(source_note) if source_note is not None else None
    protected = {str(Path(ref["path"]).resolve().parent) for ref in refs}
    if note:
        protected.add(str(Path(note["path"]).parent))
    folder = _directory(out, protected)
    code = [pin(path) for path in _code_files()]
    folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / "code_snapshot"
    snapshot.mkdir()
    copies = []
    for i, reference in enumerate(code):
        path = snapshot / f"{i:02}_{Path(reference['path']).name}"
        with path.open("xb") as handle:
            handle.write(Path(reference["path"]).read_bytes())
        copied = pin(path)
        if copied["sha256"] != reference["sha256"]:
            raise ValueError("Implementation changed during registration")
        copies.append({"source": reference, "copy": copied})
    record = {"schema": METHOD_ID + "_registration", "method_id": METHOD_ID,
        "registered_at": now(), "spec": deepcopy(SPEC), "source": meta,
        "input_arguments": {"source_run": str(Path(source_run).resolve()),
            "quality_result": str(Path(quality_result).resolve())},
        "source_note": note, "code": code, "code_snapshots": copies,
        "new_scores_built": False, "engine_advanced": False, "accounts_created": False, **FLAGS}
    save(folder / "registration.json", record)
    return record


def _registration(path):
    path = Path(path).resolve()
    record = read(path)
    if (record.get("schema") != METHOD_ID + "_registration" or record.get("spec") != SPEC
            or record.get("code") != [pin(file) for file in _code_files()]):
        raise ValueError("Registered retrospective rules or implementation changed")
    for reference in [*_refs(record["source"]), *_refs(record["code_snapshots"]),
                      *([record["source_note"]] if record.get("source_note") else [])]:
        if pin(reference["path"]) != reference:
            raise ValueError("Registered source, note or code snapshot changed")
    _validate_meta(record["source"])
    return record


class AnnualQualityValueSavedFeed(SavedRankFeed):
    """Original saved-rank signal contract plus explicitly saved whole-day NC."""
    def __init__(self, base, folder, recipe, config, dates, status_by_day):
        expected = [day for day in month_ends(base.calendar, END) if START <= day <= END]
        if set(status_by_day) != set(expected):
            raise ValueError("One shared formation status for every original month end required")
        self.unavailable = {day: row["reason"] for day, row in status_by_day.items() if row["status"] == "NO_CHANGE"}
        if any(row["status"] not in ("FULL_TARGET", "NO_CHANGE") for row in status_by_day.values()):
            raise ValueError("Unknown formation status cannot become a trade or empty target")
        if not self.unavailable:
            super().__init__(base, folder, recipe, config, dates)
            return
        self.base, self.recipe, self.config, self.fractional_allocation = base, recipe, config, None
        frame = pd.read_csv(Path(folder) / "rankings.csv", dtype={"date": str, "symbol": str})
        known = set(expected) - set(self.unavailable)
        if set(frame.date) != known or not set(expected) <= set(dates):
            raise ValueError("Saved top30 and explicit NC dates must exactly partition the original schedule")
        self.ranks = {}
        for day, batch in frame.groupby("date", sort=True):
            batch = batch.sort_values("rank")
            if (batch["rank"].tolist() != list(range(1, config.retain_rank + 1))
                    or batch.symbol.duplicated().any() or not set(batch.symbol) <= set(base.symbols)
                    or not batch.score.map(lambda value: pd.notna(value) and abs(value) < float("inf")).all()
                    or not batch.score.is_monotonic_decreasing):
                raise ValueError("The original fixed20/rank30 validation failed")
            self.ranks[day] = batch[["symbol", "score"]].to_dict("records")

    def signal_frame(self, day, config, context):
        if day in self.unavailable:
            # Retain the original REPLAY/config validation before returning NC.
            super().signal_frame(day, config, context)
            return SignalUnavailable("annual_quality_value_shared_pool_unknown:" + str(self.unavailable[day]))
        return super().signal_frame(day, config, context)


def _case_observation(folder, outcome):
    account_file = folder / "account.json"
    state = read(account_file) if account_file.exists() else None
    orders = len(state["orders"]) if state is not None else None
    fills = len(state["fills"]) if state is not None else None
    completed = outcome.get("status") == "completed" and outcome.get("completed_sessions") == SESSIONS
    available = completed and fills is not None and fills > 0
    keys = ("net_return", "max_drawdown", "fees", "final_equity", "turnover_value", "turnover_over_initial",
        "sharpe_zero_rf_252", "stale_valuation_days")
    return {"status": outcome.get("status"), "completed_sessions": outcome.get("completed_sessions"),
        "last_completed_session": state.get("day") if state else outcome.get("last_completed_session"),
        "attempted_session": outcome.get("attempted_session"), "error": outcome.get("error"),
        "complete_PnL_available": available, "metrics": {key: outcome.get(key) for key in keys} if available else None,
        "parent_orders": orders, "fill_callbacks": fills,
        "residual_positions": {symbol: deepcopy(lots) for symbol, lots in state["positions"].items()
            if sum(lot["qty"] for lot in lots) > 0} if state is not None else None,
        "pending_targets": deepcopy(state.get("pending_targets")) if state is not None else None,
        "saved_fees_rmb": sum(fill["fees"] for fill in state["fills"].values()) / 100 if state is not None else None,
        "account": pin(account_file) if state is not None else None,
        "replay": pin(folder / "replay.json") if (folder / "replay.json").exists() else None,
        "account_window_completed": completed,
        "result_boundary": "full_window_marked_account_return_not_liquidated_return" if available else "full_window_return_unavailable",
        "raw_saved_outcome": deepcopy(outcome)}


def _manifest_comparison(original, actual):
    """Permit only the newer Feed's descriptive, unselected-scope metadata."""
    allowed = {"full_canonical_codes", "listing_master_kind", "research_scope",
        "research_scope_kind", "research_scope_excluded_codes"}
    missing = sorted(set(original) - set(actual))
    changed = [key for key in original if key in actual and
        json.dumps(clean(original[key]), sort_keys=True, allow_nan=False) !=
        json.dumps(clean(actual[key]), sort_keys=True, allow_nan=False)]
    added = set(actual) - set(original)
    unsafe = sorted(added - allowed)
    metadata_ok = (not added or allowed <= set(actual) and
        type(actual["full_canonical_codes"]) is int and type(actual.get("canonical_codes")) is int and
        actual["full_canonical_codes"] == actual["canonical_codes"] and
        actual["listing_master_kind"] == "legacy_provider_listing" and
        actual["research_scope"] is None and actual["research_scope_kind"] is None and
        type(actual["research_scope_excluded_codes"]) is int and actual["research_scope_excluded_codes"] == 0)
    return {"schema": METHOD_ID + "_feed_manifest_comparison", "compared_at": now(),
        "compatible": not missing and not changed and not unsafe and metadata_ok,
        "original_key_count": len(original), "original_keys_all_exact": not missing and not changed,
        "missing_original_keys": missing, "changed_original_keys": changed,
        "added_fields": {key: actual[key] for key in sorted(added)}, "unknown_added_fields": unsafe,
        "added_fields_prove_unselected_scope": metadata_ok,
        "original_scope_price_action_reference_calendar_or_costs_changed": bool(missing or changed),
        "basis": "exact_original_JSON_types_and_values_plus_five_allowlisted_descriptive_fields_only",
        **FLAGS}


def run(registration, out):
    record = _registration(registration)
    meta = record["source"]
    protected = {Path(reference["path"]).parent for reference in _refs(meta)} | {Path(registration).resolve().parent}
    folder = _directory(out, protected, external=True)
    folder.mkdir(parents=True, exist_ok=False)
    result = {"schema": METHOD_ID + "_result", "method_id": METHOD_ID, "status": "building_new_joint_scores",
        "started_at": now(), "registration": pin(registration), "spec": deepcopy(SPEC),
        "source_clocks": meta["source_clocks"], "cases": [], "comparisons": [],
        "engine_advanced": False, "accounts_created": False, **FLAGS}
    save(folder / "started.json", result)
    base = None
    try:
        built = inputs.build_inputs(record["input_arguments"]["source_run"],
            record["input_arguments"]["quality_result"], expected_meta=meta)
        for name in ("full_pool", "pool"):
            built[name].to_parquet(folder / (name + ".parquet"), index=False)
        save(folder / "coverage.json", built["coverage"])
        save(folder / "status_by_day.json", built["status_by_day"])
        ranking_refs = {}
        for case in CASES:
            path = folder / "ranks" / case
            path.mkdir(parents=True)
            built["ranks"][case].to_csv(path / "rankings.csv", index=False)
            ranking_refs[case] = pin(path / "rankings.csv")
        seal = {"schema": METHOD_ID + "_formation_seal", "sealed_at": now(),
            "registration": result["registration"], "ranks": ranking_refs,
            "full_pool": pin(folder / "full_pool.parquet"), "joint_pool": pin(folder / "pool.parquet"),
            "coverage": pin(folder / "coverage.json"), "status_by_day": pin(folder / "status_by_day.json"),
            "future_outcomes_used_for_ranking": False, "engine_advanced": False, **FLAGS}
        save(folder / "formation_seal.json", seal)
        result.update(formation_seal=pin(folder / "formation_seal.json"), score_sealed_at=seal["sealed_at"])
        config = _validate_meta(meta)
        stock = meta["stock_inputs"]
        base = BulkResearchFeed(stock["price_view"], stock["event_view"], stock["reference_view"], stock["listing_master"])
        save(folder / "actual_feed_manifest.json", base.manifest)
        source = read(meta["source_run"])
        comparison = _manifest_comparison(source["protocol"]["stock_manifest"], base.manifest)
        save(folder / "feed_manifest_comparison.json", comparison)
        result.update(actual_feed_manifest=pin(folder / "actual_feed_manifest.json"),
            feed_manifest_comparison=pin(folder / "feed_manifest_comparison.json"))
        if not comparison["compatible"]:
            raise ValueError("The original source manifest differs beyond allowlisted unselected-scope metadata")
        dates = [day for day in base.calendar if START <= day <= END]
        if dates != meta["replay_days"] or base.calendar != meta["calendar"]:
            raise ValueError("The original exact source calendar changed")
        for case in CASES:
            feed = AnnualQualityValueSavedFeed(base, folder / "ranks" / case,
                METHOD_ID + ":" + case, config, dates, built["status_by_day"])
            destination = folder / "accounts" / case
            outcome = baseline_ledger_result(feed, config, dates, destination)
            result["cases"].append({"case_id": case, **_case_observation(destination, outcome)})
            result.update(engine_advanced=any((row.get("completed_sessions") or 0) > 0 for row in result["cases"]),
                accounts_created=any(row["account"] is not None for row in result["cases"]))
            save(folder / (case + "_observation.json"), result["cases"][-1])
        by_case = {row["case_id"]: row for row in result["cases"]}
        for left, right in (("quality_value_half_half", "value_only"),
                ("quality_value_half_half", "annual_quality_only"),
                ("quality_value_half_half", "matched_momentum")):
            a, b = by_case[left], by_case[right]
            valid = a["complete_PnL_available"] and b["complete_PnL_available"]
            result["comparisons"].append({"candidate": left, "control": right, "available": valid,
                "net_return_difference": a["metrics"]["net_return"] - b["metrics"]["net_return"] if valid else None,
                "drawdown_difference": a["metrics"]["max_drawdown"] - b["metrics"]["max_drawdown"] if valid else None})
        result["status"] = ("completed_four_retrospective_research_accounts" if all(
            row["account_window_completed"] for row in result["cases"]) else "partial_retrospective_research_accounts")
    except Exception as error:
        result.update(status="failed_retrospective_research_no_retry", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        if base is not None:
            base.close()
        result["completed_at"] = now()
        save(folder / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    declaration = commands.add_parser("register")
    declaration.add_argument("--source-run", type=Path, required=True)
    declaration.add_argument("--quality-result", type=Path, required=True)
    declaration.add_argument("--source-note", type=Path)
    declaration.add_argument("--out", type=Path, required=True)
    replay = commands.add_parser("run")
    replay.add_argument("--registration", type=Path, required=True)
    replay.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    answer = register(args.source_run, args.quality_result, args.out, source_note=args.source_note) if args.action == "register" else run(args.registration, args.out)
    print(json.dumps(clean(answer), ensure_ascii=False, allow_nan=False))
    return 0 if args.action == "register" or answer["status"] == "completed_four_retrospective_research_accounts" else 1


if __name__ == "__main__":
    raise SystemExit(main())
