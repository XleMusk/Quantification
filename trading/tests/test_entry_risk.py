from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.engine import advance, simulate_order
from trading.entry_risk import BLOCK_REASON, RAW_PRICE_FLOOR1, entry_risk_contract, raw_quote_entry_block
from trading.ledger import Ledger, new_state
from trading.research.experiment import load_replay_checkpoint, replay
from trading.research.tests import test_replay_resume as resume_cases
from trading.research.weekly_reversal import GUARDED_VARIANTS, VARIANTS, WeeklyFeed, rank
from trading.rules import FUND_PROFILES, execution_rule


def order_feed(policy=RAW_PRICE_FLOOR1):
    return SimpleNamespace(entry_risk_policy=policy,
        calendar=["2024-05-29", "2024-05-30", "2024-05-31"],
        instruments={"600321.SSE": {"ipoDate": "2001-05-24", "outDate": "2024-06-27"}},
        next_day=lambda day: "2024-05-30" if day == "2024-05-29" else "2024-05-31")


def quote(opening=.55, preclose=.60, **changes):
    return {"open": opening, "preclose": preclose, "close": 100., "isST": 0,
            "tradestatus": 1, "adjustflag": "3", **changes}


@pytest.mark.parametrize("opening,preclose", [(.55, .60), (.99, 1.01), (1.01, .99)])
def test_guard_blocks_low_raw_quotes_without_cash_or_share_mutation(opening, preclose):
    book = Ledger(new_state(140000), StrategyConfig())
    simulate_order(book, order_feed(), "2024-05-29", "600321.SSE", "buy", 100,
                   quote(opening, preclose), 1000000)
    assert book.state["cash"] == 14000000 and not book.state["positions"]
    assert not book.state["orders"] and not book.state["fills"]
    event = book.state["events"][-1]
    assert event["reason"] == BLOCK_REASON and event["raw_open_rmb"] == str(opening)
    assert event["entry_risk_policy"] == RAW_PRICE_FLOOR1


def test_opt_in_preserves_original_055_buy_and_exactly_one_is_allowed():
    old = Ledger(new_state(140000), StrategyConfig())
    simulate_order(old, order_feed(None), "2024-05-29", "600321.SSE", "buy", 100,
                   quote(), 1000000)
    assert old.quantity("600321.SSE") == 100
    assert "entry_risk_policy" not in old.state
    guarded = Ledger(new_state(140000), StrategyConfig())
    simulate_order(guarded, order_feed(), "2024-05-29", "600321.SSE", "buy", 100,
                   quote(1., 1.), 1000000)
    assert guarded.quantity("600321.SSE") == 100


def test_policy_blocks_addition_but_leaves_low_price_sell_exit_possible():
    book = Ledger(new_state(140000), StrategyConfig())
    feed = order_feed()
    simulate_order(book, feed, "2024-05-29", "600321.SSE", "buy", 100,
                   quote(1., 1.), 1000000)
    simulate_order(book, feed, "2024-05-30", "600321.SSE", "buy", 100,
                   quote(), 1000000)
    assert book.quantity("600321.SSE") == 100 and book.state["events"][-1]["reason"] == BLOCK_REASON
    simulate_order(book, feed, "2024-05-30", "600321.SSE", "sell", 100,
                   quote(), 1000000)
    assert book.quantity("600321.SSE") == 0
    assert book.state["orders"]["2024-05-30:600321.SSE:sell"]["filled"] == 100


def test_guard_does_not_inspect_future_delist_date_same_day_close_or_fund_prices():
    rule = execution_rule("600321.SSE", "2024-05-29")
    assert raw_quote_entry_block(RAW_PRICE_FLOOR1, "buy", quote(1.1, 1.1, close=.1), rule) is None
    for symbol, profile in FUND_PROFILES.items():
        rule = execution_rule(symbol, "2024-05-29", {symbol: profile})
        assert raw_quote_entry_block(RAW_PRICE_FLOOR1, "buy", quote(.5, .5), rule) is None
    for invalid in (quote(adjustflag="1"), quote(float("nan"), 1.), quote(1., 0.)):
        with pytest.raises(ValueError, match="raw quote"):
            raw_quote_entry_block(RAW_PRICE_FLOOR1, "buy", invalid,
                                  execution_rule("600321.SSE", "2024-05-29"))
        book = Ledger(new_state(140000), StrategyConfig())
        before = deepcopy(book.state)
        with pytest.raises(ValueError, match="adjusted|raw quote"):
            simulate_order(book, order_feed(), "2024-05-29", "600321.SSE", "buy", 100,
                           invalid, 1000000)
        assert book.state == before


def test_fixed_pair_keeps_original_ranks_and_requires_matching_signal_identity():
    pool = [{"symbol": "600321.SSE", "momentum": .4, "reversal": -.1},
            {"symbol": "600000.SSE", "momentum": .1, "reversal": .2}]
    for original, guarded in zip(VARIANTS, GUARDED_VARIANTS):
        assert rank(pool, original) == rank(pool, guarded)
    with pytest.raises(ValueError, match="identity"):
        WeeklyFeed(SimpleNamespace(calendar=[]), {}, StrategyConfig(), GUARDED_VARIANTS[0])


def test_policy_pinned_to_checkpoint_even_when_no_sessions_left(tmp_path):
    feed = resume_cases.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000)
    feed.entry_risk_policy = RAW_PRICE_FLOOR1
    replay(feed, config, feed.calendar, tmp_path / "guarded")
    saved = json.loads((tmp_path / "guarded/replay.json").read_text())
    assert saved["entry_risk_policy"] == entry_risk_contract(RAW_PRICE_FLOOR1)
    with pytest.raises(ValueError, match="entry-risk"):
        load_replay_checkpoint(tmp_path / "guarded", config, feed.calendar)
    checkpoint = load_replay_checkpoint(tmp_path / "guarded", config, feed.calendar,
                                       entry_risk_policy=RAW_PRICE_FLOOR1)
    feed.entry_risk_policy = None
    before = deepcopy(checkpoint["state"])
    with pytest.raises(ValueError, match="entry-risk"):
        replay(feed, config, feed.calendar, tmp_path / "removed", checkpoint=checkpoint)
    with pytest.raises(ValueError, match="Entry-risk"):
        advance(checkpoint["state"], feed, "2024-01-11", config, len(feed.calendar))
    assert checkpoint["state"] == before
    feed.entry_risk_policy = RAW_PRICE_FLOOR1
    result = replay(feed, config, feed.calendar, tmp_path / "same", checkpoint=checkpoint)
    assert result["newly_completed_sessions"] == 0


def test_guard_still_stops_unsettled_held_delist_and_does_not_delete_shares():
    feed = resume_cases.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000)
    feed.entry_risk_policy = RAW_PRICE_FLOOR1
    state = new_state(config.initial_cash)
    for i, day in enumerate(feed.calendar[:2]):
        advance(state, feed, day, config, i)
    assert Ledger(state, config).quantity("600000.SSE") > 0
    feed.instruments["600000.SSE"]["outDate"] = feed.calendar[2]
    before = deepcopy(state)
    with pytest.raises(ValueError, match="Unresolved delisted holding"):
        advance(state, feed, feed.calendar[2], config, 2)
    assert state == before
