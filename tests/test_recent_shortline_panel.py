"""Synthetic causal/maturity checks; no market source, model or replay is used."""
from hashlib import sha256
import json

import numpy as np
import pandas as pd
import pytest

from trading.research.recent_shortline_panel import _amount_context, build_tables, history_features


SYMBOL = "000001.SZSE"
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-07", "2026-09-08", "2026-09-09"]
OBSERVED = "2026-10-15T20:00:00+08:00"


class OpeningQualification:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.calls = []

    def assess(self, symbol, day, *, decision_as_of):
        self.calls.append((symbol, day, decision_as_of))
        return {"engine_block_reason": "opening_listing_evidence_unknown"
                if day in self.blocked else None}


def _features(stages):
    result = {}
    prior = 10.0
    for day, stage in zip(DAYS, stages):
        limit = stage > 0
        close = round(prior * 1.1, 2) if limit else prior
        result[day] = pd.DataFrame([{
            "symbol": SYMBOL, "date": day, "name": "synthetic", "board": "main",
            "open": prior, "high": close, "low": prior, "close": close,
            "previous_close": prior, "preclose_for_limits": prior,
            "calendar_contiguous": True, "listing_qualification_status": "supported",
            "stage_reference_known": True, "limit_up": limit,
            "unqualified_limit_up": limit, "stage": stage,
            "amount": 1_000_000.0, "volume": 100_000.0,
            "pct_chg": 10.0 if limit else 0.0,
        }])
        prior = close
    return result


def _raw(features):
    return {day: pd.DataFrame([{
        "symbol": SYMBOL, "date": day, "tradestatus": "1", "isST": "0",
        "preclose": str(frame.iloc[0].preclose_for_limits),
        "open": str(frame.iloc[0].open), "high": str(frame.iloc[0].high),
    }]) for day, frame in features.items()}


def _history(features):
    prior_days = pd.bdate_range("2026-08-20", periods=7).strftime("%Y-%m-%d")
    rows = [{"symbol": SYMBOL, "date": day, "amount": 10_000_000.0,
             "close": 10.0, "pct_chg": 0.0} for day in prior_days]
    rows.extend({key: row[key] for key in ("symbol", "date", "amount", "close", "pct_chg")}
                for frame in features.values() for row in frame.to_dict("records"))
    return pd.DataFrame(rows)


def _tables(features, *, raw=None, qualification=None, history=None):
    availability = {day: f"2026-10-15T18:00:{DAYS.index(day):02d}+08:00"
                    for day in features}
    result = build_tables(features, _raw(features) if raw is None else raw, DAYS,
        availability, _history(features) if history is None else history,
        qualification or OpeningQualification(), OBSERVED)
    return result, availability


def _event(events, day=DAYS[0]):
    return events.loc[events.date.eq(day)].iloc[0]


def test_terminal_five_board_run_is_still_unmatured_without_exact_nonlimit_witness():
    (events, _, _), _ = _tables(_features([1, 2, 3, 4, 5]))
    assert len(events) == 3
    assert events.run_max_stage_observed.tolist() == [5, 5, 5]
    assert events.label_witness_day.isna().all()
    assert events.hit_4.isna().all() and events.hit_5.isna().all()
    assert events.label_available_at.isna().all()
    assert not events.label_status.eq("complete").any()


def test_exact_nonlimit_witness_matures_labels_with_actual_derivation_clock():
    (events, _, _), availability = _tables(_features([1, 2, 3, 4, 5, 0]))
    assert events.label_status.eq("complete").all()
    assert events.hit_4.tolist() == [1, 1, 1]
    assert events.hit_5.tolist() == [1, 1, 1]
    assert events.run_end_date.eq(DAYS[4]).all()
    assert events.label_witness_day.eq(DAYS[5]).all()
    assert events.label_witness_data_available_at.eq(availability[DAYS[5]]).all()
    assert events.label_available_at.eq(OBSERVED).all()
    assert not events.label_available_at.eq(DAYS[5] + "T15:05:00+08:00").any()


