"""Training-only paired hit5 / next-open five-session price-return labels.

Both targets share one event-ID intersection and the same label-end purge.
This producer reads no 2026 endpoint, scores no model and applies no future
price-limit/IPO/entry-clearance filter to the training pool.
"""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts import MARKET_TZ
from . import shortline_native_remaining_edge as edge


METHOD_ID = "native_shortline_paired_training_hit5_adjusted_open_h5_labels_v1"
START, END = "2024-01-02", "2025-12-31"
AMBIGUOUS = {"300114.SZSE", "302132.SZSE"}
KEYS = ["event_id", "date", "symbol", "stage"]
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, model_refit=False,
    engine_advanced=False, orders_generated=False, ledger_mutated=False,
    network_accessed=False, price_returns_are_account_returns=False,
    historical_PIT_verified=False, original_inputs_modified=False)
SPEC = dict(id=METHOD_ID, start=START, end=END,
    entry="exact_D_plus1_same_source_adjusted_open", exit="exact_D_plus6_same_source_adjusted_open",
    price_target="adjusted_open_F_divided_by_adjusted_open_E_minus1",
    shared_pool="decision_eligible_and_hit5_complete_witness_le_end_and_known_finite_h5_return",
    label_end="max(hit5_label_witness_day,exit_day)_must_not_exceed_training_end",
    cutoff="purge_before_loading_any_endpoint_after_training_end",
    eligibility="no_future_limit_IPO_or_entry_exit_clearance_filter",
    identity="300114_and302132_unknown_no_alias_merge", **FLAGS)


def _clock():
    return datetime.now(MARKET_TZ).isoformat()


def _bool(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value in {"True", "False"}:
        return value == "True"
    raise ValueError("Original event decision eligibility must be explicit boolean")


def _day(value):
    if value is None or pd.isna(value) or value == "":
        return None
    parsed = pd.Timestamp(value).strftime("%Y-%m-%d")
    if str(value) != parsed:
        raise ValueError("Original label witness must be an exact date")
    return parsed


def _number(value):
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _load_formation(panel_result_path):
    result_path = Path(panel_result_path).resolve()
    metadata = edge.read(result_path)
    if (metadata.get("schema") != "native_shortline_event_panel_v1"
            or metadata.get("status") != "completed_native_causal_shortline_research_panel"):
        raise ValueError("Use the completed original native event panel")
    features_path, labels_path = Path(metadata["event_features_path"]).resolve(), Path(metadata["events_path"]).resolve()
    refs = [edge.pin(result_path)]
    for path in (features_path, labels_path):
        declared = metadata["files"][path.name]
        if path == labels_path and declared != metadata["events_sha256"]:
            raise ValueError("Original label payload declarations conflict")
        ref = {"path": str(path), "sha256": declared}
        edge.check(ref)
        refs.append(ref)
    chunks = []
    for chunk in pd.read_csv(features_path, usecols=KEYS+["decision_eligible"],
                             dtype={"event_id": str, "date": str, "symbol": str}, chunksize=8192):
        chunks.append(chunk.loc[chunk.date.between(START, END)])
    features = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=KEYS+["decision_eligible"])
    if features.empty:
        raise ValueError("No original events cover the fixed2024-2025 training window")
    labels = pd.read_parquet(labels_path, columns=KEYS+[
        "decision_eligible", "label_status", "label_witness_day", "hit_5"],
        filters=[("date", ">=", START), ("date", "<=", END)])
    if features.event_id.duplicated().any() or labels.event_id.duplicated().any():
        raise ValueError("Each original training event ID must be unique")
    if set(features.event_id) != set(labels.event_id):
        raise ValueError("Feature/label training event-ID coverage differs")
    merged = features.merge(labels, on="event_id", how="left", suffixes=("", "_label"), validate="one_to_one")
    for key in ("date", "symbol", "stage"):
        if not merged[key].eq(merged[key+"_label"]).all():
            raise ValueError("Original feature/label event identity differs: " + key)
    merged["decision_eligible"] = merged.decision_eligible.map(_bool)
    if not merged.decision_eligible.eq(merged.decision_eligible_label.map(_bool)).all():
        raise ValueError("Original feature/label decision eligibility differs")
    if not merged.stage.isin([1, 2, 3]).all():
        raise ValueError("Only the original known stages1/2/3 may enter this paired training question")
    return merged[KEYS+["decision_eligible", "label_status", "label_witness_day", "hit_5"]], refs


def _base_targets(formation, schedule):
    rows = []
    for source in formation.to_dict("records"):
        day, witness = source["date"], _day(source["label_witness_day"])
        entry, exit_day = schedule[day]["entry_day"], schedule[day]["h5_exit_day"]
        if witness is not None and witness < day:
            raise ValueError("A label witness cannot precede its event D")
        hit5 = _number(source["hit_5"])
        complete = source["label_status"] == "complete" and witness is not None and hit5 in {0., 1.}
        purge = bool((exit_day is not None and exit_day > END) or (witness is not None and witness > END))
        reasons = []
        if not source["decision_eligible"]:
            reasons.append("decision_not_eligible")
        if not complete:
            reasons.append("hit5_complete_witness_unknown")
        if witness is not None and witness > END:
            reasons.append("hit5_witness_after_training_cutoff")
        if entry is None or exit_day is None:
            reasons.append("exact_training_endpoint_schedule_unknown")
        elif exit_day > END:
            reasons.append("exit_after_training_cutoff_purged_before_price_read")
        if source["symbol"] in AMBIGUOUS:
            reasons.append("known_original_code_identity_ambiguity")
        rows.append({**{key: source[key] for key in KEYS}, "decision_eligible": source["decision_eligible"],
            "hit5": int(hit5) if complete and witness <= END else None,
            "classification_label_status": source["label_status"], "label_witness_day": witness,
            "entry_day": entry, "exit_day": exit_day,
            "max_label_day": max(witness, exit_day) if witness is not None and exit_day is not None else None,
            "h5_return": None, "entry_adjusted_open": None, "exit_adjusted_open": None,
            "purged": purge, "joint_training_eligible": False, "exclusion_reasons": reasons})
    return rows


