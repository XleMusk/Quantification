"""Synthetic close-publication timing/identity tests; no profitability claims."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.contracts import MARKET_TZ, RunContext, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research import leader_close_publication as publish
from trading.research.leader_daily_replay import EXIT_POLICIES, FAMILIES, POOLS, freeze_cohorts
from trading.research.shortline_decision_v2 import ShortlineDecisionFeedV2
from trading.research.shortline_decision_v3 import FiveSessionExpiryPolicy
from trading.research.shortline_panel_replay import ShortlinePanelFeed, _config
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view
from trading.research.tests.test_leader_forward_run import A, DAYS, bundle, pending_package, protocol


DAY = DAYS[0]


def close_bundle():
    result = bundle(1)
    result["source_packages"] = [{"day": DAY, "folder": "synthetic_completed_source",
                                   "terminal_completion_observed_at": DAY + "T16:00:00+08:00",
                                   "terminal_completion_time_basis": "synthetic_receipt_terminal_update",
                                   "captured_timestamp_not_signal_publication": True}]
    return result


def clock(monkeypatch, text):
    monkeypatch.setattr(publish, "_now", lambda: datetime.fromisoformat(text).astimezone(MARKET_TZ))


def baseline(result):
    return [case for case in result["cases"] if case["exit_policy"] == EXIT_POLICIES[0]]


def qualification(source):
    calendar, _, _ = _calendar_view(source["calendar_payload"], "2026-01-01", "2026-12-31", [])
    records = {symbol: {"ipo_date": "2000-01-01", "available_at": "2026-10-08T08:00:00+08:00",
                        "board": "main", "source_basis": "synthetic_saved_known_date"}
               for symbol in source["raw_bars"].symbol.unique()}
    return IpoQualification(records, tuple(calendar), {"synthetic": True, "pit_verified": False, "execution_admitted": False})


def test_declared_common_pool_subset_reuses_targets_without_loading_other_pool(monkeypatch):
    source = close_bundle()
    cohort = freeze_cohorts(source["raw_bars"], DAY)
    calendar, _, _ = _calendar_view(source["calendar_payload"], "2026-01-01", "2026-12-31", [])
    options = dict(cohort=cohort, head=None, day=DAY, calendar=calendar, config=_config(100000, 5),
                   ipo_qualification=qualification(source))
    original = publish.prepare_close_case_targets(source, **options)
    wanted = tuple(row["case_id"] for row in original if row["pool"] == POOLS[0] and row["exit_policy"] == EXIT_POLICIES[0])
    calls, scope = [], publish.scope_provider_batches
    def tracked(bundle, symbols):
        calls.append(list(symbols))
        return scope(bundle, symbols)
    monkeypatch.setattr(publish, "scope_provider_batches", tracked)
    selected = publish.prepare_close_case_targets(source, case_ids=wanted, **options)
    assert selected == [row for row in original if row["case_id"] in wanted]
    assert calls == [cohort["pools"][POOLS[0]]]
    with pytest.raises(ValueError, match="subset"):
        publish.prepare_close_case_targets(source, case_ids=wanted[::-1], **options)


def record_targets(source, out, **options):
    options.setdefault("ipo_qualification", qualification(source))
    return publish.publish_close_targets(source, out, **options)


def test_complete_close_publishes_unbound_weights_without_successor_bar_or_account(tmp_path, monkeypatch):
    frozen, source = protocol(tmp_path), close_bundle()
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    original = deepcopy(source["raw_bars"])
    result = record_targets(source, tmp_path / "publication", protocol_path=frozen)
    assert result["case_count"] == 20 and result["proposed_execution_day"] == DAYS[1]
    assert {case["status"] for case in baseline(result)} == {"unbound_pure_rule_target_preview"}
    assert all(case["target_weights"] == {A: .16} for case in baseline(result))
    assert all(case["portfolio_intent"]["context"] is None for case in baseline(result))
    assert all(case["portfolio_intent"]["execution_not_before"] == DAYS[1] for case in baseline(result))
    assert all(case["current_pit_eligibility"] == "unverified" and not case["formal_target_admitted"] for case in result["cases"])
    assert result["orders_generated"] is False and result["account_created"] is False
    assert result["successor_raw_used"] is False and result["future_independent_window_advanced"] is False
    assert str(source["raw_bars"].date.max()) == DAY
    pd.testing.assert_frame_equal(source["raw_bars"], original)
    assert not list(tmp_path.rglob("account.json")) and not list(tmp_path.rglob("orders.csv"))


def test_midnight_publication_keeps_actual_clock_and_unbound_execution_date(tmp_path, monkeypatch):
    frozen = protocol(tmp_path)
    clock(monkeypatch, DAYS[1] + "T07:00:00+08:00")
    result = record_targets(close_bundle(), tmp_path / "publication", protocol_path=frozen)
    assert result["status"] == "close_research_target_publication_only"
    assert result["publication_at"] == DAYS[1] + "T07:00:00+08:00"
    assert result["proposed_execution_day"] == DAYS[1] and result["execution_binding_pending"]
    assert all(case["portfolio_intent"]["execution_not_before"] is None for case in baseline(result))


def test_late_recording_cannot_be_backdated_or_silently_moved_to_later_open(tmp_path, monkeypatch):
    frozen = protocol(tmp_path)
    clock(monkeypatch, DAYS[1] + "T10:00:00+08:00")
    result = record_targets(close_bundle(), tmp_path / "publication", protocol_path=frozen,
                                           publication_at=DAY + "T17:00:00+08:00")
    assert result["status"] == "late_context_only"
    assert {case["status"] for case in baseline(result)} == {"late_context_only"}
    assert result["publication_at"] == DAYS[1] + "T10:00:00+08:00"
    assert result["claimed_publication_at"] == DAY + "T17:00:00+08:00"
    assert result["claimed_time_verified"] is False and result["publication_time_backfilled"] is False
    assert result["proposed_execution_day"] == DAYS[1] and result["execution_day_rolled_forward"] is False


@pytest.mark.parametrize("claimed", [DAY + "T15:30:00+08:00", DAY + "T17:00:00+08:00", DAY + "T16:00:00"])
def test_invalid_claim_clocks_never_create_publication(tmp_path, monkeypatch, claimed):
    frozen = protocol(tmp_path)
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    target = tmp_path / "publication"
    with pytest.raises(ValueError, match="Claimed publication|timezone"):
        record_targets(close_bundle(), target, protocol_path=frozen, publication_at=claimed)
    assert not target.exists()


def test_complete_empty_target_differs_from_unknown_batch_and_no_output_is_rewritten(tmp_path, monkeypatch):
    frozen, source = protocol(tmp_path), close_bundle()
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    source["batches"][FAMILIES[0]][DAY]["candidates"] = []
    source["batches"][FAMILIES[1]][DAY]["status"] = "incomplete"
    target = tmp_path / "publication"
    result = record_targets(source, target, protocol_path=frozen)
    first = next(case for case in baseline(result) if case["family"] == FAMILIES[0])
    missing = next(case for case in baseline(result) if case["family"] == FAMILIES[1])
    assert first["portfolio_intent"]["kind"] == "FULL_TARGET" and first["target_weights"] == {}
    assert first["target_cash_weight"] == 1
    assert missing["portfolio_intent"]["kind"] == "NO_CHANGE"
    assert missing["target_cash_weight"] is None
    before = (target / "publication.json").read_bytes()
    with pytest.raises(FileExistsError):
        record_targets(source, target, protocol_path=frozen)
    assert (target / "publication.json").read_bytes() == before


def current_ledger_checkpoint(tmp_path, frozen, source):
    config = _config(100000, 5)
    key = publish._case_key(POOLS[0], FAMILIES[0], EXIT_POLICIES[0])
    context = RunContext("leader-forward:synthetic:" + key, TradingMode.REPLAY)
    state = new_state(100000)
    book = Ledger(state, config, context)
    book.submit("known_past_buy", "2026-10-08", A, "buy", 100, 1000)
    book.fill("known_past_fill", "known_past_buy", 100, 1000, "2026-10-08", "2026-10-09")
    base = ShortlinePanelFeed(source["raw_bars"], {}, [A, "600002.SSE"])
    base.calendar = pd.bdate_range("2026-10-08", "2026-10-16").strftime("%Y-%m-%d").tolist()
    feed = ShortlineDecisionFeedV2(base, {})
    advance(state, feed, DAY, config, 0, context=context, replay_risk_exit_policy=FiveSessionExpiryPolicy())
    assert state["day"] == DAY and book.quantity(A) == 100  # No fabricated sell on absent successor.
    head = {"schema": "leader_forward_shared_ledger_checkpoint_v1", "binding": publish._protocol(frozen)[0],
            "execution_admitted": False, "start": DAY, "cohort": freeze_cohorts(source["raw_bars"], DAY),
            "cases": {key: {"pool": POOLS[0], "family": FAMILIES[0], "exit_policy": EXIT_POLICIES[0], "state": state}}}
    path = tmp_path / "existing_same_ledger_head.json"
    path.write_text(json.dumps(head), encoding="utf-8")
    return path, head, key


def test_current_same_ledger_can_bind_expiry_target_but_remains_research_and_does_not_mutate_book(tmp_path, monkeypatch):
    frozen, source = protocol(tmp_path), close_bundle()
    path, head, key = current_ledger_checkpoint(tmp_path, frozen, source)
    before = path.read_bytes()
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    result = record_targets(source, tmp_path / "publication", protocol_path=frozen, ledger_checkpoint=path)
    bound = next(case for case in result["cases"] if case["case_id"] == key)
    assert bound["status"] == "account_bound_research_target_preview" and bound["target_weights"] == {}
    assert bound["expired_held_symbols"] == [A] and bound["formal_target_admitted"] is False
    assert bound["portfolio_intent"]["context"]["account_id"].endswith(":" + key)
    assert result["ledger_mutated"] is False and result["orders_generated"] is False
    assert path.read_bytes() == before
    assert Ledger(head["cases"][key]["state"], _config(100000, 5)).quantity(A) == 100


def test_stale_ledger_is_not_used_as_current_holdings(tmp_path, monkeypatch):
    frozen, source = protocol(tmp_path), close_bundle()
    path, head, key = current_ledger_checkpoint(tmp_path, frozen, source)
    head["cases"][key]["state"]["day"] = "2026-10-14"
    path.write_text(json.dumps(head), encoding="utf-8")
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    result = record_targets(source, tmp_path / "publication", protocol_path=frozen, ledger_checkpoint=path)
    unbound = next(case for case in result["cases"] if case["case_id"] == key)
    assert unbound["status"] == "unbound_pure_rule_target_preview"
    assert unbound["ledger_status"] == "ledger_asof_not_current_source_close"
    assert unbound["target_weights"] == {A: .16} and unbound["portfolio_intent"]["context"] is None


def test_future_raw_or_missing_actual_source_completion_cannot_publish(tmp_path, monkeypatch):
    frozen = protocol(tmp_path)
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    source = close_bundle()
    source["raw_bars"] = bundle(2)["raw_bars"]
    with pytest.raises(ValueError, match="only D and earlier"):
        record_targets(source, tmp_path / "future", protocol_path=frozen)
    source = close_bundle()
    source["source_packages"][0].pop("terminal_completion_observed_at")
    with pytest.raises(ValueError, match="real recorded source terminal"):
        record_targets(source, tmp_path / "unknown_clock", protocol_path=frozen)


def test_partial_source_cli_saves_gate_only_without_publication_or_account(tmp_path, capsys):
    source = pending_package(tmp_path)
    target, gate = tmp_path / "must_not_publish", tmp_path / "source_gate.json"
    assert publish.main(["--package", str(source), "--history-snapshot", str(tmp_path / "not_read"),
                         "--protocol", str(tmp_path / "not_read.json"), "--out", str(target),
                         "--gate-output", str(gate)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "waiting_for_complete_source_close" and result["publication_created"] is False
    assert not target.exists() and not list(tmp_path.rglob("head.json"))


def test_explicit_first_five_ipo_evidence_excludes_unsupported_stage_from_new_target_only(tmp_path, monkeypatch):
    frozen, source = protocol(tmp_path), close_bundle()
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    ipo = "301660.SZSE"
    extra = source["raw_bars"].loc[lambda frame: frame.date.ge("2026-10-14") & frame.symbol.eq(A)].copy()
    extra["symbol"] = ipo
    source["raw_bars"] = pd.concat([source["raw_bars"], extra], ignore_index=True)
    for day, inventory in source["provider_inventory_by_day"].items():
        inventory["status_by_symbol"][ipo] = "1"
        inventory["active_valid_raw_symbols"].append(ipo)
        inventory["raw_valuation_usable_symbols"].append(ipo)
    for family in FAMILIES:
        source["batches"][family][DAY]["candidates"] = [{"symbol": ipo, "score": 1., "stage": 1.},
                                                       {"symbol": A, "score": .5}]
    original = deepcopy(source["batches"])
    proof = qualification(source)
    proof.records[ipo] = {"ipo_date": "2026-10-13", "available_at": "2026-10-12T23:59:59+08:00",
                           "board": "chinext", "rule_effective_from": "2026-07-06",
                           "source_basis": "synthetic_official_known_listing_date"}
    result = record_targets(source, tmp_path / "publication", protocol_path=frozen, ipo_qualification=proof)
    for case in baseline(result):
        assert ipo in case["raw_rule_weights_before_qualification"]
        assert ipo not in case["target_weights"] and case["target_weights"] == {A: .16}
        rejected = next(row for row in case["candidate_listing_qualification"] if row["symbol"] == ipo)
        assert rejected["listing_session_ordinal"] == 3
        assert rejected["ipo_window_feature_support"] is False
        assert rejected["ipo_feature_exclusion_reason"] == "no_price_limit_first_five_not_limit_up_streak"
    assert source["batches"] == original
    assert result["mechanical_support_filter"]["return_based_filter"] is False
    assert result["mechanical_support_filter"]["old_producer_or_protocol_modified"] is False


def test_unknown_ipo_qualification_does_not_turn_diagnostic_rule_rank_into_cash_target(tmp_path, monkeypatch):
    # The fixture's default can include an explicit IPO supplement. Use its
    # preserved original V3 parent here to exercise genuinely absent evidence.
    frozen = protocol(tmp_path).parent / "NEXT_WINDOW_V3_EXPIRY_SUPPLEMENT.json"
    clock(monkeypatch, DAY + "T16:30:00+08:00")
    result = record_targets(close_bundle(), tmp_path / "publication", protocol_path=frozen, ipo_qualification=None)
    for case in baseline(result):
        assert case["raw_rule_weights_before_qualification"] == {A: .16}
        assert case["portfolio_intent"]["kind"] == "NO_CHANGE"
        assert case["portfolio_intent"]["reason"] == "candidate_listing_window_qualification_unknown"
        assert case["target_weights"] == {} and case["formal_target_admitted"] is False
