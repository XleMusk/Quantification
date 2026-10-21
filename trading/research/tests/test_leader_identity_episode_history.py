"""Synthetic immutable close chains; no real price source or portfolio replay."""
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_identity_episode_history as h
from trading.research import leader_identity_registry as r
from trading.research.tests.test_leader_identity_registry import (
    append, clock, fixture, registered,
)


DAYS = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
AS_OF = datetime.fromisoformat("2026-10-23T17:00:00+08:00")


def _bytes(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def _sequence(make, path, symbols, *, start=0, end=6):
    # A fails the first measured episode and later meets the same fixed rule;
    # B remains observable throughout. Phase changes never choose members.
    closes = [[10., 10.], [9.8, 10.1], [9.7, 10.2], [10.1, 10.6], [10.2, 10.7], [10.6, 10.8]]
    for i in range(start, end):
        prior = closes[i - 1] if i else [10., 10.]
        append(path, make, DAYS[i], sign=(1, -1, 0, 1, -1, 0)[i],
            closes=dict(zip(symbols, closes[i])), reference=dict(zip(symbols, prior)),
            pcts={symbol: (value / ref - 1) * 100 for symbol, value, ref in zip(symbols, closes[i], prior)})


def test_every_transition_keeps_failed_later_matched_and_overlapping_complete_cohort(tmp_path):
    make, symbols, path = registered(tmp_path)
    _sequence(make, path, symbols)
    original_first = r.report(path, _clock=lambda: AS_OF)
    before = _bytes(path.parent)
    result = h.episode_history(path, now=AS_OF)
    assert result["schema"] == "leader_identity_episode_history_v1"
    assert result["research_result_available_at"] == AS_OF.isoformat()
    assert result["observed_close_days"] == DAYS
    assert result["counts"] == {"episodes": 6, "complete": 4, "pending": 2, "complete_member_observations": 8}
    episodes = result["episodes"]
    assert [e["transition_close_day"] for e in episodes] == DAYS
    assert episodes[0]["fixed_window_close_days"] == DAYS[:3]
    assert episodes[1]["fixed_window_close_days"] == DAYS[1:4]
    assert episodes[1]["episode_id"] in episodes[0]["overlapping_episode_ids"]
    assert episodes[0]["episode_id"] in episodes[1]["overlapping_episode_ids"]
    assert episodes[0]["members"][0]["status"] == "price_proxy_conditions_not_matched"
    assert episodes[2]["members"][0]["status"] == "price_proxy_conditions_matched"
    for episode in episodes:
        assert [m["symbol"] for m in episode["members"]] == symbols
        assert all(len(m["labels"]) == 2 for m in episode["members"])
        assert sum(episode["status_counts"].values()) == len(symbols)
        assert episode["unmatured_is_failure"] is False
        assert episode["three_closes_mean_two_intervals"] is True
        assert all(m["confirmation_day_return_used_as_performance"] is False for m in episode["members"])
    assert episodes[0]["complete_known_at"] == "2026-10-19T16:00:00+08:00"
    assert episodes[0]["complete_known_at_basis"] == "original_observation_window_only_not_new_method_publication"
    assert episodes[0]["earliest_possible_completion_close_at"] == "2026-10-19T15:00:00+08:00"
    assert len(episodes[0]["members"][0]["intervals"]) == 2
    assert episodes[-1]["complete_known_at"] is None
    assert episodes[-1]["status_counts"]["pending_three_closes"] == 2
    assert result["overlapping_windows_not_independent"] is True
    assert result["rule"]["episode_selection_by_result"] is False
    for name in ("execution_admitted", "selection_enabled", "strategy_validated", "cross_regime_leader_verified", "economic_return_or_PnL"):
        assert result[name] is False
    assert r.report(path, _clock=lambda: AS_OF) == original_first
    assert _bytes(path.parent) == before


@pytest.mark.parametrize("failure", ["absent", "reference_gap", "ipo_unknown"])
def test_unknown_member_never_becomes_failure_zero_or_survivor_filter(tmp_path, failure):
    make, symbols, path = registered(tmp_path)
    one = symbols[0]
    append(path, make, DAYS[0], sign=1)
    analysis = make(DAYS[1], sign=-1, absent=(one,) if failure == "absent" else (),
        reference={one: 9.} if failure == "reference_gap" else None)
    if failure == "ipo_unknown":
        saved = r._json(analysis)
        item = saved["input_previews"][0]
        frame = pd.read_csv(item["feature_file"])
        frame.loc[frame.symbol.eq(one), "listing_qualification_status"] = "unknown"
        frame.to_csv(item["feature_file"], index=False)
        item["feature_sha256"] = r.digest(item["feature_file"])
        analysis.write_text(json.dumps(saved), encoding="utf8")
    r.observe(path, analysis, _clock=clock(DAYS[1] + "T16:00:00+08:00"))
    append(path, make, DAYS[2], sign=0)
    result = h.episode_history(path, now=AS_OF)
    episode = result["episodes"][0]
    a, b = episode["members"]
    assert [m["symbol"] for m in episode["members"]] == symbols
    assert a["status"] == "unknown_missing_or_unqualified_or_reference_incompatible"
    assert a["provider_relative_condition"] is None
    assert a["raw_price_condition"] is None
    assert a["unadjusted_endpoint_change_pct"] is None
    assert a["unknown_reasons"]
    assert b["status"] == "price_proxy_conditions_matched"
    assert episode["status_counts"]["price_proxy_conditions_not_matched"] == 0
    assert sum(episode["status_counts"].values()) == 2


def test_exact_source_prefix_is_invariant_to_suffix_but_not_a_backdated_knowledge_archive(tmp_path):
    make, symbols, path = registered(tmp_path)
    _sequence(make, path, symbols, end=2)
    prefix = h.episode_history(path, through=DAYS[1], now=AS_OF)
    _sequence(make, path, symbols, start=2)
    assert h.episode_history(path, through=DAYS[1], now=AS_OF) == prefix
    assert prefix["observed_close_days"] == DAYS[:2]
    assert prefix["source_cutoff_day"] == DAYS[1]
    assert [p["day"] for p in prefix["source"]["observations"]] == DAYS[:2]
    assert [e["transition_close_day"] for e in prefix["episodes"]] == DAYS[:2]
    assert prefix["counts"]["complete"] == 0
    assert all(e["complete_known_at"] is None for e in prefix["episodes"])
    # Entire stored-chain integrity is checked first. through is a source-date
    # cutoff, not permission to ignore records whose knowledge clock is later.
    with pytest.raises(ValueError, match="clock|chain"):
        h.episode_history(path, through=DAYS[1], now=datetime.fromisoformat("2026-10-16T17:00:00+08:00"))
    for absent in ("2026-10-18", "2026-10-23"):
        with pytest.raises(ValueError, match="exact actually observed"):
            h.episode_history(path, through=absent, now=AS_OF)
    with pytest.raises(ValueError, match="timezone"):
        h.episode_history(path, now=datetime(2026, 10, 23))


def test_no_observation_or_no_phase_change_does_not_manufacture_episode(tmp_path):
    make, _, path = registered(tmp_path)
    result = h.episode_history(path, now=AS_OF)
    assert result["episodes"] == []
    assert result["source_cutoff_day"] is None
    assert result["member_count"] == 2
    for day in DAYS[:3]:
        append(path, make, day, sign=-1)
    result = h.episode_history(path, now=AS_OF)
    assert result["observed_close_days"] == DAYS[:3]
    assert result["episodes"] == []
    assert result["counts"]["episodes"] == 0


def test_year_end_short_window_never_invents_next_year_sessions(tmp_path):
    make, symbols = fixture(tmp_path)
    path = tmp_path / "cohort" / "registry.json"
    r.register(make("2026-12-28"), path.parent, _clock=clock("2026-12-29T12:00:00+08:00"))
    for day, sign in (("2026-12-29", -1), ("2026-12-30", 1), ("2026-12-31", -1)):
        append(path, make, day, sign=sign)
    result = h.episode_history(path, now=datetime.fromisoformat("2027-01-01T17:00:00+08:00"))
    first, last = result["episodes"]
    assert first["fixed_window_close_days"] == ["2026-12-30", "2026-12-31"]
    assert last["fixed_window_close_days"] == ["2026-12-31"]
    assert [e["missing_future_calendar_session_count"] for e in result["episodes"]] == [1, 2]
    for episode in result["episodes"]:
        assert episode["status"] == "pending"
        assert episode["complete_known_at"] is None
        assert episode["earliest_possible_completion_close_at"] is None
        assert episode["next_required_close_day"] is None
        assert episode["status_counts"]["pending_three_closes"] == len(symbols)


@pytest.mark.parametrize("fault", ["registry", "observation", "missing_session", "calendar"])
def test_modified_immutable_sources_or_missing_exact_session_are_rejected(tmp_path, fault):
    make, symbols, path = registered(tmp_path)
    _sequence(make, path, symbols, end=3)
    if fault == "registry":
        value = r._json(path)
        value["member_count"] = 1
        path.write_text(json.dumps(value), encoding="utf8")
    elif fault == "observation":
        file = path.parent / "observations" / "20261019" / "observation.json"
        value = r._json(file)
        value["members"][0]["close"] = 100.
        file.write_text(json.dumps(value), encoding="utf8")
    elif fault == "missing_session":
        (path.parent / "observations" / "20261016" / "observation.json").unlink()
    else:
        file = Path(r._json(path)["calendar"])
        value = r._json(file)
        value["closed_ranges"] = [["2026-10-16", "2026-10-16"]]
        file.write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError):
        h.episode_history(path, now=AS_OF)