def _finish_targets(rows, data):
    for row in rows:
        reasons = row["exclusion_reasons"]
        if not reasons:
            entry, entry_reason = edge.price_open(data.get((row["entry_day"], row["symbol"])))
            exit_price, exit_reason = edge.price_open(data.get((row["exit_day"], row["symbol"])))
            row.update(entry_adjusted_open=entry, exit_adjusted_open=exit_price)
            if entry is None:
                reasons.append("entry:" + str(entry_reason))
            if exit_price is None:
                reasons.append("exit:" + str(exit_reason))
            if not reasons:
                value = exit_price/entry-1
                if np.isfinite(value):
                    row.update(h5_return=value, joint_training_eligible=True)
                else:
                    reasons.append("h5_price_return_nonfinite")
        row["status"] = "shared_training_target" if row["joint_training_eligible"] else "purged" if row["purged"] else "excluded_or_unknown"
        row["exclusion_reason"] = ";".join(reasons) if reasons else None
        del row["exclusion_reasons"]
    return pd.DataFrame(rows)


def build_training_labels(panel_result_path, edge_registration_path, out):
    started = _clock()
    formation, source_refs = _load_formation(panel_result_path)
    registration_path = Path(edge_registration_path).resolve()
    record = edge.read(registration_path)
    if record.get("schema") != edge.METHOD+"_registration":
        raise ValueError("Use the original remaining-edge raw/adjusted source registration")
    calendar = record["calendar"]
    if calendar != sorted(set(calendar)) or START not in calendar or END not in calendar:
        raise ValueError("Preserve the original full calendar and exact training boundaries")
    source_refs.append(edge.pin(registration_path))
    for key in ("raw_result", "raw_plan", "adjusted_result"):
        ref = record["refs"][key]
        edge.check(ref)
        source_refs.append(ref)
    raw_plan = edge.read(record["refs"]["raw_plan"]["path"])
    if raw_plan.get("days") != calendar:
        raise ValueError("The original inline raw-plan calendar differs from the paired price calendar")
    if set(record["panels"]) != {"raw", "adjusted"}:
        raise ValueError("Use the original paired raw and adjusted panels")
    protected = [Path(panel_result_path).resolve().parent, registration_path.parent]
    protected += [Path(ref["path"]).resolve().parent for ref in source_refs+list(record["panels"].values())]
    folder = edge.separate(out, protected)
    schedule = edge.make_schedule(sorted(formation.date.unique()), calendar)
    rows = _base_targets(formation, schedule)
    # No purged/censored/ineligible/ambiguous event can cause a later price read.
    eligible_keys = {row["event_id"] for row in rows if not row["exclusion_reasons"]}
    price_formation = formation.loc[formation.event_id.isin(eligible_keys), ["date", "symbol"]]
    signatures = {}
    for kind, ref in record["panels"].items():
        edge.check(ref)  # Each original price payload checked once, not per target.
        stat = Path(ref["path"]).stat()
        signatures[kind] = (stat.st_size, stat.st_mtime_ns)
    if price_formation.empty:
        data, source_read = {}, {"rowgroup_days_read": 0, "exact_symbol_day_observations": 0}
    else:
        data, source_read = edge._load_endpoints(record, price_formation, schedule)
    if any(day > END for day, _ in data):
        raise ValueError("Training endpoint loader crossed the fixed2025 cutoff")
    targets = _finish_targets(rows, data)
    for kind, ref in record["panels"].items():
        stat = Path(ref["path"]).stat()
        if signatures[kind] != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("A training price payload changed during the bounded read")
    reason_counts = Counter(reason for row in targets.exclusion_reason.dropna() for reason in row.split(";"))
    joint = targets.loc[targets.joint_training_eligible]
    source_read = {**source_read, "loader_original_cutoff": source_read.get("cutoff"),
        "training_cutoff": END, "actual_latest_price_day_read": max((day for day, _ in data), default=None),
        "endpoint_filters_include_future_limit_or_IPO": False}
    folder.mkdir(parents=True, exist_ok=False)
    target_path = folder/"training_targets.parquet"
    targets.to_parquet(target_path, index=False)
    result = dict(schema=METHOD_ID+"_result", method_id=METHOD_ID,
        status="completed_paired_training_labels", spec=SPEC, started_at=started, completed_at=_clock(),
        source_refs=source_refs, price_panels=record["panels"], source_read=source_read,
        rows=len(targets), joint_training_rows=len(joint), purged_rows=int(targets.purged.sum()),
        excluded_or_unknown_rows=int((~targets.joint_training_eligible & ~targets.purged).sum()),
        stage_counts={str(stage): int(joint.stage.eq(stage).sum()) for stage in (1, 2, 3)},
        exclusion_reason_counts=dict(reason_counts), max_joint_label_day=joint.max_label_day.max() if len(joint) else None,
        training_targets=edge.pin(target_path), **FLAGS)
    edge.save(folder/"result.json", result)
    return result
