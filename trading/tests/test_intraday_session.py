"""The explicit opening/closing session reuses the simulated economic path."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json

import pytest

from trading import engine, intraday_session
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.tests.test_preopen_intent import CONTEXT, envelope, feed
from trading.tests.test_published_close_intent import A, B, CONFIG, D, E, F


@pytest.fixture(autouse=True)
def actual_clock(monkeypatch):
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(F + "T20:00:00+08:00"))


def phase_feed(base, day, *, kind="synthetic_fixture"):
    base.intraday_phase_binding = {"schema_version": 1, "source_id": "fixture-fixed-source",
                                   "quote_source_kind": kind}
    base.intraday_opening_source_ref = "fixture:" + day + ":opening"
    base.intraday_closing_source_ref = "fixture:" + day + ":closing"
    base.intraday_opening_observed_at = day + "T09:30:00+08:00"
    base.intraday_closing_observed_at = day + "T15:00:00+08:00"
    base.validate_opening_session = lambda state, session: None
    base.validate_closing_session = lambda state, session: None
    return base


def open_day(state, base, day=E, publication=None, *, context=CONTEXT, config=CONFIG, **changes):
    options = dict(context=context, opened_at=day + "T09:30:00+08:00",
                   source_ref=base.intraday_opening_source_ref)
    options.update(changes)
    return intraday_session.opening(state, base, day, config,
        envelope(context=context) if publication is None else publication, **options)


def close_day(state, base, day=E, *, context=CONTEXT, config=CONFIG, **changes):
    options = dict(context=context, closed_at=day + "T15:00:00+08:00",
                   source_ref=base.intraday_closing_source_ref)
    options.update(changes)
    return intraday_session.closing(state, base, day, config, **options)


def background(*, held=False):
    state, base = new_state(9000 if held else 10000), phase_feed(feed(), D)
    if held:
        state["positions"][A] = [{"qty": 100, "available": D}]
        state["prices"][A] = 1000
    original = envelope(source_day="2025-01-03", execution_day=D, no_change=True)
    open_day(state, base, D, original)
    close_day(state, base, D)
    phase_feed(base, E)
    base.calls.clear()
    return state, base


def test_two_phases_match_existing_preopen_economic_account_with_sell_buy_fees_and_T1():
    split, base = background(held=True)
    # The same economic prefix may be replayed by the unchanged original path;
    # only opt-in intraday method metadata is removed from this independent copy.
    original = {key: deepcopy(value) for key, value in split.items() if not key.startswith("intraday_")}
    legacy_feed = feed()
    base.rows[E][B]["close"] = legacy_feed.rows[E][B]["close"] = 10.2
    legacy = engine.advance(original, legacy_feed, E, CONFIG, 1,
                            context=CONTEXT, preopen_intent=envelope())
    open_day(split, base)
    assert split["day"] == D and split.get("intraday_open_session")
    book = Ledger(split, CONFIG, CONTEXT)
    assert book.quantity(A) == 0 and book.quantity(B) > 0
    assert book.available(B, E) == 0 and book.available(B, F) == book.quantity(B)
    assert {order["side"] for order in split["orders"].values() if order["day"] == E} == {"sell", "buy"}
    fills_at_open = deepcopy(split["fills"])
    result = close_day(split, base)
    assert split["day"] == E and not split.get("intraday_open_session")
    assert split["fills"] == fills_at_open and base.calls == []
    for field in ("cash", "positions", "orders", "fills", "receivables", "entitlements", "prices", "targets"):
        assert split[field] == original[field], field
    for field in ("equity", "cash", "fees", "turnover_value", "holdings", "new_orders"):
        assert result[field] == legacy[field], field
    assert sum(fill["fees"] for fill in split["fills"].values()) > 0


class OpeningRow(dict):
    """A source with no knowledge of today's high, low, close, volume or signal."""
    FORBIDDEN = {"close", "high", "low", "volume", "amount"}

    def __getitem__(self, key):
        if key in self.FORBIDDEN:
            raise AssertionError("Opening read a later field: " + key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self.FORBIDDEN:
            raise AssertionError("Opening read a later field: " + key)
        return super().get(key, default)


class PriorVolumeOnly:
    def __init__(self, volume, current_day):
        self.volume, self.current_day, self.at = volume, current_day, self

    def __getitem__(self, key):
        if key[0] >= self.current_day:
            raise AssertionError("Opening read current/future volume")
        return self.volume.at[key]


def test_opening_needs_no_future_fields_or_signal_and_only_close_phase_marks_close():
    state, base = background()
    saved_rows, saved_volume = deepcopy(base.rows[E]), base.volumes
    for symbol, row in saved_rows.items():
        base.rows[E][symbol] = OpeningRow({key: row[key] for key in
            ("open", "preclose", "isST", "tradestatus", "adjustflag")})
    base.volumes = PriorVolumeOnly(saved_volume, E)
    def forbidden_signal(*args, **kwargs):
        raise AssertionError("Opening requested a current/future signal")
    base.scores = base.signal_frame = forbidden_signal
    open_day(state, base)
    assert state["prices"][B] == 1000 and state["day"] == D
    shares, cash, fills = Ledger(state, CONFIG, CONTEXT).quantity(B), state["cash"], deepcopy(state["fills"])
    saved_rows[B]["close"] = 10.8
    base.rows[E], base.volumes = saved_rows, saved_volume
    result = close_day(state, base)
    assert state["prices"][B] == 1080
    assert state["cash"] == cash and state["fills"] == fills
    assert result["equity"] == pytest.approx((cash + shares * 1080) / 100)


def test_json_restore_no_change_close_once_and_identity_or_old_advance_conflicts_are_atomic():
    state, base = background(held=True)
    publication = envelope(no_change=True)
    open_day(state, base, publication=publication)
    assert Ledger(state, CONFIG, CONTEXT).quantity(A) == 100 and not state["orders"]
    before = deepcopy(state)
    for call in (
        lambda: open_day(state, base, publication=publication),
        lambda: engine.advance(state, base, E, CONFIG, 1, context=CONTEXT, preopen_intent=publication),
        lambda: close_day(state, base, context=RunContext("other", TradingMode.REPLAY)),
        lambda: close_day(state, base, config=replace(CONFIG, initial_cash=20000)),
        lambda: close_day(state, base, F),
    ):
        with pytest.raises((ValueError, PermissionError)):
            call()
        assert state == before
    restored = json.loads(json.dumps(state))
    close_day(restored, base)
    assert restored["day"] == E and Ledger(restored, CONFIG, CONTEXT).quantity(A) == 100
    assert not restored["orders"] and not restored["fills"]
    final = deepcopy(restored)
    with pytest.raises((ValueError, PermissionError)):
        close_day(restored, base)
    assert restored == final
    with pytest.raises(ValueError, match="intraday|phase"):
        engine.advance(restored, base, F, CONFIG, 2, context=CONTEXT,
            preopen_intent=envelope(source_day=E, execution_day=F, no_change=True))
    assert restored == final


def test_clocks_sources_phase_hooks_and_trial_failures_do_not_commit(monkeypatch):
    invalid_times = (E + "T09:29:59+08:00", E + "T12:00:00+08:00", E + "T15:00:01+08:00",
                     D + "T09:30:00+08:00")
    for value in invalid_times:
        state, base = background()
        before = deepcopy(state)
        with pytest.raises((ValueError, PermissionError)):
            open_day(state, base, opened_at=value)
        assert state == before
    for alteration in ("source", "hook", "historical_legacy", "paper_synthetic"):
        state, base = background()
        options = {}
        if alteration == "source": options["source_ref"] = "unbound-other-source"
        if alteration == "hook": del base.validate_opening_session
        if alteration == "historical_legacy":
            state = {key: value for key, value in state.items() if not key.startswith("intraday_")}
        if alteration == "paper_synthetic":
            state = new_state(CONFIG.initial_cash)
            options["context"] = RunContext(CONTEXT.account_id, TradingMode.PAPER)
        before = deepcopy(state)
        with pytest.raises((ValueError, PermissionError)):
            open_day(state, base, **options)
        assert state == before
    # A plausible publication before now cannot authorize a future quote/phase.
    state, base = background()
    before = deepcopy(state)
    with monkeypatch.context() as clocks:
        clocks.setattr(engine, "_published_now", lambda: datetime.fromisoformat(E + "T08:30:00+08:00"))
        with pytest.raises(ValueError):
            open_day(state, base)
    assert state == before
    # PAPER needs a genuinely current snapshot, not merely two old timestamps
    # that agree with each other within 90 seconds.
    paper = RunContext(CONTEXT.account_id, TradingMode.PAPER)
    state, base = new_state(CONFIG.initial_cash), phase_feed(feed(), E, kind="observed_market_snapshot")
    base.intraday_opening_source_available_at = E + "T09:30:00+08:00"
    before = deepcopy(state)
    with monkeypatch.context() as clocks:
        clocks.setattr(engine, "_published_now", lambda: datetime.fromisoformat(E + "T09:40:00+08:00"))
        with pytest.raises(ValueError):
            open_day(state, base, context=paper)
    assert state == before
    state, base = background()
    open_day(state, base)
    before = deepcopy(state)
    with pytest.raises((ValueError, PermissionError)):
        close_day(state, base, closed_at=E + "T14:59:59+08:00")
    assert state == before
    with monkeypatch.context() as clocks:
        clocks.setattr(engine, "_published_now", lambda: datetime.fromisoformat(E + "T14:30:00+08:00"))
        with pytest.raises(ValueError):
            close_day(state, base)
    assert state == before
    def partial_failure(book, *args, **kwargs):
        book.state["cash"] -= 123
        raise ValueError("synthetic close kernel failure after trial mutation")
    monkeypatch.setattr(engine, "_mark_session_close", partial_failure)
    with pytest.raises(ValueError, match="synthetic close kernel failure"):
        close_day(state, base)
    assert state == before
