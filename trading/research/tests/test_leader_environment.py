"""Synthetic behavior checks, never evidence of market or strategy advantage."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view
from trading.research.leader_environment import build_environment


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": [], "open_sessions": []}
OBSERVED = "2026-10-15T10:30:00+08:00"


def row(symbol="000001.SZSE", day="2026-10-14", **changes):
    values = dict(symbol=symbol, name="fixture", date=day, board="main", open=10.0, high=11.0,
        low=10.0, close=11.0, volume=3_000_000, amount=30_000_000.0, pct_chg=10.0,
        preclose_for_limits=10.0, limit_up=True, stage=2, stage_reference_known=True,
        previous_observed_day="2026-10-13", previous_close=10.0, previous_open=10.0,
        previous_pct_chg=1.0, calendar_contiguous=True, sma20=10.0,
        sma60=9.0, ret20=.1, ret60=.2, amount_rank_pct=1.0, daily_rebound=False)
    values.update(changes)
    return values


def envelope(frame, **changes):
    day = frame.date.iloc[0]
    n = len(frame)
    tape = dict(day=day, scope="complete_dated_provider_ordinary_SH_SZ_active_non_ST",
        active_non_st_rows=n, advance_count=int(frame.pct_chg.gt(0).sum()),
        decline_count=int(frame.pct_chg.lt(0).sum()), flat_count=int(frame.pct_chg.eq(0).sum()),
        breadth=float((frame.pct_chg.gt(0).sum()-frame.pct_chg.lt(0).sum())/n),
        median_return_pct=float(frame.pct_chg.median()), amount_cny=float(frame.amount.sum()),
        signed_turnover_association=1.0, top20_turnover_share=1.0)
    result = dict(schema="leader_complete_close_preview_v1", day=day,
        status="complete_source_close_research_preview_only", created_at=day+"T16:00:00+08:00",
        source_inventory_complete=True, execution_admitted=False, tape=tape,
        source={"read_only_verified": True, "source_folder": "synthetic-not-market-evidence",
                "plan": {"day": day, "roster_raw_sha256": "fixture"},
                "ordinary_roster": [{"code": "do-not-repeat-5000-items"}],
                "receipt": {"day": day, "status": "complete_full_dated_close_inventory_not_admitted",
                    "full_dated_roster_inventory_complete": True, "raw_daily_sha256": "fixture",
                    "updated_at": day+"T15:50:00+08:00"}})
    result.update(changes)
    return result


def qualification(*symbols, new_ipo=None):
    sessions, _, _ = _calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
    records = {symbol: {"ipo_date": "2010-01-01", "available_at": "2026-01-01T00:00:00+08:00",
        "board": "main", "source_basis": "synthetic"} for symbol in symbols}
    if new_ipo:
        records[new_ipo] = {"ipo_date": "2026-10-13", "available_at": "2026-10-10T23:59:59+08:00",
            "board": "chinext", "source_basis": "synthetic", "rule_effective_from": "2026-07-06"}
    return IpoQualification(records, tuple(sessions), {"synthetic": True})


def analyse(frame, *, prior=None, proof=None, tapes=None, observed=OBSERVED):
    return build_environment(frame, tapes or [envelope(frame)] + ([envelope(piece) for _, piece in prior.groupby("date")] if prior is not None else []),
        CALENDAR, observed, feature_history=prior, ipo_qualification=proof)


def archetype(result, identity):
    return next(item for item in result["archetypes"] if item["id"] == identity)


def test_refuses_partial_source_and_feature_subset_without_silent_fallback():
    frame = pd.DataFrame([row(), row("000002.SZSE")])
    with pytest.raises(ValueError, match="Partial"):
        analyse(frame, tapes=[envelope(frame, source_inventory_complete=False)])
    with pytest.raises(ValueError, match="subset"):
        analyse(frame.iloc[:1], tapes=[envelope(frame)])
    proof = envelope(frame)
    proof["source"]["receipt"]["status"] = "running"
    with pytest.raises(ValueError, match="complete-package"):
        analyse(frame, tapes=[proof])


def test_exact_previous_complete_close_required_not_last_observed_day():
    current = pd.DataFrame([row()])
    old = pd.DataFrame([row(day="2026-10-09", stage=1)])
    result = analyse(current, prior=old, proof=qualification("000001.SZSE"))
    change = result["market_environment"]["change"]
    assert change["status"] == "unknown" and change["previous_day_required"] == "2026-10-13"
    assert result["freshness"] == "prior_close_not_current_session_or_intraday"
    assert result["data_availability"]["current_intraday"] is False
    assert archetype(result, "cross_regime_candidate")["status"] == "unknown"
    assert result["market_environment"]["state_semantics"] == "single_close_distribution_not_market_cycle_or_forecast"


def test_IPO_first_five_is_not_a_limit_streak_or_space_leader():
    frame = pd.DataFrame([row("301660.SZSE", board="chinext", stage=9), row(stage=2)])
    result = analyse(frame, proof=qualification("000001.SZSE", new_ipo="301660.SZSE"))
    ladder = result["market_environment"]["limit_and_ladder"]
    assert ladder["raw_limit_up_proxy_count"] == 2
    assert ladder["IPO_supported_limit_up_proxy_count"] == 1
    assert ladder["IPO_unsupported_feature_rows"] == 1
    for identity in ("space_height_leader", "relay_continuation_leader", "trend_leader"):
        assert [item["symbol"] for item in archetype(result, identity)["candidates"]] == ["000001.SZSE"]


def test_post_IPO_streak_and_rebound_cannot_inherit_unrestricted_first_five_sessions():
    proof = qualification(new_ipo="301660.SZSE")
    # Oct20 is the sixth session after the fixture's Oct13 listing. A six-bar
    # old producer streak and its D-2 rebound input both start unrestricted.
    frame = pd.DataFrame([row("301660.SZSE", day="2026-10-20", board="chinext", stage=6, daily_rebound=True)])
    result = analyse(frame, proof=proof, observed="2026-10-21T10:00:00+08:00")
    ladder = result["market_environment"]["limit_and_ladder"]
    assert ladder["IPO_supported_limit_up_proxy_count"] == 1
    assert ladder["qualified_stage_rows"] == 0
    assert archetype(result, "relay_continuation_leader")["candidate_count"] == 0
    assert archetype(result, "rebound_repair_candidate")["status"] == "unknown"
    # The eighth session can start a three-session streak wholly after the
    # first five; this is a supported daily proxy, not an admitted signal.
    frame["date"], frame["stage"] = "2026-10-22", 3
    prior = pd.DataFrame([row("301660.SZSE", day="2026-10-21", board="chinext", pct_chg=-1,
        close=10.0, open=10.5, preclose_for_limits=11.0, stage=0, limit_up=False),
        row("301660.SZSE", day="2026-10-20", board="chinext", close=11.0, stage=1)])
    result = analyse(frame, prior=prior, proof=proof, observed="2026-10-23T10:00:00+08:00")
    assert archetype(result, "relay_continuation_leader")["candidate_count"] == 1
    assert archetype(result, "rebound_repair_candidate")["candidate_count"] == 1


def test_relay_denominator_keeps_disappeared_previous_leader_as_unknown():
    old = pd.DataFrame([row(day="2026-10-13", stage=1, close=10.0),
                        row("000002.SZSE", day="2026-10-13", stage=1, close=10.0)])
    current = pd.DataFrame([row(stage=2), row("000003.SZSE", pct_chg=-1.0, stage=0, limit_up=False)])
    proof = qualification("000001.SZSE", "000002.SZSE", "000003.SZSE")
    result = analyse(current, prior=old, proof=proof)
    relay = result["market_environment"]["change"]["relay_by_prior_stage"]["1_to_2"]
    assert relay["prior_cohort_count"] == 2 and relay["known_outcome_count"] == 1
    assert relay["continuation_count"] == 1 and relay["continuation_rate"] is None
    assert relay["continuation_rate_bounds"] == [.5, 1.0]
    assert relay["unknown_outcomes"][0]["symbol"] == "000002.SZSE"


def test_relay_requires_price_reference_and_exact_stage_continuity():
    old = pd.DataFrame([row(day="2026-10-13", stage=1, close=10.0)])
    current = pd.DataFrame([row(stage=2, preclose_for_limits=8.0)])
    proof = qualification("000001.SZSE")
    result = analyse(current, prior=old, proof=proof)
    relay = result["market_environment"]["change"]["relay_by_prior_stage"]["1_to_2"]
    assert relay["continuation_rate"] is None
    assert relay["unknown_outcomes"][0]["reason"] == "calendar_or_raw_price_reference_unknown"
    current["preclose_for_limits"] = 10.0
    current["stage"] = 4
    result = analyse(current, prior=old, proof=proof)
    relay = result["market_environment"]["change"]["relay_by_prior_stage"]["1_to_2"]
    assert relay["unknown_outcome_count"] == 1


def test_complete_pair_reports_relay_and_strong_stock_losses_without_strategy_PnL():
    old = pd.DataFrame([row(day="2026-10-13", stage=2, close=10.0)])
    current = pd.DataFrame([row(stage=0, limit_up=False, pct_chg=-6.0, close=9.4, low=9.0)])
    result = analyse(current, prior=old, proof=qualification("000001.SZSE"))
    change = result["market_environment"]["change"]
    assert change["relay_by_prior_stage"]["2_to_3"]["continuation_rate"] == 0
    feedback = change["strong_stock_feedback"]
    assert feedback["status"] == "complete_prior_cohort_outcomes"
    assert feedback["broken_limit_count"] == feedback["negative_return_count"] == feedback["loss_at_least5pct_count"] == 1
    assert feedback["mean_return_pct"] == -6.0 and "not_strategy_PnL" in feedback["return_basis"]


def test_prior_unknown_stage_membership_is_shared_and_full_relay_rates_remain_unknown():
    old = pd.DataFrame([row(day="2026-10-13", stage=1, close=10.0),
        row("000002.SZSE", day="2026-10-13", stage=np.nan, stage_reference_known=False, close=10.0)])
    current = pd.DataFrame([row(stage=2), row("000002.SZSE", stage=np.nan, stage_reference_known=False)])
    result = analyse(current, prior=old, proof=qualification("000001.SZSE", "000002.SZSE"))
    change = result["market_environment"]["change"]
    shared = change["unknown_stage_cohort"]
    assert shared["member_count"] == 1 and shared["symbols"] == ["000002.SZSE"]
    assert shared["assignment"] == "unassigned_shared_cohort_not_duplicated_across_stages"
    relay = change["relay_by_prior_stage"]
    first = relay["1_to_2"]
    assert first["status"] == "unknown" and first["continuation_rate"] is None
    assert first["known_stage_count"] == 1 and first["possible_denominator_range"] == [1, 2]
    assert first["known_stage_conditional_continuation_rate"] == 1.0
    assert first["continuation_rate_bounds"] == [.5, 1.0]
    assert sum(item["known_stage_count"] for item in relay.values()) == 1
    for identity in ("2_to_3", "3plus_to_next"):
        assert relay[identity]["possible_denominator_range"] == [0, 1]
        assert relay[identity]["continuation_rate"] is None and relay[identity]["continuation_rate_bounds"] == [0.0, 1.0]


def test_shared_unknown_stage_prevents_exact_strong_cohort_mean_and_keeps_missing_members():
    old = pd.DataFrame([row(day="2026-10-13", stage=2, close=10.0),
        row("000002.SZSE", day="2026-10-13", stage=np.nan, stage_reference_known=False, close=10.0)])
    # The unknown-stage member has disappeared from current active rows. It
    # must still remain possible prior stage2+ membership, rather than vanish.
    current = pd.DataFrame([row(stage=0, limit_up=False, pct_chg=-6.0, close=9.4, low=9.0)])
    result = analyse(current, prior=old, proof=qualification("000001.SZSE", "000002.SZSE"))
    change = result["market_environment"]["change"]
    assert change["unknown_stage_cohort"]["member_count"] == 1
    feedback = change["strong_stock_feedback"]
    assert feedback["status"] == "unknown" and feedback["mean_return_pct"] is None
    assert feedback["known_stage2plus_count"] == 1 and feedback["possible_cohort_count_range"] == [1, 2]
    assert feedback["conditional_known_stage_mean_return_pct"] == -6.0
    assert feedback["loss_at_least5pct_count"] == 1 and "not_full_possible_cohort" in feedback["counts_scope"]


def test_membership_and_known_member_outcome_gaps_both_widen_relay_bounds():
    old = pd.DataFrame([row(day="2026-10-13", stage=1, close=10.0),
        row("000002.SZSE", day="2026-10-13", stage=1, close=10.0),
        row("000003.SZSE", day="2026-10-13", stage=np.nan, stage_reference_known=False, close=10.0)])
    current = pd.DataFrame([row(stage=2)])
    result = analyse(current, prior=old, proof=qualification("000001.SZSE", "000002.SZSE", "000003.SZSE"))
    relay = result["market_environment"]["change"]["relay_by_prior_stage"]["1_to_2"]
    assert relay["known_stage_count"] == 2 and relay["unknown_outcome_count"] == 1
    assert relay["unknown_stage_membership_count"] == 1 and relay["possible_denominator_range"] == [2, 3]
    assert relay["continuation_rate"] is None and relay["known_stage_conditional_continuation_rate"] is None
    assert relay["continuation_rate_bounds"] == [1/3, 1.0]


def test_rebound_False_from_missing_D_minus2_is_unknown_not_a_negative_match():
    current = pd.DataFrame([row(daily_rebound=False, previous_pct_chg=-1.0)])
    result = analyse(current, proof=qualification("000001.SZSE"))
    rebound = archetype(result, "rebound_repair_candidate")
    assert rebound["status"] == "unknown" and rebound["candidate_count"] == 0
    assert rebound["unknown_rebound_path_liquid_rows"] == 1
    assert rebound["rebound_path_status_counts"] == {"matched": 0, "not_matched": 0, "unknown": 1}
    assert rebound["unresolved_rebound_symbols"] == ["000001.SZSE"]
    assert "rebound_D_minus2_exact_reference_chain_unavailable" in rebound["missing_evidence"]


@pytest.mark.parametrize("changes", [dict(pct_chg=0), dict(previous_pct_chg=0), dict(previous_pct_chg=-1, close=10.0)])
def test_rebound_observed_necessary_condition_failure_remains_a_known_negative(changes):
    frame = pd.DataFrame([row(daily_rebound=False, **changes)])
    result = analyse(frame, proof=qualification("000001.SZSE"))
    rebound = archetype(result, "rebound_repair_candidate")
    assert rebound["status"] == "not_matched"
    assert rebound["rebound_path_status_counts"]["not_matched"] == 1
    assert rebound["unknown_rebound_path_liquid_rows"] == 0


def test_rebound_cannot_trust_exported_True_without_exact_D_minus2_reference_link():
    frame = pd.DataFrame([row(daily_rebound=True)])
    prior = pd.DataFrame([row(day="2026-10-13", pct_chg=-1, close=10.0,
        preclose_for_limits=8.0, stage=0, limit_up=False), row(day="2026-10-12", close=11.0, stage=1)])
    proof = qualification("000001.SZSE")
    result = analyse(frame, prior=prior, proof=proof)
    rebound = archetype(result, "rebound_repair_candidate")
    assert rebound["status"] == "unknown" and rebound["candidate_count"] == 0
    prior.loc[prior.date.eq("2026-10-13"), "preclose_for_limits"] = 11.0
    prior.loc[prior.date.eq("2026-10-12"), "limit_up"] = False
    result = analyse(frame, prior=prior, proof=proof)
    assert archetype(result, "rebound_repair_candidate")["status"] == "not_matched"


def test_multiple_archetypes_keep_same_board_heights_and_observed_row_semantics():
    frame = pd.DataFrame([row(stage=3, daily_rebound=True), row("300001.SZSE", board="chinext", stage=4)])
    prior = pd.DataFrame([row(day="2026-10-13", pct_chg=-1, close=10.0, preclose_for_limits=11.0, stage=0, limit_up=False),
        row(day="2026-10-12", close=11.0, stage=1),
        row("300001.SZSE", day="2026-10-13", board="chinext", close=10.0, pct_chg=1.0),
        row("300001.SZSE", day="2026-10-12", board="chinext", close=10.0)])
    result = analyse(frame, prior=prior, proof=qualification("000001.SZSE", "300001.SZSE"))
    assert archetype(result, "space_height_leader")["candidate_count"] == 2
    assert archetype(result, "relay_continuation_leader")["candidate_count"] == 2
    trend = archetype(result, "trend_leader")["candidates"][0]
    assert trend["name"] == "fixture"
    assert trend["evidence"]["window_semantics"] == "valid_observed_rows_not_guaranteed_calendar_sessions"
    assert archetype(result, "liquidity_capacity_proxy")["candidate_count"] == 2
    assert archetype(result, "rebound_repair_candidate")["candidate_count"] == 1
    assert all(item["selection_enabled"] is False for item in result["archetypes"])


def test_missing_features_or_IPO_knowledge_is_unknown_not_negative_match():
    frame = pd.DataFrame([row(sma20=np.nan, sma60=np.nan, ret20=np.nan, ret60=np.nan)])
    result = analyse(frame, proof=qualification("000001.SZSE"))
    assert archetype(result, "trend_leader")["status"] == "unknown"
    assert archetype(result, "rebound_repair_candidate")["status"] == "not_matched"
    result = analyse(frame)
    assert archetype(result, "space_height_leader")["status"] == "unknown"
    assert archetype(result, "turnover_leader")["status"] == "unknown"
    assert archetype(result, "sector_catchup_leader")["status"] == "unknown"


def test_unknown_limit_stages_are_not_a_claim_that_no_high_board_or_relay_exists():
    frame = pd.DataFrame([row(stage=0, limit_up=False, pct_chg=1.0),
                          row("000002.SZSE", stage=np.nan, stage_reference_known=False)])
    result = analyse(frame, proof=qualification("000001.SZSE", "000002.SZSE"))
    assert result["implementation_revision"] == "20261015_path_and_membership_unknown_preservation_v3"
    ladder = result["market_environment"]["limit_and_ladder"]
    assert ladder["IPO_supported_limit_up_stage_unknown_count"] == 1
    assert ladder["unknown_stage_rows_by_board"] == {"main": 1}
    for identity in ("space_height_leader", "relay_continuation_leader"):
        item = archetype(result, identity)
        assert item["candidate_count"] == 0 and item["status"] == "unknown"
        assert item["unknown_stage_liquid_limit_up_rows"] == 1
        assert item["unresolved_stage_symbols"] == ["000002.SZSE"]
        assert "qualified_liquid_limit_up_peer_stage_unknown" in item["missing_evidence"]


def test_known_stage_matches_remain_local_matches_with_unknown_peer_warning():
    frame = pd.DataFrame([row(stage=2), row("000002.SZSE", stage=np.nan, stage_reference_known=False)])
    result = analyse(frame, proof=qualification("000001.SZSE", "000002.SZSE"))
    for identity in ("space_height_leader", "relay_continuation_leader"):
        item = archetype(result, identity)
        assert item["status"] == "matched" and item["candidate_count"] == 1
        assert item["unknown_stage_liquid_limit_up_rows_by_board"] == {"main": 1}
        assert "qualified_liquid_limit_up_peer_stage_unknown" in item["missing_evidence"]
    space = archetype(result, "space_height_leader")["candidates"][0]
    assert space["evidence"]["global_space_leader_verified"] is False


@pytest.mark.parametrize("unsupported_IPO", [False, True])
def test_non_comparable_unknown_stage_does_not_override_known_liquid_no_match(unsupported_IPO):
    other = row("301660.SZSE" if unsupported_IPO else "000002.SZSE", board="chinext" if unsupported_IPO else "main",
                stage=np.nan, stage_reference_known=False, amount=30_000_000 if unsupported_IPO else 1_000_000)
    frame = pd.DataFrame([row(stage=0, limit_up=False, pct_chg=1.0), other])
    proof = (qualification("000001.SZSE", new_ipo="301660.SZSE") if unsupported_IPO
             else qualification("000001.SZSE", "000002.SZSE"))
    result = analyse(frame, proof=proof)
    for identity in ("space_height_leader", "relay_continuation_leader"):
        item = archetype(result, identity)
        assert item["status"] == "not_matched" and item["unknown_stage_liquid_limit_up_rows"] == 0


def test_clocks_are_actual_timezone_aware_and_cannot_precede_source_creation():
    frame = pd.DataFrame([row()])
    with pytest.raises(ValueError, match="timezone"):
        analyse(frame, observed="2026-10-15T10:30:00")
    with pytest.raises(ValueError, match="clocks"):
        analyse(frame, observed="2026-10-14T15:30:00+08:00")


def test_result_is_JSON_safe_compact_and_input_frames_are_unchanged():
    frame = pd.DataFrame([row()])
    saved = frame.copy(deep=True)
    source = envelope(frame)
    source_before = deepcopy(source)
    result = analyse(frame, proof=qualification("000001.SZSE"), tapes=[source])
    encoded = json.dumps(result, allow_nan=False)
    assert "ordinary_roster" not in encoded and "do-not-repeat-5000-items" not in encoded
    assert result["source_clocks"][0]["source_updated_at"] == "2026-10-14T15:50:00+08:00"
    assert result["source_clocks"][0]["actual_signal_publication_verified"] is False
    assert source == source_before
    pd.testing.assert_frame_equal(frame, saved)
    assert all(result[key] is False for key in ("orders_generated", "ledger_mutated", "execution_admitted", "model_refit", "future_labels_used_for_score"))


def test_historical_features_need_own_complete_tape_and_no_future_rows():
    frame = pd.DataFrame([row()])
    prior = pd.DataFrame([row(day="2026-10-13")])
    with pytest.raises(ValueError, match="own complete"):
        build_environment(frame, [envelope(frame)], CALENDAR, OBSERVED, feature_history=prior)
