from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.engine import simulate_order
from trading.entry_risk import RAW_FLOOR1_AND_NOTICES, RAW_PRICE_FLOOR1, entry_risk_contract
from trading.ledger import Ledger, new_state
from trading.notice_risk import COVERAGE, NOTICE_BLOCK_REASON, PARSER_ID, TIME_BASIS, NoticeRiskView
from trading.research.experiment import load_replay_checkpoint, replay
from trading.research.risk_notices import extract_event
from trading.research.tests import test_replay_resume as resume_cases
from trading.research.weekly_reversal import NOTICE_VARIANTS, VARIANTS, rank


NOTICE = {"symbol": "600811.SSE", "publication_on": "2025-03-01",
    "source_url": "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-03-01/600811_20250301_VSK1.pdf"}
AFFIRMATION = "\u516c\u53f8\u5b58\u5728\u91cd\u5927\u8fdd\u6cd5\u5f3a\u5236\u9000\u5e02\u98ce\u9669\u3002"
CORROBORATION = "\u73b0\u5df2\u521d\u6b65\u67e5\u660e\uff0c\u8d22\u52a1\u4fe1\u606f\u4e25\u91cd\u4e0d\u5b9e\uff0c\u6d89\u5acc\u91cd\u5927\u8d22\u52a1\u9020\u5047\u3002"


def pages(clause=AFFIRMATION, corroboration=CORROBORATION):
    return ["\u8bc1\u5238\u4ee3\u7801\uff1a600811\n" + clause + corroboration,
            "\u4e1c\u65b9\u96c6\u56e2\u80a1\u4efd\u6709\u9650\u516c\u53f8\u8463\u4e8b\u4f1a2025\u5e743\u67081\u65e5"]


def notice_view(tmp_path, name="view"):
    root = tmp_path / name
    root.mkdir()
    event = extract_event(pages(), NOTICE)
    event.update(source_sha256="0" * 64, source_file="sources/1/notice.pdf", retrieved_at="2026-10-03T11:00:00+08:00")
    raw = json.dumps([event]).encode()
    (root / "events.json").write_bytes(raw)
    (root / "manifest.json").write_text(json.dumps({"status": "primary_notice_risk_diagnostic_v1",
        "parser": PARSER_ID, "coverage": COVERAGE, "time_basis": TIME_BASIS,
        "events": 1, "events_sha256": sha256(raw).hexdigest(), "basis": "Test-only partial evidence"}))
    return NoticeRiskView(root)


def test_explicit_risk_event_uses_conservative_date_proxy_not_retrieval_time(tmp_path):
    view = notice_view(tmp_path)
    event = view.active_event("600811.SSE", "2025-03-03")
    assert event["publication_on"] == "2025-03-01"
    assert event["known_at_proxy"] == "2025-03-02T00:00:00+08:00"
    assert event["evidence_pages"] == [1]
    assert view.active_event("600811.SSE", "2025-02-28") is None
    assert view.active_event("600811.SSE", "2025-03-01") is None
    assert view.active_event("600000.SSE", "2026-09-30") is None
    assert view.binding["coverage"]["no_event_means"] == "unknown_not_safe"
    assert view.binding["coverage"]["complete"] is False


@pytest.mark.parametrize("clause", [
    "\u516c\u53f8\u4e0d\u5b58\u5728\u91cd\u5927\u8fdd\u6cd5\u5f3a\u5236\u9000\u5e02\u98ce\u9669\u3002",
    "\u5982" + AFFIRMATION,
    "\u82e5" + AFFIRMATION,
    "\u516c\u53f8\u5df2\u88ab\u7acb\u6848\u8c03\u67e5\u3002",
])
def test_negation_hypothetical_and_ordinary_investigation_are_not_positive(clause):
    assert extract_event(pages(clause), NOTICE) is None


def test_missing_corroboration_conflicting_statement_wrong_issuer_or_date_need_review():
    for invalid in (pages(corroboration=""),
                    pages(AFFIRMATION + "\u516c\u53f8\u4e0d\u5b58\u5728\u91cd\u5927\u8fdd\u6cd5\u5f3a\u5236\u9000\u5e02\u98ce\u9669"),
                    [pages()[0].replace("600811", "600000"), pages()[1]],
                    [pages()[0], pages()[1].replace("2025", "2024")], [pages()[0], ""]):
        with pytest.raises(ValueError):
            extract_event(invalid, NOTICE)


