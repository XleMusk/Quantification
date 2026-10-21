from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import corporate_actions
from trading.ledger import Ledger, new_state
from trading.rights_issue import reference_terms, rights_replay_contract, waiver_receipt
from trading.research.experiment import load_replay_checkpoint, replay


REFERENCE = {"kind": "rights_issue", "symbol": "300176.SZSE", "date": "2026-08-21",
             "registration_date": "2026-08-12", "rights_ratio": ".3722702",
             "subscription_price": "3.36", "source_sha256": "fixture-reviewed-reference"}
KEY = "rights:300176.SZSE:2026-08-21"


def fixture(policy=None, mode=TradingMode.REPLAY):
    state = new_state(140000)
    state["day"] = "2026-08-12"
    state["positions"][REFERENCE["symbol"]] = [{"qty": 1600, "available": "2026-08-01"}]
    state["prices"][REFERENCE["symbol"]] = 600
    state["entitlements"][KEY] = 1600
    if policy:
        state["rights_replay_policy"] = rights_replay_contract(policy)
    context = RunContext("standalone_replay", mode)
    feed = SimpleNamespace(actions=[], manifest={"historical_price_adjustments": [REFERENCE]},
                           rights_replay_policy=policy)
    feed.rights_issue_receipt = lambda ref, qty, ctx: waiver_receipt(ref, qty, ctx, policy)
    return state, Ledger(state, StrategyConfig(), context), feed


def subscription(context, allocated=640):
    return {"kind": "rights_issue_resolution", "account_id": context.account_id, "mode": context.mode.value,
            "registered_quantity": 1600, "action_terms": reference_terms(REFERENCE), "decision": "subscribe",
            "source_kind": "broker_account_record", "source_ref": "TEST ONLY: encoded observed receipt",
            "holder_terms": {"rights_ratio": ".4", "subscription_start": "2026-08-13",
                             "subscription_end": "2026-08-19", "source_ref": "TEST ONLY: issuer holder terms"},
            "entitled_shares": 640, "subscribed_shares": 640, "allocated_shares": allocated,
            "cash_debit_cents": 215040, "subscription_fees_cents": 0,
            "payment_date": "2026-08-13", "allocation_date": "2026-08-21" if allocated else None,
            "listing_date": "2026-08-24" if allocated else None,
            "refund_cents": (640 - allocated) * 336, "fee_refund_cents": 0, "net_refund_interest_cents": 0,
            "refund_date": "2026-08-21" if allocated != 640 else None}


def test_missing_receipt_still_stops_and_does_not_assume_waiver():
    state, book, feed = fixture()
    before = deepcopy(state)
    corporate_actions(book, feed, "2026-08-13")
    assert state == before
    with pytest.raises(ValueError, match="subscription/allocation reconciliation"):
        corporate_actions(book, feed, "2026-08-21")
    assert state == before


def test_predeclared_waiver_preserves_cash_shares_and_is_idempotent_after_recovery():
    state, book, feed = fixture("waive")
    cash = state["cash"]
    corporate_actions(book, feed, "2026-08-13")
    corporate_actions(book, feed, "2026-08-21")
    restored = json.loads(json.dumps(state))
    restored_book = Ledger(restored, StrategyConfig())
    corporate_actions(restored_book, feed, "2026-08-21")
    corporate_actions(restored_book, feed, "2026-08-24")
    assert restored["cash"] == cash and restored_book.quantity(REFERENCE["symbol"]) == 1600
    assert restored["receivables"] == {}
    events = [row for row in restored["events"] if row["kind"] == "rights_waive"]
    assert len(events) == 1 and events[0]["source_kind"] == "explicit_replay_assumption"


@pytest.mark.parametrize("mode,bound", [(TradingMode.PAPER, False), (TradingMode.REPLAY, True)])
def test_assumptions_never_mutate_paper_or_broker_bound_accounts(mode, bound):
    state, book, feed = fixture("waive", mode)
    if bound:
        state["broker_binding"] = {"account_id": "fixture"}
    before = deepcopy(state)
    with pytest.raises(PermissionError, match="unbound replay"):
        corporate_actions(book, feed, "2026-08-21")
    assert state == before


