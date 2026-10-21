from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import leader_daily_replay as runner
from trading.research.shortline_decision_v2 import BATCH_SCHEMA
from trading.contracts import RunContext, TradingMode
from trading.research.shortline_decision_v3 import FiveSessionExpiryPolicy


SYMBOLS = ["600001.SSE", "600002.SSE"]
DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"]


def raw_source():
    rows = []
    for day in ["2024-01-02", "2025-12-31", *DAYS]:
        for symbol in SYMBOLS:
            rows.append({"date": day, "symbol": symbol, "open": 10., "high": 10.05, "low": 9.95,
                         "close": 10., "volume": 5_000_000., "amount": 50_000_000., "pct_chg": 0.,
                         "preclose_for_limits": 10., "is_st": False})
    return pd.DataFrame(rows)


def source_batch(day, candidates):
    return {"schema": BATCH_SCHEMA, "day": day, "status": "observed_source_complete",
            "source": "synthetic_frozen_source", "candidates": candidates,
            "source_coverage_scope": "pinned_provider_observed_universe", "source_verified": False,
            "market_breadth": {"scope": "pinned_provider_observed_universe", "verified": False,
                              "complete": True, "raw_counts_verified": True, "day": day,
                              "verification_basis": "physical declared source partitions"}}


def bundle(raw=None):
    batches = {day: source_batch(day, [{"symbol": SYMBOLS[0], "score": 1.}] if day < "2026-01-08" else []) for day in DAYS}
    return {"raw_bars": raw_source() if raw is None else raw, "calendar": DAYS,
            "batches": {family: deepcopy(batches) for family in runner.FAMILIES},
            "source_metadata": {"snapshot": "synthetic_snapshot"}}


def test_cohort_is_frozen_before_start_not_selected_by_future_survival():
    raw = raw_source()
    future = raw.loc[raw.date.ge("2026-01-07")].copy()
    future["symbol"] = "600003.SSE"
    # B disappears after start, but MUST remain in the frozen cohort.
    raw = raw.loc[~(raw.symbol.eq(SYMBOLS[1]) & raw.date.ge("2026-01-07"))]
    raw = pd.concat([raw, future.loc[future.symbol.eq("600003.SSE")].drop_duplicates(["date", "symbol"])])
    cohort = runner.freeze_cohorts(raw, DAYS[0])
    assert cohort["anchor"] == "2025-12-31"
    assert cohort["pools"][runner.POOLS[1]] == SYMBOLS
    assert cohort["future_survival_used"] is False
    coverage = runner.dated_coverage(raw, DAYS, SYMBOLS)
    assert coverage["2026-01-07"]["status"] == "incomplete"
    assert coverage["2026-01-07"]["missing_symbols"] == [SYMBOLS[1]]


def test_missing_unheld_next_bar_does_not_block_today_but_dated_gap_stays_no_change():
    raw = raw_source()
    raw = raw.loc[~(raw.date.eq("2026-01-07") & raw.symbol.eq(SYMBOLS[1]))]
    coverage = runner.dated_coverage(raw, DAYS, SYMBOLS)
    batches, _ = runner.scope_batches(bundle(raw)["batches"][runner.FAMILIES[0]], coverage, SYMBOLS, raw,
                                     decision_end="2026-01-09")
    base = runner.CommonCohortRawFeed(raw, DAYS, SYMBOLS)
    summary, evidence = runner._case(base, batches, runner.FAMILIES[0], runner.POOLS[1], runner.EXIT_POLICIES[0],
                                     "2026-01-05", "2026-01-09")
    observed = {row["day"]: row for row in evidence["decisions"]}
    assert observed["2026-01-06"]["no_change"] is False
    assert observed["2026-01-07"]["no_change"] is True
    assert observed["2026-01-07"]["actual_holdings"] == 1
    assert summary["decision_coverage"]["incomplete_batch_held_days"] == 1
    assert not summary["open_positions"]  # Complete empty8Jan exits through Engine9Jan.
    assert {fill["side"] for fill in evidence["state"]["fills"].values()} == {"buy", "sell"}


def test_known_suspension_is_reported_without_fabricating_missing_rows():
    raw = raw_source()
    raw.loc[raw.date.eq("2026-01-07") & raw.symbol.eq(SYMBOLS[1]), ["volume", "amount"]] = 0.
    coverage = runner.dated_coverage(raw, DAYS, SYMBOLS)
    assert coverage["2026-01-07"]["status"] == "complete"
    assert coverage["2026-01-07"]["known_suspended_symbols"] == [SYMBOLS[1]]
    assert not coverage["2026-01-07"]["missing_symbols"]


