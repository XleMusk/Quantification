"""Assemble saved qualified closes and fixed eight-type research evidence.

No source collection, feature rebuild, identity registration or account replay
occurs here. Every dated calculation receives only its own historical prefix.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile

import pandas as pd

from ..market_dataset import write_json
from .baostock_daily_slice import FIELDS, digest
from .baostock_full_close import load_complete
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import INPUT_SCHEMA, TYPE_IDS, GUARDS, CAPS, _config
from .leader_environment import build_environment, _clean, _number
from .leader_environment_run import read_preview
from .leader_identity_registry import report
from .leader_listing_features import METHOD_ID as FEATURE_METHOD
from .leader_missing_type_candidates import build_missing_type_candidates
from .leader_recent_source_history import source_proof
from .style_exposure import load_industries


OLD_TYPES = {"space_height_leader", "relay_continuation_leader", "trend_leader",
             "liquidity_capacity_proxy", "rebound_repair_candidate"}
FLAGS = {"candidate_panel_only": True, "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "orders_generated": False, "ledger_mutated": False,
    "account_created": False, "historical_publication_verified": False, "pit_verified": False,
    "corporate_actions_verified": False}


def _now():
    return datetime.now().astimezone()


def _time(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Actual evidence clocks require an explicit timezone")
    return stamp


def _max_clock(*values):
    return max(_time(value) for value in values if value is not None).isoformat()


def _industry(path, observed_at):
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "2026-09-30.parquet"
    day = path.stem
    folder, receipt_path = path.parent, path.parent / "result.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    entries = [item for item in receipt["entries"] if item["date"] == day]
    if (receipt.get("status") != "completed_requested_industry_history" or len(entries) != 1
            or (folder / entries[0]["file"]).resolve() != path):
        raise ValueError("One explicitly selected completed dated broad-industry snapshot required")
    entry = entries[0]
    available = entry["retrieved_at"]
    if _time(available) > _time(observed_at):
        raise ValueError("Industry source has not actually been retrieved at this observation")
    before, receipt_hash = digest(path), digest(receipt_path)
    frame, _ = load_industries(folder, [day])
    if (digest(path) != before or digest(receipt_path) != receipt_hash or frame.empty
            or frame.symbol.duplicated().any() or not frame.date.eq(day).all()):
        raise ValueError("Complete dated industry source changed or lost full member identities")
    classifications = sorted(frame.industryClassification.dropna().astype(str).unique())
    if len(classifications) != 1:
        raise ValueError("One saved broad-industry classification is required, not a chosen theme")
    value = {"membership": frame, "snapshot_date": day, "available_at": available,
        "source_file": str(path), "source_sha256": before, "receipt_file": str(receipt_path),
        "receipt_sha256": receipt_hash, "classification": classifications[0], "theme_membership": False,
        "member_count": len(frame), "revision_safe_PIT_verified": False}
    return value


def _old_type(item, features, ordinary, scope):
    """Retain original labels while exposing incomplete membership to replay."""
    identity = item["id"]
    unknown = set(item.get("unresolved_stage_symbols", [])) | set(item.get("unresolved_rebound_symbols", []))
    raw = ordinary.copy()
    raw["symbol"] = raw.code.str[3:] + raw.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    by_raw = raw.set_index("symbol").to_dict("index")
    feature_rows = features.set_index("symbol").to_dict("index")
    exclusions = {}
    for symbol in scope:
        row, actual = feature_rows.get(symbol), by_raw.get(symbol)
        if actual is None:
            unknown.add(symbol)
            continue
        if str(actual["isST"]) == "1" or str(actual["tradestatus"]) == "0":
            exclusions[symbol] = "known_current_ST_or_suspension"
            continue
        if row is None:
            unknown.add(symbol)
            continue
        amount = _number(row.get("amount"))
        if amount is not None and amount < 20_000_000:
            exclusions[symbol] = "known_amount_below_fixed_20m"
            continue
        listing = str(row.get("listing_qualification_status", "unknown"))
        if listing.startswith("known_"):
            exclusions[symbol] = "known_listing_window_exclusion"
            continue
        if amount is None or listing != "supported":
            unknown.add(symbol)
        if identity == "trend_leader" and any(_number(row.get(key)) is None for key in ("sma20", "sma60", "ret20", "ret60")):
            unknown.add(symbol)
        if identity == "liquidity_capacity_proxy" and _number(row.get("amount_rank_pct")) is None:
            unknown.add(symbol)
    candidates = deepcopy(item["candidates"])
    matches = {row["symbol"] for row in candidates}
    for symbol in scope:
        if symbol not in matches and symbol not in unknown and symbol not in exclusions:
            exclusions[symbol] = "observed_fixed_descriptive_proxy_not_matched"
    return {**deepcopy(item), "candidates": candidates, "unresolved_symbols": sorted(unknown),
        "scope_complete": not set(scope).intersection(unknown), "known_exclusions": exclusions,
        "scope_members": list(scope), "source_method": "unchanged_original_environment_archetype_definition",
        "additional_unknown_policy": "missing required fixed feature or qualification never treated as known empty",
        **FLAGS}


def _derive_day(index, loaded, actual, views, proofs, days, scope, payload,
                qualification, tracking, industry, turns):
    """Derive one day without revisiting any saved historical panel."""
    envelope, features, _ = loaded[index]
    day = envelope["day"]
    analysis = build_environment(features, [item[0] for item in loaded[:index + 1]], payload,
        _now().isoformat(),
        feature_history=pd.concat([item[1] for item in loaded[:index]], ignore_index=True) if index else None,
        ipo_qualification=qualification, feature_qualification_verified=True)
    missing = build_missing_type_candidates(day=day, observed_at=_now().isoformat(), calendar_payload=payload,
        scope_members=scope, complete_views=views[:index + 1], identity_tracking=tracking,
        industry_membership=industry, turnover_batches=[value for value in turns if value["day"] <= day])
    by_type = {item["id"]: _old_type(item, features, actual[index][0], scope)
        for item in analysis["archetypes"] if item["id"] in OLD_TYPES}
    by_type.update({item["id"]: deepcopy(item) for item in missing["types"]})
    if set(by_type) != set(TYPE_IDS):
        raise ValueError("Exactly five original labels and three new proxies are required")
    available = _max_clock(views[index]["available_at"], _now().isoformat(),
        *[candidate.get("available_at") for item in missing["types"] for candidate in item["candidates"]])
    panel = {"day": day, "available_at": available, "source_proof": proofs[index],
        "environment": {"source_complete": True, "breadth": envelope["tape"]["breadth"],
            "median_return_pct": envelope["tape"]["median_return_pct"]},
        "types": [by_type[identity] for identity in TYPE_IDS],
        "risk_features": _clean(features.set_index("symbol").to_dict("index")),
        "risk_feature_scope_complete": True, "source_days_used": days[:index + 1],
        "late_context": True, "historical_signal_publication_verified": False}
    return panel, analysis, missing


def _append_inputs(previews, calendar, ipo_evidence, research_master_run, identity_registry,
                   industry_snapshot, turnover_sources, protocol, out, append_from):
    """Extend a saved publication input by exactly one close; keep its past facts."""
    from .leader_eight_type_panel_replay import load_inputs
    from .leader_eight_type_paper import _source_bindings
    from .leader_dated_turnover import load_turnover_source

    if len(previews) != 1:
        raise ValueError("Append requires only one new exact-next-session preview")
    out, parent_path = Path(out).resolve(), Path(append_from).resolve()
    if out.exists():
        raise FileExistsError("Eight-type inputs require a new output directory")
    # Missing new evidence must not create either an output or a staging folder.
    new_preview = Path(previews[0]).resolve()
    if not (new_preview / "result.json").is_file() or not (new_preview / "current_features.csv").is_file():
        raise ValueError("New complete qualified preview is not available")
    parent_hash = digest(parent_path)
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    calendar, protocol = Path(calendar).resolve(), Path(protocol).resolve()
    if (calendar != Path(parent["calendar"]).resolve()
            or protocol != Path(parent["protocol_file"]).resolve()
            or digest(calendar) != parent["calendar_sha256"]
            or digest(protocol) != parent["protocol_sha256"]):
        raise ValueError("Append cannot change the frozen calendar or protocol")
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    sessions, previous, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    old_days = parent["source_days"]
    day = json.loads((new_preview / "result.json").read_text(encoding="utf-8"))["day"]
    last_index = sessions.index(old_days[-1]) if old_days else -1
    if (status != "pinned_static_exchange_calendar" or last_index < 0 or last_index + 1 >= len(sessions)
            or day != sessions[last_index + 1]):
        raise ValueError("Append accepts only the exact next session, never a skipped or rederived close")
    if (old_days != sorted(set(old_days)) or len(old_days) < 3
            or [value for value in sessions if old_days[0] <= value <= old_days[-1]] != old_days
            or [value["day"] for value in parent["panels"]] != old_days
            or len(parent["preview_sources"]) != len(old_days)):
        raise ValueError("Parent must retain its complete ordered panel and preview prefix")
    saved = load_inputs(parent_path, ipo_evidence, research_master_run)
    observed = _now()
    _source_bindings(saved, observed)
    qualification = saved["qualification"]
    if parent["IPO_qualification"] != qualification.verification:
        raise ValueError("Append cannot replace the frozen IPO qualification evidence")
    old_identity = parent["identity_report"]
    if Path(identity_registry).resolve() != Path(old_identity["registry"]).resolve():
        raise ValueError("Append cannot replace the original identity registry")
    tracking = report(identity_registry)
    observed = _now()
    if (tracking["registry"] != old_identity["registry"]
            or tracking["registry_sha256"] != old_identity["registry_sha256"]
            or _time(tracking["read_at"]) > observed):
        raise ValueError("Current identity report must retain the same frozen registry and members")
    industry = _industry(industry_snapshot, observed.isoformat())
    if {key: value for key, value in industry.items() if key != "membership"} != parent["industry_source"]:
        raise ValueError("Append cannot replace the frozen lagged broad-industry source")

    loaded, actual, views = [], [], []
    proofs = deepcopy(parent["source_proofs"])
    for expected_day, reference, expected_proof in zip(old_days, parent["preview_sources"], proofs):
        item = read_preview(Path(reference["preview"]).parent, ipo_qualification=qualification)
        if item[0]["day"] != expected_day or item[2] != reference:
            raise ValueError("Saved historical preview bytes or feature qualification changed")
        raw = saved["raw"].loc[saved["raw"].date.eq(expected_day), FIELDS.split(",")].copy()
        if item[2]["recent_source_view"]["source_proofs"][-1] != expected_proof:
            raise ValueError("Historical preview source proof prefix changed")
        loaded.append(item)
        actual.append((raw, None))
        views.append({"day": expected_day, "features": item[1], "ordinary_rows": raw,
            "available_at": _max_clock(item[0]["created_at"], expected_proof.get("source_completed_at"), expected_proof.get("source_updated_at")),
            "source_proof": expected_proof, "feature_method_id": FEATURE_METHOD,
            "feature_qualification_verified": True})
    current = read_preview(new_preview, ipo_qualification=qualification)
    envelope, features, provenance = current
    raw, metadata = load_complete(envelope["source"]["source_folder"])
    proof = source_proof(raw, metadata, parent["calendar_sha256"])
    lineage = provenance.get("recent_source_view") or {}
    days = old_days + [day]
    if (envelope["day"] != day or proof["day"] != day or not provenance.get("feature_qualification")
            or Path(envelope["calendar"]).resolve() != calendar
            or lineage.get("source_days_used") != days or lineage.get("source_proofs") != proofs + [proof]
            or lineage.get("original_history_snapshot") != parent["history_snapshot"]
            or lineage.get("original_history_manifest_sha256") != parent["original_history_manifest_sha256"]
            or lineage.get("base_history_max_observed_day") != parent["base_history_end"]
            or previous[old_days[0]] != parent["base_history_end"]):
        raise ValueError("New preview must extend the exact saved source, history and calendar prefix")
    proofs.append(proof)
    loaded.append(current)
    actual.append((raw, metadata))
    views.append({"day": day, "features": features, "ordinary_rows": raw,
        "available_at": _max_clock(envelope["created_at"], proof.get("source_completed_at"), proof.get("source_updated_at")),
        "source_proof": proof, "feature_method_id": FEATURE_METHOD, "feature_qualification_verified": True})
    if (any(_time(value["available_at"]) > observed for value in views)
            or observed < _time(day + "T15:00:00+08:00")):
        raise ValueError("Append evidence cannot be available before its actual complete close and preview")

    turns = []
    for reference in parent["turnover_sources"]:
        source = reference["sourceproof"]
        locator = source.get("manifest_file") or source.get("extras_result")
        value = load_turnover_source(locator, observed_at=observed.isoformat(), day=reference["day"])
        if {key: entry for key, entry in value.items() if key != "rows"} != reference:
            raise ValueError("Saved dated turnover prefix changed")
        turns.append(value)
    by_day = {value["day"]: value for value in turns}
    supplied_days = set()
    for locator in turnover_sources:
        value = load_turnover_source(locator, observed_at=observed.isoformat())
        date = value["day"]
        if date in supplied_days or date > day:
            raise ValueError("Append requires distinct nonfuture dated turnover sources")
        supplied_days.add(date)
        if date in by_day:
            if {key: entry for key, entry in value.items() if key != "rows"} != {key: entry for key, entry in by_day[date].items() if key != "rows"}:
                raise ValueError("An existing dated turnover source cannot be replaced")
        else:
            # A late historical source may inform only this new decision, never old panels.
            turns.append(value)
            by_day[date] = value
    panel, analysis, missing = _derive_day(len(old_days), loaded, actual, views, proofs, days,
        parent["scope_members"], payload, qualification, tracking, industry, turns)
    combined = pd.concat([saved["raw"], raw[FIELDS.split(",")]], ignore_index=True)
    if (combined.duplicated(["date", "code"]).any()
            or combined.loc[combined.date.isin(old_days)].to_dict("records") != saved["raw"].to_dict("records")):
        raise ValueError("Appended raw cannot alter, duplicate or replace the historical prefix")

    # Only the final directory is visible to consumers. Internal paths already
    # name that final directory, not its short-lived staging location.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".append-", dir=out.parent) as temporary:
        stage = Path(temporary)
        combined.to_parquet(stage / "raw_daily.parquet", index=False)
        write_json(stage / "identity_report.json", tracking)
        write_json(stage / "industry_source.json", parent["industry_source"])
        for name, record in (("environment", analysis), ("missing_type_candidates", missing)):
            (stage / name).mkdir()
            write_json(stage / name / (day.replace("-", "") + ".json"), _clean(record))
        created = _now().isoformat()
        if (_time(created) < observed or _time(parent["created_at"]) > _time(created)
                or _time(panel["available_at"]) > _time(created)
                or digest(parent_path) != parent_hash or digest(calendar) != parent["calendar_sha256"]
                or digest(protocol) != parent["protocol_sha256"]
                or digest(saved["provenance"]["raw_file"]) != parent["raw_sha256"]
                or digest(identity_registry) != tracking["registry_sha256"]):
            raise ValueError("Append inputs changed or their actual recording clocks moved backwards")
        for _, _, reference in loaded:
            if (digest(reference["preview"]) != reference["preview_sha256"]
                    or digest(reference["feature_file"]) != reference["feature_sha256"]):
                raise ValueError("A preview changed during append derivation")
        manifest = deepcopy(parent)
        manifest.update(created_at=created, raw_file=str(out / "raw_daily.parquet"),
            raw_sha256=digest(stage / "raw_daily.parquet"), source_days=days, source_proofs=proofs,
            panels=deepcopy(parent["panels"]) + [panel],
            preview_sources=deepcopy(parent["preview_sources"]) + [provenance],
            shared_view_id=lineage["shared_view_id"],
            identity_report={"file": str(out / "identity_report.json"), "sha256": digest(stage / "identity_report.json"),
                "registry": tracking["registry"], "registry_sha256": tracking["registry_sha256"], "read_at": tracking["read_at"]},
            turnover_sources=[{key: value for key, value in batch.items() if key != "rows"} for batch in turns],
            append_lineage={"method_id": "leader_eight_type_inputs_exact_next_close_append_v1",
                "parent_manifest_file": str(parent_path), "parent_manifest_sha256": parent_hash,
                "parent_created_at": parent["created_at"], "prior_last_day": old_days[-1],
                "appended_day": day, "derived_at": created, "old_panels_recomputed": False,
                "old_panel_values_unchanged": True,
                "current_identity_and_new_turnover_scope": "new_decision_only_no_historical_membership_backfill",
                "historical_sidecars": "retained_in_unchanged_parent_directory"})
        write_json(stage / "manifest.json", _clean(manifest))
        stage.rename(out)
    return manifest


def assemble(previews, calendar, ipo_evidence, research_master_run, identity_registry, industry_snapshot,
             turnover_sources, protocol, out, *, append_from=None):
    if append_from is not None:
        return _append_inputs(previews, calendar, ipo_evidence, research_master_run, identity_registry,
            industry_snapshot, turnover_sources, protocol, out, append_from)
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Eight-type inputs require a new output directory")
    calendar, protocol = Path(calendar).resolve(), Path(protocol).resolve()
    payload = json.loads(calendar.read_text(encoding="utf-8"))
    frozen = json.loads(protocol.read_text(encoding="utf-8"))
    expected_cases = [f"{identity}__{guard}__{cap}" for identity in TYPE_IDS for guard in GUARDS for cap, _ in CAPS]
    if (frozen.get("schema") != "leader_eight_type_fixed_comparison_protocol_v1"
            or frozen.get("case_count") != 32 or frozen.get("config") != _config(100000, 5).to_dict()
            or [case["id"] for case in frozen.get("cases", [])] != expected_cases
            or any(frozen.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score"))):
        raise ValueError("Exact separately frozen thirty-two-case protocol and unchanged costs required")
    calendar_hash, protocol_hash = digest(calendar), digest(protocol)
    qualification = load_ipo_qualification(ipo_evidence, calendar, master_run=research_master_run)
    loaded = [read_preview(folder, ipo_qualification=qualification) for folder in previews]
    loaded.sort(key=lambda item: item[0]["day"])
    days = [item[0]["day"] for item in loaded]
    sessions, previous, status = _calendar_view(payload, days[0], days[-1], []) if days else ([], {}, "unknown")
    if (len(days) < 3 or len(set(days)) != len(days) or status != "pinned_static_exchange_calendar" or days != sessions
            or tuple(qualification.calendar) != tuple(_calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])[0])):
        raise ValueError("At least three consecutive exact qualified preview sessions required")
    lineages = [item[2].get("recent_source_view") for item in loaded]
    if (not all(lineages) or len({value["shared_view_id"] for value in lineages}) != 1
            or not all(item[2].get("feature_qualification") for item in loaded)
            or any(value["base_history_max_observed_day"] != previous[days[0]] for value in lineages)
            or any(Path(item[0]["calendar"]).resolve() != calendar for item in loaded)):
        raise ValueError("One coherent qualified recent view, original history cutoff and pinned calendar required")
    if len({(value["original_history_snapshot"], value["original_history_manifest_sha256"],
             value["base_history_max_observed_day"]) for value in lineages}) != 1:
        raise ValueError("Qualified previews must retain the same original history reference")
    actual, views, proofs = [], [], []
    for envelope, features, provenance in loaded:
        raw, metadata = load_complete(envelope["source"]["source_folder"])
        proof = source_proof(raw, metadata, calendar_hash)
        if (proof != provenance["recent_source_view"]["source_proofs"][-1]
                or envelope["day"] != proof["day"]):
            raise ValueError("Preview and actual ordinary raw source proofs differ")
        available = _max_clock(envelope["created_at"], proof.get("source_completed_at"), proof.get("source_updated_at"))
        actual.append((raw, metadata))
        proofs.append(proof)
        views.append({"day": envelope["day"], "features": features, "ordinary_rows": raw,
            "available_at": available, "source_proof": proof, "feature_method_id": FEATURE_METHOD,
            "feature_qualification_verified": True})
    scope = sorted(row["symbol"] for row in actual[0][1]["ordinary_roster"])
    if not scope or len(scope) != len(set(scope)):
        raise ValueError("First full ordinary roster must define a distinct fixed panel including ST and halt rows")
    scope_frozen_at = _now().isoformat()
    tracking = report(identity_registry)
    industry = _industry(industry_snapshot, _now().isoformat())
    from .leader_dated_turnover import load_turnover_source
    turns = [load_turnover_source(path, observed_at=_now().isoformat()) for path in turnover_sources]
    if len({value["day"] for value in turns}) != len(turns):
        raise ValueError("Only one explicit typed turnover source per date may enter the comparison")
    panel_rows, missing_rows, analyses = [], [], []
    for index, (envelope, features, provenance) in enumerate(loaded):
        panel, analysis, missing = _derive_day(index, loaded, actual, views, proofs, days,
            scope, payload, qualification, tracking, industry, turns)
        panel_rows.append(panel)
        analyses.append(analysis)
        missing_rows.append(missing)
    created = _now().isoformat()
    lineage = lineages[0]
    if (digest(calendar) != calendar_hash or digest(protocol) != protocol_hash
            or digest(Path(lineage["original_history_snapshot"]) / "manifest.json") != lineage["original_history_manifest_sha256"]
            or any(_time(row["available_at"]) > _time(created) for row in panel_rows)):
        raise ValueError("Fixed protocol/calendar changed or derivation clocks moved backwards")
    out.mkdir(parents=True, exist_ok=False)
    raw_file = out / "raw_daily.parquet"
    pd.concat([raw[FIELDS.split(",")] for raw, _ in actual], ignore_index=True).to_parquet(raw_file, index=False)
    write_json(out / "identity_report.json", tracking)
    write_json(out / "industry_source.json", {key: value for key, value in industry.items() if key != "membership"})
    for name, records in (("environment", analyses), ("missing_type_candidates", missing_rows)):
        folder = out / name
        folder.mkdir()
        for record in records:
            write_json(folder / (record["day"].replace("-", "") + ".json"), _clean(record))
    manifest = {"schema": INPUT_SCHEMA, "created_at": created, "raw_file": str(raw_file), "raw_sha256": digest(raw_file),
        "calendar": str(calendar), "calendar_sha256": calendar_hash, "source_days": days, "source_proofs": proofs,
        "scope_members": scope, "scope_basis": "first_complete_source_ordinary_roster_including_ST_and_halt_not_future_survivors_not_PIT",
        "scope_frozen_at": scope_frozen_at, "scope_freeze_is_actual_late_derivation": True,
        "history_snapshot": lineage["original_history_snapshot"],
        "original_history_manifest_sha256": lineage["original_history_manifest_sha256"],
        "base_history_end": lineage["base_history_max_observed_day"],
        "protocol_file": str(protocol), "protocol_sha256": protocol_hash, "panels": panel_rows,
        "preview_sources": [item[2] for item in loaded], "shared_view_id": lineage["shared_view_id"],
        "identity_report": {"file": str(out / "identity_report.json"), "sha256": digest(out / "identity_report.json"),
            "registry": tracking["registry"], "registry_sha256": tracking["registry_sha256"], "read_at": tracking["read_at"]},
        "industry_source": {key: value for key, value in industry.items() if key != "membership"},
        "turnover_sources": [{key: value for key, value in batch.items() if key != "rows"} for batch in turns],
        "status": "completed_late_context_eight_type_research_inputs", "old_features_recomputed": False,
        "old_formal_protocol_modified": False, "future_raw_used_for_candidate_membership": False,
        "IPO_qualification": qualification.verification, **FLAGS}
    write_json(out / "manifest.json", _clean(manifest))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, action="append", required=True)
    for name in ("calendar", "ipo-evidence", "research-master-run", "identity-registry", "industry-snapshot", "protocol", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--turnover-source", type=Path, action="append", default=[])
    parser.add_argument("--append-from", type=Path,
        help="Preserve a saved input prefix and derive only one exact-next-session preview")
    args = parser.parse_args(argv)
    result = assemble(args.preview, args.calendar, args.ipo_evidence, args.research_master_run, args.identity_registry,
        args.industry_snapshot, args.turnover_source, args.protocol, args.out, append_from=args.append_from)
    print(json.dumps({"status": result["status"], "manifest": str(args.out.resolve() / "manifest.json"),
        "source_days": result["source_days"], "scope_members": len(result["scope_members"]), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