@pytest.mark.parametrize("future_problem", ["reference", "qualification", "stage_jump", "limit_unknown"])
def test_future_quality_does_not_rewrite_decision_mask_or_rule(future_problem):
    prefix = _features([1])
    (before, _, _), _ = _tables(prefix)
    extended = _features([1, 2, 0])
    if future_problem == "reference":
        extended[DAYS[1]].loc[0, "previous_close"] = 99.0
    elif future_problem == "qualification":
        extended[DAYS[1]].loc[0, "listing_qualification_status"] = "unknown"
    elif future_problem == "stage_jump":
        extended[DAYS[1]].loc[0, "stage"] = 4
    else:
        extended[DAYS[1]]["limit_up"] = pd.Series([pd.NA], dtype="boolean")
    (after, _, _), _ = _tables(extended)
    old, new = _event(before), _event(after)
    assert bool(old.decision_eligible) and bool(new.decision_eligible)
    assert bool(old.fixed_rule_pass) and bool(new.fixed_rule_pass)
    assert old.amount_rel20_pre == new.amount_rel20_pre
    assert pd.isna(new.label_witness_day)
    assert pd.isna(new.hit_4) and pd.isna(new.hit_5)
    assert new.label_status != "complete"


def test_unknown_event_qualification_cannot_gain_resolved_negative_label_or_false_rule():
    features = _features([1, 0])
    features[DAYS[0]].loc[0, "listing_qualification_status"] = "unknown"
    (events, _, _), _ = _tables(features)
    event = _event(events)
    assert not bool(event.decision_eligible)
    assert pd.isna(event.hit_4) and pd.isna(event.hit_5)
    assert pd.isna(event.label_witness_day)
    assert event.label_status != "complete"
    assert pd.isna(event.fixed_rule_pass)


def test_missing_exact_next_session_does_not_use_later_observed_bar():
    features = _features([1, 2, 0])
    del features[DAYS[1]]
    qualification = OpeningQualification()
    (events, _, _), _ = _tables(features, qualification=qualification)
    event = _event(events)
    assert event.next_date == DAYS[1]
    assert not bool(event.next_session_observed)
    assert pd.isna(event.next_open_clearance)
    assert pd.isna(event.next_high_touches_limit_up)
    assert pd.isna(event.next_open_locked_limit_up)
    assert pd.isna(event.label_witness_day)
    assert pd.isna(event.hit_4) and pd.isna(event.hit_5)
    assert qualification.calls == []


def test_next_raw_without_supported_opening_ipo_evidence_remains_unknown():
    features = _features([1, 0])
    qualification = OpeningQualification(blocked=[DAYS[1]])
    (events, _, _), _ = _tables(features, qualification=qualification)
    event = _event(events)
    assert event.label_status == "complete"  # Close evidence and open eligibility are distinct.
    assert not bool(event.next_session_observed)
    assert pd.isna(event.next_open_clearance)
    assert pd.isna(event.next_high_touches_limit_up)
    assert pd.isna(event.next_open_locked_limit_up)
    assert event.touchability_label_status == "exact_next_raw_or_opening_qualification_unknown"
    assert qualification.calls == [(SYMBOL, DAYS[1], DAYS[1] + "T09:30:00+08:00")]


def test_next_raw_with_unknown_reference_is_not_false_clearance():
    features = _features([1, 0])
    features[DAYS[1]].loc[0, "calendar_contiguous"] = False
    (events, _, _), _ = _tables(features)
    event = _event(events)
    assert not bool(event.next_session_observed)
    assert pd.isna(event.next_open_clearance)
    assert pd.isna(event.next_open_locked_limit_up)
    assert pd.isna(event.label_witness_day)


