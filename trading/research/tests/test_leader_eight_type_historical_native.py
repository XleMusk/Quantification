"""New historical boundaries and the original Engine; no real history runs."""
from copy import deepcopy
from pathlib import Path

import pandas as pd

from trading.research import leader_eight_type_historical_native as history
from trading.research import leader_eight_type_panel_replay as original
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view
from trading.research.leader_historical_execution_policy import HistoricalExecutionFactory
from trading.research.stockdb_action_accounting import StockDBActionAccounting


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-01-01/2026-01-03", "2026-06-19/2026-06-21"], "open_sessions": []}
SESSIONS, PREVIOUS, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
A, B = "000001.SZSE", "000002.SZSE"
OBSERVED = "2026-10-17T00:00:00+08:00"


def qualification(symbols=(A, B)):
    records = {symbol: {"ipo_date": "1991-04-03", "available_at": "2026-10-08T12:00:00+08:00",
        "source_basis": "synthetic_saved_provider_metadata", "board": "main", "reported_out_date": ""}
        for symbol in symbols}
    q = history.PosthocListingQualification(records, tuple(SESSIONS), {"posthoc_observed_at": OBSERVED})
    witness = {symbol: ["2025-12-24", "2025-12-25", "2025-12-26", "2025-12-29", "2025-12-30"] for symbol in symbols}
    return history.with_prior_listing_witness(q, witness, "2026-01-05")


def bars(days, symbols=(A, B)):
    rows = []
    for day in days:
        for index, symbol in enumerate(symbols):
            volume = 10000000 if index == 0 else 5000000
            rows.append({"date": day, "code": ("sh." if symbol.endswith("SSE") else "sz.") + symbol[:6],
                "open": "10", "high": "10.1", "low": "9.9", "close": "10", "preclose": "10",
                "volume": str(volume), "amount": str(volume * 10), "adjustflag": "3", "turn": "2",
                "tradestatus": "1", "pctChg": "0", "peTTM": "", "pbMRQ": "", "psTTM": "",
                "pcfNcfTTM": "", "isST": "0"})
    return pd.DataFrame(rows)


def accounting(symbols, raw_days, source_days, actions=()):
    universe = pd.DataFrame({"code": [("sh." if symbol.endswith("SSE") else "sz.") + symbol[:6]
                                     for symbol in symbols], "symbol": symbols})
    return StockDBActionAccounting(list(actions), [], [], {(code, 2026): True for code in universe.code},
        universe, {"rename": None}, symbols, raw_days, source_days,
        {"price_source": "independent_baostock_raw", "returns_reportable": False})


def test_posthoc_metadata_keeps_actual_clock_and_requires_explicit_prior_listing_witness():
    q = qualification()
    before = deepcopy(q.records)
    old = IpoQualification(deepcopy(q.records), q.calendar, {})
    assert old.assess(A, "2026-01-05")["knowledge_status"] == "unknown"
    proof = q.assess(A, "2026-01-05", decision_as_of="2026-01-05T09:30:00+08:00")
    assert proof["engine_block_reason"] is None and proof["ipo_window_feature_support"] is True
    assert proof["original_available_at"] == "2026-10-08T12:00:00+08:00"
    assert proof["decision_as_of"] == OBSERVED
    assert proof["historical_cutoff_knowledge_verified"] is False and proof["pit_verified"] is False
    assert proof["listing_session_ordinal_lower_bound"] == 6 and q.records == before
    unknown = history.PosthocListingQualification({A: before[A] | {"ipo_date": "2026-01-15"}}, q.calendar, q.verification)
    assert unknown.assess(A, "2026-01-05")["engine_block_reason"] == "not_yet_listed"


def test_native_producer_keeps_all_eight_types_missing_identities_and_causal_turnover():
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    raw = bars(days)
    at = raw.date.eq(days[1]) & raw.code.eq("sz.000001")
    raw.loc[at, ["open", "high", "low", "close", "preclose", "pctChg", "turn", "amount"]] = [
        "10.8", "11.1", "10.5", "11", "10", "10", "3", "110000000"]
    last = raw.date.eq(days[2]) & raw.code.eq("sz.000001")
    raw.loc[last, ["open", "high", "low", "close", "preclose", "amount"]] = ["11", "11.1", "10.9", "11", "11", "110000000"]
    result = history.derive_panels(raw, days, [A, B], CALENDAR, qualification(), observed_at=OBSERVED)
    panel = result[days[1]]
    assert [item["id"] for item in panel["types"]] == list(original.TYPE_IDS)
    for identity in ("cross_regime_candidate", "sector_catchup_leader"):
        item = next(item for item in panel["types"] if item["id"] == identity)
        assert item["status"] == "unknown" and item["unresolved_symbols"] == [A, B]
    turn = next(item for item in panel["types"] if item["id"] == "turnover_leader")
    assert [row["symbol"] for row in turn["candidates"]] == [A]
    changed = raw.copy()
    changed.loc[changed.date.eq(days[2]), "turn"] = "99999"
    changed.loc[changed.date.eq(days[2]), "amount"] = "999999999"
    second = history.derive_panels(changed, days, [A, B], CALENDAR, qualification(), observed_at=OBSERVED)
    assert second[days[1]] == result[days[1]]
    assert panel["environment"]["historical_dated_roster_verified"] is False


