import json
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import (ExecutionEvent, IntentKind, OrderIntent, PortfolioIntent, Price, PriceBasis,
                               RunContext, SecurityId, ShareQuantity, Signal, SignalFrame, SignalKind,
                               SignalTime, TradingMode)
from trading.ledger import Ledger, new_state
from trading.signals import frame_from_qlib


CONTEXT = RunContext("test-account", TradingMode.REPLAY)
SECURITY = SecurityId("000001.SZSE")
DAY = "2024-01-02"


def wire(value):
    return json.loads(json.dumps(value.to_dict(), allow_nan=False))


def order():
    return OrderIntent("order-one", CONTEXT, DAY, SECURITY, "buy", ShareQuantity(100),
                       Price(1000, PriceBasis.RAW), "target_rebalance")


def test_versioned_contracts_round_trip_through_real_ledger():
    request = OrderIntent.from_dict(wire(order()))
    book = Ledger(new_state(10000), StrategyConfig(), CONTEXT)
    assert book.submit_intent(request)["status"] == "submitted"
    assert book.submit_intent(request)["id"] == request.id
    fill = ExecutionEvent("fill-one", request.id, CONTEXT, DAY, SECURITY, "buy",
                          ShareQuantity(100), Price(1000, PriceBasis.RAW), "2024-01-03")
    restored = ExecutionEvent.from_dict(wire(fill))
    assert restored == fill and book.apply_execution(restored)
    assert not book.apply_execution(restored)
    assert book.quantity(SECURITY.symbol) == 100 and book.available(SECURITY.symbol, DAY) == 0
    assert book.state["cash"] == 899499


@pytest.mark.parametrize("value", [CONTEXT, SECURITY, Price(1000, PriceBasis.RAW), ShareQuantity(100),
                                    SignalTime(DAY + "T15:00:00+08:00", DAY + "T07:05:00Z"), order()])
def test_versions_are_read_and_future_schemas_fail_closed(value):
    payload = wire(value)
    assert type(value).from_dict(payload) == value
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="schema"):
        type(value).from_dict(payload)


def test_security_preserves_leading_zero_and_does_not_infer_exchange():
    assert wire(SECURITY)["code"] == "000001"
    with pytest.raises(ValueError):
        SecurityId("000001")
    payload = wire(SECURITY)
    payload["code"] = 1
    with pytest.raises(ValueError):
        SecurityId.from_dict(payload)


@pytest.mark.parametrize("value", [True, 100.0, 0, -100])
def test_share_quantity_rejects_ambiguous_or_invalid_numbers(value):
    with pytest.raises(ValueError):
        ShareQuantity(value)


def test_units_and_adjusted_order_prices_fail_closed():
    payload = wire(order())
    payload["quantity"]["unit"] = "lot"
    with pytest.raises(ValueError, match="unit"):
        OrderIntent.from_dict(payload)
    payload = wire(order())
    payload["limit_price"]["unit"] = "yuan"
    with pytest.raises(ValueError, match="unit"):
        OrderIntent.from_dict(payload)
    with pytest.raises(ValueError, match="unadjusted"):
        replace(order(), limit_price=Price(1000, PriceBasis.FORWARD_ADJUSTED))
    with pytest.raises(ValueError):
        replace(order(), quantity=Price(100, PriceBasis.RAW))


def test_account_mode_and_fill_identity_checked_before_mutation():
    book = Ledger(new_state(10000), StrategyConfig(), CONTEXT)
    before = deepcopy(book.state)
    for context in (RunContext("other-account", TradingMode.REPLAY), RunContext(CONTEXT.account_id, TradingMode.PAPER)):
        with pytest.raises(ValueError, match="account/mode"):
            book.submit_intent(replace(order(), context=context))
        assert book.state == before
    book.submit_intent(order())
    before = deepcopy(book.state)
    fill = ExecutionEvent("fill-one", order().id, CONTEXT, DAY, SecurityId("600000.SSE"), "buy",
                          ShareQuantity(100), Price(1000, PriceBasis.RAW), "2024-01-03")
    with pytest.raises(ValueError, match="security/side"):
        book.apply_execution(fill)
    assert book.state == before
    with pytest.raises(ValueError):
        RunContext("real-account", "live")


