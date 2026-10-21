"""Synthetic saved-account review tests; no producer, Engine or live source runs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from trading import engine
from trading.ledger import Ledger
from trading.research import leader_eight_type_position_view as view
from trading.research.leader_type_risk_exit import LeaderTypeRiskExitPolicy


TYPES = (
    "space_height_leader", "relay_continuation_leader", "trend_leader", "liquidity_capacity_proxy",
    "cross_regime_candidate", "rebound_repair_candidate", "sector_catchup_leader", "turnover_leader",
)
CASE_IDS = tuple("__".join((identity, guard, capacity)) for identity in TYPES
    for guard in ("unguarded", "two_close_envguard")
    for capacity in ("causal_first_seen_cap20", "uncapped"))
ACTIVE = "trend_leader__unguarded__uncapped"
A, B = "000001.SZSE", "000002.SZSE"
DAYS = ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
NEXT = "2026-10-19"
NOW = "2026-10-16T23:00:00+08:00"
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
BOUNDARY = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "current_signal_admitted": False}
POLICY = "leader_eight_type_published_close_structure_and_expiry_queue_v1:trend_leader"


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return {"file": str(path.resolve()), "sha256": sha256(raw).hexdigest()}


def _snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _intent(context, day):
    execution = NEXT if day == DAYS[-1] else DAYS[DAYS.index(day) + 1]
    return {"schema_version": 1, "contract_type": "PortfolioIntent", "kind": "NO_CHANGE",
        "weights": {}, "unit": "equity_fraction", "reason": "type_observation_unknown",
        "context": deepcopy(context), "decided_at": day + "T18:45:00+08:00",
        "execution_not_before": execution, "source": "saved:synthetic_publication"}


def _entry(fill_id="buy-first", *, remaining=300, fill_day=DAYS[1], available=DAYS[2], decision=DAYS[0]):
    return {"fill_id": fill_id, "fill_day": fill_day, "available_day": available,
        "surviving_qty": remaining, "decision_day": decision, "candidate_source_day": decision,
        "candidate_available_at": decision + "T18:45:00+08:00", "setup_low": 9., "setup_high": 12.,
        "setup_price_source": "exact_entry_decision_pinned_raw",
        "evidence": {"structure_close": 9., "source_basis": "synthetic_only"}}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A saved position review cannot advance, evaluate, open a Ledger or connect")

    monkeypatch.setattr(engine, "advance", forbidden)
    monkeypatch.setattr(Ledger, "__init__", forbidden)
    monkeypatch.setattr(LeaderTypeRiskExitPolicy, "evaluate", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    directory = (tmp_path / "research/leader_eight_type_forward/20261015_v1").resolve()
    package = sha256(json.dumps(str(directory), sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    publications, published_records = [], {}
    for day in DAYS:
        execution = NEXT if day == DAYS[-1] else DAYS[DAYS.index(day) + 1]
        original_cases = [{"case_id": key, "cap_candidates": [], "candidate_reference_and_setup": []}
                          for key in CASE_IDS]
        if day in (DAYS[0], DAYS[2]):
            own = next(row for row in original_cases if row["case_id"] == ACTIVE)
            own["cap_candidates"] = [{"symbol": A, "source_day": day, "evidence": deepcopy(_entry()["evidence"])}]
            own["candidate_reference_and_setup"] = [{"symbol": A, "setup_low_cny": 9.}]
        record = {"schema": "leader_eight_type_unbound_close_paper_plan_v1", **BOUNDARY,
            "source_close_day": day, "recorded_at": day + "T18:45:00+08:00",
            "proposed_execution_day": execution, "cases": original_cases}
        published_records[day] = record
        reference = _write(tmp_path / "publications" / (day + ".json"), record)
        publications.append({"day": day, **reference, "recorded_at": record["recorded_at"],
            "execution_day": execution})
    cases = {}
    for case_id in CASE_IDS:
        context = {"schema_version": 1, "contract_type": "RunContext", "mode": "historical_replay",
            "account_id": "leader-eight-type-forward:" + package + ":" + case_id}
        pending = {"schema": "replay_published_close_reduced_portfolio_intent_v1",
            "decision_day": DAYS[-1], "execution_not_before": NEXT,
            "reduced_intent": _intent(context, DAYS[-1])}
        state = {"cash": 10_000_000, "positions": {}, "orders": {}, "fills": {}, "events": [],
            "prices": {}, "day": DAYS[-1], "pending_targets": None, "pending_risk_exits": None,
            "pending_published_close_intent": pending}
        if case_id == ACTIVE:
            state.update(cash=9_400_000, positions={A: [{"qty": 300, "available": DAYS[2]},
                {"qty": 300, "available": NEXT}]}, prices={A: 1100}, fills={
                "buy-first": {"symbol": A, "side": "buy", "qty": 500, "price": 1000,
                    "day": DAYS[1], "available_day": DAYS[2]},
                "sell-part": {"symbol": A, "side": "sell", "qty": 200, "price": 1050,
                    "day": DAYS[2], "available_day": DAYS[3]},
                "buy-later": {"symbol": A, "side": "buy", "qty": 300, "price": 1080,
                    "day": DAYS[3], "available_day": NEXT}})
            state["events"] = [{"kind": "leader_type_risk_assessed", "day": DAYS[2], "symbol": A,
                    "type_id": "trend_leader", "policy_id": POLICY, "entry": _entry(),
                    "status": "not_invalid", "reasons": [], "facts": {"close": 11.},
                    "execution_admitted": False},
                {"kind": "leader_type_risk_assessed", "day": DAYS[-1], "symbol": A,
                    "type_id": "trend_leader", "policy_id": POLICY, "entry": _entry(),
                    "status": "invalid", "reasons": ["saved_structure_break"],
                    "facts": {"close": 11., "structure_price": 12.}, "execution_admitted": False}]
            state["pending_risk_exits"] = {"context": deepcopy(context), "decided_on": DAYS[-1],
                "execution_on": NEXT, "symbols": [A], "reason": "saved_structure_break",
                "source": "research:synthetic_saved_risk"}
        daily, journals, refs = [], [], []
        for index, day in enumerate(DAYS):
            latest = day == DAYS[-1]
            daily.append({"date": day, "cash": state["cash"] / 100 if latest else 100_000.,
                "equity": 100_600. if latest and case_id == ACTIVE else 100_000., "stale_symbols": []})
            journal = {"decision_day": day, "execution_day": publications[index]["execution_day"],
                "publication": deepcopy(publications[index]), "committed_at": day + "T19:00:00+08:00",
                "bound_original_intent": _intent(context, day),
                "post_session_pending_published_close_intent": deepcopy(pending) if latest else None,
                "post_session_pending_targets": None,
                "post_session_pending_risk_exits": deepcopy(state["pending_risk_exits"]) if latest else None}
            journals.append(journal)
            refs.append(_write(directory / "journal" / case_id / (day + ".json"), journal))
        cases[case_id] = {"context": context, "state": state, "daily": daily, "journal": journals,
            "journal_files": refs, "error": None, "blocking_evidence": None}
    head = {"schema": "leader_eight_type_published_close_shared_ledger_checkpoint_v1", **BOUNDARY,
        "registered_at": DAYS[0] + "T18:00:00+08:00", "first_decision_day": DAYS[0],
        "binding": {"calendar": CALENDAR, "run_directory": str(directory)},
        "publications": publications, "cases": cases}

    def save(*, reseal=False, republish=False):
        if republish:
            for index, reference in enumerate(publications):
                updated = _write(Path(reference["file"]), published_records[reference["day"]])
                reference.update(updated)
                for case in head["cases"].values():
                    if case["journal"]:
                        case["journal"][index]["publication"] = deepcopy(reference)
            reseal = True
        if reseal:
            for case_id, case in head["cases"].items():
                for index, journal in enumerate(case["journal"]):
                    path = directory / "journal" / case_id / (journal["decision_day"] + ".json")
                    case["journal_files"][index] = _write(path, journal)
        _write(directory / "head.json", head)

    save()
    return SimpleNamespace(directory=directory, data_root=tmp_path, head=head,
        case=head["cases"][ACTIVE], state=head["cases"][ACTIVE]["state"], save=save,
        published_records=published_records)


def _active(result):
    return next(row for row in result["rows"] if row["case_id"] == ACTIVE)


def test_saved_32_case_view_is_read_only_and_keeps_partial_fifo_and_latest_saved_risk(saved):
    before = _snapshot(saved.data_root)
    result = view.leader_eight_type_position_view(saved.data_root, now=NOW)
    assert result["schema"] == "leader_eight_type_position_review_v1"
    assert result["status"] == "saved_research_positions"
    assert result["case_count"] == 32 and [row["case_id"] for row in result["rows"]] == list(CASE_IDS)
    assert result["latest_processed_day"] == DAYS[-1] and result["all_cases_same_day"] is True
    assert len({row["context"]["account_id"] for row in result["rows"]}) == 32
    row = _active(result)
    position, = row["positions"]
    assert row["cash_cny"] == 94_000 and row["equity_cny"] == 100_600
    assert position["quantity_shares"] == 600
    assert position["anchor"]["remaining_earliest_fill_shares"] == 300
    assert position["anchor"]["fill_id"] == "buy-first"
    assert position["anchor"]["fill_price_cny"] == 10.
    assert position["anchor"]["decision_day"] == DAYS[0]
    assert position["anchor"]["setup_low_cny"] == 9. and position["anchor"]["setup_high_cny"] == 12.
    assert position["mark_price_cny"] == 11. and position["marked_value_cny"] == 6600.
    assert position["assessment"] == {"day": DAYS[-1], "status": "invalid",
        "reasons": ["saved_structure_break"], "facts": {"close": 11., "structure_price": 12.},
        "policy_id": POLICY}
    assert position["queued_risk_exit"] == {"execution_on": NEXT, "reason": "saved_structure_break",
        "source": "research:synthetic_saved_risk", "decided_on": DAYS[-1], "execution_day_elapsed": False}
    assert row["last_published_decision"]["kind"] == "NO_CHANGE"
    assert row["pending_decision"]["kind"] == "NO_CHANGE"
    assert row["pending_decision"]["weights"] == {} and row["position_count"] == 1
    assert all(result[key] is False for key in ("engine_advanced", "ledger_mutated", "orders_generated",
        "account_created", "broker_interacted", "provider_contacted", "risk_recomputed", "execution_admitted"))
    assert _snapshot(saved.data_root) == before


def test_t1_uses_saved_book_day_even_when_read_after_future_settlement(saved):
    position, = _active(view.read_saved_positions(saved.directory, now="2026-10-20T20:00:00+08:00"))["positions"]
    assert position["sellable_on_book_day_shares"] == 300 and position["t1_locked_shares"] == 300
    assert position["anchor"]["available_day"] == DAYS[2]


def test_blocked_old_risk_exit_queue_stays_visible_after_its_execution_day(saved):
    saved.state["pending_risk_exits"].update(decided_on=DAYS[1], execution_on=DAYS[2])
    saved.case["journal"][-1]["post_session_pending_risk_exits"] = deepcopy(
        saved.state["pending_risk_exits"])
    saved.save(reseal=True)
    position, = _active(view.read_saved_positions(saved.directory, now=NOW))["positions"]
    assert position["quantity_shares"] == 600
    assert position["queued_risk_exit"]["decided_on"] == DAYS[1]
    assert position["queued_risk_exit"]["execution_on"] == DAYS[2]
    assert position["queued_risk_exit"]["execution_day_elapsed"] is True


def test_fifo_anchor_rotates_only_after_earliest_fill_is_fully_sold(saved):
    saved.state["fills"]["sell-rest-first"] = {"symbol": A, "side": "sell", "qty": 300,
        "price": 1100, "day": DAYS[-1], "available_day": NEXT}
    saved.state["positions"][A] = [{"qty": 300, "available": NEXT}]
    saved.state["events"][-1]["entry"] = _entry("buy-later", remaining=300,
        fill_day=DAYS[-1], available=NEXT, decision=DAYS[2])
    saved.save()
    position, = _active(view.read_saved_positions(saved.directory, now=NOW))["positions"]
    assert position["quantity_shares"] == position["anchor"]["remaining_earliest_fill_shares"] == 300
    assert position["anchor"]["fill_id"] == "buy-later" and position["anchor"]["fill_price_cny"] == 10.8
    assert position["anchor"]["decision_day"] == DAYS[2]
    assert position["sellable_on_book_day_shares"] == 0 and position["t1_locked_shares"] == 300


def test_missing_current_assessment_and_unknown_mark_stay_unknown_without_using_old_event(saved):
    saved.state["events"].pop()
    saved.state["prices"].pop(A)
    saved.save()
    position, = _active(view.read_saved_positions(saved.directory, now=NOW))["positions"]
    assert position["assessment"] is None
    assert position["mark_price_cny"] is None and position["marked_value_cny"] is None
    assert position["anchor"]["fill_id"] == "buy-first"
    assert position["anchor"]["setup_low_cny"] is None and position["anchor"]["evidence"] is None


def test_saved_unknown_risk_facts_are_preserved_without_re_evaluation(saved):
    event = saved.state["events"][-1]
    event.update(status="unknown", reasons=["required_fixed_entry_structure_unavailable"],
        facts={"close": None, "structure_price": None})
    event["entry"].update(setup_low=None, setup_high=None)
    original_case = next(row for row in saved.published_records[DAYS[0]]["cases"] if row["case_id"] == ACTIVE)
    original_case["candidate_reference_and_setup"][0]["setup_low_cny"] = None
    saved.save(republish=True)
    position, = _active(view.read_saved_positions(saved.directory, now=NOW))["positions"]
    assert position["assessment"]["status"] == "unknown"
    assert position["assessment"]["facts"] == {"close": None, "structure_price": None}
    assert position["anchor"]["setup_low_cny"] is None and position["anchor"]["setup_high_cny"] is None


def test_saved_held_setup_low_cannot_refresh_from_later_candidate(saved):
    saved.state["events"][-1]["entry"]["setup_low"] = 10.5
    saved.save()
    with pytest.raises(ValueError, match="different publication low"):
        view.read_saved_positions(saved.directory, now=NOW)


@pytest.mark.parametrize("contradiction", ["invalid_buy_quantity", "sell_exceeds_settled_history",
    "invalid_buy_settlement", "saved_entry_symbol_crossed"])
def test_contradictory_actual_fifo_history_is_rejected_instead_of_unknown(saved, contradiction):
    if contradiction == "invalid_buy_quantity":
        saved.state["fills"]["buy-first"]["qty"] = 0
    elif contradiction == "sell_exceeds_settled_history":
        saved.state["fills"]["sell-part"]["qty"] = 600
    elif contradiction == "invalid_buy_settlement":
        saved.state["fills"]["buy-first"]["available_day"] = DAYS[1]
    else:
        # With no A sale, the old helper merely reports reconciliation unknown;
        # the saved explicit entry must still reject a different symbol's fill.
        saved.state["fills"].pop("sell-part")
        saved.state["fills"]["buy-first"]["symbol"] = B
    saved.save()
    before = _snapshot(saved.data_root)
    with pytest.raises(ValueError):
        view.read_saved_positions(saved.directory, now=NOW)
    assert _snapshot(saved.data_root) == before


def test_missing_actual_fill_history_without_saved_entry_remains_explicitly_unknown(saved):
    saved.state["fills"] = {}
    saved.state["events"][-1]["entry"] = None
    saved.save()
    position, = _active(view.read_saved_positions(saved.directory, now=NOW))["positions"]
    assert position["anchor"]["status"] == "unknown"
    assert position["anchor"]["reason"] == "actual_fill_position_reconciliation_unknown"
    assert position["anchor"]["fill_price_cny"] is None
    assert position["anchor"]["remaining_earliest_fill_shares"] is None


@pytest.mark.parametrize("field,value", [("fill_id", "buy-another-symbol"),
    ("decision_day", DAYS[2]), ("candidate_source_day", DAYS[2]), ("surviving_qty", 600)])
def test_saved_entry_cannot_cross_fill_symbol_decision_day_or_total_position(saved, field, value):
    saved.state["fills"]["buy-another-symbol"] = {"symbol": B, "side": "buy", "qty": 100,
        "price": 1000, "day": DAYS[1], "available_day": DAYS[2]}
    saved.state["events"][-1]["entry"][field] = value
    saved.save()
    with pytest.raises(ValueError):
        view.read_saved_positions(saved.directory, now=NOW)


@pytest.mark.parametrize("part", ["journal_bytes", "journal_copy", "pending_targets", "journal_path"])
def test_corrupt_or_cross_account_journal_never_falls_back(saved, part):
    case = saved.case
    if part == "journal_bytes":
        Path(case["journal_files"][-1]["file"]).write_text("{}", encoding="utf-8")
    elif part == "journal_copy":
        case["journal"][-1]["committed_at"] = DAYS[-1] + "T19:01:00+08:00"
        saved.save()
    elif part == "pending_targets":
        saved.state["pending_targets"] = {A: .2}
        saved.save()
    else:
        case["journal_files"][-1] = deepcopy(saved.head["cases"][CASE_IDS[0]]["journal_files"][-1])
        saved.save()
    before = _snapshot(saved.data_root)
    with pytest.raises(ValueError):
        view.read_saved_positions(saved.directory, now=NOW)
    assert _snapshot(saved.data_root) == before


@pytest.mark.parametrize("part", ["publication_bytes", "publication_day", "case_missing", "wrong_schema", "wrong_mode", "wrong_account"])
def test_saved_publication_and_fixed_case_context_must_match(saved, part):
    if part == "publication_bytes":
        Path(saved.head["publications"][0]["file"]).write_text("{}", encoding="utf-8")
    elif part == "publication_day":
        saved.head["publications"][0]["day"] = DAYS[1]
    elif part == "case_missing":
        saved.head["cases"].pop(CASE_IDS[0])
    elif part == "wrong_schema":
        saved.head["schema"] = "another_account_checkpoint_v1"
    elif part == "wrong_mode":
        saved.case["context"]["mode"] = "end_of_day_paper"
    else:
        saved.case["context"]["account_id"] = saved.head["cases"][CASE_IDS[0]]["context"]["account_id"]
    saved.save()
    with pytest.raises(ValueError):
        view.read_saved_positions(saved.directory, now=NOW)


def test_reduced_pending_intent_cannot_belong_to_another_case_even_with_matching_seal(saved):
    saved.state["pending_published_close_intent"]["reduced_intent"]["context"] = deepcopy(
        saved.head["cases"][CASE_IDS[0]]["context"])
    saved.case["journal"][-1]["post_session_pending_published_close_intent"] = deepcopy(
        saved.state["pending_published_close_intent"])
    saved.save(reseal=True)
    with pytest.raises(ValueError):
        view.read_saved_positions(saved.directory, now=NOW)


def test_uninitialized_book_equity_and_cursor_are_unknown(saved):
    for case in saved.head["cases"].values():
        case["daily"], case["journal"], case["journal_files"] = [], [], []
        case["state"].update(day=None, positions={}, fills={}, events=[], prices={},
            pending_targets=None, pending_published_close_intent=None, pending_risk_exits=None)
    saved.save()
    result = view.read_saved_positions(saved.directory, now=NOW)
    assert result["case_count"] == 32 and result["latest_processed_day"] is None
    assert all(row["processed_day"] is None and row["equity_cny"] is None and row["positions"] == []
               for row in result["rows"])


def test_missing_head_does_not_create_a_directory_or_synthetic_account(tmp_path):
    target = tmp_path / "uncreated"
    before = _snapshot(tmp_path)
    result = view.leader_eight_type_position_view(target, now=NOW)
    assert result["status"] == "unavailable" and result["rows"] == []
    assert result["case_count"] is None and result["latest_processed_day"] is None
    assert result["metrics"] is None and result["account_created"] is False
    assert not target.exists() and _snapshot(tmp_path) == before
