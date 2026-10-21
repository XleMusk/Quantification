"""Synthetic shared-Engine behavior, not a market return validation."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json

import pandas as pd
import pytest

from trading import engine
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext, SignalUnavailable, TradingMode, decision_time
from trading.ledger import Ledger, new_state
from trading.research.leader_historical_execution_policy import HistoricalExecutionPolicy
from trading.research.order_capacity_replay import (PriorVolumeReplayPolicy,
    RISK_COMPAT_FULL_EXIT_LIFECYCLE, seed_research_snapshot)
from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed
from trading.research.shortline_panel_replay import _config
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE, PREOPEN_FULL_EXIT_LIFECYCLE


A, B, C = "600001.SSE", "600002.SSE", "600003.SSE"
DAYS = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
CONFIG = _config(100000, 5)
CONTEXT = RunContext("synthetic_order_capacity_research", TradingMode.REPLAY)


class NoNewSignals(ExactSourceRawFeed):
    def signal_frame(self, day, config, context):
        return SignalUnavailable("explicit_synthetic_no_new_ranking")


class Risk:
    policy_id = "synthetic_independent_risk_exit_v1"

    def __init__(self, schedule=None):
        self.schedule = schedule or {}

    def __call__(self, day, book, feed):
        symbols = self.schedule.get(day)
        return (engine.ReplayRiskExitIntent(book.context, day, feed.next_day(day), tuple(symbols),
            "synthetic_independent_expiry", "synthetic_fixed_expiry") if symbols else None)


def setup(*, context=CONTEXT, opt_in=True):
    raw = pd.DataFrame([{"date": day, "code": "sh." + symbol[:6], "open": "10",
        "high": "10.2", "low": "9.8", "close": "10", "preclose": "10",
        "volume": "10000000", "amount": "100000000", "tradestatus": "1", "isST": "0"}
        for day in DAYS for symbol in (A, B, C)])
    feed = NoNewSignals(raw, DAYS, [A, B, C])
    for record in feed.instruments.values():
        record["ipoDate"] = "1991-04-03"
    universe = pd.DataFrame({"symbol": [A, B, C], "code": ["sh." + s[:6] for s in (A, B, C)]})
    accounting = StockDBActionAccounting([], [], [], {(code, 2026): True for code in universe.code},
        universe, {"rename": None}, [A, B, C], DAYS, DAYS[1:],
        {"price_source": "independent_baostock_raw", "returns_reportable": False})
    execution = HistoricalExecutionPolicy(feed, accounting, DAYS[1:],
        prior_volume_policy=PriorVolumeReplayPolicy(context) if opt_in else None)
    return feed, execution, new_state(CONFIG.initial_cash)


def step(state, feed, index, *, weights=None, risk=None, context=CONTEXT, lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE,
         config=CONFIG, target_policy=None):
    intent = None if weights is None else PortfolioIntent.full_target(weights, context=context,
        decided_at=decision_time(DAYS[index]), execution_not_before=DAYS[index + 1], source="synthetic_fixed_target")
    return engine.advance(state, feed, DAYS[index], config, index, context=context,
        published_close_intent=intent, replay_risk_exit_policy=risk, full_exit_lifecycle=lifecycle,
        target_policy=target_policy)


def qty(state, symbol):
    return Ledger(state, CONFIG, CONTEXT).quantity(symbol)


def test_unknown_capacity_is_per_order_not_zero_and_current_limit_reason_wins():
    feed, execution, state = setup()
    step(state, feed, 1, weights={A: .16, B: .16, C: .16})
    feed.volumes.at[DAYS[1], A] = float("nan")
    feed.volumes.at[DAYS[1], C] = -1
    feed.rows[DAYS[2]][C].update(open=11., high=11., low=10., close=10.)
    step(state, feed, 2)
    assert qty(state, A) == qty(state, C) == 0 and qty(state, B) > 0
    assert not any(order["symbol"] in {A, C} for order in state["orders"].values())
    blocks = {e["symbol"]: e for e in state["events"] if e["day"] == DAYS[2] and e["kind"] == "order_blocked"}
    assert blocks[A]["reason"] == "prior_session_volume_unknown" and blocks[A]["order_created"] is False
    assert blocks[A]["previous_day_required"] == DAYS[1] and blocks[A]["capacity_known"] is False
    assert blocks[C]["reason"] == "limit_up_queue_unknown"
    # The already-attempted buys do not silently recur on a new NO_CHANGE day.
    step(state, feed, 3)
    assert not any(order["symbol"] in {A, C} for order in state["orders"].values())
    assert state["pending_targets"] is None and execution.accounting.checks


def test_risk_exit_and_t1_full_exit_persist_on_no_change_with_partial_fills():
    feed, _, state = setup()
    risk = Risk({DAYS[2]: (A,)})
    step(state, feed, 1, weights={A: .16, B: .16}, risk=risk)
    step(state, feed, 2, risk=risk)
    original = qty(state, A)
    # Accurate prior volume is unknown, so the independent reduction remains
    # explicitly queued while the unrelated B holding stays exact shares.
    other = qty(state, B)
    feed.volumes.at[DAYS[2], A] = float("nan")
    step(state, feed, 3, risk=risk)
    request = state["full_exit_lifecycle"]["remaining_full_exits"][A]
    assert request["source"] == "synthetic_independent_expiry" and request["requested_on"] == DAYS[2]
    assert request["target_version"] is None and request["remaining_quantity"] == original
    assert state["pending_risk_exits"] is None and qty(state, B) == other
    state = json.loads(json.dumps(state))
    feed.volumes.at[DAYS[3], A] = 10000.
    step(state, feed, 4, risk=risk)
    assert 0 < qty(state, A) < original and qty(state, B) == other
    step(state, feed, 5, risk=risk)
    assert qty(state, A) == 0 and not state["full_exit_lifecycle"]["remaining_full_exits"]
    # Explicit full zero + locked lot preserves a full exit as well; it does
    # not shrink the target to whichever quantity happens to be sellable.
    second_feed, _, locked = setup()
    step(locked, second_feed, 1, weights={A: .16})
    step(locked, second_feed, 2, weights={})
    held = qty(locked, A)
    for lot in locked["positions"][A]:
        lot["available"] = DAYS[4]
    step(locked, second_feed, 3)
    assert qty(locked, A) == held and locked["full_exit_lifecycle"]["remaining_full_exits"][A]["remaining_quantity"] == held
    step(locked, second_feed, 4)
    assert qty(locked, A) == 0


def test_new_positive_full_target_replaces_residual_and_target_overlay_is_explicit():
    feed, _, state = setup()
    step(state, feed, 1, weights={A: .16})
    step(state, feed, 2, weights={})
    feed.volumes.at[DAYS[2], A] = float("nan")
    step(state, feed, 3, weights={A: .16})
    assert state["full_exit_lifecycle"]["remaining_full_exits"] == {}
    assert any(e["kind"] == "full_exit_replaced_by_new_target" for e in state["events"])
    original = qty(state, A)
    step(state, feed, 4, target_policy=lambda intent: intent)
    # The new .16 target may legitimately floor to a slightly smaller lot
    # after fees. It must not consume the obsolete full-liquidation request.
    assert qty(state, A) > 0 and not state["full_exit_lifecycle"]["remaining_full_exits"]
    assert not any(e["day"] == DAYS[4] and e["kind"] == "target_execution_diagnostic"
        and e["mechanical_reason"] == "residual_full_exit" for e in state["events"])
    assert original > 0


def test_method_restore_and_old_strict_combination_reject_before_mutation():
    feed, _, state = setup()
    step(state, feed, 1, weights={A: .16})
    for changed in ("disable", "context", "config"):
        before = deepcopy(state)
        old_policy = feed.prior_volume_policy
        if changed == "disable":
            feed.prior_volume_policy = None
        elif changed == "context":
            feed.prior_volume_policy = PriorVolumeReplayPolicy(RunContext("other", TradingMode.REPLAY))
        with pytest.raises(ValueError):
            step(state, feed, 2, config=replace(CONFIG, slippage_bps=11) if changed == "config" else CONFIG)
        assert state == before
        feed.prior_volume_policy = old_policy
    strict_feed, _, strict = setup(opt_in=False)
    step(strict, strict_feed, 1, weights={A: .16}, lifecycle=None)
    strict_feed.volumes.at[DAYS[1], A] = float("nan")
    before = deepcopy(strict)
    with pytest.raises(ValueError, match="Unknown exact prior native execution capacity"):
        step(strict, strict_feed, 2, lifecycle=None)
    assert strict == before
    fresh = new_state(CONFIG.initial_cash)
    with pytest.raises(ValueError, match="cannot be mixed"):
        step(fresh, strict_feed, 1, lifecycle=FULL_EXIT_LIFECYCLE, risk=Risk())
    assert fresh == new_state(CONFIG.initial_cash)
    envelope = PreopenPortfolioIntent(DAYS[0], DAYS[1], DAYS[0] + "T18:00:00+08:00",
        PortfolioIntent.full_target({A: .16}, context=CONTEXT, source="synthetic_saved_preopen"))
    with pytest.raises(ValueError, match="cannot be mixed"):
        engine.advance(fresh, strict_feed, DAYS[1], CONFIG, 1, context=CONTEXT,
            preopen_intent=envelope, replay_risk_exit_policy=Risk(),
            full_exit_lifecycle=PREOPEN_FULL_EXIT_LIFECYCLE)
    assert fresh == new_state(CONFIG.initial_cash)
    # A pre-existing unbound account cannot obtain the new method by attaching
    # a feed option instead of explicitly making a pinned research fork.
    strict_feed.prior_volume_policy = PriorVolumeReplayPolicy(CONTEXT)
    with pytest.raises(ValueError, match="existing account"):
        step(strict, strict_feed, 2)
    assert strict == before


def test_seed_pins_exact_source_preserves_all_ledger_fields_and_pending_version(tmp_path):
    original_context = RunContext("original_synthetic_historical", TradingMode.REPLAY)
    old_feed, _, original = setup(context=original_context, opt_in=False)
    old_risk = Risk()
    step(original, old_feed, 1, weights={A: .16, B: .16}, risk=old_risk, context=original_context, lifecycle=None)
    step(original, old_feed, 2, weights={C: .16}, risk=old_risk, context=original_context, lifecycle=None)
    path = tmp_path / "account.json"
    raw = json.dumps(original, ensure_ascii=False).encode()
    path.write_bytes(raw)
    now = datetime.now().astimezone().isoformat()
    seeded = seed_research_snapshot(path, hashlib.sha256(raw).hexdigest(), context=CONTEXT,
        config=CONFIG, calendar=DAYS, source_available_at=now, source_observed_at=now)
    assert path.read_bytes() == raw and json.loads(raw) == original
    def remapped(value):
        if isinstance(value, dict):
            return CONTEXT.to_dict() if value == original_context.to_dict() else {
                key: remapped(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remapped(item) for item in value]
        return value
    assert {key: seeded[key] for key in original} == remapped(original)
    assert seeded["replay_risk_exit_policy"]["context"] == CONTEXT.to_dict()
    latest = seeded["full_exit_lifecycle"]["latest_target"]
    assert latest["weights"] == {C: .16} and latest["version"] == 1
    assert latest["execution_not_before"] == DAYS[3]
    assert seeded["research_snapshot_seed"]["original_historical_lifecycle_version_known"] is False
    new_feed, _, _ = setup()
    step(seeded, new_feed, 3, risk=Risk())
    assert qty(seeded, A) == qty(seeded, B) == 0 and qty(seeded, C) > 0
    assert path.read_bytes() == raw
    with pytest.raises(ValueError, match="SHA256"):
        seed_research_snapshot(path, "0"*64, context=CONTEXT, config=CONFIG, calendar=DAYS,
            source_available_at=now, source_observed_at=now)


def test_new_method_does_not_relax_current_raw_or_independent_reference_checks():
    for broken in ("status", "reference", "missing_previous"):
        feed, _, state = setup()
        step(state, feed, 1, weights={A: .16})
        if broken == "status":
            feed.rows[DAYS[2]][A]["tradestatus"] = None
        elif broken == "reference":
            feed.rows[DAYS[2]][A]["preclose"] = 9.
        else:
            del feed.rows[DAYS[1]][A]
        before = deepcopy(state)
        with pytest.raises(ValueError):
            step(state, feed, 2)
        assert state == before


def test_seed_requires_pinned_calendar_first_and_subsequent_exact_sessions(tmp_path):
    original_context = RunContext("original_seed_without_pending", TradingMode.REPLAY)
    old_feed, _, original = setup(context=original_context, opt_in=False)
    step(original, old_feed, 1, risk=Risk(), context=original_context, lifecycle=None)
    assert original["pending_targets"] is None
    path = tmp_path / "no_pending_account.json"
    raw = json.dumps(original).encode()
    path.write_bytes(raw)
    now = datetime.now().astimezone().isoformat()
    seeded = seed_research_snapshot(path, hashlib.sha256(raw).hexdigest(), context=CONTEXT,
        config=CONFIG, calendar=DAYS, source_available_at=now, source_observed_at=now)
    feed, _, _ = setup()
    before = deepcopy(seeded)
    # An absent target does not permit silently skipping the first opening.
    with pytest.raises(ValueError, match="exact next session"):
        step(seeded, feed, 3, risk=Risk())
    assert seeded == before
    feed.calendar = DAYS + ["2026-09-18"]
    with pytest.raises(ValueError, match="calendar binding"):
        step(seeded, feed, 2, risk=Risk())
    assert seeded == before
    feed.calendar = list(DAYS)
    step(seeded, feed, 2, risk=Risk())
    assert seeded["day"] == DAYS[2] and seeded["pending_targets"] is None
    after_first = deepcopy(seeded)
    with pytest.raises(ValueError, match="exact next session"):
        step(seeded, feed, 4, risk=Risk())
    assert seeded == after_first
    step(seeded, feed, 3, risk=Risk())
    assert seeded["day"] == DAYS[3] and path.read_bytes() == raw