def test_signal_times_keep_unknown_source_times_and_gate_future_inputs():
    stamp = SignalTime(DAY + "T15:00:00+08:00", DAY + "T15:05:00+08:00",
                       availability_evidence="historical_eod_assumption")
    restored = SignalTime.from_dict(wire(stamp))
    assert restored.published_at is None and restored.received_at is None and restored.computed_at is None
    with pytest.raises(ValueError, match="offset-aware"):
        replace(stamp, available_at=DAY + "T15:05:00")
    with pytest.raises(ValueError, match="computation"):
        replace(stamp, computed_at=DAY + "T15:06:00+08:00")
    signal = Signal(SECURITY, .2, SignalKind.SCORE, stamp)
    with pytest.raises(ValueError, match="unavailable"):
        SignalFrame(DAY, (replace(signal, time=replace(stamp, available_at=DAY + "T15:06:00+08:00")),), "test", CONTEXT)


def test_signal_frame_roundtrip_preserves_diagnostics_and_semantics():
    rows = [{"symbol": SECURITY.symbol, "score": .2, "momentum": .1, "volatility": .5}]
    batch = SignalFrame.from_legacy(rows, DAY, "rule:momentum", CONTEXT)
    restored = SignalFrame.from_dict(wire(batch))
    assert restored == batch and restored.ranked_candidates() == rows
    assert restored.signals[0].time.availability_evidence == "historical_eod_assumption"
    with pytest.raises(ValueError, match="prediction horizon"):
        replace(batch.signals[0], kind=SignalKind.EXPECTED_RETURN)
    returns = replace(batch, signals=(replace(batch.signals[0], kind=SignalKind.EXPECTED_RETURN, horizon_sessions=5),))
    with pytest.raises(ValueError, match="explicit portfolio policy"):
        returns.ranked_candidates()
    with pytest.raises(ValueError, match="Duplicate"):
        replace(batch, signals=batch.signals * 2)


def test_model_timestamps_stay_with_sorted_security_and_not_guessed():
    frame = pd.DataFrame([
        {"datetime": DAY, "instrument": "SH600000", "score": .1, "available_at": DAY + "T15:04:00+08:00"},
        {"datetime": DAY, "instrument": "SZ000001", "score": .2, "available_at": DAY + "T07:01:00Z"},
    ])
    batch = frame_from_qlib(frame, DAY, CONTEXT)
    assert batch.signals[0].security == SECURITY
    assert batch.signals[0].time.available_at == DAY + "T07:01:00+00:00"
    assert batch.signals[0].time.computed_at is None
    frame.loc[0, "available_at"] = DAY + "T15:04:00"
    with pytest.raises(ValueError, match="offset-aware"):
        frame_from_qlib(frame, DAY, CONTEXT)


def test_target_roundtrip_distinguishes_empty_missing_and_delta():
    for intent in (PortfolioIntent.full_target({}), PortfolioIntent.no_change("missing"), PortfolioIntent.delta({SECURITY.symbol: -.1})):
        assert PortfolioIntent.from_dict(wire(intent)) == intent
    assert PortfolioIntent.from_legacy_pending(None).kind == IntentKind.NO_CHANGE
    assert PortfolioIntent.from_legacy_pending({}).kind == IntentKind.FULL_TARGET
    with pytest.raises(ValueError, match="not supported"):
        PortfolioIntent.delta({SECURITY.symbol: -.1}).to_legacy_pending()
    with pytest.raises(ValueError, match="decision day"):
        PortfolioIntent.full_target({}, decided_at="2024-01-02T23:00:00Z", execution_not_before="2024-01-03")
    with pytest.raises(ValueError, match="portfolio equity"):
        PortfolioIntent.full_target({SECURITY.symbol: .8, "600000.SSE": .8})


def test_optional_unknown_fields_stay_unknown_but_target_payload_is_required():
    stamp = SignalTime(DAY + "T15:00:00+08:00", DAY + "T15:05:00+08:00")
    payload = wire(stamp)
    for key in ("published_at", "received_at", "computed_at"):
        del payload[key]
    assert SignalTime.from_dict(payload) == stamp
    target = wire(PortfolioIntent.full_target({}))
    for key in ("context", "decided_at", "execution_not_before", "source"):
        del target[key]
    assert PortfolioIntent.from_dict(target).context is None
    del target["weights"]
    with pytest.raises(KeyError):
        PortfolioIntent.from_dict(target)
