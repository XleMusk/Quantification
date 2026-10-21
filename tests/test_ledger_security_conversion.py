"""Nontrade lot conversion mechanics; synthetic sources, no market returns."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, SECURITY_CONVERSION_METHOD, new_state
from trading.research.leader_published_risk_exit import PublishedCloseLeaderTypeRiskExitPolicy
from trading.research.leader_type_risk_exit import surviving_buy_anchor
from trading.research.shortline_decision_v2 import holding_origin
from trading.security_conversion import ConversionCredit, SecurityConversionReceipt


A, B = "600001.SSE", "600002.SSE"
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
        "2026-10-16", "2026-10-19", "2026-10-20"]
CONFIG = StrategyConfig()
CONTEXT = RunContext("synthetic_conversion_ledger", TradingMode.REPLAY)


def account(*, enabled=True):
    state = new_state(CONFIG.initial_cash)
    if enabled:
        state["security_conversion_binding"] = {"schema_version": 1,
            "method_id": SECURITY_CONVERSION_METHOD, "registry_id": "synthetic_registry",
            "context": CONTEXT.to_dict(), "config": CONFIG.to_dict(), "knowledge_mode": "dated_asof"}
    book = Ledger(state, CONFIG, CONTEXT)
    for index, quantity in ((1, 100), (2, 200)):
        order = f"buy{index}"
        book.submit(order, DAYS[index], A, "buy", quantity, 1000)
        book.fill(f"fill{index}", order, quantity, 1000, DAYS[index], DAYS[index + 1])
    state["day"] = DAYS[2]
    return book


def receipt(tmp_path, **changes):
    source = tmp_path / "conversion_source.txt"
    source.write_bytes(b"synthetic nontrade conversion settlement statement")
    values = dict(context=CONTEXT, event_id="conversion1", effective_day=DAYS[3],
        source_symbol=A, destination_symbol=B, registered_quantity=300,
        credits=(ConversionCredit("fill1", 100, 200, DAYS[5]),
                 ConversionCredit("fill2", 200, 400, DAYS[5])), cash_cents=123,
        source_file=str(source), source_sha256=sha256(source.read_bytes()).hexdigest(),
        source_available_at=DAYS[2] + "T18:00:00+08:00",
        recorded_at=DAYS[2] + "T18:01:00+08:00")
    return SecurityConversionReceipt(**{**values, **changes})


def test_atomic_conversion_preserves_trades_and_original_age_with_new_locked_lots(tmp_path):
    book = account()
    old = deepcopy(book.state)
    event = receipt(tmp_path)
    assert book.apply_security_conversion(event) is True
    assert book.quantity(A) == 0 and book.quantity(B) == 600
    assert book.state["cash"] == old["cash"] + 123
    assert book.available(B, DAYS[4]) == 0 and book.available(B, DAYS[5]) == 600
    assert book.state["orders"] == old["orders"] and book.state["fills"] == old["fills"]
    assert book.state["day"] == old["day"]
    assert holding_origin(book.state, B) == DAYS[1]
    assert book.state["security_conversion_applied"][event.event_id] == event.to_dict()
    record = book.state["security_conversion_records"][event.event_id]
    assert record["before_source_lots"] == old["positions"][A]
    assert record["after_destination_lots"] == book.state["positions"][B]
    assert book.state["events"][-1]["nontrade_event"] is True


def test_same_event_is_idempotent_and_payload_or_source_changes_are_atomic_failures(tmp_path):
    book, event = account(), receipt(tmp_path)
    book.apply_security_conversion(event)
    saved = deepcopy(book.state)
    assert book.apply_security_conversion(event) is False
    assert book.state == saved
    with pytest.raises(ValueError, match="Conflicting"):
        book.apply_security_conversion(replace(event, cash_cents=124))
    assert book.state == saved
    from pathlib import Path
    Path(event.source_file).write_bytes(b"different statement")
    with pytest.raises(ValueError):
        book.apply_security_conversion(event)
    assert book.state == saved


@pytest.mark.parametrize("failure", ["quantity", "coverage", "availability", "active_order", "collision"])
def test_partial_inconsistent_or_early_unlocked_receipt_never_changes_account(tmp_path, failure):
    book, event = account(), receipt(tmp_path)
    if failure == "quantity":
        event = replace(event, registered_quantity=301,
            credits=(replace(event.credits[0], source_quantity=101), event.credits[1]))
    elif failure == "coverage":
        event = replace(event, credits=(ConversionCredit("otherfill", 100, 200, DAYS[5]), event.credits[1]))
    elif failure == "availability":
        # New credit is after conversion day but before this source lot unlocks.
        book.state["positions"][A][1]["available"] = DAYS[5]
        book.state["fills"]["fill2"]["available_day"] = DAYS[5]
        event = replace(event, credits=(event.credits[0], replace(event.credits[1], available_on=DAYS[4])))
    elif failure == "active_order":
        book.submit("unresolved", DAYS[2], A, "buy", 100, 1000)
    else:
        book.submit("existing_destination", DAYS[2], B, "buy", 100, 1000)
        book.fill("destinationfill", "existing_destination", 100, 1000, DAYS[2], DAYS[3])
    saved = deepcopy(book.state)
    with pytest.raises(ValueError):
        book.apply_security_conversion(event)
    assert book.state == saved


def test_fifo_sales_retain_surviving_actual_fill_origin_and_structure_is_unknown(tmp_path):
    book, event = account(), receipt(tmp_path)
    book.apply_security_conversion(event)
    book.submit("sold_first_credit", DAYS[5], B, "sell", 200, 1000)
    book.fill("actualsell", "sold_first_credit", 200, 1000, DAYS[5], DAYS[6])
    assert holding_origin(book.state, B) == DAYS[1]
    anchor, reason = surviving_buy_anchor(book.state, B, DAYS, DAYS[5])
    assert reason is None and anchor["fill_id"] == "fill2"
    assert anchor["origin_symbol"] == A and anchor["surviving_qty"] == 400
    policy = PublishedCloseLeaderTypeRiskExitPolicy("trend_leader", candidate_by_day={}, feature_by_day={})
    feed = SimpleNamespace(calendar=DAYS, rows={})
    result = policy.evaluate(DAYS[5], book, feed, B)
    assert result["status"] == "unknown" and result["reasons"] == ["conversion_structure_not_rebased"]
    assert len(book.state["fills"]) == 3  # The conversion did not become a buy.


def test_continuous_age_survives_oldest_fifo_sale_and_conversion_then_resets_only_when_flat(tmp_path):
    book = account()
    book.submit("sell_oldest_source", DAYS[3], A, "sell", 100, 1000)
    book.fill("sold_source", "sell_oldest_source", 100, 1000, DAYS[3], DAYS[4])
    # This is still one continuous exposure from fill1 although only fill2 survives.
    assert holding_origin(book.state, A) == DAYS[1]
    assert surviving_buy_anchor(book.state, A, DAYS, DAYS[3])[0]["fill_day"] == DAYS[2]
    event = receipt(tmp_path, effective_day=DAYS[4], registered_quantity=200,
        credits=(ConversionCredit("fill2", 200, 400, DAYS[5]),))
    assert book.apply_security_conversion(event)
    assert holding_origin(book.state, B) == DAYS[1]
    record = book.state["security_conversion_records"][event.event_id]
    assert record["before_holding_origin"] == record["after_holding_origin"] == {
        "origin_day": DAYS[1], "origin_fill_id": "fill1", "origin_symbol": A}
    row = {"open": 10., "high": 10.1, "low": 9.9, "close": 10., "preclose": 10.,
        "volume": 1_000_000., "amount": 10_000_000., "tradestatus": 1, "isST": 0, "adjustflag": "3"}
    feed = SimpleNamespace(calendar=DAYS, rows={day: {B: row} for day in DAYS})
    policy = PublishedCloseLeaderTypeRiskExitPolicy("trend_leader", candidate_by_day={}, feature_by_day={})
    assert policy(DAYS[5], book, feed).symbols == (B,)  # Five sessions from original fill1, not four from fill2.
    book.submit("flat_destination", DAYS[5], B, "sell", 400, 1000)
    book.fill("flat", "flat_destination", 400, 1000, DAYS[5], DAYS[6])
    assert holding_origin(book.state, B) is None
    assert B not in book.state["security_conversion_holding_origins"]
    book.submit("fresh_destination", DAYS[5], B, "buy", 100, 1000)
    book.fill("fresh", "fresh_destination", 100, 1000, DAYS[5], DAYS[6])
    assert holding_origin(book.state, B) == DAYS[5]
    assert book.state["security_conversion_holding_origins"][B]["origin_fill_id"] == "fresh"


def test_conversion_does_not_reset_independent_five_session_expiry(tmp_path):
    book, event = account(), receipt(tmp_path)
    book.apply_security_conversion(event)
    row = {"open": 10., "high": 10.1, "low": 9.9, "close": 10., "preclose": 10.,
        "volume": 1_000_000., "amount": 10_000_000., "tradestatus": 1, "isST": 0, "adjustflag": "3"}
    feed = SimpleNamespace(calendar=DAYS, rows={day: {B: row} for day in DAYS})
    policy = PublishedCloseLeaderTypeRiskExitPolicy("trend_leader", candidate_by_day={}, feature_by_day={})
    pending = policy(DAYS[5], book, feed)
    assert pending.symbols == (B,) and pending.execution_on == DAYS[6]


def test_untracked_converted_lot_is_unknown_and_default_accounts_keep_original_lot_shape(tmp_path):
    legacy = account(enabled=False)
    assert legacy.state["positions"][A] == [{"qty": 100, "available": DAYS[2]},
                                           {"qty": 200, "available": DAYS[3]}]
    assert holding_origin(legacy.state, A) == DAYS[1]
    before = deepcopy(legacy.state)
    with pytest.raises(ValueError, match="bound"):
        legacy.apply_security_conversion(receipt(tmp_path))
    assert legacy.state == before
    book = account()
    book.apply_security_conversion(receipt(tmp_path))
    del book.state["positions"][B][0]["origin_fill_id"]
    with pytest.raises(ValueError, match="lineage"):
        holding_origin(book.state, B)
    assert surviving_buy_anchor(book.state, B, DAYS, DAYS[5])[1] == "actual_conversion_lot_lineage_unknown"
