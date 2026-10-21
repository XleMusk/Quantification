"""Publish one frozen Alpha158 research target using real recording time.

Existing dated top-five identities carry the cumulative capacity convention.
This consumer does not call Engine, create state, size orders, or read D+1 bars.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..contracts import IntentKind, MARKET_TZ, PortfolioIntent
from ..engine import choose_targets
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .ipo_qualification import load_ipo_qualification
from .recent_alpha158_panel_replay import (PROTOCOL, ExactSourceRawFeed, _load,
    _raw_issues, _reference)
from .recent_source_alpha158 import _aware
from .stockdb_alpha158_lgbm_diagnostic import SEEDS, _resolve_model
from .stockdb_alpha158_lgbm_panel_replay import _config, _first_seen_cap


SCHEMA = "recent_alpha158_dated_target_publication_v1"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "model_refit": False, "model_trained": False,
    "future_labels_used_for_score": False, "account_created": False, "ledger_mutated": False,
    "persistent_ledger_mutated": False, "orders_generated": False, "paper_orders_generated": False,
    "ths_payload_generated": False, "broker_interacted": False, "provider_contacted": False,
    "pit_verified": False, "corporate_actions_verified": False, "formal_target_admitted": False}
MODEL_FIELDS = ("method_id", "model_run_sha256", "seed_models", "training_segments", "catalog_sha256",
    "frozen_training_catalog_sha256", "score_aggregation")


def _now():
    return datetime.now(MARKET_TZ)


def _record(path):
    path = Path(path).resolve()
    path = path / "result.json" if path.is_dir() else path
    value = json.loads(path.read_text(encoding="utf-8"))
    return path, value


def _model_contract(record):
    if any(key not in record for key in MODEL_FIELDS):
        raise ValueError("Explicit frozen model, preprocessing, catalog and datewise aggregation proofs required")
    path, wrapper, models = _resolve_model(record["model_run"])
    if (digest(path) != record["model_run_sha256"] or wrapper.get("models_refit") is not False
            or wrapper["config"]["segments"] != record["training_segments"]
            or record["score_aggregation"] != "within_each_date_seed_percentile_mean_no_cross_date_ranking"
            or record["catalog_sha256"] != record["frozen_training_catalog_sha256"]
            or digest(path.parent / "alpha158_catalog.csv") != record["frozen_training_catalog_sha256"]):
        raise ValueError("Frozen model wrapper, catalog or aggregation changed")
    if set(record["seed_models"]) != {str(seed) for seed in SEEDS}:
        raise ValueError("Exactly the three frozen B0 seeds are required")
    for seed in SEEDS:
        saved, folder = record["seed_models"][str(seed)], models[seed]
        if (folder.name != f"B0_seed{seed}" or Path(saved["folder"]).resolve() != folder
                or digest(folder / "model.txt") != saved["model_sha256"]
                or digest(folder / "preprocessing.csv") != saved["preprocessing_sha256"]):
            raise ValueError("Frozen B0 model or preprocessing bytes changed")
    return {key: deepcopy(record[key]) for key in MODEL_FIELDS}


def _ordered_symbols(rows):
    return list(dict.fromkeys(rows.sort_values(["date", "score", "symbol"],
        ascending=[True, False, True], kind="stable").symbol))


def _prepare_case(current, prefix, base, day, previous, qualification, case):
    combined = pd.concat([prefix, current], ignore_index=True)
    if case["max_symbols"] is None:
        allowed, before, dropped = combined, prefix, 0
    else:
        allowed, dropped = _first_seen_cap(combined, case["max_symbols"])
        before, _ = _first_seen_cap(prefix, case["max_symbols"])
    selected = allowed.loc[allowed.date.eq(day)].copy()
    candidates = selected[["symbol", "score"]].to_dict("records")
    weights = choose_targets(candidates, [], _config(100000, 5))
    members = selected.symbol.tolist()
    issues = _raw_issues(base, day, members)
    refs = [_reference(base, day, symbol, previous) for symbol in members]
    ipo = [qualification.assess(symbol, day, decision_as_of=day + "T15:05:00+08:00") for symbol in members]
    reasons = []
    # The frozen replay keeps NO_CHANGE when capacity removes every event.
    # An observed top-five batch is not an explicit instruction to liquidate.
    if not candidates:
        reasons.append("no_selected_events_after_fixed_capacity_NO_CHANGE")
    if issues:
        reasons.append("selected_current_raw_unknown")
    if any(row["status"] != "consistent_proxy" for row in refs):
        reasons.append("selected_current_reference_unknown_or_incompatible")
    if any(row["engine_block_reason"] is not None for row in ipo):
        reasons.append("selected_IPO_qualification_unknown_or_unsupported_at_own_clock")
    return {"case": case["id"], "max_symbols": case["max_symbols"],
        "ranked_top5_before_cap": current.to_dict("records"), "selected_events": selected.to_dict("records"),
        "cap_symbols_before": _ordered_symbols(before), "cap_symbols_after": _ordered_symbols(allowed),
        "causal_cap_dropped_event_rows_total": dropped,
        "causal_cap_dropped_current_rows": len(current) - len(selected),
        "raw_rule_weights_before_qualification": weights, "raw_issues": issues,
        "reference_evidence": refs, "IPO_evidence": ipo, "reasons": reasons}


def publish(batch_run, prefix_batch_runs, calendar, ipo_evidence, research_master_run, out):
    """Publish an unbound typed intent; every source/score is D or earlier."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Target publication requires a new output directory")
    if not prefix_batch_runs:
        raise ValueError("Existing score top-five prefix is required; capacity cannot reset")
    current, raw, days, sessions, previous, source, anchor = _load(batch_run, calendar)
    _, record = _record(batch_run)
    contract = _model_contract(record)
    day = days[-1]
    if (record["score_dates"] != [day] or sorted(current.date.unique()) != [day]
            or str(raw.date.max()) != day or day not in sessions or sessions.index(day) + 1 >= len(sessions)):
        raise ValueError("Exactly one last complete score day and its pinned next exchange session required; no successor raw")
    execution_day = sessions[sessions.index(day) + 1]
    prefixes, prefix_proofs, prefix_dates = [], [], []
    for batch in prefix_batch_runs:
        selected, _, old_days, old_sessions, _, proof, old_anchor = _load(batch, calendar)
        _, old_record = _record(batch)
        if (_model_contract(old_record) != contract or old_sessions != sessions
                or old_days != days[:len(old_days)] or old_days[-1] >= day
                or proof["source_proofs"] != source["source_proofs"][:len(old_days)]
                or old_anchor != anchor):
            raise ValueError("Existing prefix must retain identical model and exact prior source history")
        if (not old_record["score_dates"] or max(old_record["score_dates"]) >= day
                or set(prefix_dates).intersection(old_record["score_dates"])):
            raise ValueError("Score prefix cannot overlap, include current/future dates or omit explicit dates")
        prefixes.append(selected)
        prefix_dates.extend(old_record["score_dates"])
        prefix_proofs.append(proof)
    if sorted(prefix_dates) != days[:-1]:
        raise ValueError("Existing score prefix must cover every prior provider session exactly; no reset or gap")
    prefix = pd.concat(prefixes, ignore_index=True).sort_values(["date", "score", "symbol"],
        ascending=[True, False, True], kind="stable").reset_index(drop=True)
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    if tuple(qualification.calendar) != tuple(sessions):
        raise ValueError("Publication and listing qualification calendars differ")
    base = ExactSourceRawFeed(raw, days, sorted(current.symbol.unique()), context_anchor=anchor)
    prepared = [_prepare_case(current, prefix, base, day, previous, qualification, case) for case in PROTOCOL["cases"]]
    close_at, cutoff = _aware(day + "T15:00:00+08:00"), _aware(day + "T15:05:00+08:00")
    terminals = []
    for proof in source["source_proofs"]:
        stamp = proof.get("source_completed_at") or proof.get("source_updated_at")
        if stamp is None or _aware(stamp) < _aware(proof["day"] + "T15:00:00+08:00"):
            raise ValueError("Every complete close requires a real post-close source terminal clock")
        terminals.append(_aware(stamp))
    source_ready = max(terminals)
    provider_ready, computed = _aware(source["source_available_at"]), _aware(source["score_computed_at"])
    recorded = _now().astimezone(MARKET_TZ)
    if (not close_at <= source_ready <= provider_ready <= computed <= recorded or recorded < cutoff
            or any(_aware(p["score_computed_at"]) > computed for p in prefix_proofs)):
        raise ValueError("Actual recording must follow all source, provider, frozen scores and close clocks")
    opening = _aware(execution_day + "T09:30:00+08:00")
    same_day = recorded.date().isoformat() == day
    late = recorded >= opening
    clock_bindable = same_day and not late
    cases = []
    for item in prepared:
        reasons = item["reasons"]
        if not clock_bindable:
            reasons.append("late_for_original_next_open" if late else "actual_recording_date_not_source_day_unbindable")
        metadata = {"context": None, "decided_at": recorded.isoformat(),
            "source": f"research:recent_alpha158_dated_target_publication:{item['case']}"}
        if clock_bindable:
            metadata["execution_not_before"] = execution_day
        intent = (PortfolioIntent.no_change(";".join(reasons), **metadata) if reasons else
            PortfolioIntent.full_target(item["raw_rule_weights_before_qualification"], **metadata))
        item.update(status="NO_CHANGE" if intent.kind == IntentKind.NO_CHANGE else "unbound_research_full_target",
            portfolio_intent=intent.to_dict(), target_weights=dict(intent.weights) if intent.kind == IntentKind.FULL_TARGET else None,
            target_cash_weight=1 - sum(dict(intent.weights).values()) if intent.kind == IntentKind.FULL_TARGET else None,
            execution_binding_pending=not clock_bindable, holdings_binding_pending=True,
            order_sizing_binding_pending=True, successor_raw_used=False, successor_prices_used_for_ranking_or_refill=False,
            execution_admitted=False)
        cases.append(item)
    result = {"schema": SCHEMA, "status": "late_context_only" if late else
        "unbound_recording_date_context_only" if not same_day else "close_research_target_publication_only",
        "source_close_day": day, "proposed_execution_day": execution_day,
        "next_open_deadline": opening.isoformat(), "source_ready_at": source_ready.isoformat(),
        "provider_ready_at": provider_ready.isoformat(), "score_computed_at": computed.isoformat(),
        "recorded_at": recorded.isoformat(), "publication_origin": "local_recording_now",
        "publication_time_backfilled": False, "execution_day_rolled_forward": False,
        "timeliness_uses_actual_recording": True, "timely_for_next_open": recorded < opening,
        "execution_binding_pending": not clock_bindable, "protocol": deepcopy(PROTOCOL),
        "config": _config(100000, 5).to_dict(), "holding_policy": "daily_rebalance_holdings_count_5_no_fixed_session_expiry",
        "source": source, "frozen_model_contract": contract, "capacity_prefix": {"batches": prefix_proofs,
            "score_dates": sorted(prefix_dates), "top5_events": prefix.to_dict("records"),
            "role": "existing_ranked_identities_only_not_historical_target_publication_or_PnL",
            "old_scores_recomputed": False, "capacity_reset": False},
        "calendar": str(Path(calendar).resolve()), "calendar_sha256": digest(calendar),
        "ipo_qualification": deepcopy(qualification.verification), "cases": cases, "case_count": len(cases),
        "case_selection_performed": False, "successor_raw_required_for_target_weights": False,
        "successor_raw_used": False,
        "target_holdings_generated": any(case["target_weights"] is not None for case in cases),
        "research_rule_weights_generated": True, "persistent_account_used": False,
        "engine_ledger_replay_completed": False, **FLAGS,
        "limitations": ["Frozen weights use cross-lineage research features; this does not validate Alpha or total returns.",
            "Both fixed capacities are published without a winner, refill or parameter change.",
            "Raw reference consistency is only a one-percent proxy, not proof that corporate actions are absent.",
            "Unknown selected-member evidence preserves the entire case as NO_CHANGE, never an empty cash target.",
            "Typed intents use actual recording time and no account context; later Engine binding is separate.",
            "Five holdings is a portfolio count, not a five-session expiry policy."]}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-run", type=Path, required=True)
    parser.add_argument("--prefix-batch-run", type=Path, action="append", required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path, required=True)
    parser.add_argument("--research-master-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = publish(args.batch_run, args.prefix_batch_run, args.calendar, args.ipo_evidence, args.research_master_run, args.out)
    print(json.dumps({key: result[key] for key in ("status", "source_close_day", "proposed_execution_day",
        "recorded_at", "case_count", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
