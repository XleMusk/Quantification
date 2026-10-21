"""One native shortline feature day from a sealed, bounded-history checkpoint.

The old panel supplies an explicit stage/count anchor. Bootstrap restores only
recent valid observed closes; subsequent calls read one new full cross-section.
Incomplete operational history is never disguised as a natural model null.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import tempfile

import pandas as pd

from . import shortline_native_event_panel as original
from .baostock_daily_slice import digest


METHOD_ID = "native_shortline_saved_anchor_bounded_bootstrap_single_day_features_v1"
STATE_SCHEMA = "native_shortline_incremental_state_v1"
BOOTSTRAP_SCHEMA = "native_shortline_incremental_bootstrap_v1"
RESULT_SCHEMA = "native_shortline_incremental_daily_features_v1"
FLAGS = {**original.FLAGS, "historical_seen_registry_complete": False,
    "label_tables_read": False, "old_labels_updated": False,
    "target_generation_allowed": False, "full_old_latest_table_equivalence_claimed": False}
_PIN_FIELDS = ("raw_file", "raw_sha256", "result_file", "result_sha256", "plan_file", "plan_sha256",
    "receipt_file", "receipt_sha256", "source_available_at", "row_group")


def _now():
    return datetime.now().astimezone()


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": digest(path)}


def _stamp(value, now):
    stamp = original._time(value)
    if stamp > now:
        raise ValueError("Native state or source availability lies in the future")
    return stamp


def _metadata(source, day, now):
    calendar, proof = source.calendar, source.provenance
    if not calendar or calendar != sorted(set(calendar)) or day not in calendar:
        raise ValueError("An exact unique ordered source calendar and own day are required")
    for value in calendar: original._day(value)
    _stamp(proof["source_available_at"], now)
    own = proof.get("day_sources")
    if isinstance(own, list):
        mapping = {row["day"]: row for row in own}
        if len(mapping) != len(own): raise ValueError("Duplicate source-day metadata")
    elif isinstance(own, dict):
        mapping = own
    else:
        mapping = {value: {**{key: proof.get(key) for key in _PIN_FIELDS}, "day": value,
            "row_group": index, "source_view": proof.get("source_view"), "close_file": None}
            for index, value in enumerate(calendar)}
    used = calendar[:calendar.index(day) + 1]
    if any(value not in mapping for value in used):
        raise ValueError("Every source-prefix session requires its own metadata")
    pins = []
    for value in used:
        row = mapping[value]
        available = row.get("source_available_at", proof["source_available_at"])
        stamp = _stamp(available, now)
        if stamp < original._time(value + "T15:00:00+08:00"):
            raise ValueError("Native source must be an actually completed own-day close")
        pins.append({"day": value, **{key: row.get(key) for key in _PIN_FIELDS if key != "source_available_at"},
            "source_available_at": available})
    return calendar, proof, {"days": used, "day_sources_sha256": _hash(pins)}, mapping


def _file_pins(value):
    pins = {}
    def visit(item):
        if isinstance(item, dict):
            for stem in ("raw", "result", "plan", "receipt"):
                file, expected = item.get(stem + "_file"), item.get(stem + "_sha256")
                if file is not None and expected is not None:
                    key = str(Path(file).resolve())
                    if key in pins and pins[key] != expected:
                        raise ValueError("Conflicting native physical-source hashes")
                    pins[key] = expected
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    return pins


def _source_paths(value):
    files, packages = [], []
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and key in {"raw_file", "result_file", "plan_file", "receipt_file",
                        "close_file", "calendar", "source_view", "source_folder", "roster_receipt"}:
                    path = Path(child).resolve()
                    if key in {"source_view", "source_folder"}: packages.append(path)
                    else: files.append(path)
                else: visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    return files, packages


def _output(out, proof, parent, *, other_inputs=()):
    out, parent = Path(out).resolve(), Path(parent).resolve()
    if out.exists(): raise FileExistsError("Incremental native outputs require a new directory")
    files, packages = _source_paths(proof)
    files += [parent, *[Path(path).resolve() for path in other_inputs]]
    packages += [file.parent for file in files]
    for package in packages:
        if out == package or out in package.parents or package in out.parents:
            raise ValueError("Incremental output cannot cover or sit inside an old/source package")
    return out


def _float(value):
    if value is None or pd.isna(value): return None
    return float(value)


def _same(first, second):
    if first is None or second is None: return first is None and second is None
    return math.isclose(first, second, rel_tol=1e-12, abs_tol=1e-12)


def _write(out, checkpoint, result, frame=None):
    """Close CSV/state before taking the actual result availability clock."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".increment-", dir=out.parent) as temporary:
        stage = Path(temporary)
        if frame is not None:
            name = "latest_features.csv" if result["score_input_published"] else "diagnostic_features.csv"
            frame.to_csv(stage / name, index=False)
            result["diagnostic_features_path"] = str(out / name)
            result["diagnostic_features_sha256"] = digest(stage / name)
            if result["score_input_published"]:
                result["latest_features_path"] = str(out / name)
                result["latest_features_sha256"] = digest(stage / name)
                event_name = "latest_event_features.csv"
                frame.loc[frame.stage.isin([1, 2, 3])].to_csv(stage / event_name, index=False)
                result["latest_event_features_path"] = str(out / event_name)
        (stage / "checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False,
            indent=2, allow_nan=False), encoding="utf-8")
        result["checkpoint_file"] = str(out / "checkpoint.json")
        result["checkpoint_sha256"] = digest(stage / "checkpoint.json")
        available = _now()
        if available < original._time(result["computed_at"]):
            raise ValueError("Native output clock moved backwards before file availability")
        result["available_at"] = available.isoformat()
        (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False,
            indent=2, allow_nan=False), encoding="utf-8")
        stage.rename(out)
    return result


