"""Independent expected-value tests for the bounded F2 time contracts."""

from datetime import datetime, timedelta, timezone

import pytest

from trading.research.financial_factor_contracts import (
    IMMATURE,
    MATURE,
    PRE_EVENT_PROVEN,
    PRE_EVENT_UNPROVEN,
    ReferenceObservation,
    SessionAnchorUnavailable,
    TimeWindow,
    assess_label_maturity,
    conservative_available_at,
    label_available_at,
    publication_interval,
    select_pre_event_reference,
    session_anchor,
    strictly_before,
)


TZ = timezone(timedelta(hours=8))


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def test_publication_day_is_interval_and_uses_late_boundary_for_trade_time():
    event = publication_interval("2026-10-20")

    assert event.kind == "INTERVAL"
    assert event.start == stamp("2026-10-20T00:00:00")
    assert event.end == stamp("2026-10-21T00:00:00")
    assert conservative_available_at(event, timedelta(minutes=5)) == stamp("2026-10-21T00:05:00")


def test_same_day_reference_cannot_prove_strict_pre_event_ordering():
    event = publication_interval("2026-10-20")
    same_day_asset = TimeWindow.exact("2026-10-20T09:00:00+08:00")

    assert not strictly_before(same_day_asset, event)
    result = select_pre_event_reference((ReferenceObservation(
        "asset_same_day", "2026-09-30", same_day_asset, 2_000),), event)
    assert result.status == PRE_EVENT_UNPROVEN
    assert result.selected is None
    assert result.reason == "no_eligible_reference_proven_before_event"


def test_interval_ending_at_event_start_proves_pre_event_and_older_value_is_fallback():
    event = publication_interval("2026-10-20")
    earlier = ReferenceObservation(
        "asset_2026q2", "2026-06-30",
        TimeWindow.interval("2026-10-19T12:00:00+08:00", "2026-10-20T00:00:00+08:00"),
        1_800,
    )
    same_day = ReferenceObservation(
        "asset_2026q3", "2026-09-30", TimeWindow.exact("2026-10-20T09:00:00+08:00"), 2_000)

    assert strictly_before(earlier.available, event)
    result = select_pre_event_reference((same_day, earlier), event)
    assert result.status == PRE_EVENT_PROVEN
    assert result.reference_id == "asset_2026q2"
    assert result.value == 1_800


def test_exact_timestamps_use_strict_less_than_for_exact_event():
    event = TimeWindow.exact("2026-10-20T09:00:00+08:00")
    assert strictly_before(TimeWindow.exact("2026-10-20T08:59:59+08:00"), event)
    assert not strictly_before(TimeWindow.exact("2026-10-20T09:00:00+08:00"), event)


def test_delay_one_moves_to_same_named_anchor_on_next_session():
    calendar = ("2026-10-20", "2026-10-21", "2026-10-22")
    anchors = {
        "2026-10-20": {"first_window": "10:00", "late_window": "14:00"},
        "2026-10-21": {"first_window": "10:00", "late_window": "14:00"},
        "2026-10-22": {"first_window": "10:00", "late_window": "14:00"},
    }

    result = session_anchor(calendar, "2026-10-20", 1,
                            entry_policy="first_window", anchors=anchors)
    assert result == stamp("2026-10-21T10:00:00")
    assert result != stamp("2026-10-20T14:00:00")


def test_missing_target_anchor_is_explicit_failure_and_never_skips_session():
    anchors = {"2026-10-20": {"first_window": "10:00"},
               "2026-10-21": {"late_window": "14:00"},
               "2026-10-22": {"first_window": "10:00"}}
    with pytest.raises(SessionAnchorUnavailable, match="missing 'first_window' anchor for 2026-10-21"):
        session_anchor(tuple(anchors), "2026-10-20", 1,
                       entry_policy="first_window", anchors=anchors)


def test_label_available_at_is_latest_dependency_plus_processing_delay():
    result = label_available_at((
        "2026-10-20T16:00:00+08:00",
        "2026-10-21T15:00:00+08:00",
        "2026-10-22T09:00:00+08:00",
    ), timedelta(minutes=30))
    assert result == stamp("2026-10-22T09:30:00")


def test_label_maturity_rejects_late_source_even_when_label_window_ended():
    result = assess_label_maturity(
        "2026-10-20T16:00:00+08:00",
        "2026-10-21T15:00:00+08:00",
        "2026-10-22T09:00:00+08:00",
        "2026-10-21T15:00:00+08:00",
        "COMPLETE",
    )
    assert result.status == IMMATURE
    assert not result.mature
    assert result.reasons == ("label_available_after_fit_cutoff",)


def test_label_maturity_allows_cutoff_equality_but_requires_complete_status():
    cutoff = "2026-10-21T15:00:00+08:00"
    mature = assess_label_maturity(cutoff, cutoff, cutoff, cutoff, "COMPLETE")
    assert mature.status == MATURE and mature.mature and mature.reasons == ()

    incomplete = assess_label_maturity(cutoff, cutoff, cutoff, cutoff, "RIGHT_CENSORED")
    assert incomplete.status == IMMATURE
    assert incomplete.reasons == ("label_not_complete",)


def test_unknown_label_dependency_does_not_default_to_fit_cutoff():
    result = assess_label_maturity(
        "2026-10-20T15:00:00+08:00",
        "2026-10-21T15:00:00+08:00",
        None,
        "2026-10-21T15:00:00+08:00",
        "COMPLETE",
    )
    assert result.status == IMMATURE
    assert result.reasons == ("label_available_at_unknown",)


def test_session_calendar_must_be_pinned_sorted_and_duplicate_free():
    anchors = {"2026-10-20": {"first_window": "10:00"}}
    with pytest.raises(ValueError, match="sorted and duplicate-free"):
        session_anchor(("2026-10-21", "2026-10-20"), "2026-10-20", 0,
                       entry_policy="first_window", anchors=anchors)
