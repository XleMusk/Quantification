from copy import deepcopy
from hashlib import sha256
import json

import pytest
import pandas as pd

from trading.research.dated_status_events import (
    StatusEvent, StatusEventEvidence, candidate_decision, diagnose,
    event_candidate_coverage, load_status_events,
)
from trading.research.dated_nontrading_coverage import apply_status_event_eligibility
from trading.research import leader_daily_replay as runner


A, B, OTHER = "300379.SZSE", "603056.SSE", "600001.SSE"


def events():
    return StatusEventEvidence((
        StatusEvent("period", A, "delisting_period", "2025-12-30", None,
                    "2025-12-22T23:59:59+08:00", "december",
                    {"scheduled_expected_last_trade_date": "2026-01-21"}),
        StatusEvent("delisted", A, "delisted", "2026-01-22", None,
                    "2026-01-22T23:59:59+08:00", "january",
                    {"actual_last_exchange_trade_date": "2026-01-21", "delist_date": "2026-01-22"}),
        StatusEvent("halt", B, "trading_halt", "2026-01-21", None,
                    "2026-01-21T23:59:59+08:00", "halt",
                    {"halt_end_date": None, "resume_date": None, "conditional_resume_if_shareholders_reject": "2026-01-30"}),
        StatusEvent("march", B, "delisted", "2026-03-31", None,
                    "2026-03-26T23:59:59+08:00", "march",
                    {"exchange_decision_date": "2026-03-24", "delist_date": "2026-03-31"}),
    ), {"pit_verified": False})


def coverage(day="2026-01-23", missing=None, observed=1):
    missing = [A, B, OTHER] if missing is None else missing
    return {"day": day, "status": "incomplete", "missing_symbols": missing,
            "observed_cohort_rows": observed, "expected_cohort_rows": observed + len(missing),
            "invalid_symbols": [], "st_unknown_symbols": []}


def test_unknown_publication_cannot_apply_same_close_and_missing_event_is_unknown():
    evidence = events()
    assert candidate_decision(evidence, B, "2026-01-21")["new_entry_eligible"] is None
    at_end = candidate_decision(evidence, B, "2026-01-21", decision_as_of="2026-01-21T23:59:59+08:00")
    assert at_end["primary_exclusion_reason"] == "halt"
    assert candidate_decision(evidence, B, "2026-01-22")["new_entry_eligible"] is False
    unknown = candidate_decision(evidence, OTHER, "2026-10-15")
    assert unknown["status"] == "unknown" and unknown["new_entry_eligible"] is None
    assert unknown["st_status_inferred"] is False and unknown["unknown_means_clear"] is False
    with pytest.raises(ValueError, match="explicit timezone"):
        candidate_decision(evidence, B, "2026-01-22", decision_as_of="2026-01-22T15:00:00")
    with pytest.raises(ValueError, match="exact queried day"):
        candidate_decision(evidence, B, "2026-01-22", decision_as_of="2026-01-21T23:59:59+08:00")


def test_scheduled_delisting_is_separate_from_later_confirmation():
    evidence = events()
    before_start = candidate_decision(evidence, A, "2025-12-29")
    assert before_start["status"] == "unknown"
    assert before_start["known_future_effective_events"][0]["event_id"] == "period"
    january5 = candidate_decision(evidence, A, "2026-01-05")
    assert january5["exclusion_reasons"] == ["delisting_period"]
    assert "actual_last_exchange_trade_date" not in json.dumps(january5)
    assert candidate_decision(evidence, A, "2026-01-22")["exclusion_reasons"] == ["delisting_period"]
    assert candidate_decision(evidence, A, "2026-01-23")["primary_exclusion_reason"] == "actual_delist"


def test_later_exact_delist_date_does_not_leak_to_decision_date_or_before_effective():
    evidence = events()
    for day in ("2026-03-24", "2026-03-25", "2026-03-26"):
        result = candidate_decision(evidence, B, day)
        assert "2026-03-31" not in json.dumps(result)
    known_schedule = candidate_decision(evidence, B, "2026-03-27")
    assert known_schedule["primary_exclusion_reason"] == "halt"
    assert known_schedule["known_future_effective_events"][0]["effective_from"] == "2026-03-31"
    assert candidate_decision(evidence, B, "2026-03-31")["primary_exclusion_reason"] == "actual_delist"
    assert candidate_decision(evidence, B, "2026-01-30")["halt_end_date"] is None
    assert candidate_decision(evidence, B, "2026-03-31")["halt_end_date"] is None


