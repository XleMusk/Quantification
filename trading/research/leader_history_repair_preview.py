"""Derive a new close preview after filling one explicitly empty past session.

This is a read-only, mixed-source research view. It never appends an older day
to the original snapshot, changes a source cache, trains a model or writes an
account. Incomplete source packages return a waiting result without reserving
the final output directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..market_dataset import write_frame, write_json
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete
from .leader_close_preview import SCHEMA, canonical, market_tape
from .leader_daily_signals import FAMILIES, _calendar_view, build_leader_daily_batches, read_physical_snapshot


LINEAGE_SCHEMA = "leader_historical_gap_feature_view_v1"
COMPLETE = "complete_full_dated_close_inventory_not_admitted"
BOUNDARY = {"candidate_panel_only": True, "strategy_validated": False,
            "selection_enabled": False, "signal_admitted": False,
            "execution_admitted": False, "orders_generated": False,
            "ths_payload_generated": False, "ledger_mutated": False,
            "model_refit": False, "future_labels_used_for_score": False}


def _source_gate(folder):
    """Inspect only the receipt; never read a partial aggregate or contact SDK."""
    folder = Path(folder).resolve()
    receipt = folder / "receipt.json"
    if not receipt.is_file():
        return {"source_folder": str(folder), "status": "waiting_missing_receipt"}
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    state = saved.get("status")
    if state == COMPLETE and saved.get("full_dated_roster_inventory_complete") is True:
        return None  # The full strict loader, not this gate, verifies completeness.
    if isinstance(state, str) and state.startswith("failed"):
        status = "blocked_failed_source"
    else:
        status = "waiting_source_incomplete"
    return {"source_folder": str(folder), "status": status, "source_status": state,
            "day": saved.get("day"), "completed_symbols": saved.get("completed_symbols"),
            "ordinary_symbols": saved.get("ordinary_symbols")}


def _current_raw_identity(features, raw):
    """History repair may change lags/stages, never decision-day observations."""
    actual = raw.copy()
    actual["symbol"] = actual.code.str[3:] + np.where(actual.code.str.startswith("sh."), ".SSE", ".SZSE")
    active = actual.loc[actual.tradestatus.eq("1") & actual.isST.eq("0")
                        & pd.to_numeric(actual.volume, errors="raise").gt(0)].set_index("symbol")
    if set(features.symbol) != set(active.index):
        raise ValueError("Derived features differ from the complete current active raw scope")
    indexed = features.set_index("symbol").loc[active.index]
    for feature, physical in (("open", "open"), ("high", "high"), ("low", "low"),
                              ("close", "close"), ("volume", "volume"),
                              ("amount", "amount"), ("pct_chg", "pctChg")):
        if not np.isclose(pd.to_numeric(indexed[feature], errors="raise"),
                          pd.to_numeric(active[physical], errors="raise"),
                          rtol=1e-10, atol=1e-7).all():
            raise ValueError(f"History repair changed current raw field: {feature}")


def derive_preview(repair_package, package, history_snapshot, calendar, out):
    out = Path(out).resolve()
    gates = [dict(role=role, **gate) for role, path in (("historical_repair", repair_package), ("current_close", package))
             if (gate := _source_gate(path)) is not None]
    if gates:
        return {"schema": LINEAGE_SCHEMA,
                "status": "blocked_failed_source" if any(g["status"] == "blocked_failed_source" for g in gates) else "waiting_source_incomplete",
                "checked_at": datetime.now().astimezone().isoformat(), "sources": gates,
                "output_directory_created": False, "output": str(out), **BOUNDARY}
    if out.exists():
        raise FileExistsError("A derived preview must use a new output directory")
    # Both packages are revalidated against their own dated roster and every
    # preserved raw response. A current-day survivor list never defines repair.
    repaired, repair_source = load_complete(repair_package)
    current_raw, current_source = load_complete(package)
    repair_day, day = repair_source["plan"]["day"], current_source["plan"]["day"]
    if not repaired.date.eq(repair_day).all() or not current_raw.date.eq(day).all():
        raise ValueError("Repair/current raw must each contain its own exact source day")
    if repair_day >= day:
        raise ValueError("Historical repair must precede the current close")
    calendar = Path(calendar).resolve()
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    if payload.get("year") != int(day[:4]) or repair_day[:4] != day[:4]:
        raise ValueError("Historical feature repair requires the pinned target-year calendar")
    calendar_hash = digest(calendar)
    if any(source["plan"].get("calendar_sha256") != calendar_hash for source in (repair_source, current_source)):
        raise ValueError("Source packages and derived view must use the same pinned calendar")
    base, metadata = read_physical_snapshot(Path(history_snapshot), end=day)
    history_end = str(base.date.max())
    _, previous, calendar_status = _calendar_view(payload, repair_day, day, sorted(base.date.unique()))
    if calendar_status != "pinned_static_exchange_calendar" or repair_day not in previous or day not in previous:
        raise ValueError("Repair and current close must be exact pinned open sessions")
    if previous.get(day) != history_end or previous.get(history_end) != repair_day:
        raise ValueError("Expected exact repair -> original latest -> current consecutive exchange sessions")
    original_repair_rows = int(base.date.eq(repair_day).sum())
    if original_repair_rows:
        raise ValueError("Historical repair only fills a wholly empty day; existing original rows cannot be replaced")
    if base.empty or base.date.min() >= repair_day or base.date.gt(history_end).any():
        raise ValueError("Historical repair must be an interior empty session in the original history")
    repair_physical, current_physical = canonical(repaired), canonical(current_raw)
    combined = pd.concat([base, repair_physical, current_physical], ignore_index=True)
    if combined.date.gt(day).any():
        raise ValueError("Future bars cannot enter the historical repair view")
    bundle = build_leader_daily_batches(combined, metadata, start=day, end=day,
                                       calendar_payload=payload, retain_window_features=True)
    features, candidates = bundle["window_features"], bundle["candidate_table"]
    _current_raw_identity(features, current_raw)
    tape = market_tape(current_raw)
    tape["limit_up_proxy_count"] = int(features.limit_up.sum())
    tape["limit_up_stage_unknown_count"] = int((features.limit_up & features.stage.isna()).sum())
    tape["stages"] = {str(i): int(features.stage.eq(i).sum()) for i in range(1, 6)}
    families = []
    for family in FAMILIES:
        rows = candidates.loc[candidates.family.eq(family)]
        families.append({"family": family, "candidate_count": len(rows),
                         "display_top5": json.loads(rows.head(5).to_json(orient="records")),
                         "producer_batch_status": bundle["batches"][family][day]["status"]})
    sessions, _, _ = _calendar_view(payload, f"{day[:4]}-01-01", day, sorted(combined.date.unique()))
    remaining_empty = sorted(set(sessions) - set(combined.date))
    recorded = datetime.now().astimezone().isoformat()
    history_snapshot = Path(history_snapshot).resolve()
    repair_codes, current_codes = set(repaired.code), set(current_raw.code)
    lineage = {
        "schema": LINEAGE_SCHEMA, "derivation": "mixed_source_whole_empty_historical_session_repair",
        "derived_at": recorded, "actual_signal_publication_verified": False,
        "original_history_snapshot": str(history_snapshot),
        "original_history_manifest_sha256": digest(history_snapshot / "manifest.json"),
        "original_history_max_observed_day": history_end, "original_history_rows": len(base),
        "original_repair_day_rows": original_repair_rows,
        "conflict_rule": "Refuse if any original repair-day row exists; no original value may be overwritten",
        "repair_day": repair_day, "repair_source": repair_source,
        "repair_roster_file": str(out / "repair_dated_roster.csv"),
        "repair_inventory_rows": len(repaired), "repair_active_rows": int(repaired.tradestatus.eq("1").sum()),
        "repair_suspended_rows": int(repaired.tradestatus.eq("0").sum()),
        "repair_roster_basis": "Strict complete exact repair-day provider roster, not current-day survivors",
        "repair_only_codes_count": len(repair_codes - current_codes),
        "current_only_codes_count": len(current_codes - repair_codes),
        "exact_recent_sessions": [repair_day, history_end, day],
        "raw_rows_by_recent_session": {d: int(combined.date.eq(d).sum()) for d in (repair_day, history_end, day)},
        "remaining_empty_pinned_year_sessions": remaining_empty,
        "remaining_symbol_gaps_retained": True, "continuous_history_claimed": False,
        "current_raw_fields_unchanged": True, "history_inventory_complete": False,
        "source_verified": False, "pit_verified": False, "company_actions_verified": False,
        "stockdb_modified": False, "native_features_modified": False,
        "prior_scores_modified": False, "prior_previews_modified": False,
        "future_bars_used": False, **BOUNDARY,
    }
    result = {
        "schema": SCHEMA, "created_at": recorded, "day": day,
        "status": "complete_source_close_research_preview_only", "source": current_source,
        "history_snapshot": str(history_snapshot), "history_repair_lineage": lineage,
        "history_inventory_complete": False, "calendar": str(calendar), "calendar_sha256": calendar_hash,
        "source_inventory_complete": True, "tape": tape, "families": families,
        "producer_coverage": bundle["coverage_facts"], "candidate_count": len(candidates),
        "history_semantics": {**bundle["protocol"], "source_mixing": lineage["derivation"],
                              "repair_scope": "Only one wholly empty dated session; no remaining symbol gap is filled"},
        "successor_raw_bar_present": False, "source_verified": False, "pit_verified": False,
        "company_actions_verified": False, "provider_contacted": False, **BOUNDARY,
        "limitations": [
            "Current close has a complete provider roster; mixed historical observations are not a continuous admitted snapshot",
            "The historical whole-day repair uses its own dated roster and cannot prove historical PIT eligibility or company actions",
            "Nullable suspension activity stays missing; no synthetic execution bar or per-symbol gap is filled",
            "Original observed-row 20/60 return semantics and unknown streak/reference bounds remain unchanged",
            "Created at the actual derivation time, not claimed as a signal published on the close date",
            "Displayed candidate ranks and turnover associations are research descriptions, not orders or identified investor intent",
        ],
    }
    # No final directory exists until strict source checks and derivation succeed.
    out.mkdir(parents=True, exist_ok=False)
    write_frame(out / "current_features.csv", features)
    write_frame(out / "candidates.csv", candidates)
    write_frame(out / "repair_dated_roster.csv", pd.DataFrame(repair_source["ordinary_roster"]))
    write_json(out / "history_repair_lineage.json", lineage)
    write_json(out / "result.json", result)
    lines = [f"# {day} 历史缺口修复派生研究预览", "",
             f"原历史截止 {history_end}；仅将整日零行的 {repair_day} 从该日完整名册补入新视图。",
             f"补采名册 {len(repaired)} 只；当前名册 {len(current_raw)} 只，当前活跃非 ST {len(features)} 只。",
             f"实际重建时间：{recorded}。当前原始字段未改变；旧快照、native 特征、分数和预览未修改。", "",
             "| 固定规则族 | 候选数 |", "|---|---:|"]
    lines.extend(f"| {item['family']} | {item['candidate_count']} |" for item in families)
    lines += ["", "仅为混合来源的研究视图。其余日期/个股缺口、资格与公司行动边界仍保留；不生成订单。"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair-package", type=Path, required=True, help="Strict full dated package of the wholly missing historical session")
    parser.add_argument("--package", type=Path, required=True, help="Strict full dated current close package")
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = derive_preview(args.repair_package, args.package, args.history_snapshot, args.calendar, args.out)
    print(json.dumps({key: result.get(key) for key in ("status", "day", "candidate_count", "sources", "output_directory_created")}
                     | {"output": str(args.out.resolve()), "execution_admitted": False}, ensure_ascii=False))
    return 2 if result["status"] == "blocked_failed_source" else 0


if __name__ == "__main__":
    raise SystemExit(main())
