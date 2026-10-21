"""Dated shortline diagnostics from saved qualified complete-close previews.

No training, historical-panel overwrite or account path lives here.  The fixed
emotion expression is unchanged, but its active non-ST recent-source denominator
is an explicit new method, not an extension of the old StockDB population.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from ..rules import price_limits, stock_board
from ..symbols import normalize_symbol
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_environment_run import read_preview


SCHEMA = "recent_complete_source_shortline_diagnostic_panel_v1"
METHOD = "qualified_recent_stage123_exact_calendar_witness_active_nonST_v1"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "model_refit": False,
    "future_labels_used_for_score": False, "orders_generated": False,
    "ledger_mutated": False, "provider_contacted": False, "ths_payload_generated": False}


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timezone-aware actual source clocks required")
    return result


def _known(value):
    return value is not None and not pd.isna(value)


def _true(value):
    return _known(value) and value in (True, 1, "True", "true", "1")


def _flag(value):
    if _true(value):
        return True
    if _known(value) and value in (False, 0, "False", "false", "0"):
        return False
    return None


def _reference(row):
    prior, reference = row.get("previous_close"), row.get("preclose_for_limits")
    return (_true(row.get("calendar_contiguous")) and _known(prior) and float(prior) > 0
        and _known(reference) and abs(float(reference) / float(prior) - 1) <= .01)


def _eligible(row):
    return row.get("listing_qualification_status") == "supported" and _reference(row)


def history_features(history):
    """Original 20 observed-row, min5, shifted-median expressions; no D input."""
    history = history.sort_values(["symbol", "date"], kind="stable").copy()
    if history.duplicated(["symbol", "date"]).any():
        raise ValueError("Amount history contains duplicate exact-date identities")
    for name in ("amount", "close", "pct_chg"):
        history[name] = pd.to_numeric(history[name], errors="coerce")
    reference = history.close / (1 + history.pct_chg / 100)
    history = history.loc[np.isfinite(reference) & reference.gt(0)].copy()
    grouped = history.groupby("symbol", sort=False)
    history["amount_median20_pre"] = grouped.amount.transform(lambda x: x.shift(1).rolling(20, min_periods=5).median())
    history["amount_rel20_pre"] = history.amount / history.amount_median20_pre.replace(0, np.nan)
    history["pct_median20_pre"] = grouped.pct_chg.transform(lambda x: x.shift(1).rolling(20, min_periods=5).median())
    history["amount_known_prior20_count"] = grouped.amount.transform(lambda x: x.shift(1).rolling(20, min_periods=1).count())
    history["prior_observed_row_count"] = grouped.cumcount()
    history["prior_window_row_count"] = history.prior_observed_row_count.clip(upper=20)
    return history[["symbol", "date", "amount_rel20_pre", "pct_median20_pre",
        "amount_median20_pre", "amount_known_prior20_count", "prior_observed_row_count", "prior_window_row_count"]]


def build_tables(features, raw_by_day, sessions, availability, history, qualification, observed_at):
    """Pure diagnostic transform: selection fields never depend on labels."""
    now = _stamp(observed_at)
    days = sorted(features)
    if any(_stamp(availability[day]) > now for day in days):
        raise ValueError("A source/feature clock is later than this actual diagnostic")
    next_day = dict(zip(sessions, sessions[1:]))
    lookup = {day: frame.set_index("symbol").to_dict("index") for day, frame in features.items()}
    raw_lookup = {day: frame.set_index("symbol").to_dict("index") for day, frame in raw_by_day.items()}
    def eligible(day, symbol, row):
        original = raw_lookup.get(day, {}).get(symbol, {}).get("preclose")
        try:
            original = float(original)
        except (TypeError, ValueError):
            return False
        return (_eligible(row) and np.isfinite(original) and original > 0
            and abs(original / float(row["previous_close"]) - 1) <= .01)
    hist = history_features(history).set_index(["symbol", "date"]).to_dict("index")
    events, unknown, daily = [], [], []
    for day in days:
        frame = features[day]
        n, up, down = len(frame), int(frame.pct_chg.gt(0).sum()), int(frame.pct_chg.lt(0).sum())
        breadth = (up - down) / n if n else None
        potential = frame.loc[frame.unqualified_limit_up.map(_true) & (frame.stage.isna()
            | ~frame.listing_qualification_status.eq("supported"))]
        for row in potential.to_dict("records"):
            if row.get("listing_qualification_status") in ("unknown", "supported"):
                unknown.append({"date": day, "symbol": row["symbol"], "stage": None,
                    "reason": "potential_stage_membership_unknown", "not_zero_or_negative_example": True})
        raw = raw_by_day[day]
        daily.append({"date": day, "market_row_count": n, "market_up_count": up,
            "market_down_count": down, "market_breadth": breadth,
            "full_ordinary_rows": len(raw), "suspended_rows": int(raw.tradestatus.eq("0").sum()),
            "unknown_stage_membership_count": sum(r["date"] == day for r in unknown),
            "known_excluded_listing_rows": int(frame.listing_qualification_status.str.startswith("known_").sum()),
            "market_scope": "complete_source_active_nonST_SH_SZ_not_old_all_nonST_population"})
        current = frame.loc[frame.limit_up.map(_true) & frame.stage.isin([1, 2, 3])]
        for source in current.to_dict("records"):
            symbol, stage = source["symbol"], int(source["stage"])
            row = {key: source.get(key) for key in ("symbol", "date", "name", "board", "open", "high", "low",
                "close", "amount", "volume", "pct_chg", "preclose_for_limits")}
            row.update(event_id=f"{symbol}|{day}|s{stage}", stage=stage,
                feature_available_at=availability[day], decision_feature_cutoff=day + "T15:05:00+08:00",
                decision_eligible=eligible(day, symbol, source), market_row_count=n, market_up_count=up,
                market_down_count=down, market_breadth=breadth,
                pre_close_source=raw_lookup[day][symbol].get("preclose"),
                **hist.get((symbol, day), {k: None for k in ("amount_rel20_pre", "pct_median20_pre",
                    "amount_median20_pre", "amount_known_prior20_count", "prior_observed_row_count", "prior_window_row_count")}))
            # Future run quality is diagnostic only; it cannot change the D mask.
            maximum, end, witness, cursor, status = stage, day, None, next_day.get(day), "right_censored_snapshot_end"
            if not row["decision_eligible"]:
                cursor, status = None, "unavailable_signal_qualification_or_reference"
            while cursor in lookup:
                after = lookup[cursor].get(symbol)
                if after is None:
                    status = "right_censored_symbol_inactive_or_missing_exact_bar"
                    break
                if not eligible(cursor, symbol, after):
                    status = "right_censored_future_qualification_or_reference_unknown"
                    break
                flag = _flag(after.get("limit_up"))
                if flag is None:
                    status = "right_censored_unknown_future_limit_flag"
                    break
                if flag is False:
                    witness, status = cursor, "complete"
                    break
                if not _known(after.get("stage")) or int(after["stage"]) != maximum + 1:
                    status = "right_censored_inconsistent_future_stage"
                    break
                maximum += 1
                end, cursor = cursor, next_day.get(cursor)
            row.update(run_max_stage_observed=maximum, run_end_date=end, label_witness_day=witness,
                label_status=status, hit_4=int(maximum >= 4) if witness else None,
                hit_5=int(maximum >= 5) if witness else None,
                label_witness_data_available_at=max((availability[d] for d in days if day <= d <= witness),
                    key=_stamp) if witness else None,
                label_available_at=observed_at if witness else None,
                executable_hit_4=None, executable_hit_5=None,
                execution_label_status="unavailable_no_queue_or_fill",
                future_run_quality_used_for_decision_eligibility=False)
            execution = next_day.get(day)
            row.update(next_date=execution, next_session_observed=False, next_open_clearance=None,
                next_high_touches_limit_up=None, next_open_locked_limit_up=None,
                next_open_cents=None, next_high_cents=None, next_limit_up_cents=None,
                touchability_label_status="exact_next_session_unobserved")
            if execution in raw_by_day:
                raw_next = raw_by_day[execution]
                exact = raw_next.loc[raw_next.symbol.eq(symbol)]
                assess = qualification.assess(symbol, execution, decision_as_of=execution + "T09:30:00+08:00")
                after = lookup.get(execution, {}).get(symbol)
                row["touchability_label_status"] = "exact_next_raw_status_or_fields_unknown"
                if len(exact) == 1 and after is not None and _reference(after) and assess["engine_block_reason"] is None:
                    bar = exact.iloc[0]
                    values = pd.to_numeric(bar[["preclose", "open", "high"]], errors="coerce")
                    if (bar.tradestatus == "1" and bar.isST == "0" and np.isfinite(values).all() and values.gt(0).all()):
                        _, upper = price_limits(float(bar.preclose), execution, False, stock_board(symbol))
                        op, high = int(round(float(bar.open) * 100)), int(round(float(bar.high) * 100))
                        row.update(next_session_observed=True, next_limit_up_cents=upper,
                            next_open_cents=op, next_high_cents=high, next_open_clearance=op < upper,
                            next_high_touches_limit_up=high >= upper, next_open_locked_limit_up=op >= upper,
                            touchability_label_status="exact_next_daily_OHLC_proxy_not_fill")
                else:
                    row["touchability_label_status"] = "exact_next_raw_or_opening_qualification_unknown"
            ratio = row["amount_rel20_pre"]
            row["fixed_rule_pass"] = (bool(float(ratio) <= 1 and breadth >= 0)
                if row["decision_eligible"] and _known(ratio) and np.isfinite(float(ratio)) and breadth is not None else None)
            row["fixed_rule_score"] = 1.0 if row["fixed_rule_pass"] else None
            events.append(row)
    return pd.DataFrame(events), pd.DataFrame(unknown, columns=["date", "symbol", "stage", "reason", "not_zero_or_negative_example"]), pd.DataFrame(daily)


def _amount_context(snapshot, cutoff, members, raw_by_day):
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("Original unadmitted physical amount history required")
    pieces = []
    for item in manifest["chunks"]:
        if int(item["lower_inclusive"]) > int(cutoff.replace("-", "")):
            continue
        partition = snapshot / item["parquet_file"]
        if digest(partition) != item["parquet_sha256"]:
            raise ValueError("Pinned amount history partition bytes changed")
        part = pd.read_parquet(partition, columns=["code", "date", "amount", "close", "pct_chg", "is_st"])
        if len(part) != item["rows"]:
            raise ValueError("Original history partition row count changed")
        mapping = {code: normalize_symbol(str(code)).vt_symbol for code in part.code.unique()}
        part["symbol"] = part.code.map(mapping)
        part["date"] = pd.to_datetime(part.date.astype(str), format="%Y%m%d").dt.strftime("%Y-%m-%d")
        part = part.loc[part.date.le(cutoff) & part.symbol.isin(members) & part.is_st.eq(False)].copy()
        pieces.append(part[["symbol", "date", "amount", "close", "pct_chg"]])
    for raw in raw_by_day.values():
        part = raw.loc[raw.symbol.isin(members) & raw.isST.eq("0")].rename(columns={"pctChg": "pct_chg"})
        pieces.append(part[["symbol", "date", "amount", "close", "pct_chg"]])
    return pd.concat(pieces, ignore_index=True)


def build(previews, calendar, ipo_evidence, research_master_run, out):
    out, calendar = Path(out).resolve(), Path(calendar).resolve()
    if out.exists():
        raise FileExistsError("Use a new diagnostic output; previous results are immutable")
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    loaded = sorted([read_preview(p, ipo_qualification=qualification) for p in previews], key=lambda x: x[0]["day"])
    days = [row[0]["day"] for row in loaded]
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    sessions, previous, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if not days or len(days) != len(set(days)) or days != [d for d in sessions if days[0] <= d <= days[-1]] or status != "pinned_static_exchange_calendar":
        raise ValueError("Distinct exact adjacent complete-source dates required")
    lineages = [row[2].get("recent_source_view") for row in loaded]
    if (not all(lineages) or len({v["shared_view_id"] for v in lineages}) != 1
            or not all(row[2].get("feature_qualification") for row in loaded)
            or any(Path(row[0]["calendar"]).resolve() != calendar for row in loaded)):
        raise ValueError("One coherent explicitly qualified recent source view required")
    snapshots = {(v["original_history_snapshot"], v["original_history_manifest_sha256"], v["base_history_max_observed_day"]) for v in lineages}
    if len(snapshots) != 1:
        raise ValueError("The source previews disagree on their frozen history")
    snapshot, pin, cutoff = next(iter(snapshots))
    snapshot = Path(snapshot)
    if digest(snapshot / "manifest.json") != pin or cutoff != previous[days[0]]:
        raise ValueError("Original history pin/cutoff changed")
    raw_by_day, availability = {}, {}
    for envelope, _, provenance in loaded:
        raw, _ = load_complete(provenance["source_folder"])
        raw["symbol"] = raw.code.str[3:] + np.where(raw.code.str.startswith("sh."), ".SSE", ".SZSE")
        raw_by_day[envelope["day"]] = raw
        availability[envelope["day"]] = max((envelope["created_at"], *[p.get("source_completed_at") or p["source_updated_at"]
            for p in envelope["recent_source_lineage"]["source_proofs"]]), key=_stamp)
    features = {row[0]["day"]: row[1] for row in loaded}
    members = {r.symbol for frame in features.values() for r in frame.loc[frame.stage.isin([1, 2, 3])].itertuples()}
    history = _amount_context(snapshot, cutoff, members, raw_by_day)
    created = datetime.now(MARKET_TZ).isoformat()
    events, unknown, market = build_tables(features, raw_by_day, sessions, availability, history, qualification, created)
    groups = []
    for stage in (1, 2, 3):
        group = events.loc[events.stage.eq(stage)] if len(events) else events
        complete = group.loc[group.label_status.eq("complete")] if len(group) else group
        groups.append({"stage": stage, "known_stage_events": len(group), "complete_labels": len(complete),
            "unresolved_labels": len(group) - len(complete),
            "hit4_complete_known_stage": int(complete.hit_4.sum()) if len(complete) else 0,
            "hit5_complete_known_stage": int(complete.hit_5.sum()) if len(complete) else 0,
            "scope": "conditional_on_known_stage_and_completed_run_not_full_population_win_rate"})
    out.mkdir(parents=True, exist_ok=False)
    events.to_parquet(out / "event_panel.parquet", index=False)
    events.to_csv(out / "event_panel.csv", index=False, encoding="utf-8-sig")
    unknown.to_csv(out / "unresolved_membership.csv", index=False, encoding="utf-8-sig")
    market.to_csv(out / "market_context.csv", index=False, encoding="utf-8-sig")
    # Candidate export deliberately omits every future label and touch field.
    fields = ["event_id", "symbol", "date", "stage", "decision_eligible", "feature_available_at",
        "amount_rel20_pre", "market_breadth", "fixed_rule_pass", "fixed_rule_score"]
    candidates = events[fields] if len(events) else pd.DataFrame(columns=fields)
    candidates.to_csv(out / "fixed_rule_diagnostic.csv", index=False, encoding="utf-8-sig")
    batches = []
    for day in days:
        for stage in (1, 2, 3):
            group = candidates.loc[candidates.date.eq(day) & candidates.stage.eq(stage)]
            membership_unknown = unknown.loc[unknown.date.eq(day)]
            incomplete = bool(len(membership_unknown) or group.fixed_rule_pass.isna().any())
            selected = group.loc[group.fixed_rule_pass.eq(True)]
            batches.append({"day": day, "stage": stage, "status": "incomplete" if incomplete else "complete",
                "potential_unknown_stage_members": membership_unknown.symbol.tolist(),
                "unknown_fixed_rule_fields": group.loc[group.fixed_rule_pass.isna(), "symbol"].tolist(),
                "known_matching_rows": selected[["event_id", "symbol", "fixed_rule_score"]].to_dict("records"),
                "selection_permitted": False, "unknown_is_empty_target": False})
    write_json(out / "candidate_batches.json", {"schema": "recent_shortline_fixed_rule_diagnostic_batches_v1",
        "batches": batches, "future_label_fields_included": False, **FLAGS})
    result = {"schema": SCHEMA, "method_id": METHOD, "status": "completed_recent_shortline_diagnostics",
        "created_at": created, "source_days": days, "preview_sources": [r[2] for r in loaded],
        "calendar": str(calendar), "calendar_sha256": digest(calendar), "original_history_snapshot": str(snapshot),
        "original_history_manifest_sha256": pin, "base_history_cutoff": cutoff,
        "ipo_qualification": qualification.verification, "event_rows": len(events), "by_stage": groups,
        "unresolved_membership_rows": len(unknown), "fixed_rule": "amount_rel20_pre<=1 AND market_breadth>=0",
        "fixed_rule_pass_rows": int(candidates.fixed_rule_pass.eq(True).sum()),
        "candidate_batches": str(out / "candidate_batches.json"),
        "incomplete_stage_day_batches": sum(row["status"] == "incomplete" for row in batches),
        "history_policy": "shift1_20_observed_nonST_positive_reconstructed_reference_rows_median_min5_not_20_exact_sessions",
        "market_scope": "complete_source_active_nonST_SH_SZ_explicitly_different_from_old_nonST_population",
        "old_panel_overwritten": False, "old_scores_recomputed": False, "fitted_model_loaded": False,
        "label_completion_requires_exact_nonlimit_witness": True,
        "future_labels_exported_to_candidate_file": False, "historical_publication_verified": False,
        "pit_verified": False, "corporate_actions_verified": False, "current_signal_admitted": False,
        "engine_advanced": False, "return_metrics": None, **FLAGS}
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, action="append", required=True)
    for field in ("calendar", "ipo-evidence", "research-master-run", "out"):
        parser.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(args.preview, args.calendar, args.ipo_evidence, args.research_master_run, args.out)
    print(json.dumps({k: result[k] for k in ("status", "source_days", "event_rows", "by_stage", "unresolved_membership_rows", "fixed_rule_pass_rows", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
