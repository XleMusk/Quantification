"""Bind saved complete close previews to a dated leader/environment analysis."""
from datetime import datetime
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..market_dataset import write_json
from ..settings import ROOT
from .baostock_daily_slice import digest
from .baostock_full_close import load_complete


SCHEMA = "leader_environment_analysis_run_v1"
BOUNDARY = {"candidate_panel_only": True, "strategy_validated": False,
            "selection_enabled": False, "execution_admitted": False,
            "orders_generated": False, "ledger_mutated": False,
            "model_refit": False, "future_labels_used_for_score": False}


def _qualified_feature_evidence(envelope, features, recent, qualification):
    """Bind an opted-in child to the same typed policy without recalculating it."""
    from .ipo_qualification import IpoQualification, _time
    from .leader_daily_signals import FAMILIES, _calendar_view
    from .leader_listing_features import METHOD_ID, UNQUALIFIED_FIELDS, _digest, _state
    from .leader_recent_source_preview import DEFAULT_PRODUCER_METHOD, QUALIFIED_PRODUCER_METHOD, _day_qualification
    saved = envelope.get("feature_qualification")
    method = recent.get("producer_method") if recent else None
    if saved is None:
        if method == QUALIFIED_PRODUCER_METHOD or (recent or {}).get("feature_qualification") is not None:
            raise ValueError("Qualified recent preview requires its explicit feature-policy proof")
        if method not in (None, DEFAULT_PRODUCER_METHOD):
            raise ValueError("Unknown recent-source producer method")
        return None
    day = envelope["day"]
    cutoff = day + "T15:05:00+08:00"
    audit_columns = ["unqualified_" + field for field in UNQUALIFIED_FIELDS]
    required = {*audit_columns, "listing_feature_method_id", "feature_decision_as_of", "listing_qualification_status"}
    if (not isinstance(qualification, IpoQualification) or not recent or method != QUALIFIED_PRODUCER_METHOD
            or saved != recent.get("feature_qualification") or saved.get("method_id") != METHOD_ID
            or saved.get("verification") != qualification.verification
            or saved.get("raw_prices_modified") is not False
            or saved.get("unqualified_feature_columns") != audit_columns
            or envelope.get("history_semantics", {}).get("feature_qualification_method_id") != METHOD_ID
            or not required <= set(features.columns)
            or not features.listing_feature_method_id.eq(METHOD_ID).all()
            or not features.feature_decision_as_of.eq(cutoff).all()
            or set(saved.get("decisions", {})) != {day}
            or set(saved.get("known_excluded_symbols_by_day", {})) != {day}
            or set(saved.get("unknown_symbols_by_family_by_day", {})) != set(FAMILIES)
            or any(set(values) != {day} for values in saved["unknown_symbols_by_family_by_day"].values())
            or any(saved.get(key) is not False for key in ("strategy_validated", "selection_enabled", "signal_admitted",
                "execution_admitted", "pit_verified", "model_refit", "future_labels_used_for_score"))):
        raise ValueError("Qualified recent feature method, typed proof or decision cutoff is inconsistent")
    payload = json.loads(Path(envelope["calendar"]).read_text(encoding="utf-8"))
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or tuple(sessions) != tuple(qualification.calendar):
        raise ValueError("Qualified feature and analysis calendars must match")
    root = json.loads(Path(recent["root_lineage_file"]).read_text(encoding="utf-8"))
    root_saved = root.get("feature_qualification") or {}
    if (root.get("producer_method") != method or root.get("shared_view_id") != recent["shared_view_id"]
            or set(root_saved.get("decisions", {})) != set(recent["view_sessions"])
            or saved != _day_qualification(root_saved, day)):
        raise ValueError("Qualified child and root feature-policy lineage disagree")
    visible = {symbol: qualification.records[symbol] for symbol in features.symbol
        if symbol in qualification.records and _time(qualification.records[symbol]["available_at"]) <= _time(cutoff)}
    visible_digest = _digest(visible)
    for row in features.itertuples():
        if row.listing_qualification_status != _state(qualification.qualify(day, row.symbol, decision_as_of=cutoff)):
            raise ValueError("Qualified feature current listing status contradicts the typed decision proof")
    families = {entry["family"]: entry for entry in envelope.get("families", [])}
    decisions = saved["decisions"][day]
    if set(families) != set(FAMILIES) or set(decisions) != set(FAMILIES):
        raise ValueError("Qualified recent preview requires every fixed family decision")
    for family, decision in decisions.items():
        unknown = decision.get("unknown_symbols")
        entry = families[family]
        batch_proof = dict(entry.get("feature_qualification") or {})
        batch_proof.pop("source_coverage_status_before_qualification", None)
        if (decision.get("method_id") != METHOD_ID or decision.get("day") != day
                or decision.get("decision_as_of") != cutoff
                or decision.get("decision_visible_records_sha256") != visible_digest
                or not isinstance(unknown, list) or unknown != sorted(set(unknown))
                or not set(unknown) <= set(features.symbol)
                or decision.get("qualification_complete") is not (not unknown)
                or unknown != saved["unknown_symbols_by_family_by_day"][family][day]
                or decision.get("known_excluded_symbols") != saved["known_excluded_symbols_by_day"][day]
                or decision.get("unknown_semantics") != "potential_rule_membership_unknown_requires_NO_CHANGE_not_empty_cash"
                or batch_proof != decision
                or (unknown and (entry.get("producer_batch_status") != "incomplete"
                    or entry.get("incomplete_reason") != "candidate_listing_feature_membership_unknown"))):
            raise ValueError("Qualified decision membership is inconsistent or treats unknown as known empty cash")
    return {"method_id": METHOD_ID, "verification": qualification.verification,
        "decision_as_of": cutoff, "decision_visible_records_sha256": visible_digest,
        "unqualified_feature_columns": audit_columns,
        "unknown_symbols_by_family": {family: decisions[family]["unknown_symbols"] for family in FAMILIES},
        "membership_unknown_is_cash": False}