def bootstrap_state(source, saved_panel_result, out, *, max_context_sessions=252):
    """Restore the saved own-D stage and up to 21 actual valid observed closes."""
    now = _now()
    if type(max_context_sessions) is not int or max_context_sessions < 1:
        raise ValueError("A positive bounded bootstrap session budget is required")
    path = Path(saved_panel_result).resolve()
    saved = _read(path)
    if (saved.get("feature_contract_id") != original.FEATURE_CONTRACT_ID
            or saved.get("feature_semantics") != original.FEATURE_SEMANTICS
            or saved.get("features") != list(original.FEATURES)
            or any(saved.get(key) is not False for key in ("execution_admitted", "strategy_validated"))):
        raise ValueError("The unchanged native nine-feature anchor contract is required")
    _stamp(saved["available_at"], now)
    day = original._day(saved["last_source_day"])
    calendar, provenance, prefix, mapping = _metadata(source, day, now)
    latest_file = Path(saved["latest_features_path"])
    out = _output(out, provenance, path, other_inputs=[latest_file])
    if digest(latest_file) != saved["latest_features_sha256"]:
        raise ValueError("Saved native anchor latest-feature bytes changed")
    latest = pd.read_csv(latest_file, dtype={"symbol": str, "date": str}, float_precision="round_trip")
    required = {"symbol", "date", "close", "stage", "observed_history_close_count", *original.FEATURES}
    if not required <= set(latest) or latest.symbol.duplicated().any() or not latest.date.eq(day).all():
        raise ValueError("Saved complete valid own-D anchor identities/counts are required")
    raw = source.frame(day)
    own, _, scope = original._cross_section(raw, day)
    if set(own.symbol) != set(latest.symbol):
        raise ValueError("Saved anchor valid scope differs from its exact original source")
    # Old panel proofs are compact but pin physical owners/clocks. Check their
    # overlapping source fields, not regenerated computation/publication times.
    old_days = saved.get("source_days", saved.get("source_proof", {}).get("used_source_days"))
    if old_days != prefix["days"]:
        raise ValueError("Saved anchor and current source exact session prefixes differ")
    old_pins, new_pins = _file_pins(saved["source_proof"]), _file_pins(provenance)
    if not old_pins or any(new_pins.get(file) != expected for file, expected in old_pins.items()):
        raise ValueError("Saved anchor underlying physical raw/source metadata hashes changed")
    for old in saved["source_proof"]["day_sources"]:
        current = mapping[old["day"]]
        for key in ("row_group", "raw_file", "close_file", "source_view", "source_available_at"):
            if old.get(key) != current.get(key):
                raise ValueError("Saved anchor physical source owner or actual clock changed")
    indexed = latest.set_index("symbol")
    states, needed, buffers = {}, {}, {}
    comparisons = {key: {"checked": 0, "mismatches": 0, "natural_nulls": 0, "unavailable_buffer": 0,
        "max_absolute_difference": 0.0} for key in original.FEATURES}
    for row in own.to_dict("records"):
        symbol, old = row["symbol"], indexed.loc[row["symbol"]]
        count = float(old.observed_history_close_count)
        if not math.isfinite(count) or not count.is_integer() or not 0 <= count <= 21 or float(old.close) != float(row["close"]):
            raise ValueError("Anchor close/history count contradicts the actual source")
        stage = _float(old.stage)
        if stage is not None and (not stage.is_integer() or stage < 0):
            raise ValueError("Anchor stage must remain a known nonnegative integer or null")
        needed[symbol], buffers[symbol] = min(21, int(count) + 1), []
        states[symbol] = {"last_day": day, "last_close": float(row["close"]),
            "stage": int(stage) if stage is not None else None, "closes": [],
            "history_complete": False, "proven_history_count": needed[symbol]}
        for key in original.FEATURES[:6]:
            expected, actual = _float(old[key]), _float(row[key])
            item = comparisons[key]
            item["checked"] += 1
            item["natural_nulls"] += int(expected is None)
            item["mismatches"] += int(not _same(expected, actual))
            if expected is not None and actual is not None:
                item["max_absolute_difference"] = max(item["max_absolute_difference"], abs(expected - actual))
    index, read_days = calendar.index(day), []
    for position in range(index, max(-1, index - max_context_sessions), -1):
        current_day = calendar[position]
        frame = own if current_day == day else original._cross_section(source.frame(current_day), current_day)[0]
        read_days.append(current_day)
        for row in frame.to_dict("records"):
            symbol = row["symbol"]
            if symbol in needed and len(buffers[symbol]) < needed[symbol]:
                buffers[symbol].append(float(row["close"]))
        if all(len(buffers[symbol]) == target for symbol, target in needed.items()): break
    restored = []
    for symbol, target in needed.items():
        values = list(reversed(buffers[symbol]))
        states[symbol].update(closes=values, history_complete=len(values) == target)
        old = indexed.loc[symbol]
        before_count = int(old.observed_history_close_count)
        for lag in (5, 10, 20):
            key = f"ret_{lag}_observed_rows"
            item = comparisons[key]
            if before_count < lag:
                actual = None
            elif len(values) >= lag + 1:
                actual = float(old.close) / values[-lag - 1] - 1
            else:
                item["unavailable_buffer"] += 1
                continue
            expected = _float(old[key])
            item["checked"] += 1
            item["natural_nulls"] += int(expected is None)
            item["mismatches"] += int(not _same(expected, actual))
            if expected is not None and actual is not None:
                item["max_absolute_difference"] = max(item["max_absolute_difference"], abs(expected - actual))
        restored.append({"symbol": symbol, "expected_closes": target, "restored_closes": len(values),
            "history_complete": len(values) == target, "anchor_stage": states[symbol]["stage"]})
    if any(item["mismatches"] for item in comparisons.values()):
        raise ValueError("Saved anchor features do not agree with the bounded original source context")
    computed = _now().isoformat()
    checkpoint = {"schema": STATE_SCHEMA, "method_id": METHOD_ID, "day": day,
        "feature_contract_id": original.FEATURE_CONTRACT_ID, "feature_semantics": original.FEATURE_SEMANTICS,
        "states": states, "source_prefix": prefix, "anchor_result": _ref(path),
        "anchor_features": _ref(latest_file), "computed_at": computed, **FLAGS}
    result = {"schema": BOOTSTRAP_SCHEMA, "method_id": METHOD_ID,
        "status": "completed_saved_native_anchor_bounded_bootstrap", "last_source_day": day,
        "computed_at": computed, "started_at": now.isoformat(), "anchor_result": _ref(path),
        "source_proof": {**provenance, "used_source_days": read_days, "feature_history_recomputed": False},
        "read_days": read_days, "read_sessions": len(read_days), "max_context_sessions": max_context_sessions,
        "restored_symbols": len(states), "complete_buffer_symbols": sum(row["history_complete"] for row in restored),
        "incomplete_buffer_symbols": [row["symbol"] for row in restored if not row["history_complete"]],
        "symbol_restoration": restored, "anchor_compare": comparisons,
        "anchor_stage_basis": "unchanged_saved_own_D_feature_table_not_an_independent_stage_recalculation",
        "score_input_published": False, "previously_observed_missing_symbols": None,
        "old_features_or_labels_recomputed": False, **FLAGS}
    return _write(out, checkpoint, result)


