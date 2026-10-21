"""Explicit router post-open input and recovery through the existing Engine."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from trading import engine
from trading.contracts import IntentKind, PortfolioIntent
from trading.published_close_router import PublishedCloseRouterEnvelope, POLICY_ID, RECEIPT_SCHEMA
from trading.research.tests.test_leader_type_router import fixture, A, B, DAYS, CONFIG, STATE_KEY, TYPE_IDS, _quantity


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat("2026-11-30T20:00:00+08:00"))


def published(router, state, index, **kwargs):
    original = deepcopy(state)
    envelope = router.publication_envelope(state, DAYS[index], index,
        published_at=DAYS[index] + "T19:00:00+08:00", source="saved_router:synthetic:" + DAYS[index], **kwargs)
    assert state == original
    restored = PublishedCloseRouterEnvelope.from_dict(json.loads(json.dumps(envelope.to_dict())))
    assert restored == envelope
    result = router.step_published(state, DAYS[index], index, envelope=restored, **kwargs)
    assert state == original
    return result, envelope


def test_D_only_publication_then_checkpoint_exact_E_open_T1_and_source_NC_preserves_actual_fill():
    router, state, _, _, _ = fixture(historical=True)
    following_rows = router.base.rows.pop(DAYS[1])
    router.source_days = {DAYS[0]}
    (state, row, audit), envelope = published(router, state, 0)
    assert envelope.original_intent.weights == ((A, .16),)
    assert not state["orders"] and state["pending_targets"] == {A: .16}
    assert row["event_time_frontier"] is None and row["published_close_router"]["published_at"].endswith("19:00:00+08:00")
    assert state["pending_published_close_intent"]["schema"] == RECEIPT_SCHEMA
    assert state[STATE_KEY]["active_type"] == TYPE_IDS[0]
    state = json.loads(json.dumps(state))
    router.base.rows[DAYS[1]] = following_rows
    router.source_days.add(DAYS[1])
    (state, row, audit), envelope = published(router, state, 1)
    assert envelope.original_intent.kind == IntentKind.NO_CHANGE
    assert _quantity(state, A) > 0 and state["pending_targets"] is None
    assert {fill["available_day"] for fill in state["fills"].values()} == {DAYS[2]}
    assert row["consumed_published_close_intent"]["schema"] == RECEIPT_SCHEMA
    assert "pending_published_close_intent" not in state
    assert audit["close_rule"] == "old_pending_actual_fill_preserve_exact_shares"


def test_source_NC_still_becomes_known_D_basket_empty_FULL_with_E_quote_unknown():
    router, state, panels, _, _ = fixture(historical=True)
    for index in (0, 1):
        (state, _, _), _ = published(router, state, index)
    panels[DAYS[2]]["types"][0]["status"] = "unknown"
    panels[DAYS[2]]["risk_features"][A]["qualified_limit_up"] = False
    router.base.rows.pop(DAYS[3])
    router.source_days = set(DAYS[:3])
    (state, row, audit), envelope = published(router, state, 2)
    assert envelope.original_intent.kind == IntentKind.NO_CHANGE
    assert state["pending_targets"] == {} and state[STATE_KEY]["exit_sticky"] is True
    assert row["published_close_router"]["intent"]["kind"] == "FULL_TARGET"
    assert any(event["kind"] == "risk_exit_deferred" for event in row["events"])
    assert len([event for event in row["events"] if event["kind"] == "leader_type_risk_assessed"]) == 1
    assert audit["close_rule"] == "sticky_owner_basket_empty_FULL"


def test_actual_publication_IPO_clock_is_separate_from_legacy_D_1505_cutoff():
    router, initial, _, _, _ = fixture(historical=True)
    router.qualification.records[A]["available_at"] = DAYS[0] + "T18:00:00+08:00"
    state, _, audit = router.step(initial, DAYS[0], 0)
    assert state["pending_targets"] is None and audit["status"] == "NO_CHANGE"
    (state, row, audit), envelope = published(router, initial, 0)
    assert envelope.original_intent.weights == ((A, .16),) and state["pending_targets"] == {A: .16}
    assert audit["selected_checks"]["IPO_evidence"][0]["decision_as_of"] == DAYS[0] + "T19:00:00+08:00"


def test_router_clock_caps_identity_old_overlay_and_pending_tamper_refuse_before_open():
    router, state, _, _, _ = fixture(historical=True)
    envelope = router.publication_envelope(state, DAYS[0], 0,
        published_at=DAYS[0] + "T19:00:00+08:00", source="synthetic_router")
    for change in ({"published_at": DAYS[0] + "T14:59:00+08:00"},
                   {"published_at": DAYS[1] + "T09:30:00+08:00"}, {"policy_id": "other"},
                   {"original_intent": replace(envelope.original_intent, weights=((A, .2),))}):
        with pytest.raises(ValueError):
            replace(envelope, **change)
    before = deepcopy(state)
    callback = lambda value, book, feed, config: value.original_intent
    callback.policy_id = POLICY_ID
    with pytest.raises(ValueError, match="legacy target/close overlays"):
        engine.advance(state, router.base, DAYS[0], CONFIG, 0, context=router.context,
            published_close_router=envelope, published_close_router_policy=callback,
            published_close_target_policy=lambda value: value)
    assert state == before
    (state, _, _), _ = published(router, state, 0)
    envelope = router.publication_envelope(state, DAYS[1], 1,
        published_at=DAYS[1] + "T19:00:00+08:00", source="next_router")
    for mutate in (lambda value: value["pending_targets"].update({A: .15}),
                   lambda value: value["pending_published_close_intent"]["envelope"].update(policy_id="wrong")):
        broken = deepcopy(state)
        mutate(broken)
        before = deepcopy(broken)
        with pytest.raises(ValueError):
            router.step_published(broken, DAYS[1], 1, envelope=envelope)
        assert broken == before and not broken["orders"]
    before = deepcopy(state)
    with pytest.raises(ValueError, match="explicit envelope and callback"):
        engine.advance(state, router.base, DAYS[1], CONFIG, 1, context=router.context)
    assert state == before


def test_NC_callback_cannot_create_positive_candidate_and_actual_owner_exposure_is_bound():
    router, state, panels, _, _ = fixture(historical=True)
    panels[DAYS[0]]["types"][0]["status"] = "unknown"
    envelope = router.publication_envelope(state, DAYS[0], 0,
        published_at=DAYS[0] + "T19:00:00+08:00", source="unknown_router")
    assert envelope.original_intent.kind == IntentKind.NO_CHANGE
    def callback(value, book, feed, config):
        return replace(value.original_intent, kind=IntentKind.FULL_TARGET, weights=((A, .16),))
    callback.policy_id = POLICY_ID
    attempted = deepcopy(state)
    with pytest.raises(ValueError, match="original fixed candidate slots"):
        engine.advance(attempted, router.base, DAYS[0], CONFIG, 0, context=router.context,
            published_close_router=envelope, published_close_router_policy=callback)
    assert not state["orders"] and state["day"] is None
    broken = replace(envelope, owner_before=TYPE_IDS[1])
    with pytest.raises(ValueError, match="original owner exposure"):
        engine.advance(state, router.base, DAYS[0], CONFIG, 0, context=router.context,
            published_close_router=broken, published_close_router_policy=callback)
    assert not state["orders"] and state["day"] is None