def test_history_shift20_min5_and_prefix_invariance_exclude_current_and_future():
    dates = pd.bdate_range("2026-07-01", periods=30).strftime("%Y-%m-%d")
    frame = pd.DataFrame({"symbol": SYMBOL, "date": dates,
        "amount": np.arange(1, 31, dtype=float), "close": 10.0,
        "pct_chg": np.arange(30, dtype=float)})
    result = history_features(frame).set_index("date")
    assert result.iloc[:5].amount_rel20_pre.isna().all()
    assert result.iloc[5].amount_median20_pre == 3.0
    assert result.iloc[5].amount_rel20_pre == 2.0
    assert result.iloc[5].pct_median20_pre == 2.0
    assert result.iloc[25].amount_median20_pre == 15.5  # Prior amounts 6..25 only.
    assert result.iloc[25].pct_median20_pre == 14.5
    assert result.iloc[25].amount_known_prior20_count == 20
    assert result.iloc[25].prior_observed_row_count == 25
    changed = frame.copy()
    changed.loc[25:, "amount"] = 1_000_000.0
    changed.loc[26:, "pct_chg"] = -99.0
    other = history_features(changed).set_index("date")
    assert other.iloc[25].amount_median20_pre == result.iloc[25].amount_median20_pre
    assert other.iloc[25].amount_rel20_pre == pytest.approx(1_000_000 / 15.5)
    pd.testing.assert_frame_equal(result.iloc[:25], other.iloc[:25])
    prefix = history_features(frame.iloc[:26]).set_index("date")
    pd.testing.assert_series_equal(result.iloc[25], prefix.iloc[25])


def test_history_nulls_do_not_satisfy_min5_and_zero_denominator_stays_unknown():
    dates = pd.bdate_range("2026-07-01", periods=8).strftime("%Y-%m-%d")
    missing = pd.DataFrame({"symbol": SYMBOL, "date": dates,
        "amount": [10.0, 10.0, 10.0, 10.0, np.nan, 20.0, 30.0, 40.0],
        "close": 10.0, "pct_chg": 0.0})
    result = history_features(missing)
    assert result.iloc[5].amount_known_prior20_count == 4
    assert pd.isna(result.iloc[5].amount_rel20_pre)
    assert result.iloc[6].amount_known_prior20_count == 5
    assert result.iloc[6].amount_rel20_pre == 3.0
    zeros = missing.copy()
    zeros.loc[:4, "amount"] = 0.0
    result = history_features(zeros)
    assert result.iloc[5].amount_known_prior20_count == 5
    assert result.iloc[5].amount_median20_pre == 0.0
    assert pd.isna(result.iloc[5].amount_rel20_pre)


def test_fixed_rule_missing_amount_history_is_unknown_instead_of_negative():
    features = _features([1])
    history = _history(features).tail(5).reset_index(drop=True)
    (events, _, _), _ = _tables(features, history=history)
    event = _event(events)
    assert bool(event.decision_eligible)
    assert pd.isna(event.amount_rel20_pre)
    assert pd.isna(event.fixed_rule_pass)
    assert pd.isna(event.fixed_rule_score)


def test_amount_context_rejects_same_row_count_partition_amount_change(tmp_path):
    partition = tmp_path / "chunk.parquet"
    original = pd.DataFrame({"code": "000001", "date": [20260901, 20260902,
        20260903, 20260904, 20260907, 20260908],
        "amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        "close": 10.0, "pct_chg": 0.0, "is_st": False})
    original.to_parquet(partition, index=False)
    pin = sha256(partition.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "status": "complete_inventory_only", "execution_admitted": False,
        "chunks": [{"lower_inclusive": 20260901, "parquet_file": partition.name,
                    "parquet_sha256": pin, "rows": len(original)}],
    }), encoding="utf-8")
    accepted = _amount_context(tmp_path, "2026-09-08", {SYMBOL}, {})
    assert accepted.amount.tolist() == original.amount.tolist()
    assert history_features(accepted).iloc[-1].amount_rel20_pre == 2.0

    changed = original.copy()
    changed.loc[5, "amount"] = 600.0
    changed.to_parquet(partition, index=False)
    assert len(pd.read_parquet(partition)) == len(original)
    assert sha256(partition.read_bytes()).hexdigest() != pin
    with pytest.raises(ValueError, match="Pinned amount history partition bytes changed"):
        _amount_context(tmp_path, "2026-09-08", {SYMBOL}, {})