def test_candidate_explanation_retains_held_pending_unknown_invalid_and_original_pool():
    facts = coverage()
    before = deepcopy(facts)
    evidence = events()
    result = event_candidate_coverage(facts, evidence)
    assert result["candidate_exclusion_explained_symbols"] == [A, B]
    assert result["candidate_unresolved_missing_symbols"] == [OTHER]
    assert result["status"] == "incomplete" and facts == before
    assert result["frozen_cohort_removed_symbols"] == []
    for argument in ("held_symbols", "pending_symbols"):
        held = event_candidate_coverage(facts, evidence, **{argument: [A]})
        assert held["candidate_exclusion_explained_symbols"] == [B]
        assert held["required_held_or_pending_missing_symbols"] == [A]
    qualified = event_candidate_coverage(coverage(missing=[A, B]), evidence)
    assert qualified["candidate_eligibility_observation_status"] == "resolved_exclusions_only"
    assert qualified["status"] == "incomplete" and qualified["source_or_raw_completeness_promoted"] is False
    invalid = coverage(missing=[A, B])
    invalid["invalid_symbols"] = [OTHER]
    assert event_candidate_coverage(invalid, evidence)["status"] == "incomplete"
    assert event_candidate_coverage(coverage(missing=[A, B], observed=0), evidence)["candidate_exclusion_explained_symbols"] == []
    assert event_candidate_coverage(coverage(day="2026-01-05", missing=[A]), evidence)["absence_of_exchange_quotes_supported_symbols"] == []


def test_explicit_eligibility_filter_preserves_source_fields_status_unknown_duplicates_and_inventory():
    batch = {"day": "2026-01-23", "status": "incomplete", "candidates": [
        {"symbol": A, "arbitrary_source_field": 9}, {"symbol": OTHER, "score": .5, "rank": 2},
        {"symbol": OTHER, "score": .3, "rank": 3}]}
    original = deepcopy(batch)
    assert apply_status_event_eligibility(batch, None) == original
    output = apply_status_event_eligibility(batch, events(), held_symbols=[A], pending_symbols=[A])
    assert output["status"] == "incomplete" and output["candidates"] == original["candidates"][1:]
    gate = output["dated_status_event_eligibility"]
    assert gate["new_candidate_excluded_symbols"] == [A]
    assert gate["held_or_pending_excluded_symbols_requiring_separate_accounting"] == [A]
    assert gate["held_pending_inventory_modified"] is False
    assert gate["raw_missing_prices_proven_or_repaired"] is False and batch == original


def test_scope_batches_optional_event_gate_executes_without_completing_missing_quotes():
    day = "2026-01-23"
    raw = pd.DataFrame([{"date": day, "symbol": symbol, "open": 10., "high": 10.1, "low": 9.9,
                         "close": 10., "volume": 5_000_000., "amount": 50_000_000., "pct_chg": 0.,
                         "preclose_for_limits": 10., "is_st": False} for symbol in [A, B, OTHER]])
    batch = {"day": day, "status": "complete", "candidates": [{"symbol": A}, {"symbol": B}, {"symbol": OTHER, "score": 1.}]}
    source = {day: batch}
    facts = runner.dated_coverage(raw, [day], [A, B, OTHER])
    default, _ = runner.scope_batches(source, facts, [A, B, OTHER], raw)
    explicit, observations = runner.scope_batches(source, facts, [A, B, OTHER], raw, status_event_evidence=events())
    assert default[day]["candidates"] == batch["candidates"]
    assert explicit[day]["status"] == "complete" and explicit[day]["candidates"] == [{"symbol": OTHER, "score": 1.}]
    assert observations[0]["new_candidate_status_event_excluded_rows"] == 2
    missing_raw = raw.loc[raw.symbol != A]
    missing_facts = runner.dated_coverage(missing_raw, [day], [A, B, OTHER])
    incomplete, _ = runner.scope_batches(source, missing_facts, [A, B, OTHER], missing_raw, status_event_evidence=events())
    assert incomplete[day]["status"] == "incomplete"
    assert incomplete[day]["cohort_coverage"]["missing_symbols"] == [A]
    assert "dated_status_event_eligibility" not in batch


