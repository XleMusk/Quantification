"""Known-after IPO guard tests; fixtures do not certify market facts."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import _execution_listing_reason
from trading.research.ipo_qualification import IpoQualifiedResearchFeed, load_ipo_qualification
from trading.research.leader_daily_replay import CommonCohortRawFeed
from trading.research.leader_forward_run import ForwardRawFeed
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture


@pytest.fixture
def proof(tmp_path):
    evidence, master = write_ipo_fixture(tmp_path)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                    "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    return load_ipo_qualification(evidence, calendar, master_run=master), evidence


def test_exact_listing_five_sessions_and_conservative_publication_cutoff(proof):
    overlay, _ = proof
    assert overlay.qualify("2026-10-12", "301660.SZSE")["knowledge_status"] == "unknown"
    known = overlay.qualify("2026-10-12", "301660.SZSE", decision_as_of="2026-10-12T23:59:59+08:00")
    assert known["ipo_date"] == "2026-10-13" and known["engine_block_reason"] == "not_yet_listed"
    utc_cutoff = overlay.qualify("2026-10-13", "301660.SZSE", decision_as_of="2026-10-12T16:00:00+00:00")
    assert utc_cutoff["listing_session_ordinal"] == 1
    for ordinal, day in enumerate(["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"], 1):
        view = overlay.qualify(day, "301660.SZSE")
        assert view["listing_session_ordinal"] == ordinal and view["no_price_limit_first_five"] is True
        assert view["engine_block_reason"] == "unsupported_first_five_listing_sessions"
        assert view["ipo_window_feature_support"] is False
    sixth = overlay.qualify("2026-10-20", "301660.SZSE")
    assert sixth["listing_session_ordinal"] == 6 and sixth["price_limit_pct"] == 20
    assert sixth["engine_block_reason"] is None and sixth["ipo_window_feature_support"] is True
    assert sixth["pit_verified"] is sixth["corporate_actions_verified"] is sixth["execution_admitted"] is False
    assert overlay.qualify("2027-01-04", "301660.SZSE")["engine_block_reason"] == "listing_calendar_incomplete"


def test_engine_explicit_hook_uses_full_qualification_calendar_and_old_default_stays(proof):
    overlay, _ = proof
    day = "2026-10-15"
    calendar = pd.bdate_range(day, periods=7).strftime("%Y-%m-%d").tolist()
    base = SimpleNamespace(instruments={"600001.SSE": {"ipoDate": "2026-01-02"}}, calendar=calendar, manifest={})
    before = deepcopy(base.instruments)
    replay = RunContext("synthetic", TradingMode.REPLAY)
    assert _execution_listing_reason(base, "600001.SSE", day, replay) == "listing_calendar_incomplete"
    qualified = IpoQualifiedResearchFeed(base, overlay)
    assert _execution_listing_reason(qualified, "600001.SSE", day, replay) is None
    assert base.instruments == before and qualified.calendar == base.calendar
    with pytest.raises(ValueError, match="replay-only"):
        _execution_listing_reason(qualified, "600001.SSE", day, RunContext("paper", TradingMode.PAPER))
    unknown = SimpleNamespace(instruments={"600003.SSE": {"ipoDate": "2000-01-01"}}, calendar=calendar, manifest={})
    gate = IpoQualifiedResearchFeed(unknown, overlay)
    assert gate.instruments["600003.SSE"]["ipoDate"] is None
    assert _execution_listing_reason(gate, "600003.SSE", day, replay) == "missing_listing_date"


def test_historical_feature_qualification_uses_its_decision_cutoff_but_engine_stays_same_day(proof):
    overlay, _ = proof
    assert overlay.qualify("2026-10-07", "600001.SSE")["knowledge_status"] == "unknown"
    later = overlay.qualify("2026-10-07", "600001.SSE", decision_as_of="2026-10-15T15:05:00+08:00")
    assert later["knowledge_status"] == "known_after_source_availability"
    assert later["day"] == "2026-10-07" and later["decision_as_of"] == "2026-10-15T15:05:00+08:00"
    with pytest.raises(ValueError, match="exact decision day"):
        overlay.assess("600001.SSE", "2026-10-07", decision_as_of="2026-10-15T15:05:00+08:00")
    with pytest.raises(ValueError, match="exact decision day"):
        overlay.qualify("2026-10-16", "600001.SSE", decision_as_of="2026-10-15T15:05:00+08:00")


def test_forward_feed_no_longer_invents_ipo_from_first_observation(proof):
    overlay, _ = proof
    symbols, day = ["600001.SSE", "301660.SZSE"], "2026-10-15"
    raw = pd.DataFrame([{"symbol": symbol, "date": day, "open": 10., "high": 10., "low": 10.,
                         "close": 10., "volume": 1000000., "amount": 10000000., "pct_chg": 0.,
                         "preclose_for_limits": 10., "is_st": False} for symbol in symbols])
    old = CommonCohortRawFeed(raw, [day], symbols)
    unknown = ForwardRawFeed(raw, [day], symbols)
    qualified = ForwardRawFeed(raw, [day], symbols, ipo_qualification=overlay)
    assert old.instruments["600001.SSE"]["ipoDate"] == day
    assert unknown.instruments["600001.SSE"]["ipoDate"] is None
    assert qualified.instruments["600001.SSE"]["ipoDate"] == "2026-01-02"
    assert qualified.instruments["301660.SZSE"]["ipoDate"] == "2026-10-13"
    context = RunContext("synthetic", TradingMode.REPLAY)
    assert qualified.research_listing_limit_reason("600001.SSE", day, context) is None
    assert qualified.research_listing_limit_reason("301660.SZSE", day, context) == "unsupported_first_five_listing_sessions"


def test_changed_raw_hash_and_future_master_availability_do_not_pass(proof):
    overlay, evidence = proof
    assert overlay.assess("600001.SSE", "2026-10-07")["engine_block_reason"] == "missing_listing_date"
    data = json.loads(evidence.read_text())
    receipt = json.loads(__import__("pathlib").Path(data["source_receipt"]).read_text())
    __import__("pathlib").Path(receipt["sources"][0]["raw_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        load_ipo_qualification(evidence, overlay.verification["calendar"])