def read_preview(folder, *, ipo_qualification=None):
    folder = Path(folder).resolve()
    path = folder / "result.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (envelope.get("schema") != "leader_complete_close_preview_v1"
            or envelope.get("status") != "complete_source_close_research_preview_only"
            or envelope.get("source_inventory_complete") is not True
            or any(envelope.get(key) is not value for key, value in BOUNDARY.items())):
        raise ValueError("Expected an unchanged complete-close research preview")
    source = envelope.get("source") or {}
    raw, verified = load_complete(source["source_folder"])
    if (envelope.get("day") != verified["plan"]["day"]
            or source.get("receipt", {}).get("raw_daily_sha256") != verified["receipt"]["raw_daily_sha256"]):
        raise ValueError("Preview does not match its exact completed source")
    feature_path = folder / "current_features.csv"
    features = pd.read_csv(feature_path, dtype={"symbol": str, "date": str, "code": str})
    if (features.empty or features.symbol.duplicated().any()
            or not features.date.eq(envelope["day"]).all()):
        raise ValueError("Latest feature table must contain unique exact-date symbols")
    actual = raw.copy()
    actual["symbol"] = actual.code.str[3:] + np.where(actual.code.str.startswith("sh."), ".SSE", ".SZSE")
    active = actual.loc[actual.tradestatus.eq("1") & actual.isST.eq("0")
                        & pd.to_numeric(actual.volume, errors="raise").gt(0)].set_index("symbol")
    if set(features.symbol) != set(active.index):
        raise ValueError("Feature symbols differ from the complete active non-ST raw scope")
    indexed = features.set_index("symbol").loc[active.index]
    for column, raw_column in (("open", "open"), ("high", "high"), ("low", "low"),
                               ("close", "close"), ("volume", "volume"), ("amount", "amount"), ("pct_chg", "pctChg")):
        if not np.isclose(pd.to_numeric(indexed[column], errors="raise"),
                          pd.to_numeric(active[raw_column], errors="raise"), rtol=1e-10, atol=1e-7).all():
            raise ValueError(f"Feature/raw values disagree: {column}")
    names = {row["symbol"]: row["code_name"] for row in verified["ordinary_roster"]}
    features["name"] = features.symbol.map(names)
    provenance = {"preview": str(path), "preview_sha256": digest(path),
        "feature_file": str(feature_path), "feature_sha256": digest(feature_path),
        "source_folder": verified["source_folder"], "raw_daily_sha256": verified["receipt"]["raw_daily_sha256"],
        "features_match_current_active_raw": True}
    lineage = envelope.get("history_repair_lineage")
    if lineage is not None:
        source = lineage.get("repair_source") or {}
        repair_day = lineage.get("repair_day", "")
        if (lineage.get("schema") != "leader_historical_gap_feature_view_v1"
                or lineage.get("derived_at") != envelope.get("created_at")
                or not repair_day or repair_day >= envelope["day"]
                or lineage.get("original_repair_day_rows") != 0
                or lineage.get("current_raw_fields_unchanged") is not True
                or any(lineage.get(key) is not value for key, value in BOUNDARY.items())
                or any(lineage.get(key) is not False for key in
                       ("continuous_history_claimed", "history_inventory_complete", "future_bars_used",
                        "stockdb_modified", "native_features_modified", "prior_scores_modified"))
                or source.get("read_only_verified") is not True
                or source.get("plan", {}).get("day") != repair_day
                or source.get("receipt", {}).get("day") != repair_day
                or source.get("receipt", {}).get("full_dated_roster_inventory_complete") is not True):
            raise ValueError("Historical repair lineage is incomplete or changes a research boundary")
        provenance["history_repair"] = {key: lineage.get(key) for key in
            ("schema", "derived_at", "repair_day", "original_history_snapshot", "original_history_manifest_sha256",
             "original_history_max_observed_day", "exact_recent_sessions", "raw_rows_by_recent_session",
             "remaining_empty_pinned_year_sessions", "continuous_history_claimed", "history_inventory_complete")}
        provenance["history_repair"].update(repair_source_folder=source.get("source_folder"),
            repair_raw_daily_sha256=source.get("receipt", {}).get("raw_daily_sha256"),
            historical_publication_verified=False)
    recent = envelope.get("recent_source_lineage")
    if recent is not None:
        from .leader_daily_signals import _calendar_view
        view_days = recent.get("view_sessions") or []
        used = recent.get("source_days_used") or []
        proofs = recent.get("source_proofs") or []
        payload = json.loads(Path(envelope["calendar"]).read_text(encoding="utf-8"))
        sessions, previous, status = _calendar_view(payload, view_days[0], view_days[-1], []) if view_days else ([], {}, "unknown")
        expected = [day for day in view_days if day <= envelope["day"]]
        if (recent.get("schema") != "leader_recent_source_feature_view_v1"
                or recent.get("derived_at") != envelope.get("created_at")
                or recent.get("day") != envelope["day"] or not recent.get("shared_view_id")
                or status != "pinned_static_exchange_calendar" or sessions != view_days or len(view_days) < 3
                or not used or used != expected or used[-1] != envelope["day"]
                or [proof.get("day") for proof in proofs] != used
                or recent.get("base_history_requested_end") != previous.get(view_days[0])
                or recent.get("base_history_max_observed_day") != recent.get("base_history_requested_end")
                or recent.get("original_rows_after_cutoff_loaded_into_view") != 0
                or recent.get("current_raw_fields_unchanged") is not True
                or any(recent.get(key) is not value for key, value in BOUNDARY.items())
                or any(recent.get(key) is not False for key in
                       ("continuous_history_claimed", "history_inventory_complete", "future_bars_used",
                        "actual_signal_publication_verified", "historical_publication_verified", "stockdb_modified",
                        "native_features_modified", "prior_scores_modified", "prior_previews_modified"))
                or any(proof.get("read_only_verified") is not True or proof.get("source_inventory_complete") is not True
                       or proof.get("full_dated_roster_inventory_complete") is not True
                       or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"
                       or proof.get("calendar_sha256") != verified["plan"]["calendar_sha256"] for proof in proofs)
                or proofs[-1].get("source_folder") != verified["source_folder"]
                or proofs[-1].get("raw_daily_sha256") != verified["receipt"]["raw_daily_sha256"]
                or recent.get("calendar_sha256") != verified["plan"]["calendar_sha256"]):
            raise ValueError("Recent-source view lineage has mixed, future or incomplete evidence")
        provenance["recent_source_view"] = {key: recent.get(key) for key in
            ("schema", "shared_view_id", "derived_at", "view_sessions", "source_days_used", "source_proofs",
             "base_history_requested_end", "base_history_max_observed_day", "original_history_snapshot",
             "original_history_manifest_sha256", "conflict_rule", "producer_method", "historical_publication_verified")}
    qualified = _qualified_feature_evidence(envelope, features, recent, ipo_qualification)
    if qualified is not None:
        provenance["feature_qualification"] = qualified
    return envelope, features, provenance


