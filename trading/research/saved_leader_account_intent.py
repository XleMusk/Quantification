"""Read the original 32 saved leader books as separate research instructions.

No candidate selection, risk reevaluation, Engine advancement or broker I/O is
performed.  Every export remains bound to the exact saved head and case journal.
"""
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from .leader_daily_signals import _calendar_view
from .leader_eight_type_position_view import CASE_IDS, read_saved_positions, saved_method
from .saved_account_intent import FLAGS


SCHEMA = "saved_leader_eight_type_account_intent_v1"
NOT_READY_SCHEMA = "saved_leader_eight_type_account_intent_not_ready_v1"
DATED_SCHEMA = "saved_leader_dated_catchup_account_intent_v1"
DATED_NOT_READY_SCHEMA = "saved_leader_dated_catchup_account_intent_not_ready_v1"
RESIDUAL_SCHEMA = "saved_leader_eight_type_residual_account_intent_v1"
RESIDUAL_NOT_READY_SCHEMA = "saved_leader_eight_type_residual_account_intent_not_ready_v1"
KNOWN_SCHEMA = "saved_leader_known_identity_account_intent_v1"
KNOWN_NOT_READY_SCHEMA = "saved_leader_known_identity_account_intent_not_ready_v1"
SCHEMAS = {"eight_type": SCHEMA, "dated_catchup": DATED_SCHEMA, "eight_type_residual": RESIDUAL_SCHEMA,
    "known_identity": KNOWN_SCHEMA}
NOT_READY_SCHEMAS = {"eight_type": NOT_READY_SCHEMA, "dated_catchup": DATED_NOT_READY_SCHEMA,
    "eight_type_residual": RESIDUAL_NOT_READY_SCHEMA, "known_identity": KNOWN_NOT_READY_SCHEMA}
SOURCE_BASIS = "saved_research_account_not_a_current_broker_signal"
TARGET_VERSION_BASIS = "processed_session_count_not_fixed_strategy_lifecycle_version"


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Saved leader clocks require explicit timezones")
    return stamp.astimezone(MARKET_TZ)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("An exact leader execution session is required")
    return value


def _file_ref(value):
    return {"path": str(Path(value["file"]).resolve()), "sha256": value["sha256"]}


def _load(run_dir, current, *, source_family="eight_type"):
    directory = Path(run_dir).resolve()
    view = (read_saved_positions(directory, now=current) if source_family == "eight_type" else
            read_saved_positions(directory, now=current, source_family=source_family))
    if view["status"] == "unavailable":
        return directory, view, None
    if view["status"] != "saved_research_positions":
        raise ValueError("Saved leader books could not be verified")
    # The position reader validates all original publications, case contexts
    # and latest journal seals once. Read its exact head, not a newer cursor.
    data = (directory / "head.json").read_bytes()
    if sha256(data).hexdigest() != view["head_ref"]["sha256"]:
        raise ValueError("Saved leader head changed during export")
    head = json.loads(data.decode("utf-8-sig"))
    return directory, view, head


def _weights(value, config):
    if value is None:
        return None
    if (not isinstance(value, dict) or any(
            not isinstance(symbol, str) or type(weight) not in (int, float)
            or not math.isfinite(weight) or not 0 <= weight <= config.single_limit
            for symbol, weight in value.items())
            or sum(value.values()) > config.gross_limit + 1e-12):
        raise ValueError("Original saved leader target weights are invalid")
    return deepcopy(value)


def _case_facts(directory, view, head, row, execution_day, current, *, source_family="eight_type"):
    if row.get("error") or row.get("blocking_evidence"):
        raise ValueError("Saved leader case has an unresolved error or blocking evidence")
    day = row["processed_day"]
    if day is None or row["latest_journal"] is None or row["last_commit_at"] is None:
        raise ValueError("Saved leader case has no committed processed session")
    calendar = head["binding"]["calendar"]
    year = int(calendar["year"])
    sessions, _, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
    if (status != "pinned_static_exchange_calendar" or day not in sessions
            or next((d for d in sessions if d > day), None) != execution_day):
        raise ValueError("Leader intent execution must be the exact next saved-calendar session")
    known = _stamp(row["last_commit_at"])
    if (known < _stamp(day + "T15:00:00+08:00")
            or known >= _stamp(execution_day + "T09:30:00+08:00") or known > current):
        raise ValueError("Saved leader commit was not known before the intended opening")
    case_id = row["case_id"]
    case = head["cases"][case_id]
    config = StrategyConfig(**head["binding"]["config"]).validate()
    original = _weights(case["state"].get("pending_targets"), config)
    kind = "NO_CHANGE" if original is None else "FULL_TARGET"
    pending_decision = row["pending_decision"]
    decision = row["last_published_decision"] if original is None else pending_decision
    if (decision is None or decision["kind"] != kind
            or decision["decision_day"] != day or decision["execution_day"] != execution_day):
        raise ValueError("Saved leader target queue does not belong to this exact execution day")
    if original is None:
        # Engine NO_CHANGE keeps no pending target receipt. Its real latest
        # sealed publication owns D/E; an independently queued exit can remain.
        if decision["weights"] != {} or (pending_decision is not None and pending_decision != decision):
            raise ValueError("Saved NO_CHANGE publication carries targets or a different queue")
    elif pending_decision["weights"] != original:
        raise ValueError("Saved pending FULL_TARGET weights differ from the original target queue")
    pending_risk = case["state"].get("pending_risk_exits")
    queued = None
    symbols = []
    if pending_risk is not None:
        if (not isinstance(pending_risk, dict) or pending_risk["execution_on"] != execution_day
                or pending_risk["context"] != row["context"]
                or pending_risk["decided_on"] != day
                or not isinstance(pending_risk["symbols"], list)
                or len(set(pending_risk["symbols"])) != len(pending_risk["symbols"])):
            raise ValueError("Saved leader risk exit queue is not for this exact execution day")
        symbols = pending_risk["symbols"]
        queued = deepcopy(pending_risk)
    held = {position["symbol"] for position in row["positions"] if position["quantity_shares"] > 0}
    effective = deepcopy(original)
    if effective is not None:
        for symbol in symbols:
            effective[symbol] = 0
    publication = next((p for p in view["saved_publications"] if p["day"] == day), None)
    if publication is None:
        raise ValueError("Saved leader source publication is missing")
    if known < _stamp(publication["recorded_at"]):
        raise ValueError("Saved leader commit predates its actual source publication")
    return {"schema": SCHEMAS[source_family],
        **({"source_family": source_family} if source_family != "eight_type" else {}),
        "strategy_id": case_id, "case_id": case_id,
        "run_directory": str(directory),
        "source_day": day, "execution_day": execution_day, "kind": kind, "weights": effective,
        "original_weights": original, "queued_exit": queued,
        "remaining_exit_symbols": sorted(held.intersection(symbols)),
        "target_version": len(case["daily"]), "target_version_basis": TARGET_VERSION_BASIS,
        "source_known_at": known.isoformat(), "account_scope": "whole_observed_account",
        "execution_config": config.to_dict(), "context": deepcopy(row["context"]),
        "refs": {"run_directory": str(directory), "head": _file_ref(view["head_ref"]),
                 "latest_journal": _file_ref(row["latest_journal"]),
                 "publication": {**_file_ref(publication), "day": publication["day"],
                     "recorded_at": publication["recorded_at"],
                     "execution_day": publication["execution_day"]}},
        "signal_publication_verified": False, "source_basis": SOURCE_BASIS, **FLAGS}


