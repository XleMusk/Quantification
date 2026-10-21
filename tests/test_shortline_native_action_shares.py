"""Synthetic native two-model transfer shares; no ex-price-reference claim."""
from copy import deepcopy
from pathlib import Path

from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research import shortline_native_preopen_forward as forward
from trading.research.shortline_panel_replay import _config
from trading.research.tests import test_shortline_native_preopen_forward as saved

from test_shortline_native_action_components import evidence, execute, read, write


def test_explicit_transfer_credits_both_native_books_and_locks_new_shares_until_listing(evidence):
    e = evidence
    listing_day = "2026-10-20"
    item = read(e.action_review)
    for event in item["events"]:
        event["dividReserveToStockPs"] = "0.20"
        event["dividStockMarketDate"] = listing_day
        assert event["share_term_sources"]["dividReserveToStockPs"] == event["official_sources"][0]["source_url"]
    write(e.action_review, item)

    # Actual synthetic Engine purchases are observed at the record close.
    first = execute(e, plans=e.plans[:1], days=(saved.D, saved.E))
    assert first["method_id"] == forward.ACTION_METHOD_ID and first["metrics"] is None
    head_file = e.action_run / "head.json"
    registered = read(head_file)
    first_journal = Path(registered["journal_files"][0]["file"])
    original_journal = first_journal.read_bytes(), first_journal.stat().st_mtime_ns
    entitlements = {kind: deepcopy(case["state"]["entitlements"])
                    for kind, case in registered["cases"].items()}
    for kind, case in registered["cases"].items():
        state = case["state"]
        assert state["entitlements"][saved.TARGET[kind] + ":" + saved.F] == 1600
        assert state["cash"] == 8_397_884

    # E preclose/D close remain equal; this only exercises component accounting.
    result = execute(e)
    assert result["metrics"] is None and result["new_order_count"] == 0
    assert result["newly_committed_case_sessions"] == 2
    settled = read(head_file)
    for kind, case in settled["cases"].items():
        state, symbol = case["state"], saved.TARGET[kind]
        book = Ledger(state, _config(100000, 5), RunContext.from_dict(case["context"]))
        assert state["day"] == saved.F
        assert book.quantity(symbol) == 1920
        assert book.available(symbol, saved.F) == 1600
        assert book.quantity(symbol) - book.available(symbol, saved.F) == 320
        assert [lot for lot in state["positions"][symbol] if lot["available"] == listing_day] == [
            {"qty": 320, "available": listing_day}]
        # A dated availability query is not a fabricated 20-Oct Engine session.
        assert book.available(symbol, listing_day) == 1920
        assert state["cash"] == 8_397_884 + 12800
        assert state["entitlements"] == entitlements[kind] and state["receivables"] == {}
        assert len([event for event in state["events"]
                    if event["kind"] == "corporate_action" and event["symbol"] == symbol]) == 1
        assert case["daily"][-1]["original_plan_kind"] == "NO_CHANGE"
    assert (first_journal.read_bytes(), first_journal.stat().st_mtime_ns) == original_journal

    before = head_file.read_bytes()
    restored = execute(e)
    assert restored["metrics"] is None and restored["new_order_count"] == 0
    assert head_file.read_bytes() == before
    assert restored["newly_committed_case_sessions"] == 0