def analyze(preview_folders, calendar_path, out, *, ipo_evidence=None, research_master_run=None,
            industry_snapshot=None, availability_receipt=None, bulk_extras=None):
    from .leader_environment import build_environment
    calendar_path = Path(calendar_path).resolve()
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    proof = None
    if research_master_run and not ipo_evidence:
        raise ValueError("Research master must be bound to explicit IPO evidence")
    if ipo_evidence:
        from .ipo_qualification import load_ipo_qualification
        proof = load_ipo_qualification(ipo_evidence, calendar_path, master_run=research_master_run)
    loaded = [read_preview(folder, ipo_qualification=proof) for folder in preview_folders]
    loaded.sort(key=lambda item: item[0]["day"])
    days = [item[0]["day"] for item in loaded]
    if not days or len(set(days)) != len(days):
        raise ValueError("Analysis needs distinct complete preview dates")
    recent_views = [item[2].get("recent_source_view") for item in loaded]
    if any(recent_views) and (not all(recent_views) or len({item["shared_view_id"] for item in recent_views}) != 1):
        raise ValueError("Recent-source feedback requires one coherent shared feature view")
    history = pd.concat([item[1] for item in loaded[:-1]], ignore_index=True) if len(loaded) > 1 else None
    recorded = datetime.now().astimezone().isoformat()
    qualified = [item[2].get("feature_qualification") for item in loaded]
    if any(qualified) and not all(qualified):
        raise ValueError("Environment analysis cannot mix qualified and original feature policies")
    feature_options = {"feature_qualification_verified": True} if all(qualified) else {}
    analysis = build_environment(loaded[-1][1], [item[0] for item in loaded], payload, recorded,
                                 feature_history=history, ipo_qualification=proof, **feature_options)
    result = {"schema": SCHEMA, "status": "completed_research_environment_analysis",
              "day": days[-1], "created_at": recorded, "analysis": analysis,
              "input_previews": [item[2] for item in loaded],
              "calendar": str(calendar_path), "calendar_sha256": digest(calendar_path),
              "ipo_qualification": proof.verification if proof else None,
              "provider_contacted": False, "account_created": False,
              "current_signal_admitted": False, "research_only": True, **BOUNDARY}
    if bulk_extras is not None:
        from .leader_turnover_context import turnover_context
        result["turnover_context"] = turnover_context(bulk_extras, loaded[-1][1], analysis,
            current_source=loaded[-1][0]["source"], current_provenance=loaded[-1][2], observed_at=recorded)
        result["data_availability"] = {"observed_turnover_context": True,
            "scope": "explicit_same_day_descriptive_context_not_frozen_rule_or_current_signal_change"}
    if industry_snapshot is not None:
        from .leader_sector_context import sector_context
        result["sector_context"] = sector_context(loaded[-1][1], industry_snapshot,
            decision_day=days[-1], observed_at=recorded,
            current_scope="complete_dated_provider_active_non_ST", current_source_complete=True)
    if availability_receipt is not None:
        evidence = json.loads(Path(availability_receipt).read_text(encoding="utf-8"))
        if (evidence.get("schema") != "leader_environment_local_data_availability_v1"
                or evidence.get("execution_admitted") is not False):
            raise ValueError("Explicit read-only data availability receipt required")
        probe = evidence.get("intraday_probe") or {}
        result["data_observation"] = {"source_receipt": str(Path(availability_receipt).resolve()),
            "checked_at": evidence.get("observed_at"),
            "intraday_probe": {key: probe.get(key) for key in
                ("started_at", "ended_at", "status", "fresh_quote_count", "current_advance_ratio", "report_dir")},
            "index_model_context": evidence.get("regime"),
            "index_context_used_to_select_archetypes": False}
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, action="append", required=True,
                        help="Strict complete dated preview, repeat to include earlier observations")
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--ipo-evidence", type=Path)
    parser.add_argument("--research-master-run", type=Path)
    parser.add_argument("--industry-snapshot", type=Path, help="Explicit lagged industry snapshot, never current topic membership")
    parser.add_argument("--availability-receipt", type=Path, help="Saved dated data-probe evidence; never a current quote substitute")
    parser.add_argument("--bulk-extras", type=Path, help="Explicit typed same-day turnover extras; cannot substitute another source day")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = analyze(args.preview, args.calendar, args.out, ipo_evidence=args.ipo_evidence,
                     research_master_run=args.research_master_run, industry_snapshot=args.industry_snapshot,
                     availability_receipt=args.availability_receipt, bulk_extras=args.bulk_extras)
    print(json.dumps({"status": result["status"], "day": result["day"],
                      "output": str(args.out.resolve()), "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
