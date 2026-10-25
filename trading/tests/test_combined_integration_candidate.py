"""One disposable scenario that keeps the candidate's slices on one checkout.

The scenario remains paper/replay-only.  It deliberately reports the three
financial behaviors separately inside one test so a later branch integration
can replace it with a single production-grade scenario without weakening the
acceptance boundary.
"""

from copy import deepcopy
from datetime import datetime
import json

import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger, new_state
from trading.published_close_session import attach_close
from trading.research.integration_harness import clone_split_case, run_cancel_race, source_unchanged
from trading.research.tests import test_recent_alpha158_forward_run as alpha_fixtures
from trading.research.tests import test_recent_alpha158_split_forward as alpha_split
from trading.tests import test_security_conversion_engine as conversion


def test_combined_cancel_conversion_and_alpha_split_candidate(tmp_path, monkeypatch):
    # Slice 1: a late partial fill keeps the cancel request visible.
    config = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=1,
                            gross_limit=.8, single_limit=.8, slippage_bps=0)
    book = Ledger(new_state(config.initial_cash), config)
    order = book.submit("combined-race", "2024-01-02", "600000.SSE", "buy", 200, 1000)
    book.request_cancel("combined-race", "2024-01-02")
    book.fill("combined-fill", "combined-race", 100, 1000, "2024-01-02", "2024-01-03")
    assert order["status"] == "cancel_pending"
    book.confirm_cancel("combined-race", "2024-01-02")
    assert order["status"] == "cancelled" and book.quantity("600000.SSE") == 100

    # Slice 2: a proven security conversion is not represented as a fill and
    # the converted holding remains on the same Engine/Ledger path.
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat("2025-01-10T20:00:00+08:00"))
    base, state = conversion.seeded()
    (tmp_path / "conversion").mkdir()
    conversion_receipt, conversion_view = conversion.conversion(tmp_path / "conversion", state)
    old_fills, old_orders = deepcopy(state["fills"]), deepcopy(state["orders"])
    result = conversion.step(state, base, conversion.F, {}, conversion_view=conversion_view)
    converted = Ledger(state, conversion.CONFIG, conversion.CONTEXT)
    assert converted.quantity(conversion.B) == 1600
    assert state["fills"] == old_fills and state["orders"] == old_orders
    assert state["security_conversion_applied"][conversion_receipt.event_id] == conversion_receipt.to_dict()
    assert result["new_orders"] == 0

    # Slice 3: the existing Alpha158 split lifecycle settles a raw day and
    # attaches the late publication without creating an account path.
    alpha_root = tmp_path / "alpha"
    alpha_root.mkdir()
    evidence = alpha_fixtures.evidence.__wrapped__(alpha_root, monkeypatch)
    alpha_split.test_split_settles_raw_day_then_attaches_late_publication(evidence)
    readback = alpha_split.split.status(evidence["run_dir"] / "split")
    assert readback["research_only"] is True and readback["execution_admitted"] is False
    head_file = evidence["run_dir"] / "split" / "head.json"
    clone = clone_split_case(head_file, readback["cases"][0]["case"])
    clone_receipt = run_cancel_race(clone, alpha_split.split.original._config(100000, 5),
                                    symbol="000001.SZSE", day=clone.state["day"],
                                    available_day="2026-10-19")
    assert clone_receipt["pending_status_after_late_partial"] == "cancel_pending"
    assert clone_receipt["final_order_status"] == "cancelled"
    assert source_unchanged(clone)

    receipt = {
        "schema": "combined_integration_candidate_v1",
        "status": "COMBINED_SCENARIO_PASS",
        "slices": ["cancel_pending_late_fill", "security_conversion", "alpha158_split_late_publication", "alpha158_checkpoint_clone"],
        "execution_admitted": False,
        "account_path": "shared_ledger_replay_only",
        "limitations": ["Synthetic/fixture evidence; does not prove historical branch merge or live channel readiness."],
    }
    (tmp_path / "combined_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


def test_shared_persistent_state_combines_cancel_conversion_split_and_target_exit(tmp_path, monkeypatch):
    """Exercise the conversion/split lifecycle after a cancel race on one state.

    The earlier integration case intentionally used isolated copies.  This
    case keeps the same ``state`` through the first published target, a late
    partial fill during cancellation, security conversion, split settlement,
    late close publication, and the eventual destination exit.  It remains a
    synthetic replay fixture and never accesses a broker or network.
    """

    base = conversion.feed()
    state = new_state(conversion.CONFIG.initial_cash)
    monkeypatch.setattr(engine, "_published_now", lambda: datetime.fromisoformat(
        conversion.H + "T20:00:00+08:00"))

    # Enable the conversion-aware Engine/Ledger path and publish the first
    # target on the persistent account state.
    conversion.step(state, base, conversion.D, {conversion.A: .8}, split=True)

    # The same state consumes the target, converts the source holding, settles
    # the split day, and attaches the late publication.
    conversion.step(state, base, conversion.E, None, split=True)
    receipt, current = conversion.conversion(tmp_path, state)
    waiting = engine.advance(
        state, base, conversion.F, conversion.CONFIG, 2,
        context=conversion.CONTEXT,
        security_conversion_view=current,
        published_close_session_method=conversion.SPLIT_METHOD,
        published_close_target_policy=conversion.Target(),
        replay_risk_exit_policy=conversion.Risk(),
    )
    assert waiting["new_orders"] == 0
    assert state["last_published_close_session"]["status"] == "awaiting_publication"

    attached = attach_close(
        state, base, conversion.F, conversion.publication(conversion.F, {}),
        conversion.CONFIG, 2, context=conversion.CONTEXT,
        published_close_target_policy=conversion.Target(),
        replay_risk_exit_policy=conversion.Risk(),
    )
    assert attached["new_orders"] == attached["new_fills"] == 0

    # Inject a late partial fill/cancel race into that same persistent state
    # before the destination exit is applied.
    book = Ledger(state, conversion.CONFIG, conversion.CONTEXT)
    order = book.submit("shared-state-cancel", conversion.F, conversion.B,
                        "buy", 100, 500)
    book.request_cancel(order["id"], conversion.F)
    book.fill("shared-state-fill", order["id"], 50, 500,
              conversion.F, conversion.G)
    book.confirm_cancel(order["id"], conversion.F)
    assert order["status"] == "cancelled"

    exited = conversion.step(state, base, conversion.G,
                             conversion_view=current, split=True)
    assert exited["new_orders"] == 1
    assert Ledger(state, conversion.CONFIG, conversion.CONTEXT).quantity(conversion.B) == 0
    assert state["security_conversion_applied"][receipt.event_id] == receipt.to_dict()
