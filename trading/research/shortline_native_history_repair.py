"""Recover older valid-close buffers for one unchanged native bootstrap anchor.

This reads only the still-incomplete symbols before the bootstrap's oldest
already-read session. It publishes no feature table, score, target or label.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re

import pandas as pd

from . import shortline_native_incremental as increment
from . import shortline_native_event_panel as original
from .baostock_daily_slice import digest


METHOD_ID = "native_shortline_bootstrap_anchor_older_subset_history_recovery_v1"
RESULT_SCHEMA = "native_shortline_bootstrap_history_repair_v1"


def _anchor(state, receipt, prefix, provenance):
    if (receipt.get("schema") != increment.BOOTSTRAP_SCHEMA
            or receipt.get("status") != "completed_saved_native_anchor_bounded_bootstrap"
            or receipt.get("last_source_day") != state["day"]
            or "parent_checkpoint_receipt" in state or "recovery_provenance" in state
            or receipt.get("score_input_published") is not False):
        raise ValueError("History repair accepts only an original unadvanced bootstrap anchor")
    if prefix != state["source_prefix"]:
        raise ValueError("The exact bootstrap source prefix changed")
    for field in ("anchor_result", "anchor_features"):
        ref = state[field]
        if digest(ref["file"]) != ref["sha256"]:
            raise ValueError("The original anchor result or feature bytes changed")
    saved = increment._read(state["anchor_result"]["file"])
    if (saved.get("last_source_day") != state["day"]
            or saved.get("feature_contract_id") != original.FEATURE_CONTRACT_ID
            or saved.get("feature_semantics") != original.FEATURE_SEMANTICS
            or Path(saved["latest_features_path"]).resolve() != Path(state["anchor_features"]["file"]).resolve()
            or saved["latest_features_sha256"] != state["anchor_features"]["sha256"]):
        raise ValueError("Bootstrap anchor descriptor and exact original feature table disagree")
    old_pins = increment._file_pins(receipt["source_proof"])
    new_pins = increment._file_pins(provenance)
    if not old_pins or any(new_pins.get(file) != expected for file, expected in old_pins.items()):
        raise ValueError("Bootstrap physical source files or metadata changed")
    frame = pd.read_csv(state["anchor_features"]["file"], dtype={"symbol": str, "date": str},
        float_precision="round_trip")
    required = {"symbol", "date", "close", "stage", "observed_history_close_count",
        *[f"ret_{lag}_observed_rows" for lag in (5, 10, 20)]}
    if (not required <= set(frame) or frame.symbol.duplicated().any()
            or not frame.date.eq(state["day"]).all() or set(frame.symbol) != set(state["states"])):
        raise ValueError("The original anchor must contain exactly the saved valid symbol states")
    return frame.set_index("symbol")


def repair_anchor_state(source, checkpoint, out, *, max_context_sessions=None):
    """Extend only original bootstrap buffers, without touching their recent suffix."""
    if max_context_sessions is not None and (type(max_context_sessions) is not int or max_context_sessions < 1):
        raise ValueError("An explicit history-repair budget must be a positive session count")
    now = increment._now()
    state, parent = increment._load_checkpoint(checkpoint, now)
    receipt = increment._read(parent["file"])
    day = original._day(state["day"])
    calendar, provenance, prefix, _ = increment._metadata(source, day, now)
    anchor = _anchor(state, receipt, prefix, provenance)
    read_before = receipt.get("read_days")
    old_budget = receipt.get("max_context_sessions")
    index = calendar.index(day)
    if (not isinstance(read_before, list) or not read_before or len(read_before) > index + 1
            or type(old_budget) is not int or old_budget < 1
            or len(read_before) != min(old_budget, index + 1)
            or read_before != list(reversed(calendar[index - len(read_before) + 1:index + 1]))
            or receipt.get("read_sessions") != len(read_before)
            or receipt["source_proof"].get("used_source_days") != read_before):
        raise ValueError("Bootstrap read_days must be its exact continuous reverse source segment")
    checkpoint_file = Path(receipt["checkpoint_file"]).resolve()
    out = increment._output(out, provenance, parent["file"], other_inputs=[checkpoint_file,
        state["anchor_result"]["file"], state["anchor_features"]["file"]])
    states = deepcopy(state["states"])
    selected, provider_codes = [], {}
    for symbol, item in states.items():
        old = anchor.loc[symbol]
        count = float(old.observed_history_close_count)
        if not math.isfinite(count) or not count.is_integer() or not 0 <= count <= 21:
            raise ValueError("The anchor's proven observed history count is invalid")
        target = min(21, int(count) + 1)
        stage = increment._float(old.stage)
        values = item.get("closes")
        if (not re.fullmatch(r"[0-9]{6}\.(?:SSE|SZSE)", symbol)
                or item.get("last_day") != day or item.get("last_close") != float(old.close)
                or item.get("stage") != (int(stage) if stage is not None else None)
                or item.get("proven_history_count") != target
                or not isinstance(values, list) or not 1 <= len(values) <= target
                or any(type(value) not in (float, int) or not math.isfinite(value) or value <= 0 for value in values)
                or values[-1] != float(old.close)
                or type(item.get("history_complete")) is not bool
                or item["history_complete"] != (len(values) == target)):
            raise ValueError("Saved bootstrap symbol state contradicts its unchanged exact anchor")
        if not item["history_complete"]:
            selected.append(symbol)
            provider_codes[symbol] = ("sh." if symbol.endswith(".SSE") else "sz.") + symbol[:6]
    if not selected:
        raise ValueError("This bootstrap has no incomplete observed-close buffers to recover")
    selected.sort()
    older = {symbol: [] for symbol in selected}
    oldest_position = index - len(read_before)
    remaining_sessions = oldest_position + 1
    budget = remaining_sessions if max_context_sessions is None else min(max_context_sessions, remaining_sessions)
    read_days, requests = [], []
    for position in range(oldest_position, oldest_position - budget, -1):
        current_day = calendar[position]
        wanted = [symbol for symbol in selected
            if len(older[symbol]) + len(states[symbol]["closes"]) < states[symbol]["proven_history_count"]]
        if not wanted:
            break
        # The source API calls canonical identities "original codes". Its
        # validator requires .SSE/.SZSE; raw provider codes are audited below.
        raw = source.frame(current_day, symbols=wanted)
        if not set(raw.symbol) <= set(wanted):
            raise ValueError("A subset source returned an unrequested symbol")
        frame, _, _ = original._cross_section(raw, current_day)
        read_days.append(current_day)
        requests.append({"day": current_day, "symbols": wanted,
            "provider_codes": [provider_codes[symbol] for symbol in wanted]})
        for row in frame.to_dict("records"):
            if row["code"] != provider_codes[row["symbol"]]:
                raise ValueError("Subset raw code and original canonical identity disagree")
            older[row["symbol"]].append(float(row["close"]))
    comparisons, restored = [], []
    for symbol in selected:
        item, old = states[symbol], anchor.loc[symbol]
        suffix = state["states"][symbol]["closes"]
        values = list(reversed(older[symbol])) + suffix
        target = item["proven_history_count"]
        if len(values) > target or values[-len(suffix):] != suffix:
            raise ValueError("Older context cannot replace the original recent-close suffix")
        item.update(closes=values, history_complete=len(values) == target)
        for lag in (5, 10, 20):
            field = f"ret_{lag}_observed_rows"
            expected = increment._float(old[field])
            if int(old.observed_history_close_count) < lag:
                actual, status = None, "checked_original_proven_short_history_null"
            elif len(values) >= lag + 1:
                actual = float(old.close) / values[-lag - 1] - 1
                status = "checked_original_anchor"
            else:
                actual, status = None, "unavailable_operational_buffer_not_natural_null"
            if status != "unavailable_operational_buffer_not_natural_null" and not increment._same(expected, actual):
                raise ValueError(f"Recovered {symbol} {field} disagrees with the exact original anchor")
            comparisons.append({"symbol": symbol, "feature": field, "expected": expected,
                "actual": actual, "status": status,
                "absolute_difference": abs(expected - actual) if expected is not None and actual is not None else None})
        restored.append({"symbol": symbol, "provider_code": provider_codes[symbol],
            "old_closes": len(suffix), "added_closes": len(older[symbol]), "restored_closes": len(values),
            "expected_closes": target, "history_complete": item["history_complete"]})
    computed = increment._now().isoformat()
    recovery = {"method_id": METHOD_ID, "anchor_day": day, "parent_bootstrap_receipt": parent,
        "parent_checkpoint": increment._ref(checkpoint_file), "old_read_days": read_before,
        "read_days": read_days, "subset_requests": requests,
        "source_prefix": deepcopy(prefix), "computed_at": computed}
    new_state = {**state, "states": states, "computed_at": computed, "recovery_provenance": recovery}
    incomplete = [symbol for symbol in selected if not states[symbol]["history_complete"]]
    result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID,
        "status": "partial_bootstrap_history_recovery_buffers_still_unknown" if incomplete
            else "completed_exact_bootstrap_history_recovery",
        "last_source_day": day, "started_at": now.isoformat(), "computed_at": computed,
        "read_days": read_days, "read_sessions": len(read_days),
        "max_context_sessions": max_context_sessions, "older_source_sessions_available": remaining_sessions,
        "source_prefix": deepcopy(prefix),
        "source_proof": {**provenance, "used_source_days": read_days,
            "subset_only_history_recovery": True, "full_cross_section_features_recomputed": False},
        "recovery_provenance": recovery, "symbol_restoration": restored,
        "anchor_return_compare": comparisons,
        "complete_buffer_symbols": sum(item["history_complete"] for item in states.values()),
        "incomplete_buffer_symbols": incomplete,
        "untouched_symbol_count": len(states) - len(selected),
        "score_input_published": False, "engine_advanced": False, "metrics": None,
        "old_features_or_labels_recomputed": False,
        "previously_observed_missing_symbols": None, **increment.FLAGS}
    return increment._write(out, new_state, result)


def main(argv=None):
    from .native_history_chain_source import NativeHistoryChainSource
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-view", type=Path, required=True)
    parser.add_argument("--extension-view", type=Path, action="append", default=[])
    parser.add_argument("--full-close", type=Path, action="append", default=[])
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-context-sessions", type=int)
    args = parser.parse_args(argv)
    with NativeHistoryChainSource(args.base_view, args.extension_view, args.full_close, calendar=args.calendar) as source:
        result = repair_anchor_state(source, args.checkpoint, args.out,
            max_context_sessions=args.max_context_sessions)
    print(json.dumps({key: result[key] for key in ("schema", "status", "last_source_day", "read_sessions",
        "complete_buffer_symbols", "incomplete_buffer_symbols", "score_input_published")}
        | {"result": str(args.out.resolve() / "result.json"), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
