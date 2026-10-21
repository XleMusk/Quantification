"""One public quote batch for the exact saved eight-type candidate union.

The saved publication owns every member.  This does not extend a registry,
rank candidates, normalize source volume, or produce account/order evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import requests

from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .exchange_calendar import DEFAULT_CALENDAR_PATH
from .index_observation import _aware, _collector_lock, trading_session
from .leader_cohort_observation import ENDPOINT, _member, _provider_symbol, parse_batch
from .leader_eight_type_paper_view import LOCAL, read_saved_plan


SCHEMA = "saved_leader_candidate_quote_observation_v1"
LATEST_SCHEMA = "saved_leader_candidate_quote_latest_v1"
SOURCE_SCOPE = "exact_saved_eight_type_candidate_union_not_registry_or_current_universe"
SUCCESS = "completed_saved_candidate_quote_observation"
FAILURE = "unavailable_single_attempt_stopped"
FLAGS = dict(research_only=True, candidate_panel_only=True, no_account_access=True,
    account_accessed=False, registry_mutated=False, close_observation_chain_mutated=False,
    engine_advanced=False, account_created=False, orders_generated=False, ledger_mutated=False,
    execution_admitted=False, selection_enabled=False, strategy_validated=False,
    current_signal_admitted=False, model_refit=False, scores_recomputed=False,
    is_BrokerQuote=False, all_A_breadth=False, used_in_historical_close_signals=False,
    identified_capital_flow=False, participant_intent_inferred=False)


def _now():
    return datetime.now(LOCAL)


def _unchanged(path, original):
    if Path(path).read_bytes() != original:
        raise ValueError("Original saved paper or pinned calendar changed during the observation")


def run(*, paper, calendar=DEFAULT_CALENDAR_PATH, data_root=DATA_ROOT, _clock=None, _get=None):
    clock, get = _clock or _now, _get or requests.get
    actual = _aware(clock())
    paper, calendar, data_root = Path(paper).resolve(), Path(calendar).resolve(), Path(data_root).resolve()
    calendar_bytes = calendar.read_bytes()
    calendar_payload = json.loads(calendar_bytes)
    # Reject before creating a lock, output directory, job or latest pointer.
    session = trading_session(calendar_payload, actual)
    paper_bytes = paper.read_bytes()
    paper_ref = dict(file=str(paper), sha256=sha256(paper_bytes).hexdigest(), byte_count=len(paper_bytes))
    view = read_saved_plan(paper, now=actual)
    _unchanged(paper, paper_bytes)
    published = _aware(datetime.fromisoformat(view["recorded_at"].replace("Z", "+00:00")))
    if (view["status"] != "saved_paper_plan" or view["execution_day_bound"] != session["session_day"]
            or view["source_close_day"] >= session["session_day"] or published >= actual):
        raise ValueError("Only the original actually published plan for this exact execution day may be observed")
    members_by_symbol = {}
    appearances = 0
    for case in view["rows"]:
        details = case.get("candidate_details")
        if not isinstance(details, list) or {item["symbol"] for item in details} != {
                item["symbol"] for item in case["candidates"]}:
            raise ValueError("Every original case must retain its exact saved candidate details")
        for candidate in details:
            symbol = candidate["symbol"]
            _provider_symbol(symbol)
            appearances += 1
            if symbol not in members_by_symbol:
                members_by_symbol[symbol] = dict(symbol=symbol, name=candidate.get("name") or symbol, labels=[])
    members = list(members_by_symbol.values())
    if not 1 <= len(members) <= 100:
        raise ValueError("The exact nonempty saved candidate union must fit one batch of at most 100")
    root = data_root / "research/leader_candidate_observation"
    with _collector_lock(root):
        started = _aware(clock())
        if trading_session(calendar_payload, started) != session:
            raise ValueError("Trading slot changed before the single attempt; no job started")
        _unchanged(paper, paper_bytes)
        _unchanged(calendar, calendar_bytes)
        out = root / (started.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8])
        out.mkdir(parents=True, exist_ok=False)
        receipt_path, raw_path = out / "receipt.json", out / "batch_000.raw"
        receipt = dict(schema=SCHEMA, status="started", source_scope=SOURCE_SCOPE,
            paper_ref=paper_ref, paper_published_at=view["recorded_at"], source_close_day=view["source_close_day"],
            execution_day_bound=view["execution_day_bound"],
            calendar_ref=dict(file=str(calendar), sha256=sha256(calendar_bytes).hexdigest(), byte_count=len(calendar_bytes)),
            **{key: value.isoformat() if isinstance(value, datetime) else value for key, value in session.items()},
            started_at=started.isoformat(), finished_at=None, response_received_at=None,
            pid=os.getpid(), parent_pid=os.getppid(), quote_request_count=0,
            target_symbols=[member["symbol"] for member in members], target_count=len(members),
            captured_scope_count=len(members), candidate_appearances=appearances,
            current_count=0, unknown_count=len(members), raw_file=None, raw_sha256=None,
            quotes=[_member(member, 0) for member in members],
            auto_retry=False, watch=False, redirect_followed=False,
            freshness_is_as_of_observation_only=True, consumer_must_recompute_source_age=True,
            name_reference_basis="original_saved_candidate_name_not_registry_membership",
            units=dict(prices="CNY", volume_and_amount="original_source_strings_not_normalized"), **FLAGS)
        write_json(receipt_path, receipt)
        try:
            before = _aware(clock())
            if trading_session(calendar_payload, before) != session:
                raise ValueError("Trading slot changed; the single attempt stops without retry")
            remaining = (session["stop_at"] - before).total_seconds()
            endpoint = ENDPOINT + ",".join(_provider_symbol(member["symbol"]) for member in members)
            receipt["quote_request_count"] = 1
            receipt["requested_at"] = before.isoformat()
            write_json(receipt_path, receipt)
            with get(endpoint, timeout=(min(5, remaining), min(10, remaining)),
                    allow_redirects=False, stream=True,
                    headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}) as response:
                receipt.update(http_status=response.status_code,
                    response_headers={key: response.headers.get(key) for key in
                        ("Date", "Content-Type", "Content-Length", "Location")})
                size = 0
                with raw_path.open("xb") as handle:
                    for piece in response.iter_content(16384):
                        handle.write(piece)
                        size += len(piece)
                        if size > 262144:
                            raise ValueError("Single candidate quote batch exceeds 256 KB; stopped without retry")
                received = _aware(clock())
                receipt["response_received_at"] = received.isoformat()
                if trading_session(calendar_payload, received) != session:
                    raise ValueError("Response crossed the current trading slot")
                if response.status_code != 200:
                    raise ValueError("Expected one nonredirect HTTP200 response; stopped without retry")
            _unchanged(paper, paper_bytes)
            _unchanged(calendar, calendar_bytes)
            quotes, counts = parse_batch(raw_path.read_bytes(), members, received, session=session,
                registered_at=published, batch_id=0)
            receipt.update(status=SUCCESS, quotes=quotes, **counts)
        except (Exception, KeyboardInterrupt) as error:
            receipt.update(status=FAILURE, error=f"{type(error).__name__}: {error}")
            for quote in receipt["quotes"]:
                quote.update(status="unknown_failed_batch", unknown_reason="single_transport_or_grammar_attempt_failed",
                    received_at=receipt["response_received_at"])
        if raw_path.is_file():
            raw = raw_path.read_bytes()
            receipt.update(raw_file=str(raw_path), raw_sha256=sha256(raw).hexdigest(), raw_byte_count=len(raw))
        receipt["finished_at"] = _aware(clock()).isoformat()
        receipt["current_count"] = sum(quote["current_quote"] for quote in receipt["quotes"])
        receipt["unknown_count"] = len(members) - receipt["current_count"]
        write_json(receipt_path, receipt)
        # A failed fresh attempt must remain visible; never point back to an
        # older successful quote snapshot.
        write_json(root / "latest.json", dict(schema=LATEST_SCHEMA, receipt_file=str(receipt_path),
            paper_ref=paper_ref, status=receipt["status"], updated_at=receipt["finished_at"],
            source_scope=SOURCE_SCOPE, **FLAGS))
        return out, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    args = parser.parse_args(argv)
    try:
        out, receipt = run(paper=args.paper, calendar=args.calendar)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({"folder": str(out), **receipt}, ensure_ascii=False), flush=True)
    return 0 if receipt["status"] == SUCCESS else 2


if __name__ == "__main__":
    raise SystemExit(main())
