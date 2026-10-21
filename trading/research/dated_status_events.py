"""Known-after official status events for new-candidate decisions only.

Official pages downloaded today are not an archived PIT feed. Publication-day
23:59:59 is a conservative availability assumption, never same-close evidence.
No event makes an unknown instrument eligible, prices a holding, or settles it.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlparse


TZ = timezone(timedelta(hours=8))
SCHEMA = "dated_status_event_consumer_v1"
INPUT_SCHEMA = "instrument_status_event_evidence_v1"
REASONS = {"termination_decision": "confirmed_termination", "delisting_period": "delisting_period",
           "trading_halt": "halt", "delisted": "actual_delist"}
PRIORITY = ("actual_delist", "halt", "delisting_period", "confirmed_termination")
IDENTITIES = {"300379.SZSE": "北京东方通科技股份有限公司", "603056.SSE": "德邦物流股份有限公司"}


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected canonical ISO date")
    return value


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Decision and publication timestamps require explicit timezone")
    return result.astimezone(TZ)


def _unique(rows, key):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValueError(f"Missing or duplicate {key}")
        result[identity] = row
    return result


def _official_url(url, symbol):
    parsed = urlparse(url)
    suffix = "szse.cn" if symbol.endswith(".SZSE") else "sse.com.cn"
    return parsed.scheme == "https" and (parsed.hostname == suffix or
           (parsed.hostname or "").endswith("." + suffix))


@dataclass(frozen=True)
class StatusEvent:
    event_id: str
    symbol: str
    event_type: str
    effective_from: str
    effective_until: str | None
    available_at: str
    source_id: str
    facts: dict


@dataclass(frozen=True)
class StatusEventEvidence:
    events: tuple[StatusEvent, ...]
    verification: dict


def load_status_events(path):
    """Validate the curated two-name evidence and its acquisition byte pins.

    This accepts the existing narrowly scoped schema rather than interpreting
    arbitrary notice prose. Identity, venue, publisher, dates and source roles
    must agree with each referenced source and original download receipt.
    """
    path = Path(path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError("Unknown official status event evidence schema")
    safety = payload.get("safety", {})
    if any(safety.get(key) is not expected for key, expected in {
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False, "stockdb_fields_modified": False,
            "instrument_master_modified": False, "persistent_ledger_modified": False}.items()):
        raise ValueError("Status evidence does not preserve research-only boundaries")
    scope = payload.get("scope_symbols", [])
    if len(scope) != len(set(scope)) or set(scope) != set(IDENTITIES):
        raise ValueError("Only the declared two-name identity scope is supported")
    source_rows = payload.get("sources", [])
    sources = _unique(source_rows, "source_id")
    if payload.get("source_count") != len(sources):
        raise ValueError("Source count does not match declared evidence")
    original_path = Path(payload["source_receipt_path"]).resolve()
    original = json.loads(original_path.read_text(encoding="utf-8"))
    if (original.get("schema") != "primary_source_download_receipt_v1"
            or original.get("execution_admitted") is not False
            or original.get("canonical_sources_modified") is not False):
        raise ValueError("Invalid acquisition receipt")
    original_sources = _unique(original["sources"], "source_id")
    event_rows = _unique(payload.get("events", []), "event_id")
    verified = {}
    events = []
    for row in event_rows.values():
        symbol, event_type = row.get("symbol"), row.get("event_type")
        if symbol not in IDENTITIES or event_type not in REASONS:
            raise ValueError("Unsupported symbol or status event type")
        source_id = row.get("source_id")
        source, acquired = sources.get(source_id), original_sources.get(source_id)
        if not source or not acquired or source.get("status") not in {"downloaded_html", "downloaded_pdf"}:
            raise ValueError("Event has no usable acquisition source")
        role = "exchange" if event_type == "termination_decision" else "issuer_exchange_hosted"
        publisher = ("深圳证券交易所" if symbol.endswith(".SZSE") else "上海证券交易所") if role == "exchange" else IDENTITIES[symbol] + "董事会"
        if (row.get("source_role") != role or source.get("source_role") != role
                or row.get("publisher") != publisher or source.get("publisher") != publisher
                or row.get("document_title") != source.get("document_title")
                or IDENTITIES[symbol] not in row.get("document_title", "") and role == "exchange"):
            raise ValueError("Event issuer/exchange identity or source role mismatch")
        if row.get("source_url") != source.get("url") or not _official_url(source["url"], symbol):
            raise ValueError("Event source is not the declared official venue")
        if not _official_url(source.get("final_url", ""), symbol):
            raise ValueError("Source redirect left the official venue")
        for key in ("url", "raw_path", "sha256", "byte_count", "status", "publication_date", "source_role"):
            if source.get(key) != acquired.get(key):
                raise ValueError(f"Source differs from acquisition receipt: {key}")
        raw_path = Path(source["raw_path"]).resolve()
        if Path(row.get("raw_source_path", "")).resolve() != raw_path:
            raise ValueError("Event raw source path differs")
        if source_id not in verified:
            raw = raw_path.read_bytes()
            if len(raw) != source["byte_count"] or sha256(raw).hexdigest() != source["sha256"]:
                raise ValueError("Official raw source bytes do not match acquisition hash")
            text_path = Path(source["fulltext_path"]).resolve()
            if Path(row.get("fulltext_path", "")).resolve() != text_path or source.get("full_usable_text_read") is not True:
                raise ValueError("Source full-text provenance is incomplete")
            text = text_path.read_text(encoding="utf-8")
            if symbol[:6] not in text and IDENTITIES[symbol] not in text:
                raise ValueError("Source text does not identify the instrument")
            verified[source_id] = {"source_id": source_id, "source_role": role, "publisher": publisher,
                                   "raw_source_path": str(raw_path), "sha256": source["sha256"],
                                   "url": source["url"], "publication_date": source["publication_date"]}
        published = _day(row.get("publication_date"))
        available = _timestamp(row["available_at"])
        conservative = datetime.combine(date.fromisoformat(published), time(23, 59, 59), TZ)
        if (source.get("publication_date") != published or row.get("available_at") != source.get("available_at")
                or row.get("intraday_publication_time_known") is not False
                or source.get("publication_time_known") is not False or available != conservative):
            raise ValueError("Unknown intraday publication must use same-date 23:59:59 availability")
        start = _day(row.get("effective_from"))
        end = row.get("effective_until")
        if end is not None and _day(end) < start:
            raise ValueError("Event ends before effective start")
        facts = deepcopy(row.get("facts", {}))
        if event_type == "delisted" and facts.get("delist_date") != start:
            raise ValueError("Actual delist date differs from source event")
        if event_type == "trading_halt" and (facts.get("halt_end_date") != end or facts.get("resume_date") is not None):
            raise ValueError("Unknown halt end or resume must not be replaced")
        events.append(StatusEvent(row["event_id"], symbol, event_type, start, end, row["available_at"], source_id, facts))
    usable = [source for source in sources.values() if source.get("status") in {"downloaded_html", "downloaded_pdf"}]
    if payload.get("usable_source_count") != len(usable) or len(verified) != len(usable):
        raise ValueError("Usable source count or event-source coverage differs")
    return StatusEventEvidence(tuple(events), {"evidence_file": str(path),
        "evidence_sha256": sha256(path.read_bytes()).hexdigest(), "source_receipt": str(original_path),
        "source_receipt_sha256": sha256(original_path.read_bytes()).hexdigest(),
        "verified_source_count": len(verified), "verified_sources": list(verified.values()),
        "raw_acquisition_hashes_verified": True, "publication_time_basis": "publication_date_23_59_59_assumption",
        "historical_time_archive_verified": False, "pit_verified": False,
        "scope": "two_named_instruments_and_six_events_only"})


def candidate_decision(evidence, symbol, day, *, decision_as_of=None):
    """Return known exclusions; an absent event is unknown, never eligible."""
    if not isinstance(evidence, StatusEventEvidence):
        raise ValueError("Verified typed status event evidence is required")
    _day(day)
    if not re.fullmatch(r"\d{6}\.(?:SSE|SZSE)", symbol):
        raise ValueError("Expected normalized SH/SZ instrument symbol")
    cutoff = _timestamp(decision_as_of or day + "T15:05:00+08:00")
    if cutoff.date().isoformat() != day:
        raise ValueError("Decision cutoff must be on the exact queried day")
    known = [event for event in evidence.events if event.symbol == symbol and _timestamp(event.available_at) <= cutoff]
    active = [event for event in known if event.effective_from <= day
              and (event.effective_until is None or day < event.effective_until)]
    reasons = [reason for reason in PRIORITY if any(REASONS[event.event_type] == reason for event in active)]
    def describe(event):
        return {"event_id": event.event_id, "event_type": event.event_type,
                "effective_from": event.effective_from, "effective_until": event.effective_until,
                "available_at": event.available_at, "source_id": event.source_id, "facts": deepcopy(event.facts)}
    return {"schema": SCHEMA, "symbol": symbol, "day": day, "decision_as_of": cutoff.isoformat(),
            "status": "known_exclusion" if reasons else "unknown", "new_entry_eligible": False if reasons else None,
            "exclusion_reasons": reasons, "primary_exclusion_reason": reasons[0] if reasons else None,
            "active_known_events": [describe(event) for event in active],
            "known_future_effective_events": [describe(event) for event in known if event.effective_from > day],
            "halt_end_date": next((event.effective_until for event in active if event.event_type == "trading_halt"), None),
            "unknown_means_clear": False, "st_status_inferred": False,
            "historical_time_archive_verified": False, "pit_verified": False,
            "new_candidate_eligibility_only": True, "held_and_pending_inventory_must_be_retained": True,
            "price_bars_created": False, "cash_settlement_created": False,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False}


def event_candidate_coverage(raw_coverage, evidence, *, held_symbols=(), pending_symbols=(), decision_as_of=None):
    """Explain absent unheld candidates by dated exclusions; do not shrink pool.

    A termination decision or delisting-period rule is an eligibility exclusion,
    not a claim that exchange quotes must be absent. Open-ended halt evidence is
    retained with its unknown end and conditional-resume caveat.
    """
    day = _day(raw_coverage["day"])
    missing = raw_coverage["missing_symbols"]
    if (len(missing) != len(set(missing))
            or raw_coverage["observed_cohort_rows"] + len(missing) != raw_coverage["expected_cohort_rows"]):
        raise ValueError("Inconsistent frozen raw cohort coverage")
    required = set(held_symbols) | set(pending_symbols)
    whole_day = raw_coverage["observed_cohort_rows"] == 0
    decisions = {symbol: candidate_decision(evidence, symbol, day, decision_as_of=decision_as_of) for symbol in missing}
    explained = [] if whole_day else [symbol for symbol, item in decisions.items()
                  if item["status"] == "known_exclusion" and symbol not in required]
    unresolved = [symbol for symbol in missing if symbol not in explained]
    complete = not whole_day and not unresolved and not raw_coverage.get("invalid_symbols") and not raw_coverage.get("st_unknown_symbols")
    return {"schema": SCHEMA, "day": day, "status": raw_coverage["status"],
            "candidate_eligibility_observation_status": "resolved_exclusions_only" if complete else "unresolved",
            "raw_coverage": deepcopy(raw_coverage), "raw_coverage_modified": False,
            "source_or_raw_completeness_promoted": False,
            "raw_missing_count": len(missing), "candidate_exclusion_explained_symbols": explained,
            "candidate_unresolved_missing_symbols": unresolved,
            "required_held_or_pending_missing_symbols": sorted(required.intersection(missing)),
            "whole_raw_day_absent": whole_day,
            "missing_decisions": decisions, "frozen_cohort_removed_symbols": [],
            "absence_of_exchange_quotes_supported_symbols": [symbol for symbol in explained
                if any(reason in {"actual_delist", "halt"} for reason in decisions[symbol]["exclusion_reasons"])],
            "candidate_exclusion_is_not_quote_reconstruction": True,
            "open_halt_end_is_unknown_not_confirmed_continuity": True,
            "raw_prices_required_for_held_pending_and_selected_candidates": True,
            "historical_time_archive_verified": False, "pit_verified": False,
            "price_bars_created": False, "cash_settlement_created": False,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False}


def diagnose(evidence, coverage, *, start="2026-01-05", end="2026-10-09"):
    selected = [row for row in coverage if start <= row["day"] <= end]
    if len({row["day"] for row in selected}) != len(selected):
        raise ValueError("Duplicate frozen coverage day")
    rows = [event_candidate_coverage(row, evidence) for row in selected]
    explained, unknown, reasons = Counter(), Counter(), Counter()
    for row in rows:
        explained.update(row["candidate_exclusion_explained_symbols"])
        unknown.update(row["candidate_unresolved_missing_symbols"])
        reasons.update(row["missing_decisions"][symbol]["primary_exclusion_reason"]
                       for symbol in row["candidate_exclusion_explained_symbols"])
    return {"schema": SCHEMA, "start": start, "end": end, "coverage_days": len(rows),
            "raw_missing_symbol_days": sum(row["raw_missing_count"] for row in rows),
            "candidate_exclusion_explained_symbol_days": sum(explained.values()),
            "candidate_unresolved_symbol_days": sum(unknown.values()),
            "explained_by_symbol": dict(explained), "remaining_unknown_top20": unknown.most_common(20),
            "qualification_exclusions_by_primary_reason": dict(reasons),
            "raw_missing_quote_reason_not_proven_by_eligibility_filter": True,
            "strict_known_after_actual_delist_symbol_days": reasons["actual_delist"],
            "open_halt_unknown_continuity_symbol_days": reasons["halt"],
            "delisting_period_not_no_quote_proof_symbol_days": reasons["delisting_period"],
            "complete_candidate_coverage_days": sum(row["status"] == "complete" for row in rows),
            "hypothetical_no_held_or_pending_diagnostic": True, "decisions": rows,
            "evidence_verification": deepcopy(evidence.verification), "old_replays_modified": False,
            "model_refit": False, "raw_prices_created": False, "persistent_ledger_modified": False,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = load_status_events(args.evidence)
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    result = diagnose(evidence, coverage)
    matrix_days = {"300379.SZSE": ["2025-12-22", "2025-12-23", "2025-12-29", "2025-12-30", "2026-01-05", "2026-01-21", "2026-01-22", "2026-01-23"],
                   "603056.SSE": ["2026-01-20", "2026-01-21", "2026-01-22", "2026-01-30", "2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31"]}
    matrix = [candidate_decision(evidence, symbol, day, decision_as_of=day + "T" + clock + "+08:00")
              for symbol, days in matrix_days.items() for day in days for clock in ("15:05:00", "23:59:59")]
    args.out.mkdir(parents=True, exist_ok=False)
    result.update(coverage_file=str(args.coverage.resolve()), coverage_sha256=sha256(args.coverage.read_bytes()).hexdigest())
    for name, data in (("result.json", result), ("date_matrix.json", matrix)):
        (args.out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("coverage_days", "raw_missing_symbol_days", "candidate_exclusion_explained_symbol_days", "candidate_unresolved_symbol_days", "explained_by_symbol", "complete_candidate_coverage_days")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
