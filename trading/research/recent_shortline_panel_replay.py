"""Replay the recent fixed emotion diagnostic through the sole Engine/Ledger.

This is a late-derived two-case mechanical research diagnostic.  Whole-day
membership uncertainty and known empty selections both mean NO_CHANGE.  The
successor raw bar is a quality witness, never a ranking or replacement input.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .ipo_qualification import load_ipo_qualification
from .leader_daily_replay import CommonCohortDecisionFeed
from .recent_alpha158_forward_run import _provider, _sessions
from .recent_alpha158_panel_replay import (ExactSourceRawFeed, _QualifiedDecisionFeed,
    _raw_issues, _reference)
from .recent_shortline_panel import FLAGS as PANEL_FLAGS, METHOD as PANEL_METHOD, SCHEMA as PANEL_SCHEMA, _flag, _stamp
from .shortline_decision_v2 import BATCH_SCHEMA
from .shortline_panel_replay import _config
from .stockdb_alpha158_lgbm_panel_replay import _first_seen_cap


SCHEMA = "recent_shortline_fixed_emotion_engine_ledger_diagnostic_v1"
FIELDS = ["event_id", "symbol", "date", "stage", "decision_eligible", "feature_available_at",
    "amount_rel20_pre", "market_breadth", "fixed_rule_pass", "fixed_rule_score"]
PROTOCOL = {"id": "recent_source_fixed_emotion_two_panels_v1",
    "rule": "amount_rel20_pre<=1 AND market_breadth>=0", "top_per_day": 5,
    "initial_cash": 100000, "holdings": 5,
    "cases": [{"id": "causal_first_seen_cap20", "max_symbols": 20},
              {"id": "uncapped", "max_symbols": None}],
    "ranking": "fixed_binary_score_desc_symbol_asc_stage_asc_before_quality_checks",
    "capacity": "first_seen_from_known_raw_top5_before_whole_day_unknown_gate_no_refill",
    "incomplete_stage_batch": "whole_day_NO_CHANGE", "empty_selection": "NO_CHANGE",
    "holding_policy": "daily_rebalance_holdings_5_no_fixed_session_expiry",
    "reference_bound": .01, "metrics_policy": "always_null_no_complete_PnL_claim",
    "successor_raw": "exact_calendar_quality_witness_not_ranking_or_survival_filter"}
FLAGS = {"candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
    "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "broker_interacted": False, "ths_payload_generated": False,
    "persistent_ledger_mutated": False, "user_account_mutated": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "historical_signal_publication_verified": False, "current_signal_admitted": False}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _candidates(path, days, feature_clocks, created_at):
    """Read only signal-date columns; no event/continuation label file is read."""
    frame = pd.read_csv(path, usecols=FIELDS, dtype={"symbol": str, "date": str, "event_id": str})
    frame["stage"] = pd.to_numeric(frame.stage, errors="raise")
    if (frame.stage.isna().any() or not frame.stage.isin([1, 2, 3]).all()
            or not frame.date.isin(days).all()
            or not frame.symbol.str.fullmatch(r"(?:60\d{4}\.SSE|688\d{3}\.SSE|00\d{4}\.SZSE|30\d{4}\.SZSE)").all()
            or frame.duplicated(["date", "symbol"]).any()):
        raise ValueError("Unique exact-date ordinary stage123 event identities required")
    frame["stage"] = frame.stage.astype(int)
    if not frame.event_id.eq(frame.symbol + "|" + frame.date + "|s" + frame.stage.astype(str)).all():
        raise ValueError("Diagnostic event identity changed")
    for field in ("amount_rel20_pre", "market_breadth", "fixed_rule_score"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    eligible, passed = [], []
    for row in frame.to_dict("records"):
        known, actual = _flag(row["decision_eligible"]), _flag(row["fixed_rule_pass"])
        if known is None:
            raise ValueError("Signal-date eligibility must retain the producer's explicit boolean")
        ratio, breadth = row["amount_rel20_pre"], row["market_breadth"]
        if not np.isfinite(breadth) or not -1 <= breadth <= 1:
            raise ValueError("Signal-date active-source breadth missing or invalid")
        expected = bool(ratio <= 1 and breadth >= 0) if known and np.isfinite(ratio) else None
        if actual is not expected or (actual is True and row["fixed_rule_score"] != 1.0) or (
                actual is not True and pd.notna(row["fixed_rule_score"])):
            raise ValueError("Fixed expression, unknown state or binary compatibility score changed")
        if (_stamp(row["feature_available_at"]) != _stamp(feature_clocks[row["date"]])
                or _stamp(row["feature_available_at"]) > _stamp(created_at)):
            raise ValueError("Signal feature actual clock differs from its own preview/source")
        eligible.append(known)
        passed.append(actual)
    frame["decision_eligible"] = pd.Series(eligible, index=frame.index, dtype=object)
    frame["fixed_rule_pass"] = pd.Series(passed, index=frame.index, dtype=object)
    return frame


def _batch_status(record, frame, days):
    if (record.get("schema") != "recent_shortline_fixed_rule_diagnostic_batches_v1"
            or record.get("future_label_fields_included") is not False
            or any(record.get(k) is not v for k, v in PANEL_FLAGS.items())):
        raise ValueError("Explicit unadmitted fixed-rule candidate batches required")
    batches = record.get("batches", [])
    keys = [(r.get("day"), r.get("stage")) for r in batches]
    if len(keys) != len(set(keys)) or set(keys) != {(day, stage) for day in days for stage in (1, 2, 3)}:
        raise ValueError("All three explicit stage batches are required for every source day")
    result = {}
    for day in days:
        group_batches = sorted((r for r in batches if r["day"] == day), key=lambda r: r["stage"])
        day_unknown = None
        for batch in group_batches:
            group = frame.loc[frame.date.eq(day) & frame.stage.eq(batch["stage"])]
            selected = group.loc[group.fixed_rule_pass.eq(True)]
            missing = group.loc[group.fixed_rule_pass.isna(), "symbol"].tolist()
            unknown = batch.get("potential_unknown_stage_members")
            expected = selected[["event_id", "symbol", "fixed_rule_score"]].to_dict("records")
            if (not isinstance(unknown, list) or len(unknown) != len(set(unknown))
                    or batch.get("unknown_fixed_rule_fields") != missing
                    or batch.get("known_matching_rows") != expected
                    or batch.get("selection_permitted") is not False
                    or batch.get("unknown_is_empty_target") is not False
                    or batch.get("status") != ("incomplete" if unknown or missing else "complete")):
                raise ValueError("Stage coverage must preserve every unknown and exact known match")
            if day_unknown is not None and set(unknown) != day_unknown:
                raise ValueError("The shared unknown-stage cohort cannot differ across stage batches")
            day_unknown = set(unknown)
        result[day] = {"status": "incomplete" if any(r["status"] != "complete" for r in group_batches) else "complete",
            "stage_batches": group_batches, "unknown_is_empty_target": False}
    return result


def _rank(frame):
    selected = frame.loc[frame.fixed_rule_pass.eq(True),
        ["event_id", "symbol", "date", "stage", "fixed_rule_score", "feature_available_at"]].rename(
            columns={"fixed_rule_score": "score"})
    return (selected.sort_values(["date", "score", "symbol", "stage"],
        ascending=[True, False, True, True], kind="stable").groupby("date", sort=True).head(5).reset_index(drop=True))


def _load(panel, provider_view, calendar, qualification, now):
    panel = Path(panel).resolve()
    path = panel / "result.json" if panel.is_dir() else panel
    record = _json(path)
    calendar = Path(calendar).resolve()
    sessions, previous = _sessions(calendar)
    if (record.get("schema") != PANEL_SCHEMA or record.get("method_id") != PANEL_METHOD
            or record.get("status") != "completed_recent_shortline_diagnostics"
            or any(record.get(k) is not v for k, v in PANEL_FLAGS.items())
            or record.get("engine_advanced") is not False or record.get("return_metrics") is not None
            or record.get("future_labels_exported_to_candidate_file") is not False
            or record.get("historical_publication_verified") is not False
            or record.get("fixed_rule") != PROTOCOL["rule"]
            or record.get("ipo_qualification") != qualification.verification
            or tuple(qualification.calendar) != tuple(sessions)
            or Path(record["calendar"]).resolve() != calendar or digest(calendar) != record["calendar_sha256"]
            or _stamp(record["created_at"]) > now):
        raise ValueError("Completed qualified recent shortline panel and its original false boundaries required")
    provider = _provider(provider_view, calendar, now)
    physical, days = provider["record"], provider["days"]
    if (record["source_days"] != days or len(record["preview_sources"]) != len(days)
            or Path(record["original_history_snapshot"]).resolve() != Path(physical["history_snapshot"]).resolve()
            or record["original_history_manifest_sha256"] != physical["original_history_manifest_sha256"]
            or record["base_history_cutoff"] != physical["base_history_end"]):
        raise ValueError("Diagnostic and execution raw must share the exact recent-source/history prefix")
    pins = {str(path): digest(path), str(calendar): digest(calendar), provider["path"]: digest(provider["path"]),
        physical["recent_raw_observations_file"]: physical["recent_raw_observations_sha256"]}
    clocks, shared = {}, set()
    for day, proof in zip(days, record["preview_sources"]):
        preview, features = Path(proof["preview"]).resolve(), Path(proof["feature_file"]).resolve()
        if digest(preview) != proof["preview_sha256"] or digest(features) != proof["feature_sha256"]:
            raise ValueError("Saved preview or qualified feature bytes changed")
        pins.update({str(preview): proof["preview_sha256"], str(features): proof["feature_sha256"]})
        envelope, recent = _json(preview), proof.get("recent_source_view") or {}
        expected = [p for p in physical["source_proofs"] if p["day"] <= day]
        current = expected[-1]
        qualification_proof = proof.get("feature_qualification") or {}
        if (envelope.get("day") != day or envelope.get("schema") != "leader_complete_close_preview_v1"
                or envelope.get("status") != "complete_source_close_research_preview_only"
                or recent.get("source_days_used") != [p["day"] for p in expected]
                or recent.get("source_proofs") != expected
                or envelope.get("recent_source_lineage", {}).get("source_proofs") != expected
                or recent.get("historical_publication_verified") is not False
                or recent.get("derived_at") != envelope["created_at"]
                or qualification_proof.get("verification") != qualification.verification
                or qualification_proof.get("decision_as_of") != day + "T15:05:00+08:00"
                or proof.get("features_match_current_active_raw") is not True
                or proof.get("raw_daily_sha256") != current["raw_daily_sha256"]
                or Path(proof["source_folder"]).resolve() != Path(current["source_folder"]).resolve()
                or _stamp(envelope["created_at"]) > _stamp(record["created_at"])):
            raise ValueError("Each qualified preview needs the identical own-dated provider proof prefix")
        shared.add(recent.get("shared_view_id"))
        clocks[day] = max((envelope["created_at"], *[p.get("source_completed_at") or p["source_updated_at"]
            for p in expected]), key=_stamp)
    if len(shared) != 1 or None in shared:
        raise ValueError("One coherent recent qualified feature view required")
    csv = path.parent / "fixed_rule_diagnostic.csv"
    batch_path = Path(record["candidate_batches"]).resolve()
    batch_path.relative_to(path.parent)
    if batch_path != path.parent / "candidate_batches.json":
        raise ValueError("Use the producer's own named candidate batch sidecar")
    pins.update({str(csv): digest(csv), str(batch_path): digest(batch_path)})
    frame = _candidates(csv, days, clocks, record["created_at"])
    if len(frame) != record["event_rows"] or int(frame.fixed_rule_pass.eq(True).sum()) != record["fixed_rule_pass_rows"]:
        raise ValueError("Diagnostic rows or rule count changed")
    status = _batch_status(_json(batch_path), frame, days)
    if sum(r["status"] == "incomplete" for day in status.values() for r in day["stage_batches"]) != record["incomplete_stage_day_batches"]:
        raise ValueError("Unknown-stage batch count changed")
    raw = provider["raw"]
    for day, group in frame.groupby("date", sort=True):
        own = raw.loc[raw.date.eq(day) & raw.tradestatus.eq("1") & raw.isST.eq("0")
            & pd.to_numeric(raw.volume, errors="coerce").gt(0)]
        pct = pd.to_numeric(own.pctChg, errors="coerce")
        breadth = (int(pct.gt(0).sum()) - int(pct.lt(0).sum())) / len(own) if len(own) else None
        if breadth is None or not np.isfinite(pct).all() or not np.allclose(group.market_breadth, breadth, rtol=0, atol=1e-10):
            raise ValueError("Rule breadth must match its entire exact active non-ST raw day")
    for ref in provider["anchor_proof"].get("partitions", []):
        pins[ref["file"]] = ref["sha256"]
    return {"selected": _rank(frame), "raw": raw, "days": days, "sessions": sessions, "previous": previous,
        "anchor": provider["anchor"], "day_status": status, "pins": pins,
        "source": {"panel": str(path), "panel_sha256": pins[str(path)], "panel_created_at": record["created_at"],
            "candidate_file": str(csv), "candidate_sha256": pins[str(csv)], "candidate_batches": str(batch_path),
            "candidate_batches_sha256": pins[str(batch_path)], "provider_view": provider["path"],
            "provider_result_sha256": pins[provider["path"]], "source_proofs": physical["source_proofs"],
            "context_anchor": provider["anchor_proof"], "input_files": pins,
            "rank_fields": ["fixed_rule_score", "symbol", "stage"], "future_label_columns_read": False}}


class _ExactCalendarFeed(ExactSourceRawFeed):
    def __init__(self, raw, days, symbols, sessions, *, context_anchor=None):
        super().__init__(raw, days, symbols, context_anchor=context_anchor)
        self.next_sessions = dict(zip(sessions, sessions[1:]))

    def next_day(self, day):
        if day not in self.next_sessions:
            raise ValueError("Exact next calendar session unavailable; never infer an execution date")
        return self.next_sessions[day]


def _case(selected, raw, days, sessions, previous, day_status, qualification, case, *, context_anchor=None):
    candidates, dropped = (selected.copy(), 0) if case["max_symbols"] is None else _first_seen_cap(selected, 20)
    symbols = sorted(candidates.symbol.unique())
    base = _ExactCalendarFeed(raw, days, symbols, sessions, context_anchor=context_anchor)
    config, state = _config(100000, 5), new_state(100000)
    batches = {day: {"schema": BATCH_SCHEMA, "day": day, "status": "incomplete", "candidates": [],
        "source": "research:recent_shortline_fixed_emotion_binary"} for day in days}
    common = CommonCohortDecisionFeed(base, batches, state, market_defense=False)
    feed = _QualifiedDecisionFeed(common, qualification)
    context = RunContext("recent-shortline-fixed-emotion:" + case["id"], TradingMode.REPLAY)
    grouped = {day: group[["symbol", "score", "stage"]].to_dict("records")
               for day, group in candidates.groupby("date", sort=True)}
    daily, decisions, blocked = [], [], None
    for index, day in enumerate(days):
        events = grouped.get(day, [])
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        held_refs = [_reference(base, day, symbol, previous) for symbol in sorted(held)]
        held_raw = _raw_issues(base, day, held)
        if held_raw or any(r["status"] != "consistent_proxy" for r in held_refs):
            blocked = {"day": day, "reason": "held_raw_or_action_reference_unresolved", "raw_issues": held_raw,
                "reference_evidence": held_refs, "session_not_committed": True}
            break
        following = base.next_sessions.get(day)
        required = held | set(state.get("pending_targets") or {}) | {r["symbol"] for r in events}
        reasons, raw_issues, refs, ipo = [], [], [], []
        if day_status[day]["status"] != "complete":
            reasons.append("whole_day_stage_membership_or_fixed_rule_fields_incomplete")
        if not events:
            reasons.append("no_fixed_rule_candidates_after_fixed_capacity_NO_CHANGE")
        if following not in days:
            reasons.append("withheld_missing_exact_successor_full_source")
        else:
            raw_issues = _raw_issues(base, day, required) + _raw_issues(base, following, required)
            refs = [_reference(base, d, symbol, previous) for d in (day, following) for symbol in sorted(required)]
            ipo = [qualification.assess(symbol, d, decision_as_of=d + ("T15:05:00+08:00" if d == day else "T09:30:00+08:00"))
                for d in (day, following) for symbol in sorted(required)]
            if raw_issues:
                reasons.append("required_current_or_successor_raw_unknown")
            if any(r["status"] != "consistent_proxy" for r in refs):
                reasons.append("current_or_execution_reference_unknown_or_incompatible")
            if any(r["engine_block_reason"] is not None for r in ipo):
                reasons.append("required_IPO_qualification_unknown_or_unsupported_at_own_clock")
        common.batches[day].update(status="incomplete" if reasons else "complete", candidates=events)
        trial = deepcopy(state)
        common.state = trial
        try:
            row = advance(trial, feed, day, config, index, context=context)
        except (ValueError, KeyError) as error:
            blocked = {"day": day, "reason": "shared_engine_session_blocked",
                "error": f"{type(error).__name__}: {error}", "session_not_committed": True}
            break
        state = trial
        daily.append(row)
        no_change = next((event for event in row["events"] if event["kind"] == "no_decision"), None)
        decisions.append({"day": day, "execution_day": following,
            "status": "NO_CHANGE" if no_change else "FULL_TARGET", "candidates": events,
            "required_members": sorted(required), "reasons": reasons, "raw_issues": raw_issues,
            "reference_evidence": refs, "IPO_evidence": ipo, "source_stage_batches": day_status[day]["stage_batches"],
            "decision_cutoff": day + "T15:05:00+08:00", "no_change_reason": no_change["reason"] if no_change else None,
            "successor_prices_used_for_ranking_or_refill": False, "successor_gate_is_retrospective_quality_witness": True})
    return {"case": case["id"], "max_symbols": case["max_symbols"], "causal_cap_dropped_rows": dropped,
        "status": "blocked_diagnostic_no_complete_PnL" if blocked else "completed_mechanical_research_only",
        "selected_events": candidates.to_dict("records"), "future_path_dropped_rows": 0,
        "decisions": decisions, "daily": daily, "orders": list(state["orders"].values()),
        "fills": list(state["fills"].values()), "account": state, "config": config.to_dict(),
        "blocking_evidence": blocked, "metrics": None, "complete_PnL_available": False,
        "metrics_reason": "short_late_derived_candidate_panel_with_unexecuted_tail_not_validated_economic_return",
        "engine_ledger_replay_completed": blocked is None, "five_session_expiry_enabled": False, **FLAGS}


def run(panel, provider_view, calendar, ipo_evidence, research_master_run, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("A new recent shortline replay output is required")
    now = datetime.now(MARKET_TZ)
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    inputs = _load(panel, provider_view, calendar, qualification, now)
    cases = [_case(inputs["selected"], inputs["raw"], inputs["days"], inputs["sessions"], inputs["previous"],
        inputs["day_status"], qualification, case, context_anchor=inputs["anchor"]) for case in PROTOCOL["cases"]]
    if any(digest(path) != pin for path, pin in inputs["pins"].items()):
        raise ValueError("A replay input changed while the local diagnostic was being read")
    result = {"schema": SCHEMA, "status": "completed_recent_shortline_fixed_rule_engine_ledger_diagnostics",
        "created_at": datetime.now(MARKET_TZ).isoformat(), "protocol": PROTOCOL, "source": inputs["source"],
        "calendar": str(Path(calendar).resolve()), "calendar_sha256": digest(calendar),
        "ipo_qualification": qualification.verification, "source_days": inputs["days"], "cases": cases,
        "metrics": None, "complete_PnL_available": False, "case_selection_performed": False,
        "persistent_account_used": False, "in_memory_research_states": 2,
        "paper_orders_generated": any(c["orders"] for c in cases),
        "target_holdings_generated": any(d["status"] == "FULL_TARGET" for c in cases for d in c["decisions"]), **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    for case in cases:
        folder = out / case["case"]
        folder.mkdir()
        for key in ("decisions", "daily", "orders", "fills", "account"):
            write_json(folder / (key + ".json"), case[key])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("panel", "provider-view", "calendar", "ipo-evidence", "research-master-run", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    result = run(args.panel, args.provider_view, args.calendar, args.ipo_evidence, args.research_master_run, args.out)
    print(json.dumps({"status": result["status"], "cases": [{"case": c["case"], "status": c["status"],
        "orders": len(c["orders"]), "fills": len(c["fills"]), "metrics": None} for c in result["cases"]],
        "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
