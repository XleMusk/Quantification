import numpy as np
import pandas as pd

from trading.config import StrategyConfig
from trading.research.factor_families import forward_open_labels, rank_group_means
from trading.research.price_boundaries import OLD, NEW, EFFECTIVE, RENAME_SHA256
from trading.research.stockdb_factor_diagnostics import (
    BLEND, FEATURES, fixed_signals, identify_source, join_inputs, price_panels, scoped_labels)


def test_reviewed_alias_has_one_dated_source_identity():
    master = pd.DataFrame({"code": [OLD, NEW], "type": ["1", "1"]})
    rename = {"old": OLD, "new": NEW, "effective_date": EFFECTIVE, "published_date": "2025-02-14",
              "disclosure_sha256": RENAME_SHA256, "share_count_unchanged_on_rename": True,
              "pre_rename_forward_scale": "0.986265"}
    source = pd.DataFrame({"code": ["300114", "302132"], "symbol": ["300114.SZSE", "302132.SZSE"],
                           "date": ["2023-01-03", "2023-01-03"]})
    actual = identify_source(source, master, rename)
    assert actual.security_id.tolist() == [OLD, OLD]
    assert actual.source_is_dated_code.tolist() == [True, False]
    assert len(actual) == 2


def test_active_endpoint_and_full_calendar_missing_not_next_available():
    dates = pd.bdate_range("2023-01-02", periods=24)
    prices = pd.DataFrame({"date": dates, "code": "sh.600000", "security_id": "sh.600000",
        "tradable_code": "sh.600000", "alias_shadow": False, "adjustflag": "2", "open": 10.,
        "volume": 1000., "amount": 30000000., "tradestatus": "1", "isST": "0"})
    prices["open"] = prices.open.astype(object)
    prices.loc[21, ["tradestatus", "open"]] = ["0", ""]
    master = pd.DataFrame({"code": ["sh.600000"], "ipoDate": ["2000-01-01"], "outDate": [""]})
    metadata, opening = price_panels(prices, dates, master, StrategyConfig())
    assert not metadata.dated_reference_eligible.iloc[:19].any()
    assert metadata.dated_reference_eligible.iloc[19]
    labels = forward_open_labels(opening, 1)
    assert np.isnan(labels.iloc[19, 0])
    assert np.isnan(labels.iloc[20, 0])
    assert labels.iloc[18, 0] == 0
    source = pd.DataFrame({"date": [dates[19]], "security_id": ["sh.600000"], "pair_state": ["both"],
        "source_is_dated_code": [True], "ohlc_pair_within_2c": [True],
        "volume_pair_agrees": [True], "amount_pair_agrees": [True]})
    assert join_inputs(source, metadata).decision_eligible.iloc[0]
    source.loc[0, "ohlc_pair_within_2c"] = False
    assert not join_inputs(source, metadata).decision_eligible.iloc[0]


def test_fixed_blend_no_missing_family_reweight_and_no_outcome_pool_filter():
    index = pd.MultiIndex.from_product([[pd.Timestamp("2023-03-01")], list("abcdef")], names=["date", "symbol"])
    inputs = pd.DataFrame({FEATURES[0]: range(1, 7), FEATURES[1]: range(-3, 3),
        FEATURES[2]: range(6, 0, -1), FEATURES[3]: range(6, 0, -1)}, index=index, dtype=float)
    scores = fixed_signals(inputs)
    assert scores[BLEND].iloc[-1] == 1
    inputs.loc[index[0], FEATURES[1]] = np.nan
    scores = fixed_signals(inputs)
    assert np.isnan(scores[BLEND].iloc[0])
    assert len(scores) == 6
    labels = pd.Series([np.nan, .1, .2, .3, .4, .5], index=index)
    groups = rank_group_means(scores, labels, min_pairs=2)
    assert groups.loc[groups.factor.eq(FEATURES[0]), "signal_rows"].iloc[0] == 6
    assert groups.loc[groups.factor.eq(FEATURES[0]), "missing_labels"].iloc[0] == 1


def test_scopes_purge_exit_not_signal():
    index = pd.MultiIndex.from_tuples([(pd.Timestamp("2023-06-29"), "600000.SSE")], names=["date", "symbol"])
    labels = pd.Series([.1], index=index)
    exits = pd.Series([pd.Timestamp("2023-07-03")], index=index)
    assert scoped_labels(labels, exits, "2023-01-01", "2023-06-30").isna().all()
    assert scoped_labels(labels, exits, "2023-01-01", "2023-12-31").iloc[0] == .1
    assert labels.iloc[0] == .1
