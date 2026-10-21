"""Fixed synthetic behavior, never a market or strategy validation."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import leader_missing_type_candidates as module

C, L, P = "000001.SZSE", "600000.SSE", "600001.SSE"
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []}


def fixture():
    views, closes = [], {symbol: 10. for symbol in [C, L, P]}
    pcts = [{C: 0., L: 10., P: 2.}, {C: 10., L: 2., P: 1.}, {C: 4., L: 1., P: 2.}]
    for date, values in zip(DAYS, pcts):
        rows = []
        for symbol, pct in values.items():
            previous = closes[symbol]; close = previous * (1 + pct / 100); closes[symbol] = close
            limit = (symbol == L and date == DAYS[0]) or (symbol == C and date == DAYS[1])
            rows.append({"date": date, "symbol": symbol, "amount": 30_000_000., "pct_chg": pct,
                "open": previous, "high": max(previous, close) + .1, "low": min(previous, close) - .1,
                "close": close, "preclose_for_limits": previous, "limit_up": limit,
                "stage": 2 if symbol == L and date == DAYS[0] else 1 if limit else 0,
                "listing_qualification_status": "supported"})
        proof = {"day": date, "read_only_verified": True, "source_inventory_complete": True,
            "full_dated_roster_inventory_complete": True, "receipt_status": "complete_full_dated_close_inventory_not_admitted",
            "raw_daily_sha256": "raw-" + date, "roster_raw_sha256": "roster-" + date,
            "source_updated_at": date + "T17:00:00+08:00"}
        views.append({"day": date, "features": pd.DataFrame(rows), "ordinary_rows": pd.DataFrame([
            {"date": date, "symbol": symbol, "isST": "0", "tradestatus": "1"} for symbol in values]),
            "available_at": date + "T17:10:00+08:00", "source_proof": proof,
            "feature_method_id": module.FEATURE_METHOD, "feature_qualification_verified": True})
    tracking = {"schema": "leader_prospective_proxy_tracking_report_v1", "registry_sha256": "frozen",
        "source": {"day": "2026-10-09"},
        "registered_at": DAYS[0] + "T10:00:00+08:00", "read_at": DAYS[2] + "T18:00:00+08:00",
        "rule": {"id": "first_close_phase_change_three_closes_two_intervals_v1"},
        "observed_close_days": DAYS[:], "member_count": 2,
        "episode": {"fixed_window_close_days": DAYS[:], "complete_close_count": 3},
        "members": [{"symbol": C, "labels": ["trend_leader"], "status": "price_proxy_conditions_matched"},
                    {"symbol": L, "labels": ["relay_continuation_leader"], "status": "price_proxy_conditions_not_matched"}]}
    industry = {"membership": pd.DataFrame([{"symbol": symbol, "industry": "G"} for symbol in [C, L, P]]),
        "snapshot_date": "2026-09-30", "available_at": "2026-10-04T03:41:51+08:00",
        "source_file": "saved_mapping", "source_sha256": "industry", "classification": "CSRC broad",
        "theme_membership": False}
    turns = [{"day": date, "rows": pd.DataFrame([{"symbol": symbol, "turn_percent": turn} for symbol in [C, L, P]]),
              "unit": "percent", "units_verified": True, "available_at": date + "T18:10:00+08:00",
              "price_source_raw_sha256": "raw-" + date, "source_proof": {"day": date}}
             for date, turn in zip(DAYS[:2], ["3.5000", "4.2500"])]
    return {"day": DAYS[2], "observed_at": DAYS[2] + "T20:00:00+08:00", "calendar_payload": CALENDAR,
            "scope_members": [C, L, P], "complete_views": views, "identity_tracking": tracking,
            "industry_membership": industry, "turnover_batches": turns}


def result(options, identity):
    return next(value for value in module.build_missing_type_candidates(**options)["types"] if value["id"] == identity)


def at13(options):
    options["day"] = DAYS[1]
    options["observed_at"] = DAYS[1] + "T20:00:00+08:00"
    options["identity_tracking"]["read_at"] = DAYS[1] + "T19:00:00+08:00"
    options["identity_tracking"]["observed_close_days"] = DAYS[:2]
    options["identity_tracking"]["episode"] = None
    return options


def test_turnover_expansion_uses_exact_own_day_percent_and_preserves_clocks():
    options = at13(fixture()); value = result(options, "turnover_leader")
    assert value["status"] == "matched" and [row["symbol"] for row in value["candidates"]] == [C]
    candidate = value["candidates"][0]
    assert candidate["evidence"]["current_turn_percent"] == "4.2500"
    assert candidate["available_at"] == DAYS[1] + "T18:10:00+08:00" and candidate["confirmed_leader"] is False
    json.dumps(module.build_missing_type_candidates(**options), allow_nan=False)


def test_absent_turnover_source_is_unknown_not_empty_and_keeps_full_scope():
    options = at13(fixture()); options["turnover_batches"] = options["turnover_batches"][:1]
    value = result(options, "turnover_leader")
    assert value["status"] == "unknown" and value["unresolved_symbols"] == [C]
    assert value["scope_members"] == [C, L, P] and value["scope_complete"] is False
    assert set(value["known_exclusions"]) == {L, P}


@pytest.mark.parametrize("turn", ["", "NaN", "-1"])
def test_unknown_invalid_turn_is_never_replaced_by_zero(turn):
    options = at13(fixture()); options["turnover_batches"][1]["rows"].loc[0, "turn_percent"] = turn
    value = result(options, "turnover_leader")
    assert C in value["unresolved_symbols"] and not value["candidates"]


def test_equal_turn_is_known_negative_not_a_turnover_expansion():
    options = at13(fixture()); options["turnover_batches"][1]["rows"]["turn_percent"] = "3.5"
    value = result(options, "turnover_leader")
    assert value["status"] == "not_matched" and value["scope_complete"]


def test_catchup_is_broad_industry_lag_then_qualified_first_board():
    options = at13(fixture()); value = result(options, "sector_catchup_leader")
    assert [row["symbol"] for row in value["candidates"]] == [C]
    assert value["candidates"][0]["evidence"]["frozen_prior_leaders"] == [L]
    assert value["candidates"][0]["evidence"]["not_verified_theme_catchup_leader"]


def test_catchup_accepts_prior_close_identity_registered_that_evening_but_not_future_source_identity():
    options = at13(fixture())
    options["identity_tracking"]["source"]["day"] = DAYS[0]
    options["identity_tracking"]["registered_at"] = DAYS[0] + "T18:00:00+08:00"
    assert [row["symbol"] for row in result(options, "sector_catchup_leader")["candidates"]] == [C]
    options["identity_tracking"]["source"]["day"] = DAYS[2]
    assert C in result(options, "sector_catchup_leader")["unresolved_symbols"]


def test_prior_tie_is_not_lag_and_missing_sector_peer_is_not_survivor_median():
    options = at13(fixture())
    options["complete_views"][0]["features"].loc[0, "pct_chg"] = 2.
    assert C in result(options, "sector_catchup_leader")["known_exclusions"]
    options = at13(fixture())
    options["complete_views"][1]["features"] = options["complete_views"][1]["features"].iloc[:2].copy()
    value = result(options, "sector_catchup_leader")
    assert C in value["unresolved_symbols"] and P in value["unresolved_symbols"] and not value["candidates"]


def test_real_parquet_null_industry_is_unknown_not_absence_of_sector_leader():
    options = at13(fixture())
    options["industry_membership"]["membership"].loc[0, "industry"] = float("nan")
    value = result(options, "sector_catchup_leader")
    assert C in value["unresolved_symbols"] and C not in value["known_exclusions"]


def test_cross_confirmation_is_available_only_after_third_close_and_all_late_proofs():
    options = fixture(); value = result(options, "cross_regime_candidate")
    assert [row["symbol"] for row in value["candidates"]] == [C]
    candidate = value["candidates"][0]
    assert candidate["evidence"]["confirmation_day"] == DAYS[2]
    assert candidate["available_at"] == DAYS[2] + "T18:00:00+08:00"
    options["day"] = DAYS[1]
    assert C in result(options, "cross_regime_candidate")["unresolved_symbols"]


def test_late_registration_and_future_report_never_backfill_prior_identity():
    options = fixture(); options["identity_tracking"]["registered_at"] = DAYS[0] + "T16:00:00+08:00"
    assert C in result(options, "cross_regime_candidate")["unresolved_symbols"]
    options = fixture(); options["identity_tracking"]["observed_close_days"].append("2026-10-15")
    assert C in result(options, "cross_regime_candidate")["unresolved_symbols"]


def test_future_source_suffix_does_not_change_earlier_result():
    options = at13(fixture())
    first = module.build_missing_type_candidates(**options)
    options["complete_views"][-1]["features"]["pct_chg"] = 999.
    assert first == module.build_missing_type_candidates(**options)


def test_known_ST_exclusion_and_missing_fixed_member_are_separate():
    options = fixture(); options["complete_views"][-1]["ordinary_rows"].loc[0, "isST"] = "1"
    options["scope_members"].append("009999.SZSE")
    value = result(options, "turnover_leader")
    assert value["known_exclusions"][C] == "known_current_ST_excluded"
    assert "009999.SZSE" in value["unresolved_symbols"] and value["scope_member_count"] == 4


def test_partial_source_or_mismatched_turnover_binding_is_rejected():
    options = fixture(); options["complete_views"][0]["source_proof"]["source_inventory_complete"] = False
    with pytest.raises(ValueError, match="strict complete"):
        module.build_missing_type_candidates(**options)
    options = at13(fixture()); options["turnover_batches"][0]["price_source_raw_sha256"] = "another"
    with pytest.raises(ValueError, match="exact price source"):
        module.build_missing_type_candidates(**options)
