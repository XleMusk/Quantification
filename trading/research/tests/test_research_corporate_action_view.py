"""Synthetic pinned action evidence through the real opt-in feed and Engine.

No original issuer documents are reparsed, providers contacted or returns
computed. Initial inventory is acquired by the real Engine/Ledger on record day.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.baostock_daily_slice import digest
from trading.research.leader_forward_run import ForwardRawFeed
from trading.research.ipo_qualification import IpoQualification
from trading.research.research_corporate_action_view import load_research_corporate_action_view

S = "600001.SSE"
DAYS = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
NOW = datetime.fromisoformat("2026-06-10T18:00:00+08:00")
CONTEXT = RunContext("synthetic-corporate-action-component", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, commission=0, minimum_commission=0,
    sell_tax=0, transfer_fee=0, slippage_bps=0, dividend_tax_reserve=.2).validate()
IPO = IpoQualification({S: {"ipo_date": "1999-01-01", "available_at": "2026-05-28T10:00:00+08:00",
    "board": "main", "source_basis": "explicit_synthetic_only"}},
    tuple(pd.bdate_range("2026-01-05", "2026-12-31").strftime("%Y-%m-%d")), {"synthetic_only": True})


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def source(folder, *, bonus="0", pay=DAYS[3], late=False, pdf_suffix=""):
    """A tiny explicit synthetic copy of the reviewed JSON/receipt contract."""
    folder.mkdir()
    retrieved = "2026-06-10T16:00:00+08:00" if late else "2026-05-29T16:00:00+08:00"
    reviewed = "2026-06-10T17:00:00+08:00" if late else "2026-05-29T17:00:00+08:00"
    pdf = folder / "synthetic_implementation.pdf"
    pdf.write_bytes(("%PDF-1.4\nSYNTHETIC ONLY; not an issuer notice; " + pdf_suffix).encode())
    url = "https://static.cninfo.com.cn/finalpage/2026-05-25/900001.PDF"
    metadata = folder / "synthetic_official_metadata.json"
    save(metadata, {"hasMore": False, "announcements": [{"secCode": S[:6], "announcementId": "900001",
        "announcementTitle": "SYNTHETIC ONLY 权益分派实施公告", "adjunctUrl": "finalpage/2026-05-25/900001.PDF",
        "announcementTime": 1779667200000}]})
    event = {"symbol": S, "name": "SYNTHETIC ONLY", "event_status": "official_cash_dividend_event_and_dates_confirmed",
        "publication_date": "2026-05-25", "publication_time_known": False,
        "local_primary_evidence_available_at": retrieved,
        "dividRegistDate": DAYS[0], "dividOperateDate": DAYS[2], "dividPayDate": pay,
        "dividCashPsBeforeTax": "0.10", "dividStocksPs": bonus, "dividReserveToStockPs": "0",
        "share_terms_basis": "Explicit synthetic terms, never inferred from a blank provider field",
        "share_terms_not_inferred_from_blank_provider_fields": True,
        "share_term_sources": {"dividStocksPs": url if bonus is not None else None, "dividReserveToStockPs": url},
        "rights_offer_ratio_ps": None, "rights_offer_price_rmb": None,
        "concurrent_rights_event_coverage": "unknown_not_established_by_cash_distribution_notices",
        "differentiated_distribution": None,
        "official_sources": [{"role": "implementation_notice", "source_url": url, "source_file": str(pdf),
            "source_sha256": digest(pdf), "publication_date": "2026-05-25", "retrieved_at": retrieved,
            "official_primary_source": True, "historical_publication_archive_verified": False}],
        "official_announcement_metadata": {"file": str(metadata), "sha256": digest(metadata),
            "announcement_id": "900001", "selected_security_code_matches": True, "query_hasMore": False},
        "unknown_fields": ["rights_offer_ratio_ps", "rights_offer_price_rmb", "independent_concurrent_action_coverage"],
        "historical_publication_archive_verified": False, "pit_verified": False,
        "corporate_actions_verified": False, "reference_formula_admitted": False,
        "feature_chain_admitted": False, "selection_enabled": False, "execution_admitted": False}
    receipts = []
    for path, method, loc in ((pdf, "GET", url), (metadata, "POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query")):
        receipts.append({"file": path.name, "method": method, "url": loc, "status_code": 200,
            "status": "downloaded", "sha256": digest(path), "bytes": path.stat().st_size,
            "started_at": retrieved, "retrieved_at": retrieved})
    save(folder / "retrieval_receipt.json", {"sources": receipts, "no_account_access": True,
        "provider_session_opened": False, "execution_admitted": False, "selection_enabled": False, "ledger_mutated": False})
    path = folder / "result.json"
    save(path, {"schema": "bounded_official_action_reference_review_v1", "synthetic_only": True,
        "status": "three_official_cash_dividend_events_confirmed_partial_share_and_PIT_boundaries_retained",
        "reviewed_at": reviewed, "scope": {"symbols": [S], "exact_event_day": DAYS[2], "all_market_coverage_claimed": False}, "events": [event],
        **{key: False for key in ("provider_session_opened", "broker_interacted", "orders_generated", "ledger_mutated",
            "raw_prices_modified", "master_modified", "feature_rules_modified", "admission_changed", "selection_enabled", "execution_admitted")}})
    return path


def view(path, *, mode="dated_asof", context=CONTEXT, config=CONFIG):
    return load_research_corporate_action_view(path, knowledge_mode=mode, context=context, config=config, now=NOW)


def raw_feed(action_view=None):
    calendar = ["2026-05-29", *DAYS]
    raw = pd.DataFrame([{"date": day, "symbol": S, "open": 10., "high": 10.05, "low": 9.95, "close": 10.,
        "preclose_for_limits": 10., "volume": 1000000., "amount": 10000000., "is_st": False,
        "provider_raw_observation": True, "preclose": 10., "tradestatus": 1, "isST": 0, "adjustflag": "3"}
        for day in calendar])
    kwargs = {} if action_view is None else {"corporate_action_view": action_view}
    return ForwardRawFeed(raw, calendar, [S], ipo_qualification=IPO, **kwargs)


def published(state, feed, day, weights, *, config=CONFIG, context=CONTEXT):
    intent = PortfolioIntent.full_target(weights, context=context, decided_at=day+"T18:30:00+08:00",
        execution_not_before=feed.next_day(day), source="research:synthetic-action-component-target")
    return advance(state, feed, day, config, 0, context=context, published_close_intent=intent)


def seeded(action_view=None, config=CONFIG, context=CONTEXT):
    """Start cash-only; real previous-close Engine decision queues record-day buy."""
    state = new_state(config.initial_cash)
    published(state, raw_feed(action_view), "2026-05-29", {S: .1}, config=config, context=context)
    return state


def serialized(state):
    return json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False)


def step(state, feed, day, *, config=CONFIG, context=CONTEXT):
    return advance(state, feed, day, config, 1, context=context)


def test_recorded_holder_still_receives_cash_after_real_engine_sale_before_ex_date(tmp_path):
    evidence = source(tmp_path / "cash")
    action_view = view(evidence)
    feed, state = raw_feed(action_view), seeded(action_view)
    exit_intent = PortfolioIntent.full_target({}, context=CONTEXT, decided_at=DAYS[0]+"T18:30:00+08:00",
        execution_not_before=DAYS[1], source="research:synthetic-record-holder-sale")
    advance(state, feed, DAYS[0], CONFIG, 0, context=CONTEXT, published_close_intent=exit_intent)
    entitlement = dict(state["entitlements"])
    assert list(entitlement.values()) == [100]
    step(state, feed, DAYS[1])
    assert Ledger(state, CONFIG, CONTEXT).quantity(S) == 0
    assert any(fill["side"] == "sell" and fill["day"] == DAYS[1] for fill in state["fills"].values())
    after_sale_cash = state["cash"]
    step(state, feed, DAYS[2])
    assert state["cash"] == after_sale_cash and sum(state["receivables"].values()) == 800
    assert state["entitlements"] == entitlement
    step(state, feed, DAYS[3])
    assert not state["receivables"] and state["cash"] == after_sale_cash + 800
    assert len([e for e in state["events"] if e["kind"] == "dividend_paid"]) == 1


def test_ex_date_receivable_is_equity_component_not_available_cash_until_payment(tmp_path):
    action_view = view(source(tmp_path / "hold"))
    feed, state = raw_feed(action_view), seeded(action_view)
    for day in DAYS[:2]:
        step(state, feed, day)
    before = state["cash"]
    ex_row = step(state, feed, DAYS[2])
    book = Ledger(state, CONFIG, CONTEXT)
    assert state["cash"] == before == book.cash_available()
    assert sum(state["receivables"].values()) == 800 and ex_row["receivables"] == 8.
    assert book.equity() == state["cash"] + book.position_value(S) + 800
    step(state, feed, DAYS[3])
    assert state["cash"] == before + 800 and not state["receivables"]
    assert action_view.events[0]["rights_offer_ratio_ps"] is None
    assert action_view.events[0]["rights_offer_price_rmb"] is None
    assert action_view.binding["pit_verified"] is False and action_view.binding["corporate_actions_verified"] is False
    assert state["research_corporate_action_binding"] == {**action_view.binding, "replay_start_day": "2026-05-29"}


@pytest.mark.parametrize("fault", ["unknown_bonus", "missing_payment"])
def test_incomplete_action_terms_stop_relevant_session_before_mutation(tmp_path, fault):
    path = source(tmp_path / fault, bonus=None if fault == "unknown_bonus" else "0",
                  pay="" if fault == "missing_payment" else DAYS[3])
    action_view = view(path)
    state, feed = seeded(action_view), raw_feed(action_view)
    assert (action_view.events[0]["dividStocksPs"] is None) if fault == "unknown_bonus" else not action_view.events[0]["dividPayDate"]
    before = serialized(state)
    with pytest.raises(ValueError):
        step(state, feed, DAYS[0])
    assert serialized(state) == before


def test_late_dated_evidence_rejected_but_explicit_posthoc_component_is_not_PIT(tmp_path):
    path = source(tmp_path / "late", late=True)
    dated = view(path)
    state = seeded(dated)
    before = serialized(state)
    with pytest.raises(ValueError):
        step(state, raw_feed(dated), DAYS[0])
    assert serialized(state) == before
    context = RunContext("synthetic-new-posthoc-component", TradingMode.REPLAY)
    diagnostic = view(path, mode="posthoc_component_diagnostic", context=context)
    state, feed = seeded(diagnostic, context=context), raw_feed(diagnostic)
    for day in DAYS[:4]:
        step(state, feed, day, context=context)
    assert state["cash"] == 900800 and not state["receivables"]
    assert diagnostic.binding["pit_verified"] is False and diagnostic.binding["execution_admitted"] is False
    assert diagnostic.binding["corporate_actions_verified"] is False


def test_restored_account_cannot_change_action_source_mode_tax_context_or_disable_view(tmp_path):
    path = source(tmp_path / "restore")
    original = view(path)
    state = seeded(original)
    step(state, raw_feed(original), DAYS[0])
    frozen = serialized(state)
    changed_source = view(source(tmp_path / "different_source", pdf_suffix="different pinned bytes"))
    changed_mode = view(path, mode="posthoc_component_diagnostic")
    tax_config = replace(CONFIG, dividend_tax_reserve=.1)
    changed_tax = view(path, config=tax_config)
    context = RunContext("synthetic-other-account", TradingMode.REPLAY)
    changed_context = view(path, context=context)
    for changed, config, ctx in ((changed_source, CONFIG, CONTEXT), (changed_mode, CONFIG, CONTEXT),
            (changed_tax, tax_config, CONTEXT), (changed_context, CONFIG, context), (None, CONFIG, CONTEXT)):
        trial = deepcopy(state)
        with pytest.raises(ValueError):
            step(trial, raw_feed(changed), DAYS[1], config=config, context=ctx)
        assert serialized(trial) == frozen


def test_no_opt_in_keeps_legacy_feed_and_state_free_of_action_binding():
    state = seeded()
    feed = raw_feed()
    assert feed.actions == []
    step(state, feed, DAYS[0])
    assert "research_corporate_action_binding" not in state
    assert not state["entitlements"] and not state["receivables"]


def test_fresh_cash_account_after_old_ex_can_buy_without_inheriting_old_dividend(tmp_path):
    action_view = view(source(tmp_path / "old_unknown_event", bonus=None))
    feed, state = raw_feed(action_view), new_state(CONFIG.initial_cash)
    published(state, feed, DAYS[3], {S: .1})
    assert state["research_corporate_action_binding"] == {**action_view.binding, "replay_start_day": DAYS[3]}
    assert not state["entitlements"] and not state["receivables"] and state["cash"] == 1000000
    step(state, feed, DAYS[4])
    assert Ledger(state, CONFIG, CONTEXT).quantity(S) == 100 and state["cash"] == 900000
    assert not state["entitlements"] and not state["receivables"]
    assert not any(e["kind"] in {"corporate_action", "dividend_paid"} for e in state["events"])


def test_zero_record_for_partial_unknown_is_saved_and_later_buy_gets_no_old_dividend(tmp_path):
    action_view = view(source(tmp_path / "record_zero_unknown", bonus=None))
    feed, state = raw_feed(action_view), new_state(CONFIG.initial_cash)
    step(state, feed, "2026-05-29")
    step(state, feed, DAYS[0])
    identifier = f"{S}:{DAYS[2]}"
    assert state["entitlements"][identifier] == 0
    published(state, feed, DAYS[1], {S: .1})
    step(state, feed, DAYS[2])
    assert Ledger(state, CONFIG, CONTEXT).quantity(S) == 100
    assert state["entitlements"][identifier] == 0 and not state["receivables"]
    after_buy = state["cash"]
    step(state, feed, DAYS[3])
    assert state["cash"] == after_buy == 900000 and not state["receivables"]
    assert not any(e["kind"] in {"corporate_action", "dividend_paid"} for e in state["events"])


def test_new_opt_in_cannot_bypass_old_registration_by_manually_seeding_history(tmp_path):
    action_view = view(source(tmp_path / "no_manual_history", bonus=None))
    feed = raw_feed(action_view)
    identifier = f"{S}:{DAYS[2]}"
    for field, value in (("positions", {S: [{"qty": 100, "available": DAYS[0]}]}),
            ("entitlements", {identifier: 100}), ("pending_targets", {S: .1})):
        state = new_state(CONFIG.initial_cash)
        state[field] = value
        before = serialized(state)
        with pytest.raises(ValueError):
            step(state, feed, DAYS[3])
        assert serialized(state) == before
        assert "research_corporate_action_binding" not in state
