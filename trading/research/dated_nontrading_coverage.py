"""Dated provider suspension evidence for candidate coverage, never price bars.

Historical status downloads may explain an unheld symbol's absent quote in a
retrospective diagnostic. They do not price holdings, prove PIT eligibility or
establish that the status was available when a historical decision was made.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path

from .baostock_all_stock_probe import _documentation_pin, load_cached_day


SCHEMA = "dated_nontrading_candidate_coverage_v1"


def apply_status_event_eligibility(batch, event_evidence, *, held_symbols=(), pending_symbols=(), decision_as_of=None):
    """Explicit new-entry filter; never repairs a source/raw completeness flag.

    Existing holdings/pending inventory still require the Engine's own raw-price
    and accounting checks. Removing a signal candidate is not an inventory write,
    forced cash settlement, proof of missing prices, or evidence of eligibility
    for any remaining symbol. All other candidate fields/duplicates are retained.
    """
    from .dated_status_events import candidate_decision
    output = deepcopy(batch)
    if event_evidence is None:
        return output
    day = batch["day"]
    candidates = batch.get("candidates", [])
    if not isinstance(candidates, list) or any(not isinstance(row, dict) or "symbol" not in row for row in candidates):
        raise ValueError("Candidate eligibility requires candidate dictionaries with symbols")
    decisions = {row["symbol"]: candidate_decision(event_evidence, row["symbol"], day,
                                                  decision_as_of=decision_as_of) for row in candidates}
    excluded = {symbol for symbol, decision in decisions.items() if decision["status"] == "known_exclusion"}
    output["candidates"] = [row for row in output.get("candidates", []) if row["symbol"] not in excluded]
    required = set(held_symbols) | set(pending_symbols)
    output["dated_status_event_eligibility"] = {
        "schema": "dated_event_new_candidate_eligibility_v1", "day": day,
        "candidate_rows_before": len(candidates), "candidate_rows_after": len(output["candidates"]),
        "new_candidate_excluded_symbols": sorted(excluded),
        "new_candidate_excluded_rows": len(candidates) - len(output["candidates"]),
        "remaining_candidate_eligibility_unknown_symbols": sorted(set(decisions) - excluded),
        "excluded_candidate_decisions": {symbol: decisions[symbol] for symbol in sorted(excluded)},
        "held_or_pending_excluded_symbols_requiring_separate_accounting": sorted(required.intersection(excluded)),
        "status_before": batch.get("status"), "status_after": output.get("status"),
        "source_and_raw_completeness_changed": False, "raw_missing_prices_proven_or_repaired": False,
        "evidence_verification": deepcopy(event_evidence.verification),
        "held_pending_inventory_modified": False, "cash_settlement_created": False,
        "pit_verified": False, "candidate_panel_only": True, "execution_admitted": False}
    return output


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Status availability timestamps require an explicit timezone")
    return result


@dataclass(frozen=True)
class DatedStatusEvidence:
    day: str
    statuses: dict
    verification: dict
    published_available_at: str | None = None


def load_dated_status(folder, day, *, legacy_hash_pin=None, documentation_root=None):
    """Verify an existing exact-day source; no network or original-file edits.

    A legacy receipt without an acquisition hash requires the separately saved
    exact raw-byte pin. A hash made now is kept distinct from an acquisition pin.
    Provider documentation is independently rechecked, including legacy inputs.
    """
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("Status day must be canonical")
    folder = Path(folder).resolve()
    receipt, raw, verification = load_cached_day(
        folder, day, allow_legacy_existing_evidence=legacy_hash_pin is not None)
    if not verification["original_acquisition_hash_verified"]:
        pins = json.loads(Path(legacy_hash_pin).read_text(encoding="utf-8"))
        matches = [row for row in pins if row.get("day") == day]
        if len(matches) != 1:
            raise ValueError("Legacy status needs exactly one same-day saved raw hash pin")
        pin = matches[0]
        if (Path(pin.get("original_receipt", "")).resolve() != folder / "receipt.json"
                or Path(pin.get("raw_csv", "")).resolve() != Path(verification["raw_csv"])
                or pin.get("verified_current_raw_sha256") != verification["verified_current_raw_sha256"]
                or pin.get("raw_hash_origin") != "post_acquisition_pin_now_original_receipt_unmodified"
                or pin.get("original_acquisition_hash_verified") is not False
                or pin.get("reacquired") is not False or pin.get("execution_admitted") is not False):
            raise ValueError("Legacy status saved pin does not match the exact current source bytes")
        verification["legacy_saved_pin"] = str(Path(legacy_hash_pin).resolve())
    stored_docs = receipt.get("provider_documentation")
    if documentation_root is None:
        if not isinstance(stored_docs, dict) or receipt.get("provider_semantics_verified") is not True:
            raise ValueError("Status0 requires independently pinned provider field semantics")
        documentation_root = Path(stored_docs["local_document"]).parent
    docs = _documentation_pin(Path(documentation_root))
    if stored_docs is not None and (stored_docs.get("sha256") != docs["sha256"]
                                  or stored_docs.get("provider_semantics_verified") is not True):
        raise ValueError("Status receipt and pinned provider documentation disagree")
    available_at = receipt.get("public_available_at", receipt.get("available_at"))
    if (receipt.get("public_available_at") is not None and receipt.get("available_at") is not None
            and receipt["public_available_at"] != receipt["available_at"]):
        raise ValueError("Status receipt has conflicting explicit availability timestamps")
    if available_at is not None:
        _timestamp(available_at)
    verification.update(provider_documentation=docs,
                        retrieved_at=receipt["completed_at"],
                        public_available_at=available_at,
                        temporal_basis="retrospective_retrieval_not_point_in_time",
                        pit_verified=False, source_verified=False)
    statuses = {}
    for row in raw.to_dict("records"):
        code = row["code"]
        if len(code) == 9 and code[:3] in {"sh.", "sz."} and code[3:].isdigit():
            symbol = code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE")
            statuses[symbol] = row["tradeStatus"]
    return DatedStatusEvidence(day, statuses, verification, available_at)


def candidate_coverage(raw_coverage, evidence, *, held_symbols=(), pending_symbols=(), decision_as_of=None):
    """Explain only absent, unheld status0 rows; leave raw facts untouched.

    A complete candidate observation remains conditional on the consumer's
    separate raw-price check for all actual held/pending/candidate symbols.
    An explicitly late publication is never used. Unknown publication time is
    retained as unknown PIT, usable only for retrospective source diagnostics.
    """
    if not isinstance(evidence, DatedStatusEvidence) or raw_coverage["day"] != evidence.day:
        raise ValueError("Candidate coverage requires verified evidence for the exact raw day")
    day = evidence.day
    cutoff = _timestamp(decision_as_of or day + "T15:05:00+08:00")
    if cutoff.date().isoformat() != day:
        raise ValueError("Candidate coverage cutoff must be on the exact decision day")
    if evidence.published_available_at is not None and _timestamp(evidence.published_available_at) > cutoff:
        raise ValueError("Status publication is later than the declared decision cutoff")
    missing = list(raw_coverage["missing_symbols"])
    invalid = list(raw_coverage["invalid_symbols"])
    observed, expected = raw_coverage["observed_cohort_rows"], raw_coverage["expected_cohort_rows"]
    if len(set(missing)) != len(missing) or observed + len(missing) != expected:
        raise ValueError("Raw coverage counts or missing-symbol uniqueness are inconsistent")
    required = set(held_symbols) | set(pending_symbols)
    source_zero = [symbol for symbol in missing if evidence.statuses.get(symbol) == "0"]
    source_one = [symbol for symbol in missing if evidence.statuses.get(symbol) == "1"]
    unknown = [symbol for symbol in missing if symbol not in evidence.statuses]
    whole_day_missing = observed == 0
    explained = [] if whole_day_missing else [symbol for symbol in source_zero if symbol not in required]
    unresolved = [symbol for symbol in missing if symbol not in set(explained)]
    complete = observed > 0 and not unresolved and not invalid and not raw_coverage.get("st_unknown_symbols")
    return {"schema": SCHEMA, "day": day, "status": "complete" if complete else "incomplete",
            "raw_coverage": deepcopy(raw_coverage), "raw_coverage_status": raw_coverage["status"],
            "raw_missing_count": len(missing), "candidate_nontrading_observed_symbols": explained,
            "candidate_nontrading_observed_count": len(explained), "candidate_unresolved_missing_symbols": unresolved,
            "provider_status0_missing_symbols": source_zero, "provider_status1_missing_symbols": source_one,
            "absent_provider_row_unknown_symbols": unknown,
            "required_held_or_pending_missing_symbols": sorted(required.intersection(missing)),
            "whole_raw_day_absent": whole_day_missing, "status_evidence": deepcopy(evidence.verification),
            "temporal_basis": "retrospective_retrieval_not_point_in_time", "pit_verified": False,
            "raw_prices_required_for_actual_holdings_pending_and_candidates": True,
            "price_bars_created": False, "raw_coverage_modified": False, "eligibility_inferred_from_names": False,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False}