def test_existing_order_path_blocks_after_notice_but_keeps_prepublication_and_sell(tmp_path):
    view = notice_view(tmp_path)
    days = ["2025-02-28", "2025-03-03", "2025-03-04", "2025-03-05"]
    feed = SimpleNamespace(entry_risk_policy=RAW_FLOOR1_AND_NOTICES, risk_notice_view=view,
        calendar=days, instruments={"600811.SSE": {"ipoDate": "1994-01-06", "outDate": "2025-04-30"}},
        next_day=lambda day: days[days.index(day) + 1])
    q = {"open": 1.57, "preclose": 1.60, "isST": 0, "tradestatus": 1, "adjustflag": "3"}
    book = Ledger(new_state(140000), StrategyConfig())
    simulate_order(book, feed, days[0], "600811.SSE", "buy", 100, q, 1000000)
    assert book.quantity("600811.SSE") == 100
    before = book.state["cash"]
    simulate_order(book, feed, days[1], "600811.SSE", "buy", 100, q, 1000000)
    assert book.quantity("600811.SSE") == 100 and book.state["cash"] == before
    event = book.state["events"][-1]
    assert event["reason"] == NOTICE_BLOCK_REASON and event["publication_on"] == "2025-03-01"
    simulate_order(book, feed, days[1], "600811.SSE", "sell", 100, q, 1000000)
    assert book.quantity("600811.SSE") == 0
    paper = Ledger(new_state(140000), StrategyConfig(), RunContext("paper", TradingMode.PAPER))
    with pytest.raises(ValueError, match="replay-only"):
        simulate_order(paper, feed, days[1], "600811.SSE", "buy", 100, q, 1000000)
    assert not paper.state["orders"]


def test_notice_policy_and_view_cannot_be_omitted_or_switched_at_checkpoint(tmp_path):
    view = notice_view(tmp_path)
    with pytest.raises(ValueError, match="pinned primary view"):
        entry_risk_contract(RAW_FLOOR1_AND_NOTICES)
    with pytest.raises(ValueError, match="pinned primary view"):
        entry_risk_contract(RAW_PRICE_FLOOR1, view)
    feed = resume_cases.ReplayResumeTests().feed()
    config = StrategyConfig(initial_cash=140000)
    feed.entry_risk_policy, feed.risk_notice_view = RAW_FLOOR1_AND_NOTICES, view
    replay(feed, config, feed.calendar, tmp_path / "ledger")
    checkpoint = load_replay_checkpoint(tmp_path / "ledger", config, feed.calendar,
        entry_risk_policy=RAW_FLOOR1_AND_NOTICES, risk_notice_view=view)
    other = notice_view(tmp_path, "other_snapshot")
    with pytest.raises(ValueError, match="entry-risk"):
        load_replay_checkpoint(tmp_path / "ledger", config, feed.calendar,
            entry_risk_policy=RAW_FLOOR1_AND_NOTICES, risk_notice_view=other)
    feed.risk_notice_view = other
    before = deepcopy(checkpoint["state"])
    with pytest.raises(ValueError, match="entry-risk"):
        replay(feed, config, feed.calendar, tmp_path / "changed", checkpoint=checkpoint)
    assert checkpoint["state"] == before


def test_event_file_mutation_is_refused_and_fixed_pair_does_not_filter_ranks(tmp_path):
    view = notice_view(tmp_path)
    (view.folder / "events.json").write_text("[]")
    with pytest.raises(ValueError, match="pinned partial"):
        NoticeRiskView(view.folder)
    pool = [{"symbol": "600811.SSE", "momentum": .5, "reversal": .7},
            {"symbol": "600000.SSE", "momentum": .3, "reversal": .1}]
    for old, new in zip(VARIANTS, NOTICE_VARIANTS):
        assert rank(pool, old) == rank(pool, new)