def _load_checkpoint(path, now):
    path = Path(path).resolve()
    record = _read(path)
    receipt_path = path.parent / "result.json" if record.get("schema") == STATE_SCHEMA else path
    receipt = _read(receipt_path)
    state_file = Path(receipt["checkpoint_file"]).resolve()
    if state_file.parent != receipt_path.parent or digest(state_file) != receipt["checkpoint_sha256"]:
        raise ValueError("Sealed native checkpoint bytes or parent receipt changed")
    state = _read(state_file)
    if (state.get("schema") != STATE_SCHEMA or state.get("method_id") != METHOD_ID
            or state.get("feature_contract_id") != original.FEATURE_CONTRACT_ID
            or state.get("feature_semantics") != original.FEATURE_SEMANTICS
            or any(state.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Native checkpoint method/feature contract/boundaries changed")
    _stamp(receipt["available_at"], now)
    return state, _ref(receipt_path)


def advance_day(source, checkpoint, day, out):
    """Append one exact source day, never re-score history or complete old labels."""
    now, day = _now(), original._day(day)
    state, parent = _load_checkpoint(checkpoint, now)
    calendar, provenance, prefix, _ = _metadata(source, day, now)
    old_day = state["day"]
    if old_day not in calendar or calendar.index(old_day) + 1 >= len(calendar) or calendar[calendar.index(old_day) + 1] != day:
        raise ValueError("Native feature append requires the exact next source session; no skip/backfill")
    _, _, previous_prefix, _ = _metadata(source, old_day, now)
    if previous_prefix != state["source_prefix"]:
        raise ValueError("Previously consumed exact native source prefix changed")
    out = _output(out, provenance, parent["file"], other_inputs=[
        state["anchor_result"]["file"], state["anchor_features"]["file"]])
    frame, observed, scope = original._cross_section(source.frame(day), day)
    states = deepcopy(state["states"])
    valid = set(frame.symbol)
    for symbol, item in states.items():
        if symbol not in valid:
            item.update(last_day=day, last_close=None, stage=None)
    rows, unknown_stage, excluded, operational = [], [], set(observed - valid), []
    for row in frame.to_dict("records"):
        symbol = row["symbol"]
        item = states.setdefault(symbol, {"last_day": None, "last_close": None, "stage": None,
            "closes": [], "history_complete": False, "proven_history_count": None})
        reference = bool(item["last_day"] == old_day and item["last_close"] is not None
            and abs(row["preclose"] / item["last_close"] - 1) <= .01)
        if not bool(row["limit_up_proxy"]): stage, known = 0, True
        elif reference and item["stage"] is not None: stage, known = item["stage"] + 1, True
        else: stage, known = None, False
        if not known: unknown_stage.append(symbol)
        eligible = reference and known
        if not eligible: excluded.add(symbol)
        history, uncertain = item["closes"], []
        features = {key: _float(row[key]) for key in original.FEATURES[:6]}
        for lag in (5, 10, 20):
            key = f"ret_{lag}_observed_rows"
            features[key] = row["close"] / history[-lag] - 1 if len(history) >= lag else None
            if len(history) < lag and not item["history_complete"]: uncertain.append(key)
        if eligible and stage in (1, 2, 3) and uncertain:
            operational.append({"symbol": symbol, "stage": stage, "unknown_history_features": uncertain})
        feature = {"event_id": f"{symbol}|{day}|s{stage}" if stage in (1, 2, 3) else None,
            "symbol": symbol, "date": day, "source_day": day, "board": row["board"],
            "stage": stage, "stage_reference_known": known, "limit_up_proxy": bool(row["limit_up_proxy"]),
            "decision_eligible": eligible, "decision_reference_known": reference,
            "decision_exclusion_reason": None if eligible else "exact_prior_reference_or_stage_unknown",
            **{key: float(row[key]) for key in ("open", "high", "low", "close", "preclose", "volume", "amount")},
            "observed_history_close_count": item["proven_history_count"], **features,
            "history_context_complete": not uncertain, "operational_unknown_history_features": "|".join(uncertain)}
        rows.append(feature)
        history.append(float(row["close"]))
        item["closes"] = history[-21:]
        count = item["proven_history_count"]
        item["proven_history_count"] = min(21, count + 1) if count is not None else 21 if len(item["closes"]) == 21 else None
        item["history_complete"] = item["history_complete"] or len(item["closes"]) == 21
        item.update(last_day=day, last_close=float(row["close"]), stage=stage)
    computed = _now().isoformat()
    for row in rows: row["feature_available_at"] = computed
    latest_scope = {**scope, "stage_membership_scope": original.FEATURE_SEMANTICS["scope"],
        "stage_membership_complete": not unknown_stage, "full_market_stage_membership_complete": False,
        "unknown_stage_symbols": sorted(unknown_stage), "unknown_stage_count": len(unknown_stage),
        "decision_excluded_symbols": sorted(excluded), "decision_excluded_count": len(excluded),
        "previously_observed_missing_symbols": None, "historical_seen_registry_complete": False,
        "known_stage_eligible_count": sum(row["decision_eligible"] for row in rows),
        "stage123_event_count": sum(row["stage"] in (1, 2, 3) for row in rows),
        "operational_history_unknown_eligible_events": operational}
    new_state = {**state, "day": day, "states": states, "source_prefix": prefix,
        "computed_at": computed, "parent_checkpoint_receipt": parent}
    result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID,
        "status": "blocked_operational_history_context_no_score_input" if operational else "completed_native_incremental_D_features",
        "source_day": day, "last_source_day": day, "source_days": prefix["days"],
        "feature_contract_id": original.FEATURE_CONTRACT_ID, "feature_semantics": original.FEATURE_SEMANTICS,
        "features": list(original.FEATURES), "started_at": now.isoformat(), "computed_at": computed,
        "source_proof": {**provenance, "used_source_days": [day], "history_from_saved_checkpoint": parent,
            "future_row_groups_read_by_producer": False},
        "latest_scope": latest_scope, "stage_membership_complete": not unknown_stage,
        "unknown_stage_symbols": sorted(unknown_stage), "latest_stageunknown_count": len(unknown_stage),
        "latest_decisionexcluded_count": len(excluded), "full_market_stage_membership_complete": False,
        "read_days": [day], "read_sessions": 1, "score_input_published": not operational,
        "scorable_event_batch_context_complete": not operational,
        "eligible_stage123_rows": sum(row["decision_eligible"] and row["stage"] in (1, 2, 3) for row in rows),
        "operational_history_unknown_events": operational, "parent_checkpoint_receipt": parent,
        "no_eligible_events_requires_existing_score_loader_to_refuse_empty_scores": True,
        "feature_history_recomputed": False, "metrics": None, **FLAGS}
    table = pd.DataFrame(rows)
    if table.empty:
        table = pd.DataFrame(columns=["event_id", "date", "symbol", "stage", "stage_reference_known",
            "decision_eligible", *original.FEATURES])
    return _write(out, new_state, result, table)


def main(argv=None):
    from .native_history_chain_source import NativeHistoryChainSource
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "advance"):
        command = commands.add_parser(name)
        command.add_argument("--base-view", type=Path, required=True)
        command.add_argument("--extension-view", type=Path, action="append", default=[])
        command.add_argument("--full-close", type=Path, action="append", default=[])
        command.add_argument("--calendar", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "bootstrap":
            command.add_argument("--previous-panel", type=Path, required=True)
            command.add_argument("--max-context-sessions", type=int, default=252)
        else:
            command.add_argument("--checkpoint", type=Path, required=True)
            command.add_argument("--day", required=True)
    args = parser.parse_args(argv)
    with NativeHistoryChainSource(args.base_view, args.extension_view, args.full_close, calendar=args.calendar) as source:
        result = (bootstrap_state(source, args.previous_panel, args.out, max_context_sessions=args.max_context_sessions)
            if args.command == "bootstrap" else advance_day(source, args.checkpoint, args.day, args.out))
    print(json.dumps({key: result[key] for key in ("schema", "status", "last_source_day", "read_sessions", "score_input_published")}
        | {"result": str(args.out.resolve() / "result.json"), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
