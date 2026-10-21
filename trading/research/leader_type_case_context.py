"""Independent source-native history for already-missing fixed case slots.

Only the original pinned StockDB snapshot through its saved context cutoff is
read. No case, classification, sample, strategy or prior review is rewritten.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

import pandas as pd
import pyarrow.parquet as pq

from .baostock_daily_slice import digest
from .leader_case_study import RAW_FIELDS, _clean, _write
from .leader_daily_signals import _calendar_view
from .leader_type_case_study import CASE_SCHEMA, SCHEMA as CASE_MANIFEST_SCHEMA


SCHEMA = "leader_type_missing_history_context_v1"
PROTOCOL_SCHEMA = "leader_type_case_context_protocol_v1"
METHOD_ID = "original_missing_slot_stockdb_native_context_v1"
PROTOCOL = {
    "id": METHOD_ID,
    "requested_rows": "only_original_missing_raw_row_slots_on_or_before_original_context_anchor_day",
    "source": "only_original_case_context_anchor_history_snapshot_and_manifest_hash",
    "fields": list(RAW_FIELDS),
    "source_native_names_and_values_preserved": True,
    "units_and_adjustment_not_inferred": True,
    "volume_or_amount_automatic_scaling": False,
    "legacy_pre_close_is_not_execution_reference": True,
    "no_BaoStock_adjustflag_or_tradestatus_synthesized": True,
    "exact_original_calendar_slots_no_nearby_date_fallback": True,
    "all_original_cases_retained": True,
    "outside_history_cutoff_missing_slots_retained": True,
    "sample_classification_strategy_or_original_case_recomputed": False,
    "cross_source_comparison": "optional_exact_next_session_saved_Bao_preclose_vs_native_close_numeric_1pct_proxy_only",
    "retrospective_only": True,
}
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "provider_contacted": False,
    "orders_generated": False, "targets_generated": False, "account_created": False,
    "ledger_mutated": False, "engine_advanced": False, "original_cases_mutated": False,
    "original_reviews_mutated": False, "cases_reselected": False, "types_reclassified": False,
    "historical_publication_verified": False, "corporate_actions_verified": False,
    "price_adjustment_basis_verified": False, "units_verified": False, "pit_verified": False}
UNITS = {field: "unknown_source_native_no_scaling" for field in
    ("open", "high", "low", "close", "volume", "amount", "pct_chg", "pre_close", "turnover")}


def _now():
    return datetime.now().astimezone()


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Explicit actual timezone-aware recording time required")
    return result


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _decimal(value):
    if isinstance(value, bool) or value is None or value is pd.NA:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(SSE|SZSE)", value):
        raise ValueError("Canonical original case symbol required")
    return value


def load_case_requests(cases_manifest):
    """Read immutable case JSONs and their exact missing slots; no new selection."""
    path = Path(cases_manifest).resolve()
    manifest = _json(path)
    if (manifest.get("schema") != CASE_MANIFEST_SCHEMA or not manifest.get("cases")
            or any(manifest.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Original unadmitted fixed case manifest required")
    anchor = manifest["source"]["context_anchor"]
    cutoff = anchor["day"]
    datetime.strptime(cutoff, "%Y-%m-%d")
    if not manifest.get("source_days") or cutoff >= min(manifest["source_days"]):
        raise ValueError("Original history cutoff must remain strictly before the supplied dated source window")
    qualification = manifest["source"]["IPO_qualification"]
    calendar_file = Path(qualification["calendar"]).resolve()
    if digest(calendar_file) != qualification["calendar_sha256"]:
        raise ValueError("Original case exchange calendar changed")
    calendar = _json(calendar_file)
    sessions, _, status = _calendar_view(calendar, str(calendar["year"]) + "-01-01", str(calendar["year"]) + "-12-31", [])
    if status != "pinned_static_exchange_calendar" or cutoff not in sessions:
        raise ValueError("Original pinned exact-session calendar required")
    cases, requests, future, seen = [], {}, [], set()
    for entry in manifest["cases"]:
        file = Path(entry["json_file"]).resolve()
        file.relative_to(path.parent)
        if digest(file) != entry["case_sha256"]:
            raise ValueError("Original fixed case JSON bytes changed")
        case = _json(file)
        if (case.get("schema") != CASE_SCHEMA or case["case_id"] in seen
                or any(case.get(k) is not False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))
                or any(case.get(k) != entry[k] for k in ("case_id", "symbol", "day"))
                or case["source"]["context_anchor"] != anchor
                or case["source"]["IPO_qualification"]["calendar_sha256"] != qualification["calendar_sha256"]):
            raise ValueError("Case identity, source cutoff or immutable evidence differs from its manifest")
        seen.add(case["case_id"])
        symbol = _symbol(case["symbol"])
        if case["day"] not in sessions:
            raise ValueError("Original event date is outside its pinned calendar")
        origin = sessions.index(case["day"])
        slots = case["daily_slots"]
        if [r["offset_sessions"] for r in slots] != [-2, -1, 0, 1, 2]:
            raise ValueError("Preserve the original five exact calendar slots")
        references = []
        for slot in slots:
            index = origin + slot["offset_sessions"]
            expected = sessions[index] if 0 <= index < len(sessions) else None
            if slot["day"] != expected:
                raise ValueError("An original slot is not its exact calendar session")
            if slot["status"] != "missing_raw_row":
                continue
            if slot.get("raw") is not None:
                raise ValueError("A source row cannot simultaneously be an original missing slot")
            day = slot["day"]
            key = symbol + "|" + str(day)
            eligible = day is not None and day <= cutoff
            ref = {"case_id": case["case_id"], "symbol": symbol, "day": day,
                "offset_sessions": slot["offset_sessions"], "original_status": slot["status"],
                "history_context_eligible": eligible, "context_row_key": key if eligible else None}
            references.append(ref)
            if eligible:
                requests.setdefault((symbol, day), []).append(deepcopy(ref))
            else:
                future.append({**deepcopy(ref), "state": "original_missing_slot_outside_allowed_history_cutoff",
                    "later_than_last_saved_source_day": day is not None and day > max(manifest["source_days"]),
                    "raw": None, "actual_future_publication": None})
        cases.append({"case_id": case["case_id"], "case_file": str(file), "case_sha256": entry["case_sha256"],
            "symbol": symbol, "day": case["day"], "type_id": case["type_id"], "phase": case["phase"],
            "original_missing_slots": references,
            "original_nonmissing_slots_unchanged": 5 - len(references), "_saved_case": case})
    return {"manifest_file": str(path), "manifest_sha256": digest(path), "manifest": manifest,
        "cases": cases, "requests": requests, "retained_outside_cutoff_missing": future,
        "anchor": deepcopy(anchor), "calendar": {"file": str(calendar_file), "sha256": qualification["calendar_sha256"]},
        "sessions": sessions}


def _quality(raw, missing_fields):
    numbers = {key: _decimal(raw.get(key)) for key in ("open", "high", "low", "close", "volume", "amount")}
    prices = [numbers[key] for key in ("open", "high", "low", "close")]
    all_known = all(v is not None for v in prices)
    valid = (all(v > 0 for v in prices) and numbers["high"] >= max(prices)
        and numbers["low"] <= min(prices)) if all_known else None
    return {"valid_native_OHLC": valid,
        "native_volume_nonnegative": numbers["volume"] >= 0 if numbers["volume"] is not None else None,
        "native_amount_nonnegative": numbers["amount"] >= 0 if numbers["amount"] is not None else None,
        "missing_native_fields": missing_fields,
        "quality_is_numeric_shape_only_not_price_basis_units_or_trade_status": True}


def read_native_context(loaded, *, retrieved_at):
    """Read only manifest-pinned partitions containing requested exact pairs."""
    anchor = loaded["anchor"]
    snapshot = Path(anchor["history_snapshot"]).resolve()
    manifest_file = snapshot / "manifest.json"
    if digest(manifest_file) != anchor["original_history_manifest_sha256"]:
        raise ValueError("Original StockDB snapshot manifest changed")
    manifest = _json(manifest_file)
    if (manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False
            or len(manifest.get("chunks", [])) != manifest.get("chunks_expected")):
        raise ValueError("Original inventory-only snapshot boundary changed")
    requests = loaded["requests"]
    chosen = {}
    for symbol, day in requests:
        date_int = int(day.replace("-", ""))
        chunks = [c for c in manifest["chunks"] if str(c["prefix"]) == symbol[0]
            and int(c["lower_inclusive"]) <= date_int <= int(c["upper_inclusive"])]
        if len(chunks) > 1:
            raise ValueError("Ambiguous original dated partition; never choose a convenient copy")
        if chunks:
            chosen.setdefault(chunks[0]["parquet_file"], {"chunk": chunks[0], "pairs": set()})["pairs"].add((symbol, day))
    found, proofs, missing_partition_pairs, field_gaps = {}, [], set(requests), {}
    for relative, selection in chosen.items():
        item = selection["chunk"]
        file = (snapshot / relative).resolve()
        file.relative_to(snapshot)
        if digest(file) != item["parquet_sha256"]:
            raise ValueError("Original selected StockDB partition bytes changed")
        arrow = pq.ParquetFile(file)
        if arrow.metadata.num_rows != int(item["rows"]):
            raise ValueError("Original partition row count changed")
        present = [key for key in RAW_FIELDS if key in arrow.schema_arrow.names]
        absent = [key for key in RAW_FIELDS if key not in present]
        proof = {"file": str(file), "sha256": item["parquet_sha256"], "rows": item["rows"],
            "prefix": str(item["prefix"]), "lower_inclusive": item["lower_inclusive"],
            "upper_inclusive": item["upper_inclusive"], "columns_read": present, "missing_native_fields": absent,
            "original_msgpack_file": item.get("raw_file"), "original_msgpack_declared_sha256": item.get("raw_sha256"),
            "msgpack_reloaded_or_rehashed": False}
        proofs.append(proof)
        for pair in selection["pairs"]:
            missing_partition_pairs.discard(pair)
            field_gaps[pair] = absent
        if not {"code", "date"} <= set(present):
            continue
        frame = pd.read_parquet(file, columns=present)
        numeric_dates = pd.to_numeric(frame.date, errors="raise")
        if (not frame.code.astype(str).str.fullmatch(r"\d{6}").all()
                or not frame.code.astype(str).str.startswith(str(item["prefix"])).all()
                or not numeric_dates.between(int(item["lower_inclusive"]), int(item["upper_inclusive"])).all()):
            raise ValueError("Partition date/code identity disagrees with its pinned query")
        pairs = selection["pairs"]
        wanted_codes, wanted_dates = {s[:6] for s, _ in pairs}, {int(d.replace("-", "")) for _, d in pairs}
        selected = frame.loc[frame.code.isin(wanted_codes) & numeric_dates.isin(wanted_dates)]
        for record in selected.to_dict("records"):
            code = record["code"]
            symbol = code + (".SSE" if code.startswith("6") else ".SZSE")
            day = datetime.strptime(str(int(record["date"])), "%Y%m%d").date().isoformat()
            pair = symbol, day
            if pair not in pairs:
                continue
            if pair in found:
                raise ValueError("Duplicate exact native context row; never deduplicate by price")
            native = {key: _clean(record.get(key)) for key in RAW_FIELDS}
            found[pair] = {"raw": native, "quality": _quality(native, absent),
                "partition_file": str(file), "partition_sha256": item["parquet_sha256"]}
    result = []
    for pair in sorted(requests, key=lambda pair: (pair[1], pair[0])):
        symbol, day = pair
        record = found.get(pair)
        if record is not None:
            reason = None
        elif pair in missing_partition_pairs:
            reason = "original_manifest_has_no_exact_date_prefix_partition"
        elif set(field_gaps.get(pair, [])) & {"code", "date"}:
            reason = "original_partition_identity_fields_missing"
        else:
            reason = "exact_symbol_day_absent_in_original_pinned_partition"
        result.append({"row_key": symbol + "|" + day, "symbol": symbol, "day": day,
            "state": "source_native_row_available" if record is not None else "missing_source_row",
            "unknown_reason": reason, "retrieved_at": retrieved_at,
            "raw": record["raw"] if record else None, "quality": record["quality"] if record else None,
            "partition_file": record["partition_file"] if record else None,
            "partition_sha256": record["partition_sha256"] if record else None,
            "case_slot_reference_count": len(requests[pair]), "case_slot_references": deepcopy(requests[pair]),
            "field_units": deepcopy(UNITS), "price_adjustment_basis": "unknown_source_native",
            "trade_status": None, "trade_status_unknown_reason": "no_tradestatus_field_in_original_snapshot",
            "historical_publication_verified": False, "corporate_actions_verified": False})
    return result, {"history_snapshot": str(snapshot), "manifest_file": str(manifest_file),
        "manifest_sha256": anchor["original_history_manifest_sha256"], "base_history_end": anchor["day"],
        "snapshot_recorded_created_at_field": manifest.get("created_at"),
        "snapshot_created_at_is_not_verified_historical_publication_or_availability": True,
        "actual_observed_at": retrieved_at, "status": manifest["status"],
        "source": manifest.get("source"), "original_limitations": manifest.get("limitations", []),
        "partitions_read": proofs, "declared_units": None, "declared_price_adjustment_basis": None}


def cross_source_reference_proxy(rows, loaded):
    """Numerical next-session check only, never use legacy pre_close as a ref."""
    sessions = loaded["sessions"]
    observed = {}
    for record in loaded["cases"]:
        case = record["_saved_case"]
        for slot in case["daily_slots"]:
            if slot["raw"] is None:
                continue
            key = case["symbol"], slot["day"]
            if key in observed and observed[key]["raw"] != slot["raw"]:
                raise ValueError("Same saved symbol/day differs across immutable case cards")
            observed.setdefault(key, {"raw": slot["raw"], "case_ids": []})["case_ids"].append(case["case_id"])
    result = []
    for row in rows:
        if row["day"] != loaded["anchor"]["day"]:
            continue
        index = sessions.index(row["day"])
        next_day = sessions[index + 1] if index + 1 < len(sessions) else None
        saved = observed.get((row["symbol"], next_day))
        native_close = _decimal((row["raw"] or {}).get("close"))
        preclose = _decimal(saved["raw"].get("preclose")) if saved else None
        usable = (saved is not None and saved["raw"].get("adjustflag") == "3"
            and native_close is not None and native_close > 0 and preclose is not None and preclose > 0)
        difference = abs(preclose / native_close - 1) if usable else None
        result.append({"symbol": row["symbol"], "native_day": row["day"], "exact_next_session": next_day,
            "status": "unknown_missing_or_nonpositive_exact_pair" if difference is None else
                "consistent_within_one_percent_numeric_proxy" if difference <= Decimal("0.01") else "numeric_reference_mismatch",
            "native_close": _clean((row["raw"] or {}).get("close")),
            "next_saved_Bao_preclose": _clean(saved["raw"].get("preclose")) if saved else None,
            "absolute_relative_difference": str(difference) if difference is not None else None,
            "tolerance": "0.01", "saved_next_row_case_ids": sorted(set(saved["case_ids"])) if saved else [],
            "legacy_native_pre_close_used": False, "numeric_agreement_is_not_unit_adjustment_action_or_total_return_verification": True,
            "original_case_reference_or_classification_modified": False})
    return result


def _read_protocol(path, loaded, observed_at):
    path = Path(path).resolve()
    value = _json(path)
    if (value.get("schema") != PROTOCOL_SCHEMA or value.get("method_id") != METHOD_ID
            or value.get("status") != "late_registered_historical_context_only"
            or value.get("protocol") != PROTOCOL
            or Path(value.get("cases_manifest", "")).resolve() != Path(loaded["manifest_file"])
            or value.get("cases_manifest_sha256") != loaded["manifest_sha256"]
            or not _stamp(loaded["manifest"]["created_at"]) <= _stamp(value["recorded_at"]) <= _stamp(observed_at)):
        raise ValueError("Exact late-recorded sidecar protocol, original case manifest and actual clocks required")
    if any(value.get(k) not in (None, False) for k in ("execution_admitted", "selection_enabled", "strategy_validated")):
        raise ValueError("Context cannot admit a trade or strategy")
    return {"file": str(path), "sha256": digest(path), "recorded_at": value["recorded_at"], "protocol": deepcopy(PROTOCOL)}


def _show(value):
    return "待核验/缺失" if value is None else str(value).replace("|", "／")


def _markdown(result):
    counts, source = result["counts"], result["source"]
    lines = ["# 原案例历史上下文附录", "",
        f"只补原来缺失的历史槽位，原{counts['original_cases']}案式样、选样、类型、环境及逐案解读均未修改；本附录也不代表重新深读。", "",
        f"实际读取：{result['read_started_at']} → {result['derived_at']}；允许历史截止：{source['base_history_end']}。",
        f"原案例{counts['original_cases']}；历史槽引用{counts['eligible_case_slot_references']}，去重请求{counts['unique_historical_symbol_days']}。"
        f"找到原行{counts['found_native_rows']}，OHLC数值形状有效{counts['valid_native_OHLC']}，确缺源行{counts['missing_source_rows']}。",
        f"原截止外缺槽{counts['retained_outside_history_cutoff_slots']}保持原样待观察。", "",
        "StockDB原字段和值原样保留，复权口径、价格/量额单位、交易状态及真实换手分母均未验证。"
        "数值形状有效只说明OHLC有限且区间合理；不等于未复权、可交易、公司行动齐备或账户收益。",
        f"快照created_at原字段：{source['snapshot_recorded_created_at_field']}，不是已核实的历史发布/可得时间；本附录的可得时间以本次实际派生为准。", "",
        "| 日期 | 代码 | 原行状态 | 开 | 高 | 低 | 收 | 原量字段 | 原金额字段 | 原pre_close | 原turnover | 引用槽数 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in result["unique_context_rows"]:
        raw = row["raw"] or {}
        values = [row["day"], row["symbol"], row["state"],
            *[raw.get(k) for k in ("open", "high", "low", "close", "volume", "amount", "pre_close", "turnover")],
            row["case_slot_reference_count"]]
        lines.append("| " + " | ".join(_show(v) for v in values) + " |")
    lines += ["", "9日原收盘与准确后继Bao前收只作单独数值差异代理，绝不使用旧pre_close作为执行参考；一致不证明单位、复权或行动齐备。", "",
        "## 原案例引用", ""]
    lines += [f"- {case['case_id']}：历史引用{sum(r['history_context_eligible'] for r in case['original_missing_slots'])}槽；"
        f"原截止外缺槽{sum(not r['history_context_eligible'] for r in case['original_missing_slots'])}；"
        f"[原案]({Path(case['case_file']).as_posix()})（原sha已绑定）。" for case in result["cases"]]
    return "\n".join(lines) + "\n"


def build_context(*, cases_manifest, protocol, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("History context must use a new independent output directory")
    read_started_at = _now().isoformat()
    loaded = load_case_requests(cases_manifest)
    registered = _read_protocol(protocol, loaded, _now().isoformat())
    rows, source = read_native_context(loaded, retrieved_at=_now().isoformat())
    read_finished_at = _now().isoformat()
    for row in rows:
        row["retrieved_at"] = read_finished_at
    source["actual_observed_at"] = read_finished_at
    diagnostic = cross_source_reference_proxy(rows, loaded)
    derived_at = _now().isoformat()
    cases = [{k: deepcopy(v) for k, v in case.items() if k != "_saved_case"} for case in loaded["cases"]]
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "status": "completed_independent_native_history_context_only",
        "read_started_at": read_started_at, "read_finished_at": read_finished_at,
        "derived_at": derived_at, "available_at": derived_at,
        "historical_asof_backfilled": False, "protocol": registered,
        "original_cases_manifest": {"file": loaded["manifest_file"], "sha256": loaded["manifest_sha256"],
            "created_at": loaded["manifest"]["created_at"]}, "calendar": loaded["calendar"], "source": source,
        "field_units": deepcopy(UNITS), "unique_context_rows": rows, "cases": cases,
        "retained_outside_history_cutoff_missing": loaded["retained_outside_cutoff_missing"],
        "cross_source_next_session_reference_proxy": diagnostic,
        "counts": {"original_cases": len(cases),
            "original_missing_slots": sum(len(c["original_missing_slots"]) for c in cases),
            "eligible_case_slot_references": sum(len(refs) for refs in loaded["requests"].values()),
            "unique_historical_symbol_days": len(rows),
            "found_native_rows": sum(r["raw"] is not None for r in rows),
            "found_native_case_slot_references": sum(r["case_slot_reference_count"] for r in rows if r["raw"] is not None),
            "valid_native_OHLC": sum(r["quality"] is not None and r["quality"]["valid_native_OHLC"] is True for r in rows),
            "invalid_or_unknown_native_OHLC": sum(r["quality"] is not None and r["quality"]["valid_native_OHLC"] is not True for r in rows),
            "missing_source_rows": sum(r["raw"] is None for r in rows),
            "missing_source_case_slot_references": sum(r["case_slot_reference_count"] for r in rows if r["raw"] is None),
            "retained_outside_history_cutoff_slots": len(loaded["retained_outside_cutoff_missing"])},
        "individual_case_interpretation_completed_by_this_module": False, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "result.json", result)
    (out / "CONTEXT.md").write_text(_markdown(result), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases-manifest", "protocol", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_context(**vars(args))
    print(json.dumps({"status": result["status"], "out": str(args.out.resolve()), **result["counts"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
