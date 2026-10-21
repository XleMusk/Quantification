import numpy as np
import pandas as pd
import pytest

from factor_lab import FACTORS, LABEL, compute_factors_and_labels, evaluate_cross_sections, synthetic_panel


@pytest.fixture
def sample():
    calendar = pd.bdate_range("2025-02-03", periods=32).delete(8)
    i = np.arange(len(calendar), dtype=float)
    panel = pd.DataFrame({"date": calendar, "symbol": "X", "open": 100 + i,
                          "close": 102 + i, "volume": 1000 + 10 * i})
    return panel, calendar


def test_hand_calculated_factors_and_open_label(sample):
    panel, calendar = sample
    row = compute_factors_and_labels(panel, calendar).iloc[20]
    assert row["reversal_5"] == pytest.approx(-5 / 117)
    assert row["momentum_20"] == pytest.approx(20 / 102)
    assert row["relative_volume_20"] == pytest.approx(1200 / 1095)
    assert row[LABEL] == pytest.approx(5 / 121)
    assert row["entry_date"] == calendar[21]
    assert row["exit_date"] == calendar[26]


@pytest.mark.parametrize("missing_session", [21, 26])
def test_missing_entry_or_exit_does_not_move_to_next_row(sample, missing_session):
    panel, calendar = sample
    incomplete = panel.loc[panel["date"] != calendar[missing_session]]
    row = compute_factors_and_labels(incomplete, calendar).iloc[20]
    assert pd.isna(row[LABEL])
    assert row["entry_date"] == calendar[21]
    assert row["exit_date"] == calendar[26]


def test_missing_historical_session_is_not_forward_filled(sample):
    panel, calendar = sample
    panel = panel.loc[panel["date"] != calendar[15]]
    row = compute_factors_and_labels(panel, calendar).iloc[20]
    assert pd.isna(row["reversal_5"])
    assert pd.isna(row["relative_volume_20"])
    assert row["momentum_20"] == pytest.approx(20 / 102)


def test_future_price_and_volume_changes_leave_past_factors_unchanged():
    panel, calendar = synthetic_panel()
    original = compute_factors_and_labels(panel, calendar)
    changed = panel.copy()
    changed.loc[changed["date"] > calendar[70], ["open", "close", "volume"]] *= 7
    updated = compute_factors_and_labels(changed, calendar)
    before = original["date"] <= calendar[70]
    pd.testing.assert_frame_equal(original.loc[before, list(FACTORS)], updated.loc[before, list(FACTORS)])


def test_unavailable_future_labels_and_warmup_remain_missing(sample):
    panel, calendar = sample
    calculated = compute_factors_and_labels(panel, calendar)
    assert calculated[LABEL].tail(6).isna().all()
    assert calculated["momentum_20"].iloc[:20].isna().all()


def cross_section(values, labels):
    n = len(values)
    return pd.DataFrame({"date": pd.Timestamp("2025-02-03"), "symbol": [f"X{i}" for i in range(n)],
                         **{factor: values for factor in FACTORS}, LABEL: labels})


@pytest.mark.parametrize("values,labels,reason", [
    ([1, 2], [2, 1], "insufficient_pairs"),
    ([1] * 5, list(range(5)), "constant_factor"),
    (list(range(5)), [1] * 5, "constant_label"),
])
def test_undefined_ic_has_reason_and_is_not_zero(values, labels, reason):
    daily, _ = evaluate_cross_sections(cross_section(values, labels))
    assert daily["rank_ic"].isna().all()
    assert set(daily["ic_reason"]) == {reason}


def test_spearman_uses_same_date_and_average_tie_ranks():
    first = cross_section([1, 1, 3, 4, 5], [1, 1, 3, 4, 5])
    second = first.assign(date=pd.Timestamp("2025-02-04"), **{LABEL: [5, 5, 3, 2, 1]})
    daily, _ = evaluate_cross_sections(pd.concat([first, second], ignore_index=True))
    np.testing.assert_allclose(daily["rank_ic"], [1, 1, 1, -1, -1, -1])


def test_quantile_membership_does_not_depend_on_future_availability():
    frame = cross_section(list(range(10)), list(range(10)))
    _, complete = evaluate_cross_sections(frame)
    frame.loc[frame["symbol"] == "X0", LABEL] = np.nan
    _, missing = evaluate_cross_sections(frame)
    pd.testing.assert_frame_equal(complete[["factor", "group", "n_signals"]],
                                  missing[["factor", "group", "n_signals"]])
    assert (missing.loc[missing["group"] == 1, "n_observed"] == 1).all()


def test_synthetic_generation_is_reproducible_and_ohlc_valid():
    first, dates1 = synthetic_panel()
    second, dates2 = synthetic_panel()
    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_index_equal(dates1, dates2)
    assert first["source"].eq("synthetic").all()
    assert (first["high"] >= first[["open", "close"]].max(axis=1)).all()
    assert (first["low"] <= first[["open", "close"]].min(axis=1)).all()


def test_duplicate_observations_and_unknown_dates_are_rejected(sample):
    panel, calendar = sample
    with pytest.raises(ValueError, match="Duplicate"):
        compute_factors_and_labels(pd.concat([panel, panel.iloc[:1]]), calendar)
    with pytest.raises(ValueError, match="outside"):
        compute_factors_and_labels(panel, calendar[1:])
