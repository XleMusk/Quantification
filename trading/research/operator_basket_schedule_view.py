"""Read-only application view for the frozen paper basket schedule.

The API route supplies its helper namespace so this consumer can be tested
without opening a broker/THS client or mutating the unified Ledger.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


def build_view(namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Project the saved sell-before-buy paper sequence for the desk UI."""

    HTTPException = namespace["HTTPException"]
    Counter = namespace["Counter"]
    _package_artifact = namespace["_package_artifact"]
    _same_ledger_bundle_reconciliation_view = namespace["_same_ledger_bundle_reconciliation_view"]
    _same_ledger_compat_fields = namespace["_same_ledger_compat_fields"]
    _ths_journal_reconciliation_view = namespace["_ths_journal_reconciliation_view"]
    operator_package = namespace["operator_package"]

    package = operator_package()
    journal_reconciliation = _ths_journal_reconciliation_view()
    same_ledger_bundle_reconciliation = _same_ledger_bundle_reconciliation_view()
    schedule_path = _package_artifact(package, "execution_schedule")
    if not schedule_path:
        raise HTTPException(404, "当前离线包缺少篮子执行计划")

    try:
        with Path(schedule_path).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as error:
        raise HTTPException(409, f"纸面篮子执行计划尚不可读取：{error}") from error

    phase_sequence: list[str] = []
    for row in rows:
        phase = row.get("phase") or "unknown"
        if phase not in phase_sequence:
            phase_sequence.append(phase)
    phase_rank = {phase: index for index, phase in enumerate(phase_sequence)}
    phase_order = [phase_rank.get(row.get("phase") or "unknown", -1) for row in rows]
    sell_index = next((index for index, row in enumerate(rows) if row.get("side") == "sell"), None)
    buy_index = next((index for index, row in enumerate(rows) if row.get("side") == "buy"), None)
    sell_before_buy = buy_index is None or (sell_index is not None and sell_index < buy_index)
    manual_only = all(row.get("submission_mode") == "manual_ths_prefill_only" for row in rows)
    live_disabled_count = sum(row.get("live_submission") == "disabled" for row in rows)
    manual_release = None
    manual_release_path = _package_artifact(package, "paper_manual_release_sequence_index")
    if manual_release_path:
        try:
            manual_release_payload = json.loads(Path(manual_release_path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(409, f"人工批次释放索引尚不可读取：{error}") from error
        if manual_release_payload.get("schema") != "paper_manual_release_sequence_index_v1":
            raise HTTPException(409, "人工批次释放索引版本不受支持")
        manual_rows = manual_release_payload.get("rows") or []
        detail_rows = []
        detail_by_key = {}
        for index_row in manual_rows:
            sequence_path = Path(str(index_row.get("sequence_json") or ""))
            if not sequence_path.is_file():
                continue
            try:
                sequence_payload = json.loads(sequence_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise HTTPException(409, f"人工逐单释放序列尚不可读取：{error}") from error
            for detail in sequence_payload.get("rows") or []:
                detail_rows.append(detail)
                detail_by_key[(detail.get("strategy_id"), detail.get("symbol"), detail.get("side"), detail.get("qty"))] = detail
        enriched_rows = []
        for row in rows:
            key = (row.get("strategy_id"), row.get("symbol"), row.get("side"), row.get("qty"))
            detail = detail_by_key.get(key)
            enriched = dict(row)
            if detail:
                for field in ("release_sequence", "release_phase", "release_gate", "required_observation", "release_blocker_reason", "target_reconciliation_status"):
                    enriched[field] = detail.get(field)
            enriched_rows.append(enriched)
        rows = enriched_rows
        manual_release = {
            "schema": manual_release_payload.get("schema"),
            "status": manual_release_payload.get("status", "paper_manual_release_sequences_indexed"),
            "candidate_count": manual_release_payload.get("candidate_count", len(manual_rows)),
            "ordering_verified_count": sum(bool(row.get("ordering_verified")) for row in manual_rows),
            "route_available_count": sum(int(row.get("route_available_count", 0) or 0) for row in manual_rows),
            "route_unavailable_count": sum(int(row.get("route_unavailable_count", 0) or 0) for row in manual_rows),
            "rows": manual_rows,
            "detail_rows": detail_rows,
            "automatic_strategy_selection": False,
            "execution_admitted": False,
            "source_file": str(manual_release_path),
            "basis": "selected-plan paper release index; sell review precedes buy release; no broker or account observation",
        }
    return {
        "schema": "offline_basket_schedule_view_v1",
        "status": "paper_manual_sequence_only",
        "execution_admitted": False,
        "account_observed": False,
        "quotes_observed": False,
        "ths_journal_reconciliation": journal_reconciliation,
        "same_ledger_bundle_reconciliation": same_ledger_bundle_reconciliation,
        **_same_ledger_compat_fields(same_ledger_bundle_reconciliation, journal_reconciliation),
        "ledger_mutated": journal_reconciliation["ledger_mutated"],
        "schedule": {
            "row_count": len(rows),
            "phase_sequence": phase_sequence,
            "phase_counts": dict(Counter(row.get("phase") or "unknown" for row in rows)),
            "side_counts": dict(Counter(row.get("side") or "unknown" for row in rows)),
            "sell_before_buy": sell_before_buy,
            "sequence_monotonic": phase_order == sorted(phase_order),
            "manual_only": manual_only,
            "live_submission_disabled_count": live_disabled_count,
            "manual_release": manual_release,
            "rows": rows,
            "basis": "frozen target basket and reference prices; sell-before-buy paper sequence; no account, quote, fill or broker observation",
        },
        "source_file": str(schedule_path),
    }


__all__ = ["build_view"]
