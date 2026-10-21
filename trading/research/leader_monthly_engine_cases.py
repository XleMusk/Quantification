"""Fixed monthly cases from existing Engine evidence, without new trading.

Selection precedes all lifecycle and price inspection. Original unavailable
cases and empty cells remain visible; generated cards are not deep reviews.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from .baostock_daily_slice import digest
from .leader_case_study import _write
from .leader_daily_signals import FAMILIES
from .leader_environment_policy import STRENGTH, MIXED, WEAKNESS
from .leader_historical_case_annotations import COMPLETED, _read, _time, load_environment
from .leader_type_case_context import read_native_context


SCHEMA = "monthly_original_engine_case_manifest_v1"
CASE_SCHEMA = "monthly_original_engine_lifecycle_case_v1"
PROTOCOL_SCHEMA = "monthly_original_engine_cases_protocol_v1"
METHOD_ID = "calendar_month_family_signal_phase_first_original_buy_v1"
PHASES = (STRENGTH, MIXED, WEAKNESS)
PROTOCOL = {"id": METHOD_ID, "families": list(FAMILIES), "phases": list(PHASES),
    "months": [f"2026-{month:02d}" for month in range(1, 11)], "cells": 150,
    "selection": "first_original_submitted_buy_per_cell_by_signal_date_target_sequence_symbol_order_id",
    "outcomes_fill_status_price_coverage_not_used_to_select": True,
    "empty_cell_no_replacement": True, "blocked_events_separate_not_submitted_orders": True,
    "raw_offsets_from_signal_date": list(range(-5, 6)), "raw_history_cutoff": "2026-10-09",
    "native_raw_units_adjustment_unknown": True,
    "lifecycle": "read_only_existing_settled_FIFO_quantity_attribution_no_cash_cost_fees_or_PnL",
    "unavailable_original_cases_preserved": True, "original_twenty_symbol_sample_bias_retained": True,
    "retrospective_not_independent_test": True}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "engine_advanced": False,
    "ledger_mutated": False, "orders_generated": False, "targets_generated": False,
    "provider_contacted": False, "returns_recomputed": False, "model_refit": False,
    "future_labels_used_for_score": False, "historical_publication_verified": False,
    "corporate_actions_verified": False, "pit_verified": False,
    "original_artifacts_mutated": False, "individual_case_review_completed": False}


def select_monthly_cases(completed, environment_days, *, months=None):
    """Use only original order identity and signal-day phase, never outcomes."""
    months = list(months or PROTOCOL["months"])
    phase_days = {(month, phase): [row["day"] for row in environment_days
                  if row["day"].startswith(month) and row["phase"] == phase]
                  for month in months for phase in PHASES}
    by_cell, unassigned = {}, []
    for sidecar in completed:
        family = sidecar["original_case"]["family"]
        if family not in FAMILIES:
            raise ValueError("Unknown original family")
        for annotated in sidecar["order_annotations"]:
            order, link = annotated["original_order"], annotated["signal_link"]
            if order["side"] != "buy":
                continue
            day, phase = link["signal_date"], annotated["signal_date_phase"]["phase"]
            key = (day[:7], family, phase) if day else None
            if (not day or day[:7] not in months or phase not in PHASES
                    or link["status"] != "bound_by_original_ledger_event_sequence"):
                unassigned.append({"case_key": sidecar["case_key"], "order_id": annotated["order_id"],
                                   "reason": "original_buy_signal_or_phase_not_bound_to_fixed_grid"})
                continue
            by_cell.setdefault(key, []).append({"case_key": sidecar["case_key"],
                "family": family, "signal_day": day, "phase": phase,
                "order_id": annotated["order_id"], "symbol": order["symbol"],
                "target_sequence": link["target_sequence"], "annotated_order": annotated})
    cells, selected = [], []
    for month in months:
        for family in FAMILIES:
            for phase in PHASES:
                key = month, family, phase
                candidates = sorted(by_cell.get(key, []), key=lambda row:
                    (row["signal_day"], row["target_sequence"], row["symbol"], row["order_id"]))
                case_id = f"{month}|{family}|{phase}"
                row = {"cell_id": case_id, "month": month, "family": family, "phase": phase,
                    "observed_phase_days": len(phase_days[(month, phase)]),
                    "original_buy_orders": len(candidates), "selected_order_id": candidates[0]["order_id"] if candidates else None,
                    "status": "selected_first_original_buy" if candidates else
                        "no_observed_phase_in_month" if not phase_days[(month, phase)] else "observed_phase_no_original_buy"}
                cells.append(row)
                if candidates:
                    selected.append({"case_id": case_id, **candidates[0]})
    return cells, selected, unassigned


def _markdown(case):
    order = case["original_order_annotation"]["original_order"]
    lines = [f"# {case['case_id']}", "", f"代码：{case['symbol']}；原信号日：{case['signal_day']}；原委托日：{order['day']}。",
        "本卡仅整理旧Engine记录，尚未逐案解读；来源是固定20票研究样本，不是个人交割单。", "",
        f"原买委托：{case['order_id']}；原数量{order['qty']}，原filled字段{order['filled']}，状态{order['status']}。",
        "原价格、金额和费用单位保留，未重算成本或收益。FIFO只解释原成交数量归属，不是新交易或新账户。", "",
        f"生命周期：{case['quantity_lifecycle']['status']}；具体原买成交与分摊见JSON。", "",
        "|偏移|准确日期|原始行状态|开|高|低|收|", "|---:|---|---|---:|---:|---:|---:|"]
    for slot in case["daily_slots"]:
        raw = slot.get("raw") or {}
        lines.append("|" + "|".join(str(value) if value is not None else "未知" for value in
            [slot["offset_sessions"], slot["day"], slot["status"], *[raw.get(key) for key in ("open", "high", "low", "close")]]) + "|")
    lines += ["", "历史单位、复权、行动、资格和公开时点尚未核实；后继只作回顾，不参与取样或排名。",
              "不因缺价、未成交、后续下跌或未退出替换原样本。"]
    return "\n".join(lines) + "\n"


def build(*, annotations_index, protocol, out):
    from .leader_fill_attribution import trace_fifo_fill_attribution
    out, protocol = Path(out).resolve(), Path(protocol).resolve()
    if out.exists():
        raise FileExistsError("Monthly learning requires a new output directory")
    started = datetime.now().astimezone().isoformat()
    index, index_proof = _read(annotations_index)
    registration = json.loads(protocol.read_text(encoding="utf-8"))
    if (index.get("schema") != "leader_existing_replay_historical_phase_annotations_v1"
            or index.get("status") != "completed_read_only_annotations_no_new_strategy_run"
            or index["counts"].get("original_cases") != 20
            or len(index["cases"]) != 20
            or any(index.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))
            or registration.get("schema") != PROTOCOL_SCHEMA or registration.get("method_id") != METHOD_ID
            or registration.get("protocol") != PROTOCOL or registration.get("status") != "late_registered_retrospective_learning_only"
            or Path(registration["annotations_index"]).resolve() != Path(annotations_index).resolve()
            or registration.get("annotations_index_sha256") != index_proof["sha256"]
            or not _time(index["created_at"]) <= _time(registration["recorded_at"]) <= _time(started)
            or any(registration.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Pinned original annotation index and exact late-recorded protocol required")
    env_file = index["source"]["environment_result"]
    if digest(env_file["file"]) != env_file["sha256"]:
        raise ValueError("Original environment evidence changed")
    environment = load_environment(env_file["file"], read_at=started)
    completed, provenance = [], []
    for entry in index["cases"]:
        if entry["original_status"] != COMPLETED:
            continue
        sidecar, proof = _read(entry["sidecar_file"])
        if proof["sha256"] != entry["sidecar_sha256"]:
            raise ValueError("Original annotated case changed")
        completed.append(sidecar)
        provenance.append(proof)
    if len(completed) != 5 or {row["original_case"]["family"] for row in completed} != set(FAMILIES):
        raise ValueError("Exactly five original completed cases required, never replace unavailable cases")
    cells, samples, unassigned = select_monthly_cases(completed, environment["record"]["days"])
    loaded, blocked = {}, []
    for sidecar in completed:
        account_proof = next(item for item in sidecar["source_files"] if Path(item["file"]).name == "account.json")
        account, proof = _read(account_proof["file"])
        if proof["sha256"] != account_proof["sha256"]:
            raise ValueError("Original Ledger evidence changed")
        batches, batch_proof = _read(Path(account_proof["file"]).parent / "decision_batches.json")
        loaded[sidecar["case_key"]] = {"account": account, "batches": batches, "sidecar": sidecar,
            "proofs": [proof, batch_proof], "traces": {}}
        blocked += [{"case_key": sidecar["case_key"], "family": sidecar["original_case"]["family"],
                     "original_event": deepcopy(event)} for event in account["events"] if event.get("kind") == "order_blocked"]
    sessions = environment["full_calendar"]
    requests, sample_slots = {}, {}
    for sample in samples:
        position, slots = sessions.index(sample["signal_day"]), []
        for offset in PROTOCOL["raw_offsets_from_signal_date"]:
            target = position + offset
            day = sessions[target] if 0 <= target < len(sessions) else None
            eligible = day is not None and day <= PROTOCOL["raw_history_cutoff"]
            slots.append({"day": day, "offset_sessions": offset, "eligible_history": eligible})
            if eligible:
                requests.setdefault((sample["symbol"], day), []).append({"case_id": sample["case_id"], "offset_sessions": offset})
        sample_slots[sample["case_id"]] = slots
    source = environment["record"]["source"]
    native, native_source = read_native_context({"anchor": {"history_snapshot": source["snapshot"],
        "original_history_manifest_sha256": source["manifest_sha256"], "day": PROTOCOL["raw_history_cutoff"]},
        "requests": requests}, retrieved_at=datetime.now().astimezone().isoformat())
    native_read_finished_at = datetime.now().astimezone().isoformat()
    native_source["actual_observed_at"] = native_read_finished_at
    for row in native:
        row["retrieved_at"] = native_read_finished_at
    native_by_pair = {(row["symbol"], row["day"]): row for row in native}
    prepared = []
    for sample in samples:
        original = loaded[sample["case_key"]]
        account, symbol = original["account"], sample["symbol"]
        if symbol not in original["traces"]:
            original["traces"][symbol] = trace_fifo_fill_attribution(account, symbol, sessions)
        trace = original["traces"][symbol]
        selected_fills = [deepcopy(fill) for fill in original["sidecar"]["fill_annotations"] if fill["order_id"] == sample["order_id"]]
        buy_fills = {fill["fill_id"]: deepcopy((trace.get("buy_fills") or {}).get(fill["fill_id"])) for fill in selected_fills}
        if trace["status"] == "attributed_original_fifo_quantities" and any(value is None for value in buy_fills.values()):
            raise ValueError("Known FIFO attribution omitted an original selected buy fill")
        slots = []
        for slot in sample_slots[sample["case_id"]]:
            row = native_by_pair.get((symbol, slot["day"])) if slot["eligible_history"] else None
            slots.append({**slot, "status": row["state"] if row else "outside_allowed_history_or_calendar",
                          "raw": deepcopy(row["raw"]) if row else None,
                          "native_evidence": deepcopy(row) if row else None})
        batch = original["batches"].get(sample["signal_day"])
        target = [deepcopy(event) for event in account["events"] if event.get("sequence") == sample["target_sequence"]]
        prepared.append({"schema": CASE_SCHEMA, "case_id": sample["case_id"], "case_key": sample["case_key"],
            "family": sample["family"], "phase": sample["phase"], "signal_day": sample["signal_day"],
            "symbol": symbol, "order_id": sample["order_id"],
            "original_order_annotation": deepcopy(sample["annotated_order"]),
            "original_buy_fill_annotations": selected_fills, "original_target_events": target,
            "original_execution_diagnostics": [deepcopy(e) for e in account["events"]
                if e.get("kind") == "target_execution_diagnostic" and sample["order_id"] in e.get("order_ids", [])],
            "original_decision_record": next((deepcopy(r["original_decision_record"]) for r in original["sidecar"]["daily_annotations"] if r["day"] == sample["signal_day"]), None),
            "original_candidate_records": [deepcopy(row) for row in (batch or {}).get("candidates", []) if row["symbol"] == symbol],
            "original_batch_status": batch.get("status") if batch else None,
            "quantity_lifecycle": {"status": trace["status"], "unknown_reason": trace.get("unknown_reason"),
                "buy_fills": buy_fills, "zero_buy_fills": not selected_fills,
                "original_order_filled_field": sample["annotated_order"]["original_order"]["filled"],
                "whole_symbol_final_position_checked": trace["status"] == "attributed_original_fifo_quantities",
                "unknown_details": deepcopy(trace.get("unknown_details")),
                "whole_symbol_final_original_qty": trace.get("final_original_position_qty"),
                "whole_symbol_attributed_remaining_qty": trace.get("attributed_remaining_qty"),
                "no_cost_fee_or_return_attribution": True},
            "daily_slots": slots, "original_case_source_files": original["proofs"], **FLAGS})
    created = datetime.now().astimezone().isoformat()
    out.mkdir(parents=True, exist_ok=False)
    entries = []
    for number, case in enumerate(prepared, 1):
        case["created_at"] = created
        file = out / f"case_{number:03d}.json"
        markdown = file.with_suffix(".md")
        _write(file, case)
        markdown.write_text(_markdown(case), encoding="utf-8")
        entries.append({"case_id": case["case_id"], "symbol": case["symbol"], "family": case["family"],
            "phase": case["phase"], "signal_day": case["signal_day"], "order_id": case["order_id"],
            "case_file": str(file), "case_sha256": digest(file), "markdown_file": str(markdown)})
    manifest = {"schema": SCHEMA, "method_id": METHOD_ID,
        "status": "completed_fixed_original_event_cards_not_individual_review", "created_at": created,
        "available_at": created, "read_started_at": started,
        "protocol": {"file": str(protocol), "sha256": digest(protocol), "recorded_at": registration["recorded_at"]},
        "annotations_index": index_proof, "annotation_source_files": provenance,
        "original_twenty_cases": deepcopy(index["cases"]), "native_history_source": native_source,
        "cells": cells, "cases": entries, "unassigned_original_buys": unassigned,
        "original_blocked_events": blocked, "counts": {"cells": len(cells), "selected_cases": len(entries),
            "empty_cells": sum(row["selected_order_id"] is None for row in cells),
            "original_buy_orders_in_grid": sum(row["original_buy_orders"] for row in cells),
            "original_blocked_events": len(blocked), "unique_native_context_rows": len(native),
            "missing_native_rows": sum(row["raw"] is None for row in native),
            "selected_case_raw_slots": sum(slot["raw"] is not None for case in prepared for slot in case["daily_slots"]),
            "outside_history_or_calendar_case_slots": sum(not slot["eligible_history"] for case in prepared for slot in case["daily_slots"]),
            "unknown_quantity_lifecycle_cases": sum(case["quantity_lifecycle"]["status"] != "attributed_original_fifo_quantities" for case in prepared),
            "selected_orders_with_zero_buy_fills": sum(case["quantity_lifecycle"]["zero_buy_fills"] for case in prepared)}, **FLAGS}
    _write(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("annotations-index", "protocol", "out"):
        parser.add_argument("--" + field, type=Path, required=True)
    result = build(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "created_at": result["created_at"], **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