def test_original_all_32_cases_run_with_native_action_hook_and_same_fixed_costs(tmp_path):
    at = SESSIONS.index("2026-01-12")
    days = SESSIONS[at:at+10]
    before = SESSIONS[at-1]
    raw = bars([before] + days)
    q = qualification()
    panels = history.derive_panels(raw, days, [A, B], CALENDAR, q, observed_at=OBSERVED)
    overlay = accounting([A, B], [before]+days, days)
    result = original.replay_eight_types(panel_by_day=panels, raw=raw.loc[raw.date.isin(days)], source_days=days,
        calendar_payload=CALENDAR, scope_members=[A, B], qualification=q,
        context_anchor={"day": before, "closes": {A: 10., B: 10.}}, start=days[0], end=days[1], out=tmp_path / "run",
        context_raw=raw.loc[raw.date.eq(before)], execution_policy_factory=HistoricalExecutionFactory(overlay))
    assert result["case_count"] == 32 and result["protocol"] == original.PROTOCOL
    assert result["config"] == original._config(100000, 5).to_dict()
    assert any(case["fills"] for case in result["cases"])
    assert all(case["returns_reportable"] is False for case in result["cases"])
    assert overlay.checks == []  # Each of the 32 cases owns its own overlay.
    assert all(case["metrics"] is None for case in result["cases"] if not case["fills"])


def test_opt_in_action_formula_passes_legitimate_split_that_old_proxy_rejects():
    symbol = "600000.SSE"
    days = ["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25"]
    before = "2026-06-18"
    raw = bars([before]+days, [symbol])
    action = {"code": "sh.600000", "symbol": symbol, "dividRegistDate": days[1],
        "dividOperateDate": days[2], "dividPayDate": days[2], "dividStockMarketDate": days[3],
        "dividCashPsBeforeTax": "0.20", "dividStocksPs": "0", "dividReserveToStockPs": "0.45"}
    raw.loc[raw.date.eq(days[2]), ["open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus"]] = [
        "0", "0", "0", "6.76", str(9.8/1.45), "", "", "0"]
    raw.loc[raw.date.eq(days[3]), ["open", "high", "low", "close", "preclose"]] = ["6.76", "6.8", "6.7", "6.76", "6.76"]
    panels = {day: {"day": day, "available_at": OBSERVED,
        "environment": {"source_complete": True, "breadth": .5, "median_return_pct": 1.},
        "types": [{"id": identity, "status": "matched" if day in days[:2] else "not_matched",
            "candidates": [{"symbol": symbol, "amount_cny": 100000000.}] if day in days[:2] else [],
            "unresolved_symbols": []} for identity in original.TYPE_IDS]} for day in days}
    paths, _ = original._rank_path(panels, {symbol}, days[0], days[1])
    env = original._environment(panels, SESSIONS, OBSERVED)
    args = (raw.loc[raw.date.isin(days)], days, SESSIONS, PREVIOUS, panels,
        paths["liquidity_capacity_proxy"]["uncapped"], env, {symbol}, qualification([symbol]),
        {"day": before, "closes": {symbol: 10.}}, days[0], days[1], "liquidity_capacity_proxy", "uncapped", "unguarded")
    unchanged = original._case(*args, "synthetic-old")
    overlay = accounting([symbol], [before]+days, days, [action])
    fixed = original._case(*args, "synthetic-new", context_raw=raw.loc[raw.date.eq(before)],
        execution_policy_factory=HistoricalExecutionFactory(overlay))
    assert unchanged["engine_sessions_committed"] < len(days)
    assert fixed["engine_sessions_committed"] == len(days) and fixed["blocking_evidence"] is None, fixed["blocking_evidence"]
    assert any(event["kind"] == "corporate_action" for event in fixed["account"]["events"])
    assert any(row["reference_formula_status"] == "reference_match" for row in fixed["action_reference_checks"])
    assert sum(lot["qty"] for lot in fixed["account"]["positions"][symbol]) > sum(
        fill["qty"] for fill in fixed["fills"] if fill["side"] == "buy")


def test_missing_successor_quote_keeps_recorded_D_target_and_blocks_only_E():
    days = ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15"]
    before = "2026-01-09"
    raw = bars([before]+days)
    raw = raw.loc[~(raw.date.eq(days[1]) & raw.code.eq("sz.000001"))].copy()
    panels = {day: {"day": day, "available_at": OBSERVED,
        "environment": {"source_complete": True, "breadth": .5, "median_return_pct": 1.},
        "types": [{"id": identity, "status": "matched",
            "candidates": [{"symbol": A, "amount_cny": 100000000.}], "unresolved_symbols": []}
            for identity in original.TYPE_IDS]} for day in days}
    paths, _ = original._rank_path(panels, {A, B}, days[0], days[0])
    env = original._environment(panels, SESSIONS, OBSERVED)
    overlay = accounting([A, B], [before]+days, days)
    result = original._case(raw.loc[raw.date.isin(days)], days, SESSIONS, PREVIOUS, panels,
        paths["liquidity_capacity_proxy"]["uncapped"], env, {A, B}, qualification(),
        {"day": before, "closes": {A: 10., B: 10.}}, days[0], days[0],
        "liquidity_capacity_proxy", "uncapped", "unguarded", "synthetic-causal-D",
        context_raw=raw.loc[raw.date.eq(before)], execution_policy_factory=HistoricalExecutionFactory(overlay))
    assert result["engine_sessions_committed"] == 1
    assert result["decisions"][0]["status"] == "FULL_TARGET"
    assert result["decisions"][0]["successor_quotes_used_for_candidate_clearance"] is False
    assert result["account"]["day"] == days[0] and A in result["account"]["pending_targets"]
    assert result["blocking_evidence"]["day"] == days[1]
    assert result["blocking_evidence"]["session_not_committed"] is True
    assert not result["fills"] and result["metrics"] is None