@pytest.mark.parametrize("allocated", [640, 600, 0])
def test_subscription_cash_pending_principal_refund_and_listing_are_accounted_once(allocated):
    state, book, feed = fixture()
    receipt = subscription(book.context, allocated)
    feed.rights_issue_receipt = lambda *_: deepcopy(receipt)
    opening = book.equity()
    cash = state["cash"]
    corporate_actions(book, feed, "2026-08-13")
    assert state["cash"] == cash - 215040 and state["receivables"][KEY] == 215040
    assert book.equity() == opening  # Paid principal is unsettled, not spendable cash or a missing asset.
    restored = json.loads(json.dumps(state))
    book = Ledger(restored, StrategyConfig())
    corporate_actions(book, feed, "2026-08-21")
    corporate_actions(book, feed, "2026-08-21")
    corporate_actions(book, feed, "2026-08-24")
    assert book.quantity(REFERENCE["symbol"]) == 1600 + allocated
    assert book.available(REFERENCE["symbol"], "2026-08-21") == 1600
    assert book.available(REFERENCE["symbol"], "2026-08-24") == 1600 + allocated
    assert restored["cash"] == cash - allocated * 336
    assert restored["receivables"] == {}
    assert restored["rights_issue_progress"][KEY].count("payment") == 1
    if allocated:
        assert restored["rights_issue_progress"][KEY].count("allocation") == 1


@pytest.mark.parametrize("change", [{"account_id": "wrong"}, {"registered_quantity": 1599},
                                    {"allocated_shares": 641}, {"cash_debit_cents": 215039},
                                    {"listing_date": None}, {"payment_date": "2026-08-20"},
                                    {"source_kind": "explicit_replay_assumption"},
                                    {"holder_terms": {"rights_ratio": ".3722702", "subscription_start": "2026-08-13",
                                                      "subscription_end": "2026-08-19", "source_ref": "incorrect ex ratio"}}])
def test_unknown_or_conflicting_economics_are_rejected_before_mutation(change):
    state, book, feed = fixture()
    receipt = {**subscription(book.context), **change}
    feed.rights_issue_receipt = lambda *_: receipt
    before = deepcopy(state)
    with pytest.raises((ValueError, TypeError)):
        corporate_actions(book, feed, "2026-08-13")
    assert state == before


def test_late_receipt_and_insufficient_cash_do_not_retroactively_debit():
    state, book, feed = fixture()
    feed.rights_issue_receipt = lambda *_: subscription(book.context)
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Skipped rights"):
        corporate_actions(book, feed, "2026-08-21")
    assert state == before
    state["cash"] = 215039
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Insufficient unreserved"):
        corporate_actions(book, feed, "2026-08-13")
    assert state == before


def test_checkpoint_cannot_adopt_or_discard_predeclared_rights_policy(tmp_path):
    config = StrategyConfig()
    folder = tmp_path / "empty"
    folder.mkdir()
    state = new_state(config.initial_cash)
    state["rights_replay_policy"] = rights_replay_contract("waive")
    (folder / "account.json").write_text(json.dumps(state), encoding="utf-8")
    (folder / "daily.csv").write_text("date\n", encoding="utf-8")
    receipt = {"status": "completed", "completed_sessions": 0, "last_completed_session": None,
               "account_basis": "last_successfully_completed_session", "config": config.to_dict(),
               "target_policy_enabled": False, "target_policy_contract": None,
               "portfolio_policy_enabled": False, "portfolio_policy_contract": None,
               "commission_deadband": False, "lot_allocation": None,
               "entry_risk_policy": None, "rights_replay_policy": rights_replay_contract("waive")}
    (folder / "replay.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="rights policy changed"):
        load_replay_checkpoint(folder, config, [])
    feed = SimpleNamespace(manifest={}, rights_replay_policy="waive")
    checkpoint = load_replay_checkpoint(folder, config, [], rights_replay_policy="waive")
    assert checkpoint["rights_replay_policy"] == rights_replay_contract("waive")
    with pytest.raises(ValueError, match="configuration/policy mismatch"):
        replay(SimpleNamespace(manifest={}), config, [], tmp_path / "wrong", checkpoint=checkpoint)
