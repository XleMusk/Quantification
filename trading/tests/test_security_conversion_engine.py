"""Non-trade security credits share the real simulated order/account path."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, SignalUnavailable, TradingMode
from trading.ledger import Ledger, new_state
from trading.published_close_session import METHOD_ID as SPLIT_METHOD, attach_close
from trading.research.shortline_decision_v2 import holding_origin
from trading.research.leader_type_risk_exit import LeaderTypeRiskExitPolicy
from trading.research.order_capacity_replay import PriorVolumeReplayPolicy
from trading.security_conversion import (
    METHOD_ID, ConversionCredit, SecurityConversionReceipt, SecurityConversionView,
)
from trading.target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE


A, B = "600001.SSE", "600002.SSE"
D, E, F, G, H = "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"
DAYS = ["2025-01-03", D, E, F, G, H, "2025-01-13"]
CONTEXT = RunContext("explicit_nontrade_conversion_engine_fixture", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=2, gross_limit=.8,
                        single_limit=.8, rebalance_sessions=1, slippage_bps=0)


@pytest.fixture(autouse=True)
def actual_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(H + "T20:00:00+08:00"))


def feed():
    rows = {}
    for day in DAYS:
        rows[day] = {symbol: dict(open=price, high=price * 1.01, low=price * .99,
            close=price, preclose=price, volume=1_000_000., amount=10_000_000.,
            tradestatus=1, isST=0, adjustflag="3") for symbol, price in ((A, 10.), (B, 5.))}
        if day >= F:
            rows[day].pop(A)
    calls = []
    def scores(day, config):
        calls.append(day)
        return SignalUnavailable("no_new_fixed_strategy_publication")
    return SimpleNamespace(calendar=DAYS, rows=rows,
        volumes=pd.DataFrame(1_000_000., index=DAYS, columns=[A, B]),
        instruments={A: {"ipoDate": "2000-01-01", "outDate": F},
                     B: {"ipoDate": "2000-01-01", "outDate": ""}},
        actions=[], next_day=lambda day: DAYS[DAYS.index(day)+1], scores=scores, calls=calls)


def view(receipts=()):
    return SecurityConversionView("synthetic_conversion_registry", CONTEXT, "dated_asof", tuple(receipts))


def publication(day, weights):
    return (PortfolioIntent.no_change("actual_saved_unknown", context=CONTEXT,
        decided_at=day+"T18:00:00+08:00", execution_not_before=DAYS[DAYS.index(day)+1],
        source="actual_saved_publication:"+day) if weights is None else
        PortfolioIntent.full_target(weights, context=CONTEXT, decided_at=day+"T18:00:00+08:00",
            execution_not_before=DAYS[DAYS.index(day)+1], source="actual_saved_publication:"+day))


def step(state, base, day, weights=None, *, conversion_view=None, split=False, risk=None,
         lifecycle=None):
    options = {"context": CONTEXT, "published_close_intent": publication(day, weights),
               "security_conversion_view": view() if conversion_view is None else conversion_view}
    if split:
        options.update(published_close_session_method=SPLIT_METHOD,
                       published_close_target_policy=Target(), replay_risk_exit_policy=risk or Risk())
    elif risk is not None:
        options["replay_risk_exit_policy"] = risk
    if lifecycle is not None:
        options["full_exit_lifecycle"] = lifecycle
    return engine.advance(state, base, day, CONFIG, DAYS.index(day)-1, **options)


class Target:
    policy_id = "conversion_fixture_identity_target"
    def __call__(self, intent):
        return intent


class Risk:
    policy_id = "conversion_fixture_independent_risk"
    def __call__(self, day, book, base):
        return None


def seeded(base=None, *, e_weights=None, risk=None, lifecycle=None, split=False):
    base, state = base or feed(), new_state(CONFIG.initial_cash)
    step(state, base, D, {A: .8}, split=split, risk=risk, lifecycle=lifecycle)
    step(state, base, E, e_weights, split=split, risk=risk, lifecycle=lifecycle)
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 800
    return base, state


def conversion(tmp_path, state, *, available_on=G, effective_day=F):
    original = state["positions"][A]
    source = tmp_path / "actual_security_conversion_evidence.json"
    source.write_text(json.dumps({"fixture": "explicit credit allocation, no price-ratio inference"}),
                      encoding="utf-8")
    prior = DAYS[DAYS.index(effective_day)-1]
    receipt = SecurityConversionReceipt(CONTEXT, "synthetic_exchange_credit_1", effective_day, A, B,
        sum(lot["qty"] for lot in original),
        tuple(ConversionCredit(lot["lot_id"], lot["qty"], 2*lot["qty"], available_on) for lot in original),
        123, str(source.resolve()), hashlib.sha256(source.read_bytes()).hexdigest(),
        prior+"T18:10:00+08:00", prior+"T18:11:00+08:00")
    return receipt, view((receipt,))


def test_conversion_cash_and_locked_credits_are_not_fills_then_shared_engine_sells_once(tmp_path):
    base, state = seeded()
    receipt, current = conversion(tmp_path, state)
    old_fills, old_orders, old_cash = deepcopy(state["fills"]), deepcopy(state["orders"]), state["cash"]
    result = step(state, base, F, {}, conversion_view=current)
    book = Ledger(state, CONFIG, CONTEXT)
    assert book.quantity(A) == 0 and book.quantity(B) == 1600
    assert book.available(B, F) == 0 and book.available(B, G) == 1600
    assert state["cash"] == old_cash + 123
    assert state["fills"] == old_fills and state["orders"] == old_orders
    assert result["new_orders"] == 0 and holding_origin(state, B) == E
    assert state["security_conversion_applied"][receipt.event_id] == receipt.to_dict()
    assert state["security_conversion_binding"]["method_id"] == METHOD_ID
    assert state["prices"][B] == 500
    assert all(lot["origin_symbol"] == A and lot["origin_day"] == E
               for lot in state["positions"][B])
    structure = LeaderTypeRiskExitPolicy("rebound_repair_candidate",
        candidate_by_day={D: [{"symbol": A}]}, feature_by_day={}).evaluate(F, book, base, B)
    assert structure["status"] == "unknown" and structure["reasons"] == ["conversion_structure_not_rebased"]
    restored = json.loads(json.dumps(state))
    sold = step(restored, base, G, conversion_view=current)
    assert sold["new_orders"] == 1 and Ledger(restored, CONFIG, CONTEXT).quantity(B) == 0
    assert sum(fill["qty"] for fill in restored["fills"].values() if fill["side"] == "sell") == 1600
    assert len(restored["security_conversion_applied"]) == 1
    checkpoint = deepcopy(restored)
    with pytest.raises(ValueError):
        step(restored, base, G, conversion_view=current)
    assert restored == checkpoint


def test_pending_published_target_maps_to_destination_without_rewriting_actual_publication(tmp_path):
    base, state = seeded(e_weights={A: .8})
    original = deepcopy(state["pending_published_close_intent"])
    receipt, current = conversion(tmp_path, state)
    result = step(state, base, F, conversion_view=current)
    adjusted = result["consumed_published_close_intent"]
    assert adjusted["schema"] == "security_conversion_adjusted_published_target_v1"
    assert adjusted["original_receipt"] == original
    assert adjusted["original_targets"] == {A: .8} and adjusted["adjusted_targets"] == {B: .8}
    assert adjusted["conversion_event_ids"] == [receipt.event_id]
    diagnostics = [event for event in result["events"] if event["kind"] == "target_execution_diagnostic"]
    assert any(event["symbol"] == B and event["target_weight"] == .8 for event in diagnostics)
    assert not any(event["symbol"] == A for event in diagnostics)
    assert len(state["orders"]) == 1 and Ledger(state, CONFIG, CONTEXT).quantity(B) == 1600


def test_queued_risk_and_residual_exit_remap_retaining_request_and_origin(tmp_path):
    class Exit(Risk):
        def __call__(self, day, book, base):
            return (engine.ReplayRiskExitIntent(CONTEXT, E, F, (A,), "actual_independent_risk", "fixed_expiry")
                    if day == E else None)
    risk = Exit()
    base = feed()
    base.prior_volume_policy = PriorVolumeReplayPolicy(CONTEXT)
    base, state = seeded(base, e_weights={}, risk=risk, lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    receipt, current = conversion(tmp_path, state)
    original_risk = deepcopy(state["pending_risk_exits"])
    result = step(state, base, F, conversion_view=current, risk=risk,
                  lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    executed = [event for event in result["events"] if event["kind"] == "risk_exit_execution"]
    assert len(executed) == 1 and executed[0]["symbols"] == [B]
    assert executed[0]["decided_on"] == original_risk["decided_on"] == E
    assert executed[0]["source"] == original_risk["source"]
    residual = state["full_exit_lifecycle"]["remaining_full_exits"]
    assert A not in residual and residual[B]["remaining_quantity"] == 1600
    assert residual[B]["requested_on"] == E and residual[B]["risk_reason"] == "fixed_expiry"
    assert not any(order["day"] == F for order in state["orders"].values())
    state = json.loads(json.dumps(state))
    step(state, base, G, conversion_view=current, risk=risk,
         lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 0
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}


def test_missing_receipt_keeps_old_delisting_failure_without_inventing_zero_cash_or_price():
    base, state = seeded()
    before = deepcopy(state)
    with pytest.raises(ValueError, match="delisted"):
        step(state, base, F)
    assert state == before and Ledger(state, CONFIG, CONTEXT).quantity(A) == 800
    assert not state.get("security_conversion_applied")


def test_existing_blocked_full_exit_survives_conversion_until_explicit_share_availability(tmp_path):
    class Exit(Risk):
        def __call__(self, day, book, base):
            return (engine.ReplayRiskExitIntent(CONTEXT, F, G, (A,), "original_expiry_queue", "fixed_expiry")
                    if day == F else None)
    base, risk = feed(), Exit()
    base.prior_volume_policy = PriorVolumeReplayPolicy(CONTEXT)
    base.instruments[A]["outDate"] = G
    base.rows[F][A] = {**base.rows[E][A], "tradestatus": 0, "volume": 0., "amount": 0.}
    base, state = seeded(base, e_weights={}, risk=risk, lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    step(state, base, F, risk=risk, lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    original = deepcopy(state["full_exit_lifecycle"]["remaining_full_exits"][A])
    assert original["requested_on"] == E and original["remaining_quantity"] == 800
    assert state["pending_risk_exits"]["symbols"] == [A]
    receipt, current = conversion(tmp_path, state, effective_day=G, available_on=H)
    step(state, base, G, conversion_view=current, risk=risk,
         lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    remaining = state["full_exit_lifecycle"]["remaining_full_exits"]
    assert A not in remaining and remaining[B]["remaining_quantity"] == 1600
    assert remaining[B]["requested_on"] == original["requested_on"]
    assert remaining[B]["reason"] == original["reason"] and remaining[B]["source"] == original["source"]
    assert not any(order["day"] == G for order in state["orders"].values())
    step(state, base, H, conversion_view=current, risk=risk,
         lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    assert not state["full_exit_lifecycle"]["remaining_full_exits"]
    assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 0


@pytest.mark.parametrize("fault", ["missing_destination", "invalid_destination_close", "source_tampered"])
def test_bad_conversion_session_rolls_back_entire_account(tmp_path, fault):
    base, state = seeded(e_weights={A: .8})
    receipt, current = conversion(tmp_path, state)
    if fault == "missing_destination":
        base.rows[F].pop(B)
    elif fault == "invalid_destination_close":
        base.rows[F][B]["close"] = float("nan")
    else:
        from pathlib import Path
        Path(receipt.source_file).write_text("changed original evidence", encoding="utf-8")
    before = deepcopy(state)
    with pytest.raises(ValueError):
        step(state, base, F, conversion_view=current)
    assert state == before


def test_view_cannot_be_added_to_old_account_dropped_or_replaced_after_restore(tmp_path):
    base = feed()
    old = new_state(CONFIG.initial_cash)
    engine.advance(old, base, D, CONFIG, 0, context=CONTEXT, published_close_intent=publication(D, {A: .8}))
    before = deepcopy(old)
    with pytest.raises(ValueError):
        step(old, base, E)
    assert old == before
    base, state = seeded()
    receipt, current = conversion(tmp_path, state)
    step(state, base, F, conversion_view=current)
    state = json.loads(json.dumps(state))
    before = deepcopy(state)
    with pytest.raises(ValueError):
        engine.advance(state, base, G, CONFIG, 3, context=CONTEXT,
                       published_close_intent=publication(G, None))
    assert state == before
    with pytest.raises(ValueError):
        step(state, base, G, conversion_view=view())
    assert state == before


def test_split_settlement_and_later_publication_reuse_converted_holdings_without_repeated_fill(tmp_path):
    base, state = seeded(split=True)
    receipt, current = conversion(tmp_path, state)
    row = engine.advance(state, base, F, CONFIG, 2, context=CONTEXT,
        security_conversion_view=current, published_close_session_method=SPLIT_METHOD,
        published_close_target_policy=Target(), replay_risk_exit_policy=Risk())
    assert row["new_orders"] == 0 and state["last_published_close_session"]["status"] == "awaiting_publication"
    before = deepcopy(state)
    attached = attach_close(state, base, F, publication(F, {}), CONFIG, 2, context=CONTEXT,
        published_close_target_policy=Target(), replay_risk_exit_policy=Risk())
    assert attached["new_orders"] == attached["new_fills"] == 0
    for field in ("cash", "positions", "orders", "fills", "security_conversion_applied"):
        assert state[field] == before[field]
    step(state, base, G, conversion_view=current, split=True)
    assert Ledger(state, CONFIG, CONTEXT).quantity(B) == 0
    assert len([order for order in state["orders"].values() if order["side"] == "sell"]) == 1


def test_default_legacy_path_is_unchanged_and_untracked():
    base, first, second = feed(), new_state(CONFIG.initial_cash), new_state(CONFIG.initial_cash)
    for number, day in enumerate((D, E)):
        left = engine.advance(first, base, day, CONFIG, number, context=CONTEXT,
                              published_close_intent=publication(day, {A: .8} if day == D else None))
        right = engine.advance(second, base, day, CONFIG, number, context=CONTEXT,
            published_close_intent=publication(day, {A: .8} if day == D else None),
            security_conversion_view=None)
        assert first == second and left == right
    assert not any(key.startswith("security_conversion") for key in first)
    assert all("lot_id" not in lot for lot in first["positions"][A])


def test_conversion_cannot_skip_an_unsettled_calendar_session_then_resumes_exact_next(tmp_path):
    base, state = seeded()
    late_receipt, late_view = conversion(tmp_path, state, effective_day=G, available_on=G)
    before = deepcopy(state)
    # A's outDate is F. Jumping directly from E to G must not bypass F's
    # unresolved holding and retrospectively replace it with the G credit.
    assert base.instruments[A]["outDate"] == F
    with pytest.raises(ValueError, match="exact next|skip"):
        step(state, base, G, conversion_view=late_view)
    assert state == before and state["day"] == E
    assert late_receipt.event_id not in state.get("security_conversion_applied", {})
    # A separately explicit F receipt can settle the actual next session;
    # restoring its saved state still permits the following G exactly once.
    receipt, current = conversion(tmp_path, state, effective_day=F, available_on=G)
    step(state, base, F, conversion_view=current)
    restored = json.loads(json.dumps(state))
    step(restored, base, G, conversion_view=current)
    assert restored["day"] == G
    assert restored["security_conversion_applied"] == {receipt.event_id: receipt.to_dict()}
    assert Ledger(restored, CONFIG, CONTEXT).quantity(B) == 1600