def export_requests(run_dir, execution_day, *, current=None, source_family="eight_type", holding_reviews=None):
    """Export all 32 separate books in original order; unavailable is explicit."""
    current = _stamp(current or datetime.now(MARKET_TZ))
    execution_day = _day(execution_day)
    if holding_reviews is not None and (not isinstance(holding_reviews, dict) or holding_reviews):
        raise ValueError("Holding review output requires a new empty mapping")
    method = saved_method(source_family)
    directory, view, head = _load(run_dir, current, source_family=source_family)
    rows = {row["case_id"]: row for row in view["rows"]}
    result = {}
    for case_id in method["case_ids"]:
        try:
            if head is None:
                raise ValueError(view.get("reason") or "No saved leader head")
            facts = _case_facts(directory, view, head, rows[case_id], execution_day, current,
                source_family=source_family)
            result[case_id] = {**facts, "created_at": current.isoformat()}
            if holding_reviews is not None:
                # Keep the intent contract byte-for-byte equivalent. This
                # optional export uses the position review verified by this
                # same _load, never a second or later account cursor.
                holding_reviews[case_id] = {"schema": "saved_leader_holding_review_v1",
                    "strategy_id": facts["strategy_id"], "source_family": source_family,
                    "source_day": facts["source_day"], "execution_day": execution_day,
                    "source_known_at": facts["source_known_at"], "exported_at": current.isoformat(),
                    "refs": deepcopy(facts["refs"]), "source_basis": SOURCE_BASIS,
                    "saved_position_review": deepcopy(rows[case_id]), **FLAGS}
        except ValueError as error:
            result[case_id] = {"schema": NOT_READY_SCHEMAS[source_family],
                **({"source_family": source_family} if source_family != "eight_type" else {}), "status": "not_ready",
                "case_id": case_id, "run_directory": str(directory),
                "execution_day": execution_day, "reason": str(error),
                "refs": {"run_directory": str(directory),
                    "head": _file_ref(view["head_ref"]) if view["head_ref"] else None},
                "created_at": current.isoformat(), **FLAGS}
    if head is not None and sha256((directory / "head.json").read_bytes()).hexdigest() != view["head_ref"]["sha256"]:
        raise ValueError("Saved leader head changed during export")
    return result


def export_request(run_dir, case_id, execution_day, *, current=None, source_family="eight_type"):
    if case_id not in saved_method(source_family)["case_ids"]:
        raise ValueError("Unknown original eight-type research case")
    result = export_requests(run_dir, execution_day, current=current, source_family=source_family)[case_id]
    if result.get("status") == "not_ready":
        raise ValueError(result["reason"])
    return result


def read_request(payload, *, current=None):
    """Verify the original saved source again, never adopt a replacement head."""
    current = _stamp(current or datetime.now(MARKET_TZ))
    family = next((key for key, schema in SCHEMAS.items()
        if isinstance(payload, dict) and payload.get("schema") == schema), "eight_type")
    if (not isinstance(payload, dict) or payload.get("schema") not in set(SCHEMAS.values())
            or (family != "eight_type" and payload.get("source_family") != family)
            or any(payload.get(key) is not value for key, value in FLAGS.items())
            or payload.get("signal_publication_verified") is not False
            or payload.get("source_basis") != SOURCE_BASIS):
        raise ValueError("Saved leader research-only boundary changed")
    created = _stamp(payload["created_at"])
    if not _stamp(payload["source_known_at"]) <= created <= current:
        raise ValueError("Saved leader creation/knowledge clock changed")
    expected = export_request(payload["refs"]["run_directory"], payload["case_id"],
        payload["execution_day"], current=current, source_family=family)
    expected["created_at"] = payload["created_at"]
    if payload != expected:
        raise ValueError("Saved leader intent no longer equals its exact head, case or source")
    return expected
