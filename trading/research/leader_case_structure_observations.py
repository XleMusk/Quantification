"""Retrospective native-price comparisons bound to original Engine lot quantities.

These are diagnostic observations, never executable invalidation signals. No
account, prices, fees or returns are recomputed and no provider is contacted.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from .baostock_daily_slice import digest
from .leader_case_study import _write
from .leader_daily_signals import _calendar_view
from .leader_monthly_engine_cases import CASE_SCHEMA, SCHEMA as CASE_MANIFEST_SCHEMA

SCHEMA = "leader_original_case_structure_observations_v1"
METHOD = "fixed_signal_native_low_exact_five_successor_sessions_original_quantity_context_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "engine_advanced": False, "ledger_mutated": False, "orders_generated": False,
    "targets_generated": False, "returns_recomputed": False, "provider_contacted": False,
    "original_artifacts_mutated": False, "individual_case_review_completed": False,
    "price_basis_verified": False, "corporate_actions_verified": False,
    "pit_verified": False, "executable_structure_failure_admitted": False}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _bar(slot):
    """A valid numeric native bar still has unverified adjustment/status/units."""
    if slot.get("status") != "source_native_row_available" or not slot.get("raw"):
        return None
    raw = slot["raw"]
    values = {key: _number(raw.get(key)) for key in ("open", "high", "low", "close")}
    if any(v is None for v in values.values()):
        return None
    if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
        return None
    # A carried halt/zero-volume bar is not an active price observation. Positive
    # native volume does not itself verify trade status or a broker fill.
    volume = _number(raw.get("volume"))
    if volume is None or str(raw.get("tradestatus", "unknown")) == "0":
        return None
    return values


def _quantity_context(lifecycle, day):
    """Use saved settled FIFO allocations only; never pair new fills or prices."""
    unknown = {"status": "unknown_original_quantity_chain", "selected_buy_qty_through_day": None,
        "selected_remaining_before_day_qty": None, "selected_settled_remaining_before_day_qty": None,
        "selected_sell_qty_on_day": None, "selected_remaining_through_day_qty": None,
        "all_selected_buys_available_by_day": None}
    if lifecycle.get("status") != "attributed_original_fifo_quantities" or lifecycle.get("buy_fills") is None:
        return unknown
    lots = list(lifecycle["buy_fills"].values())
    if not lots:
        return {**unknown, "status": "no_original_selected_buy_fills"}
    acquired = before = settled = sold_today = through = 0
    for lot in lots:
        sells = lot["sell_allocations"]
        prior_sold = sum(s["qty"] for s in sells if s["day"] < day)
        day_sold = sum(s["qty"] for s in sells if s["day"] == day)
        acquired += lot["original_qty"] if lot["day"] <= day else 0
        remainder_before = (lot["original_qty"] if lot["day"] < day else 0) - prior_sold
        before += remainder_before
        if lot["available_day"] <= day:
            settled += remainder_before
        sold_today += day_sold
        through += (lot["original_qty"] if lot["day"] <= day else 0) - prior_sold - day_sold
    if min(acquired, before, settled, sold_today, through) < 0 or settled > before:
        raise ValueError("Original attributed quantities contradict their dated allocations")
    return {"status": "known_original_quantity_context_not_executability",
        "selected_buy_qty_through_day": acquired, "selected_remaining_before_day_qty": before,
        "selected_settled_remaining_before_day_qty": settled, "selected_sell_qty_on_day": sold_today,
        "selected_remaining_through_day_qty": through,
        "all_selected_buys_available_by_day": all(lot["available_day"] <= day for lot in lots)}


def _first(rows, field):
    unknown_before = []
    for row in rows:
        value = row[field]
        if value is None:
            unknown_before.append(row["day"])
        elif value:
            return {"status": "first_exact_comparison_observed" if not unknown_before else
                    "observed_breach_but_earlier_comparisons_unknown",
                "first_observed_day": row["day"],
                "first_exact_day": row["day"] if not unknown_before else None,
                "earlier_unknown_days": unknown_before,
                "original_quantity_context_on_observed_day": row["original_quantity_context"]}
    return {"status": "not_observed_in_complete_five_session_window" if not unknown_before else
            "unknown_incomplete_five_session_window", "first_observed_day": None,
        "first_exact_day": None, "earlier_unknown_days": unknown_before,
        "original_quantity_context_on_observed_day": None}


def observe_case(case, sessions):
    """Exactly D+1..D+5, strict comparisons and separate observation clocks."""
    if case.get("schema") != CASE_SCHEMA:
        raise ValueError("Original monthly Engine lifecycle card required")
    day = case["signal_day"]
    if day not in sessions:
        raise ValueError("Signal day outside original exact calendar")
    pos = sessions.index(day)
    slots = case["daily_slots"]
    by_offset = {slot["offset_sessions"]: slot for slot in slots}
    if len(slots) != 11 or set(by_offset) != set(range(-5, 6)):
        raise ValueError("All eleven original exact slots, including missing ones, required")
    for offset, slot in by_offset.items():
        idx = pos + offset
        expected = sessions[idx] if 0 <= idx < len(sessions) else None
        if slot["day"] != expected:
            raise ValueError("A next observed row cannot replace the exact session")
    if any(pos + offset >= len(sessions) for offset in range(1, 6)):
        raise ValueError("Five exact successor calendar sessions required; do not infer a new year")
    anchor = _bar(by_offset[0])
    low = anchor["low"] if anchor else None
    lifecycle = case["quantity_lifecycle"]
    rows = []
    for offset in range(1, 6):
        slot = by_offset[offset]
        current = _bar(slot)
        quantity = _quantity_context(lifecycle, slot["day"])
        rows.append({"offset_sessions": offset, "day": slot["day"],
            "original_slot_status": slot["status"],
            "numeric_comparison_status": "source_native_numeric_proxy_only" if anchor and current else
                "unknown_anchor_or_active_numeric_native_bar",
            "native_low": str(current["low"]) if current else None,
            "native_close": str(current["close"]) if current else None,
            "native_intraday_low_leq_signal_low": current["low"] <= low if current and low else None,
            "native_intraday_low_lt_signal_low": current["low"] < low if current and low else None,
            "native_close_lt_signal_low": current["close"] < low if current and low else None,
            "original_quantity_context": quantity,
            "original_sale_on_same_day_intraday_order_unknown":
                quantity["selected_sell_qty_on_day"] > 0 if quantity["selected_sell_qty_on_day"] is not None else None,
            "close_comparison_is_only_known_after_this_close": True,
            "intraday_low_time_relative_to_original_fills_unknown": True,
            "executable_structure_failure_admitted": False})
    diagnostics = case["original_execution_diagnostics"]
    reasons = sorted({d["mechanical_reason"] for d in diagnostics if d.get("mechanical_reason")})
    fills = lifecycle.get("buy_fills")
    lots = list(fills.values()) if fills is not None and lifecycle.get("status") == "attributed_original_fifo_quantities" else None
    remaining = sum(lot["remaining_qty"] for lot in lots) if lots else None
    sell_days = [s["day"] for lot in lots or [] for s in lot["sell_allocations"]]
    return {"case_id": case["case_id"], "family": case["family"], "phase": case["phase"],
        "symbol": case["symbol"], "signal_day": day, "original_order_id": case["order_id"],
        "original_order_day": case["original_order_annotation"]["original_order"]["day"],
        "original_mechanical_reasons": reasons,
        "fixed_signal_native_low": str(low) if low else None,
        "selected_buy_quantity": sum(lot["original_qty"] for lot in lots) if lots else None,
        "selected_original_available_days": sorted({lot["available_day"] for lot in lots}) if lots else [],
        "selected_final_remaining_qty": remaining,
        "selected_final_original_sell_day": max(sell_days) if lots and remaining == 0 and sell_days else None,
        "first_native_intraday_low_touch": _first(rows, "native_intraday_low_leq_signal_low"),
        "first_native_intraday_low_breach": _first(rows, "native_intraday_low_lt_signal_low"),
        "first_native_close_breach": _first(rows, "native_close_lt_signal_low"),
        "native_intraday_below_but_same_day_close_not_below_days": [r["day"] for r in rows
            if r["native_intraday_low_lt_signal_low"] is True and r["native_close_lt_signal_low"] is False],
        "daily_observations": rows, **FLAGS}


def build(*, cases_manifest, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("New observations require a new output directory")
    manifest_path = Path(cases_manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != CASE_MANIFEST_SCHEMA or any(manifest.get(k) is not False for k in
            ("execution_admitted", "selection_enabled", "strategy_validated")):
        raise ValueError("Pinned research-only monthly cases manifest required")
    index_proof = manifest["annotations_index"]
    if digest(index_proof["file"]) != index_proof["sha256"]:
        raise ValueError("Original annotation index changed")
    index = json.loads(Path(index_proof["file"]).read_text(encoding="utf-8"))
    calendar_proof = index["source"]["calendar"]
    if digest(calendar_proof["file"]) != calendar_proof["sha256"]:
        raise ValueError("Original exact calendar changed")
    payload = json.loads(Path(calendar_proof["file"]).read_text(encoding="utf-8"))
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Pinned original exchange calendar required")
    observations = []
    for item in manifest["cases"]:
        if digest(item["case_file"]) != item["case_sha256"]:
            raise ValueError("Original monthly case changed")
        case = json.loads(Path(item["case_file"]).read_text(encoding="utf-8"))
        if case["case_id"] != item["case_id"] or any(case.get(k) is not False for k in
                ("execution_admitted", "selection_enabled", "strategy_validated")):
            raise ValueError("Original case identity mismatch")
        observations.append({**observe_case(case, sessions), "case_file": item["case_file"],
                             "case_sha256": item["case_sha256"]})
    if len({r["case_id"] for r in observations}) != len(observations):
        raise ValueError("Duplicate original cases")
    groups = []
    # Fixed dimensions come from the original grid, including empty cells.
    for family, phase in dict.fromkeys((c["family"], c["phase"]) for c in manifest["cells"]):
        selected = [r for r in observations if (r["family"], r["phase"]) == (family, phase)]
        groups.append({"family": family, "phase": phase, "selected_cases": len(selected),
            "complete_five_native_comparisons": sum(all(d["native_close_lt_signal_low"] is not None
                for d in r["daily_observations"]) for r in selected),
            "cases_with_observed_native_intraday_breach": sum(r["first_native_intraday_low_breach"]["first_observed_day"] is not None for r in selected),
            "cases_with_observed_native_close_breach": sum(r["first_native_close_breach"]["first_observed_day"] is not None for r in selected),
            "cases_with_intraday_only_breach_day": sum(bool(r["native_intraday_below_but_same_day_close_not_below_days"]) for r in selected),
            "not_a_success_rate_or_effectiveness_measure": True})
    created = datetime.now().astimezone().isoformat()
    result = {"schema": SCHEMA, "method_id": METHOD, "created_at": created,
        "status": "completed_retrospective_native_numeric_observations_not_strategy_test",
        "source": {"cases_manifest": {"file": str(manifest_path), "sha256": digest(manifest_path)},
                   "original_calendar": calendar_proof},
        "definitions": {"anchor": "original signal-day native low", "window": "exact D+1 through D+5 sessions",
            "comparison": "strict less-than; equality is not a breach; missing/zero-volume/invalid bars are unknown",
            "first_day": "first_exact_day remains null if an earlier expected comparison is unknown",
            "quantities": "saved selected BUY lots and their original FIFO sale allocations, not whole-symbol holdings",
            "timing": "close observations are unavailable before that close; intraday low ordering versus fills is unknown",
            "price_basis": "same-source numeric comparisons only; unverified adjustment, units, actions, trade status and historical availability",
            "selection": "all fixed selected original cards and empty grid dimensions preserved; no new ranking or filtering"},
        "counts": {"cases": len(observations), "exact_successor_slots": len(observations) * 5,
            "unknown_close_comparison_slots": sum(d["native_close_lt_signal_low"] is None for r in observations for d in r["daily_observations"]),
            "cases_with_observed_native_close_breach": sum(r["first_native_close_breach"]["first_observed_day"] is not None for r in observations),
            "cases_with_intraday_only_breach_day": sum(bool(r["native_intraday_below_but_same_day_close_not_below_days"]) for r in observations)},
        "group_observations": groups, "cases": observations, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "result.json", result)
    lines = ["# 原案例结构与结算观察", "", f"本表是原生数值路径复盘，不是策略回测、胜率或可执行的失效信号。输入中全部{len(observations)}个固定样本原序保留；价格调整、行动和历史可得性仍未知。", "",
        "|原策略|信号日环境|案例|五日比较完整|出现盘中低于D低|出现收盘低于D低|有盘中跌破而当日收盘未破|",
        "|---|---|---:|---:|---:|---:|---:|"]
    for g in groups:
        lines.append("|" + "|".join(str(g[k]) for k in ("family", "phase", "selected_cases",
            "complete_five_native_comparisons", "cases_with_observed_native_intraday_breach",
            "cases_with_observed_native_close_breach", "cases_with_intraday_only_breach_day")) + "|")
    lines += ["", "每案的准确首次日、原买批可卖日期、当日原卖量和当日末剩余量见result.json。可卖日期不证明停牌/跌停队列可成交；收盘触发不能解释同日更早的原买卖。", "",
        "早期缺槽时保留首次观察日与首次准确日的区别；窗口内没看到不等于窗口外从未发生。机械加仓不当作目标总仓的新建仓。未改原卡、原策略、Engine、Ledger或收益。", ""]
    (out / "OBSERVATIONS.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    result = build(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "created_at": result["created_at"], **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
