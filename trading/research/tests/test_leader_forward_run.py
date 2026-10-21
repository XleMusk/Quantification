"""Synthetic future lifecycle only; no inspected historical PnL rerun."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_forward_run as runner
from trading.research.leader_daily_replay import EXIT_POLICIES, FAMILIES
from trading.research.provider_close_batches import CONTRACT
from trading.research.shortline_decision_v2 import BATCH_SCHEMA
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture


A, B = "600001.SSE", "600002.SSE"
DAYS = pd.bdate_range("2026-10-15", periods=12).strftime("%Y-%m-%d").tolist()
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []}


def protocol(tmp_path):
    original = Path(__file__).resolve().parents[3] / "outputs/leader_strategy_research/20261015_frozen_comparison"
    parent = json.loads((original / "NEXT_WINDOW_PROTOCOL.json").read_text(encoding="utf-8"))
    supplement = json.loads((original / "NEXT_WINDOW_V3_EXPIRY_SUPPLEMENT.json").read_text(encoding="utf-8"))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    parent["evaluation_window"]["calendar"] = str(calendar)
    (tmp_path / "NEXT_WINDOW_PROTOCOL.json").write_text(json.dumps(parent), encoding="utf-8")
    path = tmp_path / "NEXT_WINDOW_V3_EXPIRY_SUPPLEMENT.json"
    path.write_text(json.dumps(supplement), encoding="utf-8")
    evidence, master = write_ipo_fixture(tmp_path)
    qualified = tmp_path / "NEXT_WINDOW_V3_IPO_QUALIFICATION_SUPPLEMENT.json"
    qualified.write_text(json.dumps({"schema": "leader_next_window_ipo_qualification_supplement_v1",
        "parent_protocol": path.name, "qualification_evidence": str(evidence), "research_master_run": str(master),
        "fixed_alpha_thresholds_changed": False, "first_five_session_matching_enabled": False,
        "execution_admitted": False}), encoding="utf-8")
    return qualified


def bundle(count, *, nullable_owned_day=None):
    source_days = ["2026-10-14", *DAYS[:count]]
    rows = []
    for day in ["2024-01-02", *source_days]:
        for symbol in (A, B):
            rows.append({"date": day, "symbol": symbol, "open": 10., "high": 10.05, "low": 9.95,
                         "close": 10., "volume": 10_000_000., "amount": 100_000_000., "pct_chg": 0.,
                         "preclose_for_limits": 10., "is_st": False, "is_st_unknown": False, "source_row_valid": True,
                         "provider_raw_observation": day in source_days, "preclose": 10.,
                         "tradestatus": 1, "isST": 0, "adjustflag": "3"})
    raw = pd.DataFrame(rows)
    inventory, batches = {}, {family: {} for family in FAMILIES}
    for day in source_days:
        nullable = day == nullable_owned_day
        if nullable:
            hit = raw.date.eq(day) & raw.symbol.eq(A)
            raw.loc[hit, ["volume", "amount", "pct_chg"]] = float("nan")
            raw.loc[hit, "tradestatus"] = 0
            raw.loc[hit, "source_row_valid"] = False
        inventory[day] = {"schema": CONTRACT, "day": day, "candidate_inventory_complete": True,
                          "status_by_symbol": {A: "0" if nullable else "1", B: "1"},
                          "active_valid_raw_symbols": [B] if nullable else [A, B],
                          "raw_valuation_usable_symbols": [B] if nullable else [A, B],
                          "nullable_status0_symbols": [A] if nullable else []}
        # Only the first true window day has a strategy observation. Later
        # missing decisions must remain missing while source raw is complete.
        for family in FAMILIES:
            batches[family][day] = {"schema": BATCH_SCHEMA, "day": day, "status": "complete",
                                    "source": "synthetic_dated_provider_candidates", "candidates": [],
                                    "market_breadth": {"scope": "synthetic_provider_inventory", "verified": False}}
            if day == DAYS[0]:
                batches[family][day]["candidates"] = [{"symbol": A, "score": 1.}]
            elif day in DAYS:
                batches[family][day]["status"] = "incomplete"
    return {"schema": "provider_close_leader_signal_bundle_v1", "input_source_contract": CONTRACT,
            "raw_bars": raw, "source_days_complete": source_days, "calendar_payload": deepcopy(CALENDAR),
            "provider_inventory_by_day": inventory, "batches": batches,
            "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
            "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False}


def head(run_dir):
    return json.loads((run_dir / "head.json").read_text(encoding="utf-8"))


def test_saved_accounts_continue_exact_orders_and_expiry_without_reset_or_window_drift(tmp_path):
    frozen = protocol(tmp_path)
    target = tmp_path / "external_research"
    first = runner.run_bundle(bundle(3), target, protocol_path=frozen)
    old = head(target)
    assert first["case_count"] == 20 and {case["common_window_start"] for case in first["cases"]} == {DAYS[0]}
    assert {case["completed_sessions"] for case in first["cases"]} == {2}
    base_keys = [key for key, case in old["cases"].items() if case["exit_policy"] == EXIT_POLICIES[0]]
    assert len(base_keys) == 10 and all(old["cases"][key]["state"]["orders"] for key in base_keys)
    second = runner.run_bundle(bundle(9), target, protocol_path=frozen)
    saved = head(target)
    assert second["newly_completed_case_sessions"] == 20 * 6
    for key in base_keys:
        previous, current = old["cases"][key], saved["cases"][key]
        assert current["daily"][:2] == previous["daily"]
        assert all(current["state"]["orders"][identifier] == order for identifier, order in previous["state"]["orders"].items())
        assert {fill["day"] for fill in current["state"]["fills"].values() if fill["side"] == "buy"} == {DAYS[1]}
        assert {fill["day"] for fill in current["state"]["fills"].values() if fill["side"] == "sell"} == {DAYS[6]}
        assert current["daily"][5]["no_decision_reason"] == "shortline_v2_decision_batch_incomplete"
        assert not any(current["state"]["positions"].values())
    assert all(case["metrics"] is None for case in second["cases"])
    assert sum(case["status"] == "unavailable_verified_full_market_breadth" for case in second["cases"]) == 10
    before = (target / "head.json").read_bytes()
    again = runner.run_bundle(bundle(9), target, protocol_path=frozen)
    assert again["status"] == "no_new_session" and again["newly_completed_case_sessions"] == 0
    assert (target / "head.json").read_bytes() == before


def test_terminal_complete_source_waits_before_directory_head_or_book_creation(tmp_path, monkeypatch):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    def forbidden(*args, **kwargs):
        pytest.fail("Waiting for an exact successor must precede locks, books and checkpoints")
    with monkeypatch.context() as guard:
        guard.setattr(runner, "new_state", forbidden)
        guard.setattr(runner, "lock", forbidden)
        waiting = runner.run_bundle(bundle(1), target, protocol_path=frozen)
    assert waiting["status"] == "waiting_for_exact_successor_complete_source_close"
    assert waiting["first_decision_day"] == DAYS[0] and waiting["required_successor_day"] == DAYS[1]
    assert waiting["case_count"] == 0 and waiting["research_checkpoint_created"] is waiting["account_created"] is False
    assert not target.exists()
    ready = runner.run_bundle(bundle(2), target, protocol_path=frozen)
    assert ready["case_count"] == 20 and {case["completed_sessions"] for case in ready["cases"]} == {1}
    saved = head(target)
    baseline = next(case for case in saved["cases"].values() if case["exit_policy"] == EXIT_POLICIES[0])
    assert baseline["state"]["pending_targets"] == {A: .16}
    assert baseline["daily"][0]["no_decision_reason"] is None


def test_registered_terminal_source_and_candidates_are_frozen_before_successor_arrives(tmp_path):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    # Existing heads still freeze each newly observed terminal source prefix.
    # Only a brand-new account must await its first exact successor.
    runner.run_bundle(bundle(3), target, protocol_path=frozen)
    previous = (target / "head.json").read_bytes()
    incoming = bundle(4)
    incoming["batches"][FAMILIES[0]][DAYS[2]]["candidates"] = [{"symbol": A, "score": .25}]
    with pytest.raises(ValueError, match="prefix changed"):
        runner.run_bundle(incoming, target, protocol_path=frozen)
    assert (target / "head.json").read_bytes() == previous


@pytest.mark.parametrize("change", ["raw", "batch", "calendar", "costs"])
def test_consumed_prefix_or_protocol_revision_is_rejected_without_checkpoint_mutation(tmp_path, change):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    runner.run_bundle(bundle(3), target, protocol_path=frozen)
    before = (target / "head.json").read_bytes()
    incoming = bundle(4)
    if change == "raw":
        incoming["raw_bars"].loc[lambda frame: frame.date.eq(DAYS[0]) & frame.symbol.eq(A), "close"] = 10.01
    elif change == "batch":
        incoming["batches"][FAMILIES[0]][DAYS[0]]["candidates"][0]["score"] = .5
    elif change == "calendar":
        incoming["calendar_payload"]["closed_ranges"] = [DAYS[0] + "/" + DAYS[0]]
    else:
        parent_path = tmp_path / "NEXT_WINDOW_PROTOCOL.json"
        parent = json.loads(parent_path.read_text(encoding="utf-8"))
        parent["common_rules"]["commission"] = .0004
        parent_path.write_text(json.dumps(parent), encoding="utf-8")
    with pytest.raises(ValueError, match="prefix changed|calendars differ|protocol|costs"):
        runner.run_bundle(incoming, target, protocol_path=frozen)
    assert (target / "head.json").read_bytes() == before


def test_unpriceable_owned_nullable_source_fails_trial_without_advancing_its_saved_ledger(tmp_path):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    source = bundle(5, nullable_owned_day=DAYS[2])
    runner.run_bundle(source, target, protocol_path=frozen, through=DAYS[1])
    previous = head(target)
    failed = runner.run_bundle(source, target, protocol_path=frozen)
    saved = head(target)
    keys = [key for key, case in saved["cases"].items() if case["exit_policy"] == EXIT_POLICIES[0]]
    assert len(keys) == 10
    for key in keys:
        assert saved["cases"][key]["state"] == previous["cases"][key]["state"]
        assert saved["cases"][key]["daily"] == previous["cases"][key]["daily"]
        assert "Unpriceable risk-exit holding" in saved["cases"][key]["error"]
    assert sum(case["status"] == "blocked_source_or_engine" for case in failed["cases"]) == 10
    before = (target / "head.json").read_bytes()
    repeated = runner.run_bundle(source, target, protocol_path=frozen)
    assert repeated["status"] == "blocked_research_only" and repeated["newly_completed_case_sessions"] == 0
    assert (target / "head.json").read_bytes() == before


def test_missing_exact_successor_is_not_replaced_with_later_complete_close(tmp_path, monkeypatch):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    source = bundle(3)
    source["source_days_complete"].remove(DAYS[1])
    source["raw_bars"] = source["raw_bars"].loc[lambda frame: frame.date.ne(DAYS[1])]
    source["provider_inventory_by_day"].pop(DAYS[1])
    for family in FAMILIES:
        source["batches"][family].pop(DAYS[1])
    def forbidden(*args, **kwargs):
        pytest.fail("A later complete source cannot stand in for the calendar successor")
    monkeypatch.setattr(runner, "new_state", forbidden)
    result = runner.run_bundle(source, target, protocol_path=frozen)
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert result["required_successor_day"] == DAYS[1] and result["case_count"] == 0
    assert not target.exists()


def test_no_independent_source_returns_waiting_without_creating_research_directory(tmp_path, monkeypatch):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    monkeypatch.setattr(runner, "new_state", lambda *args, **kwargs: pytest.fail("No future source means no research books"))
    result = runner.run_bundle(bundle(0), target, protocol_path=frozen)
    assert result["status"] == "waiting_for_first_complete_unexamined_source_close" and result["case_count"] == 0
    assert not target.exists()


def test_failed_session_commit_resumes_saved_pending_target_without_duplicate_orders(tmp_path, monkeypatch):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    actual_writer = runner.write_json
    calls = 0
    def failed_commit(path, value):
        nonlocal calls
        calls += 1
        if calls == 3:  # Init, day0 commit, then interruption before day1 commit.
            raise OSError("synthetic_atomic_commit_interruption")
        actual_writer(path, value)
    monkeypatch.setattr(runner, "write_json", failed_commit)
    with pytest.raises(OSError, match="commit_interruption"):
        runner.run_bundle(bundle(4), target, protocol_path=frozen)
    saved = head(target)
    assert {len(case["daily"]) for case in saved["cases"].values()} == {1}
    assert all(not case["state"]["orders"] for case in saved["cases"].values())
    monkeypatch.setattr(runner, "write_json", actual_writer)
    resumed = runner.run_bundle(bundle(4), target, protocol_path=frozen)
    assert {case["completed_sessions"] for case in resumed["cases"]} == {3}
    for case in head(target)["cases"].values():
        if case["exit_policy"] == EXIT_POLICIES[0]:
            assert len(case["state"]["orders"]) == 1
            assert {fill["day"] for fill in case["state"]["fills"].values()} == {DAYS[1]}


def test_saved_suite_or_backward_cutoff_cannot_silently_change(tmp_path):
    frozen, target = protocol(tmp_path), tmp_path / "external_research"
    runner.run_bundle(bundle(3), target, protocol_path=frozen)
    before = (target / "head.json").read_bytes()
    with pytest.raises(ValueError, match="cannot move behind"):
        runner.run_bundle(bundle(3), target, protocol_path=frozen, through=DAYS[0])
    assert (target / "head.json").read_bytes() == before
    saved = head(target)
    saved["cases"].pop(next(iter(saved["cases"])))
    (target / "head.json").write_text(json.dumps(saved), encoding="utf-8")
    corrupted = (target / "head.json").read_bytes()
    with pytest.raises(ValueError, match="twenty-case suite changed"):
        runner.run_bundle(bundle(3), target, protocol_path=frozen)
    assert (target / "head.json").read_bytes() == corrupted


def pending_package(tmp_path, *, status="running", complete=False):
    from trading.research.baostock_full_close import SCHEMA, WHEEL_SHA256
    from trading.research.baostock_daily_slice import digest
    folder = tmp_path / "partial_source"
    folder.mkdir()
    roster_folder = folder / "roster"
    roster_folder.mkdir()
    raw_path = roster_folder / "raw.csv"
    roster = pd.DataFrame([["sh.600001", "1", "A"], ["sh.600002", "1", "B"]],
                          columns=["code", "tradeStatus", "code_name"])
    roster.to_csv(raw_path, index=False)
    roster_receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "day": "2026-10-14",
                      "response_day": "2026-10-14", "method": "query_all_stock", "kwargs": {"day": "2026-10-14"},
                      "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
                      "fields_returned": list(roster), "sdk": {"sha256": WHEEL_SHA256},
                      "execution_admitted": False, "price_bars_generated": False,
                      "raw_csv": str(raw_path), "raw_sha256": digest(raw_path), "rows_preserved": 2}
    (roster_folder / "receipt.json").write_text(json.dumps(roster_receipt), encoding="utf-8")
    aggregate = folder / "raw_daily.csv"
    aggregate.write_text("preserved_partial_inventory\n", encoding="utf-8")
    flags = {key: False for key in ("source_verified", "pit_verified", "strategy_validated", "selection_enabled",
                                    "execution_admitted", "stockdb_modified", "ledger_mutated")}
    plan = {"schema": SCHEMA, "day": "2026-10-14", "symbols": [A, B], "ordinary_symbols": 2,
            "roster_raw_sha256": digest(raw_path), **flags}
    receipt = {"schema": SCHEMA, "day": "2026-10-14", "status": status,
               "full_dated_roster_inventory_complete": complete,
               "pending_symbols": [B], "completed_symbols": 1, "ordinary_symbols": 2,
               "raw_daily_sha256": digest(aggregate),
               "created_at": "2026-10-15T08:00:00+08:00", "updated_at": "2026-10-15T09:00:00+08:00", **flags}
    (folder / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return folder


@pytest.mark.parametrize("status", ["running", "partial_budget_checkpoint_not_full_close"])
def test_cli_known_partial_source_waits_without_constructing_research_accounts(tmp_path, capsys, status):
    source = pending_package(tmp_path, status=status)
    target = tmp_path / "must_not_create"
    assert runner.main(["--package", str(source), "--history-snapshot", str(tmp_path / "not_loaded"),
                        "--run-dir", str(target), "--protocol", str(tmp_path / "not_loaded.json")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "waiting_for_complete_source_close" and result["case_count"] == 0
    assert result["research_checkpoint_created"] is False and not target.exists()


@pytest.mark.parametrize("status,complete", [("failed_stopped_no_retry", False),
                                            ("complete_full_dated_close_inventory_not_admitted", True)])
def test_failed_or_unverified_complete_package_is_never_silently_treated_as_pending(tmp_path, status, complete):
    source = pending_package(tmp_path, status=status, complete=complete)
    target = tmp_path / "must_not_create"
    with pytest.raises(ValueError, match="complete successful roster inventory|boundary flags|identity|roster"):
        runner.main(["--package", str(source), "--history-snapshot", str(tmp_path / "not_loaded"),
                     "--run-dir", str(target), "--protocol", str(tmp_path / "not_loaded.json")])
    assert not target.exists()


def test_pending_raw_hash_tampering_fails_instead_of_returning_wait(tmp_path):
    source = pending_package(tmp_path)
    (source / "raw_daily.csv").write_text("changed_bytes\n", encoding="utf-8")
    target = tmp_path / "must_not_create"
    with pytest.raises(ValueError, match="raw hash changed"):
        runner.main(["--package", str(source), "--history-snapshot", str(tmp_path / "not_loaded"),
                     "--run-dir", str(target), "--protocol", str(tmp_path / "not_loaded.json")])
    assert not target.exists()


def test_status_only_retains_actual_source_failure_and_exit_two_without_account(tmp_path, capsys):
    source = pending_package(tmp_path, status="failed_stopped_no_retry")
    receipt_path = source / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update(error_type="ConnectionResetError", error="synthetic WinError10054", logical_price_queries=2,
                   preserved_failed_batches=[{"batch": 0, "in_flight": {"symbol": B}, "status": "failed_stopped"}])
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    saved = tmp_path / "status_evidence" / "receipt.json"
    assert runner.main(["--package", str(source), "--status-only", "--status-output", str(saved)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed_source_close" and result["research_checkpoint_created"] is False
    evidence = result["source_packages"][0]
    assert evidence["source_status"] == "failed_stopped_no_retry" and evidence["error"] == "synthetic WinError10054"
    assert evidence["preserved_failed_batches"][0]["in_flight"]["symbol"] == B
    assert json.loads(saved.read_text(encoding="utf-8")) == result
    assert not list(tmp_path.rglob("head.json"))
