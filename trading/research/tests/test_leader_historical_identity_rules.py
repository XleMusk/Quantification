"""Synthetic causal identity rules only; no Engine, registry or market run."""
from copy import deepcopy

import pytest

from trading.research.leader_historical_identity_rules import HistoricalIdentityRules, METHOD_ID, OLD_TYPES, SPEC


A, B, C = "600000.SSE", "000001.SZSE", "600004.SSE"
DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14"]
CLOCK = "2026-10-17T16:00:00+08:00"


def industry(anchor):
    return {"membership": {A: "industry", B: "industry", C: "industry"}, "snapshot_date": DAYS[0],
        "source_file": "synthetic-dated-industry.parquet", "source_sha256": "a" * 64,
        "actual_available_at": "2026-10-04T10:00:00+08:00", "posthoc_only": True,
        "revision_safe_PIT_verified": False, "theme_membership": False}


def observation(i, *, phase="strong", labels=None, unknown=(), closes=(10., 10., 10.),
                previous=(10., 10., 10.), pcts=(0., 0., 0.), stages=(0, 0, 0), limits=(False, False, False)):
    day = DAYS[i]
    items = [{"id": type_id, "status": "matched", "candidates": [
        {"symbol": symbol, "source_day": day} for symbol in (labels or {}).get(type_id, [])],
        "unresolved_symbols": list(unknown) if type_id == "trend_leader" else []} for type_id in OLD_TYPES]
    panel = {"day": day, "types": items, "environment": {"breadth": .5 if phase == "strong" else -.5,
        "median_return_pct": 1. if phase == "strong" else -1., "source_complete": True}}
    features, ordinary = {}, {}
    for symbol, close, prior, pct, stage, limit in zip((A, B, C), closes, previous, pcts, stages, limits):
        features[symbol] = {"date": day, "symbol": symbol, "close": close, "preclose_for_limits": prior,
            "pct_chg": pct, "amount": 50_000_000., "open": close, "high": close + .2, "low": close - .2,
            "stage": stage, "limit_up": limit, "listing_qualification_status": "supported"}
        ordinary[symbol] = {"date": day, "symbol": symbol, "tradestatus": "1", "isST": "0", "close": close}
    return panel, {"features": features, "ordinary": ordinary, "available_at": CLOCK}


def cross_prefix(rules):
    rules.observe(DAYS[0], *observation(0, phase="weak", labels={"trend_leader": [A, B]}, unknown=[C]))
    first = rules.observe(DAYS[1], *observation(1))
    second = rules.observe(DAYS[2], *observation(2, closes=(11., 9., 10.), pcts=(10., -10., 0.)))
    return first, second


def typed(result, kind):
    return next(item for item in result["types"] if item["id"] == kind)


def test_cross_freezes_all_prior_labels_and_failures_then_retains_anchor_until_next_change():
    rules = HistoricalIdentityRules([A, B, C], DAYS, industry, CLOCK, SPEC)
    first, second = cross_prefix(rules)
    assert typed(first, "cross_regime_candidate")["unresolved_symbols"] == [A, B]
    assert typed(second, "cross_regime_candidate")["candidates"] == []
    result = rules.observe(DAYS[3], *observation(3, closes=(12., 8., 10.), previous=(11., 9., 10.), pcts=(9., -11., 0.)))
    cross = typed(result, "cross_regime_candidate")
    assert [row["symbol"] for row in cross["candidates"]] == [A]
    selected = deepcopy(cross["candidates"][0])
    assert selected["evidence"]["structure_close"] == 10.
    episode = result["identity_evidence"]["episode"]
    assert [row["symbol"] for row in episode["identity"]["members"]] == sorted([A, B])
    assert episode["member_results"][B]["status"] == "not_matched"
    assert episode["identity"]["excluded_identity_unknown"]["trend_leader"] == [C]
    assert episode["fixed_window_close_days"] == DAYS[1:4] and episode["complete_close_count"] == 3
    assert episode["industry"]["actual_available_at"] == "2026-10-04T10:00:00+08:00"
    assert episode["industry"]["posthoc_only"] is True and "available_at" not in episode["industry"]
    assert cross["known_exclusions"][C] == "outside_defined_observed_identity_universe"
    later = rules.observe(DAYS[4], *observation(4, labels={"trend_leader": [C]}, closes=(11., 8., 10.), previous=(12., 8., 10.), pcts=(-8., 0., 0.)))
    assert typed(later, "cross_regime_candidate")["candidates"][0]["evidence"]["structure_close"] == 10.
    assert later["identity_evidence"]["episode"]["complete_close_count"] == 3
    changed = rules.observe(DAYS[5], *observation(5, phase="weak"))
    assert [row["symbol"] for row in changed["identity_evidence"]["episode"]["identity"]["members"]] == [C]
    assert selected["evidence"]["structure_close"] == 10. and selected["source_day"] == DAYS[3]
    assert result["historical_signal_publication_verified"] is False


