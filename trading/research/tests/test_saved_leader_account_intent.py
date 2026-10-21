"""Synthetic saved files; no market replay, model, broker or network activity."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.config import StrategyConfig
from trading.research import saved_leader_account_intent as m
from trading.research.saved_account_intent import read_request as dispatch


D, E = "2026-10-16", "2026-10-19"
NOW = datetime.fromisoformat("2026-10-17T09:00:00+08:00")
A, B = "600001.SSE", "000002.SZSE"
FALSE = {"execution_admitted": False, "selection_enabled": False,
         "strategy_validated": False, "current_signal_admitted": False}


def write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    return {"file": str(path.resolve()), "sha256": sha256(data).hexdigest()}


@pytest.fixture
def books(tmp_path):
    directory = (tmp_path / "synthetic_books").resolve()
    publication = {"schema": "leader_eight_type_unbound_close_paper_plan_v1",
        "source_close_day": D, "proposed_execution_day": E,
        "recorded_at": D + "T18:45:00+08:00", "research_only": True,
        "candidate_panel_only": True, "cases": [{"case_id": case, "cap_candidates": []} for case in m.CASE_IDS], **FALSE}
    pub = {**write(tmp_path / "synthetic_publication.json", publication), "day": D,
           "execution_day": E, "recorded_at": publication["recorded_at"]}
    cases = {}
    for index, case_id in enumerate(m.CASE_IDS):
        identity = sha256(json.dumps(str(directory), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()[:16]
        context = {"account_id": "leader-eight-type-forward:" + identity + ":" + case_id,
                   "mode": "historical_replay"}
        original = {A: .16, B: .16} if index == 0 else {} if index == 2 else None
        kind = "NO_CHANGE" if original is None else "FULL_TARGET"
        intent = {"contract_type": "PortfolioIntent", "context": context, "kind": kind,
                  "decided_at": publication["recorded_at"], "execution_not_before": E,
                  "weights": original if original is not None else {}}
        positions = {A: [{"qty": 300, "available": E}]} if index == 0 else {B: [{"qty": 200, "available": D}]} if index == 1 else {}
        risk = {"context": context, "decided_on": D, "execution_on": E, "symbols": [A if index == 0 else B],
                "reason": "synthetic_saved_rule", "source": "synthetic_saved_assessment"} if index in (0, 1) else None
        state = {"day": D, "cash": 9_000_000, "positions": positions, "prices": {A: 1000, B: 2000},
                 "orders": {}, "fills": {}, "events": [], "pending_targets": original,
                 "pending_published_close_intent": intent if original is not None else None,
                 "pending_risk_exits": risk}
        entry = {"decision_day": D, "execution_day": E, "publication": pub,
                 "committed_at": D + "T19:00:00+08:00", "bound_original_intent": intent,
                 **{"post_session_" + key: deepcopy(state[key]) for key in
                    ("pending_targets", "pending_published_close_intent", "pending_risk_exits")}}
        journal = write(directory / "journal" / case_id / "synthetic_session.json", entry)
        cases[case_id] = {"context": context, "state": state,
            "daily": [{"date": D, "cash": 90000, "equity": 93000 if index == 0 else 94000 if index == 1 else 90000}],
            "journal": [entry], "journal_files": [journal], "error": "synthetic existing failure" if index == 3 else None,
            "blocking_evidence": None}
    head = {"schema": "leader_eight_type_published_close_shared_ledger_checkpoint_v1",
        "registered_at": D + "T18:50:00+08:00", "research_only": True, "candidate_panel_only": True,
        "binding": {"run_directory": str(directory), "config": StrategyConfig().to_dict(),
                    "calendar": {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                 "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}},
        "publications": [pub], "cases": cases, **FALSE}
    write(directory / "head.json", head)
    return directory, head


def test_exact_full_nc_and_empty_full_preserve_all32_order_and_single_validation(books, monkeypatch):
    directory, head = books
    original_head = (directory / "head.json").read_bytes()
    calls = []
    reader = m.read_saved_positions
    monkeypatch.setattr(m, "read_saved_positions", lambda *a, **kw: (calls.append(a), reader(*a, **kw))[1])
    exports = m.export_requests(directory, E, current=NOW)
    assert len(calls) == 1 and list(exports) == list(m.CASE_IDS)
    full, nc, empty, blocked = list(exports.values())[:4]
    assert full["kind"] == "FULL_TARGET" and full["weights"] == {A: 0, B: .16}
    assert full["original_weights"] == {A: .16, B: .16}
    assert full["remaining_exit_symbols"] == [A]
    assert full["queued_exit"]["execution_on"] == E
    assert full["target_version"] == 1 and full["target_version_basis"] == m.TARGET_VERSION_BASIS
    assert full["source_known_at"] == D + "T19:00:00+08:00"
    assert nc["kind"] == "NO_CHANGE" and nc["weights"] is None
    assert nc["remaining_exit_symbols"] == [B]
    assert head["cases"][m.CASE_IDS[1]]["state"]["pending_published_close_intent"] is None
    plain_nc = exports[m.CASE_IDS[4]]
    assert plain_nc["kind"] == "NO_CHANGE" and plain_nc["weights"] is None
    assert plain_nc["remaining_exit_symbols"] == [] and plain_nc["queued_exit"] is None
    assert head["cases"][m.CASE_IDS[4]]["state"]["pending_published_close_intent"] is None
    assert empty["kind"] == "FULL_TARGET" and empty["weights"] == {}
    assert blocked["status"] == "not_ready" and "kind" not in blocked
    assert all(item["execution_admitted"] is False and item["prefill_allowed"] is False for item in exports.values())
    assert original_head == (directory / "head.json").read_bytes()
    # A sealed but internally inconsistent full queue must not silently use
    # the publication or source targets as a replacement for one another.
    case = head["cases"][m.CASE_IDS[0]]
    case["state"]["pending_targets"] = {A: .16, B: .15}
    case["journal"][0]["post_session_pending_targets"] = deepcopy(case["state"]["pending_targets"])
    case["journal_files"][0] = write(Path(case["journal_files"][0]["file"]), case["journal"][0])
    write(directory / "head.json", head)
    invalid = m.export_requests(directory, E, current=NOW)[m.CASE_IDS[0]]
    assert invalid["schema"] == m.NOT_READY_SCHEMA and "weights differ" in invalid["reason"]


def test_roundtrip_dispatch_rejects_reweighted_or_admitted_payload(books):
    directory, _ = books
    request = m.export_request(directory, m.CASE_IDS[0], E, current=NOW)
    assert dispatch(request, current=NOW) == request
    bad = deepcopy(request)
    bad["weights"][B] = .2
    with pytest.raises(ValueError, match="exact head"):
        dispatch(bad, current=NOW)
    bad = deepcopy(request)
    bad["execution_admitted"] = True
    with pytest.raises(ValueError, match="boundary"):
        dispatch(bad, current=NOW)


def test_head_advancement_cannot_silently_refresh_old_request(books):
    directory, head = books
    request = m.export_request(directory, m.CASE_IDS[1], E, current=NOW)
    head["synthetic_extra_observation"] = "head bytes changed"
    write(directory / "head.json", head)
    with pytest.raises(ValueError, match="exact head"):
        m.read_request(request, current=NOW)


@pytest.mark.parametrize("part", ["journal", "publication"])
def test_corrupt_sealed_source_is_not_a_nochange_fallback(books, part):
    directory, head = books
    path = (Path(head["cases"][m.CASE_IDS[0]]["journal_files"][0]["file"])
            if part == "journal" else Path(head["publications"][0]["file"]))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bytes changed"):
        m.export_requests(directory, E, current=NOW)


def test_notready_cases_do_not_roll_execution_or_forge_commit_publication(books):
    directory, head = books
    wrong_day = m.export_requests(directory, "2026-10-20", current=NOW)
    assert all(row["status"] == "not_ready" for row in wrong_day.values())
    case = head["cases"][m.CASE_IDS[0]]
    case["journal"][0]["committed_at"] = D + "T18:30:00+08:00"
    case["journal_files"][0] = write(Path(case["journal_files"][0]["file"]), case["journal"][0])
    write(directory / "head.json", head)
    exports = m.export_requests(directory, E, current=NOW)
    assert exports[m.CASE_IDS[0]]["status"] == "not_ready"
    assert "predates" in exports[m.CASE_IDS[0]]["reason"]
    assert exports[m.CASE_IDS[1]]["schema"] == m.SCHEMA


def test_missing_head_is_explicit_notready_and_single_export_rejects(books, tmp_path):
    directory = tmp_path / "does_not_exist"
    exports = m.export_requests(directory, E, current=NOW)
    assert list(exports) == list(m.CASE_IDS)
    assert all(row["schema"] == m.NOT_READY_SCHEMA for row in exports.values())
    with pytest.raises(ValueError, match="no_saved"):
        m.export_request(directory, m.CASE_IDS[0], E, current=NOW)
    assert not directory.exists()


def test_optional_holding_reviews_share_one_verified_load_and_do_not_change_intents(books, monkeypatch):
    directory, _ = books
    expected = m.export_requests(directory, E, current=NOW)
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    calls, reviews = [], {}
    reader = m.read_saved_positions
    monkeypatch.setattr(m, "read_saved_positions", lambda *a, **kw: (calls.append(a), reader(*a, **kw))[1])
    actual = m.export_requests(directory, E, current=NOW, holding_reviews=reviews)
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert len(calls) == 1
    ready = [case for case, row in actual.items() if row.get("status") != "not_ready"]
    assert list(reviews) == ready
    first = reviews[m.CASE_IDS[0]]
    assert first["refs"] == actual[m.CASE_IDS[0]]["refs"]
    assert first["saved_position_review"]["context"] == actual[m.CASE_IDS[0]]["context"]
    held = first["saved_position_review"]["positions"][0]
    assert held["quantity_shares"] == 300 and held["sellable_on_book_day_shares"] == 0
    assert held["t1_locked_shares"] == 300 and held["queued_risk_exit"]["execution_on"] == E
    assert held["anchor"]["status"] == "unknown"  # No synthetic replacement for absent fills.
    assert before == {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