def source(tmp_path):
    raw = tmp_path / "source.pdf"
    raw.write_bytes(b"saved official source bytes")
    text = tmp_path / "source.txt"
    text.write_text("300379 北京东方通科技股份有限公司 退市整理期", encoding="utf-8")
    source = {"source_id": "one", "source_role": "issuer_exchange_hosted", "publisher": "北京东方通科技股份有限公司董事会",
              "document_title": "关于公司股票进入退市整理期交易的公告", "publication_date": "2025-12-22",
              "available_at": "2025-12-22T23:59:59+08:00", "publication_time_known": False,
              "url": "https://disc.static.szse.cn/source.pdf", "final_url": "https://disc.static.szse.cn/source.pdf",
              "raw_path": str(raw), "sha256": sha256(raw.read_bytes()).hexdigest(), "byte_count": raw.stat().st_size,
              "status": "downloaded_pdf", "fulltext_path": str(text), "full_usable_text_read": True}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"schema": "primary_source_download_receipt_v1", "execution_admitted": False,
                                  "canonical_sources_modified": False, "sources": [source]}), encoding="utf-8")
    event = {"event_id": "one_event", "symbol": A, "event_type": "delisting_period", "effective_from": "2025-12-30", "effective_until": None,
             "publication_date": "2025-12-22", "available_at": "2025-12-22T23:59:59+08:00", "intraday_publication_time_known": False,
             "source_id": "one", "source_role": source["source_role"], "source_url": source["url"],
             "raw_source_path": str(raw), "fulltext_path": str(text), "publisher": source["publisher"],
             "document_title": source["document_title"], "facts": {"scheduled_expected_last_trade_date": "2026-01-21"}}
    document = {"schema": "instrument_status_event_evidence_v1", "scope_symbols": [A, B],
                "source_count": 1, "usable_source_count": 1, "source_receipt_path": str(receipt),
                "sources": [source], "events": [event], "safety": {"candidate_panel_only": True,
                "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
                "stockdb_fields_modified": False, "instrument_master_modified": False, "persistent_ledger_modified": False}}
    path = tmp_path / "event_evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document, raw


def test_loader_executes_source_bytes_and_dated_decision(tmp_path):
    path, _, _ = source(tmp_path)
    evidence = load_status_events(path)
    assert evidence.verification["verified_source_count"] == 1
    assert evidence.verification["historical_time_archive_verified"] is False
    assert candidate_decision(evidence, A, "2026-01-05")["new_entry_eligible"] is False
    result = diagnose(evidence, [coverage("2026-01-05", missing=[A, OTHER])])
    assert result["candidate_exclusion_explained_symbol_days"] == 1
    assert result["candidate_unresolved_symbol_days"] == 1
    assert result["old_replays_modified"] is False


@pytest.mark.parametrize("mutation", ["schema", "duplicate_event", "duplicate_source", "hash", "role", "publisher", "official_url", "earlier_available", "unsafe"])
def test_bad_provenance_and_early_availability_are_rejected(tmp_path, mutation):
    path, data, raw = source(tmp_path)
    if mutation == "schema":
        data["schema"] = "anything"
    elif mutation == "duplicate_event":
        data["events"].append(deepcopy(data["events"][0]))
    elif mutation == "duplicate_source":
        data["sources"].append(deepcopy(data["sources"][0]))
    elif mutation == "hash":
        raw.write_bytes(raw.read_bytes() + b" changed")
    elif mutation == "role":
        data["events"][0]["source_role"] = "exchange"
    elif mutation == "publisher":
        data["events"][0]["publisher"] = "another issuer"
    elif mutation == "official_url":
        data["events"][0]["source_url"] = "https://szse.cn.example.com/source.pdf"
    elif mutation == "earlier_available":
        data["events"][0]["available_at"] = "2025-12-22T09:00:00+08:00"
    else:
        data["safety"]["execution_admitted"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_status_events(path)