def test_explicit_research_context_keeps_economics_and_rejects_paper():
    raw = raw_source()
    batches, _ = runner.scope_batches(bundle()["batches"][runner.FAMILIES[0]],
        runner.dated_coverage(raw, DAYS, SYMBOLS), SYMBOLS, raw, decision_end="2026-01-09")
    base = runner.CommonCohortRawFeed(raw, DAYS, SYMBOLS)
    args = (base, batches, runner.FAMILIES[0], runner.POOLS[1], runner.EXIT_POLICIES[0],
            "2026-01-05", "2026-01-09")
    original, _ = runner._case(*args, replay_risk_exit_policy=FiveSessionExpiryPolicy())
    scoped, evidence = runner._case(*args, replay_risk_exit_policy=FiveSessionExpiryPolicy(),
                                  context=RunContext("turnover-test:isolated", TradingMode.REPLAY))
    assert scoped["error"] is None
    assert scoped["metrics"] == original["metrics"]
    assert scoped["fills"] == original["fills"] > 0
    assert evidence["state"]["replay_risk_exit_policy"]["context"]["account_id"] == "turnover-test:isolated"
    with pytest.raises(ValueError, match="replay RunContext"):
        runner._case(*args, context=RunContext("paper-disallowed", TradingMode.PAPER))


def test_all_twenty_cases_retained_and_unverified_defense_has_no_fake_return(tmp_path):
    result = runner.compare_bundle(bundle(), tmp_path / "outputs", data_out=tmp_path / "external_data",
                                   start="2026-01-05", end="2026-01-09")
    assert result["case_count"] == 20
    assert len({(row["family"], row["pool"], row["exit_policy"]) for row in result["cases"]}) == 20
    assert result["window_already_inspected"] and not result["future_independent_40_session_window_run"]
    assert result["execution_admitted"] is False
    blocked = [row for row in result["cases"] if row["exit_policy"] == runner.EXIT_POLICIES[1]]
    assert len(blocked) == 10 and all(row["metrics"] is None for row in blocked)
    assert all(row["status"] == "unavailable_verified_full_market_breadth" for row in blocked)
    base = [row for row in result["cases"] if row["exit_policy"] == runner.EXIT_POLICIES[0]]
    assert all(row["orders"] > 0 and row["fills"] > 0 for row in base)
    assert len({row["metrics"]["net_return"] for row in base}) == 1  # Same synthetic signals/costs.
    assert not list((tmp_path / "outputs").rglob("account.json"))
    assert len(list((tmp_path / "external_data").rglob("account.json"))) == 10
    protocol = json.loads((tmp_path / "outputs/protocol.json").read_text(encoding="utf-8"))
    assert protocol["execution_tail_sessions"] == ["2026-01-12", "2026-01-13"]


def test_tail_keeps_missing_first_exchange_session_and_no_new_tail_signals():
    raw = raw_source().loc[lambda frame: frame.date.ne("2026-01-12")]
    coverage = runner.dated_coverage(raw, DAYS, SYMBOLS)
    batches, _ = runner.scope_batches(bundle(raw)["batches"][runner.FAMILIES[0]], coverage, SYMBOLS, raw,
                                     decision_end="2026-01-09")
    assert coverage["2026-01-12"]["status"] == "incomplete"
    assert batches["2026-01-13"]["status"] == "incomplete"
    assert batches["2026-01-13"]["incomplete_reason"] == "execution_tail_no_new_decision"
    assert batches["2026-01-13"]["candidates"] == []
    base = runner.CommonCohortRawFeed(raw, DAYS, SYMBOLS)
    assert base.next_day("2026-01-09") == "2026-01-12"


def test_zero_complete_batches_have_no_strategy_return_and_blocker_types_remain_distinct(tmp_path):
    raw = raw_source()
    # Keep B in the anchor cohort, then retain its observed facts separately:
    # one genuine missing row, one explicit zero-OHLC halt, one ST-unknown row.
    raw = raw.loc[~(raw.symbol.eq(SYMBOLS[1]) & raw.date.eq("2026-01-05"))].copy()
    raw.loc[raw.symbol.eq(SYMBOLS[1]) & raw.date.eq("2026-01-06"), ["volume", "amount", "open", "high", "low", "close"]] = 0.
    raw["is_st_unknown"] = raw.symbol.eq(SYMBOLS[1]) & raw.date.eq("2026-01-07")
    raw["source_row_valid"] = ~raw.is_st_unknown
    coverage = runner.dated_coverage(raw, DAYS, SYMBOLS)
    assert coverage["2026-01-05"]["missing_symbols"] == [SYMBOLS[1]]
    assert coverage["2026-01-06"]["known_halt_zero_ohlc_symbols"] == [SYMBOLS[1]]
    assert coverage["2026-01-07"]["st_unknown_symbols"] == [SYMBOLS[1]]
    runner.export_data_blockers(list(coverage.values()), tmp_path)
    audit = pd.read_csv(tmp_path / "data_blocker_daily.csv")
    assert audit.loc[audit.day.eq("2026-01-05"), "missing_count"].item() == 1
    assert audit.loc[audit.day.eq("2026-01-06"), "known_halt_zero_ohlc_count"].item() == 1
    assert audit.loc[audit.day.eq("2026-01-07"), "st_unknown_count"].item() == 1
    summary = {"status": "completed_already_inspected_engine_diagnostic", "metrics": {"net_return": 0.},
               "decision_coverage": {"complete_batch_days": 0}}
    runner.reporting_boundaries(summary)
    assert summary["status"] == "unavailable_complete_cohort_decision_batches"
    assert summary["metrics"] is None and summary["engine_no_change_account_metrics"]["net_return"] == 0.