def test_missing_exact_episode_day_stays_unknown_instead_of_using_next_observation():
    rules = HistoricalIdentityRules([A, B, C], DAYS, industry, CLOCK)
    rules.observe(DAYS[0], *observation(0, phase="weak", labels={"trend_leader": [A]}))
    rules.observe(DAYS[1], *observation(1))
    result = rules.observe(DAYS[3], *observation(3, pcts=(10., 0., 0.)))
    assert typed(result, "cross_regime_candidate")["unresolved_symbols"] == [A]
    episode = result["identity_evidence"]["episode"]
    assert episode["complete_close_count"] == 2
    assert episode["member_results"][A]["reason"] == "three_exact_episode_closes_missing"


def test_catchup_uses_prior_saved_space_role_and_frozen_sector_not_current_survivors():
    rules = HistoricalIdentityRules([A, B, C], DAYS, industry, CLOCK)
    rules.observe(DAYS[0], *observation(0, phase="weak", labels={"space_height_leader": [A]},
        pcts=(10., -2., 0.), stages=(2, 0, 0), limits=(True, False, False)))
    result = rules.observe(DAYS[1], *observation(1, phase="weak", labels={"space_height_leader": [C]},
        pcts=(2., 5., 1.), stages=(0, 1, 2), limits=(False, True, True)))
    catchup = typed(result, "sector_catchup_leader")
    assert [row["symbol"] for row in catchup["candidates"]] == [B]
    candidate = catchup["candidates"][0]
    assert candidate["evidence"]["frozen_prior_leaders"] == [A]
    assert candidate["evidence"]["identity_source_day"] == DAYS[0]
    assert candidate["setup_low"] == 9.8 and candidate["structure_high"] == 10.2
    assert result["identity_evidence"]["actual_created_at"] == CLOCK


def test_missing_sector_qualification_and_reference_remain_unknown_and_future_clock_rejected():
    for fault in ("industry", "qualification", "reference"):
        rules = HistoricalIdentityRules([A, B, C], DAYS, (lambda anchor: None) if fault == "industry" else industry, CLOCK)
        cross_prefix(rules)
        panel, view = observation(3, closes=(12., 8., 10.), previous=(11., 9., 10.), pcts=(9., -11., 0.))
        if fault == "qualification":
            view["features"][A]["listing_qualification_status"] = "unknown"
        elif fault == "reference":
            view["features"][A]["preclose_for_limits"] = 5.
        result = rules.observe(DAYS[3], panel, view)
        assert A in typed(result, "cross_regime_candidate")["unresolved_symbols"]
        assert not typed(result, "cross_regime_candidate")["candidates"]
    rules = HistoricalIdentityRules([A, B, C], DAYS, industry, CLOCK)
    panel, view = observation(0)
    view["available_at"] = "2026-10-18T16:00:00+08:00"
    with pytest.raises(ValueError, match="actual clock"):
        rules.observe(DAYS[0], panel, view)
    assert rules.last_day is None and rules.frames == {}


def test_incomplete_phase_is_unknown_and_replaced_episodes_keep_full_interrupted_or_mature_evidence():
    rules = HistoricalIdentityRules([A, B, C], DAYS, industry, CLOCK)
    rules.observe(DAYS[0], *observation(0, phase="weak", labels={"trend_leader": [A]}))
    panel, view = observation(1)
    panel["environment"]["source_complete"] = False
    incomplete = rules.observe(DAYS[1], panel, view)
    assert incomplete["identity_evidence"]["current_environment_phase"] == "unknown"
    assert incomplete["identity_evidence"]["episode"] is None
    no_bridge = rules.observe(DAYS[2], *observation(2, labels={"trend_leader": [A]}))
    assert no_bridge["identity_evidence"]["episode"] is None
    rules.observe(DAYS[3], *observation(3, phase="weak", labels={"trend_leader": [B]}))
    replaced = rules.observe(DAYS[4], *observation(4, labels={"trend_leader": [C]}))
    old = replaced["identity_evidence"]["superseded_episode"]
    assert old["E"] == DAYS[3] and old["superseded_on"] == DAYS[4]
    assert old["status_at_supersession"] == "superseded_before_three_closes" and old["complete_close_count"] == 2
    assert [member["symbol"] for member in old["identity"]["members"]] == [A]
    assert old["member_results"] == {}
    rules.observe(DAYS[5], *observation(5))
    rules.observe(DAYS[6], *observation(6))
    mature_replaced = rules.observe(DAYS[7], *observation(7, phase="weak"))
    mature = mature_replaced["identity_evidence"]["superseded_episode"]
    assert mature["status_at_supersession"] == "entry_context_superseded" and mature["complete_close_count"] == 3
    assert mature["E"] == DAYS[4] and mature["member_results"][B]["status"] == "not_matched"
    assert len(mature["member_results"][B]["legs"]) == 2
