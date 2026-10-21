import numpy as np
import pandas as pd
import pytest

from trading.research.stockdb_factor_diagnostics import BLEND, DIRECTIONS
from trading.research.stockdb_size_diagnostics import CONDITIONED, SIZE, conditioned_scores


def frame():
    index = pd.MultiIndex.from_product([pd.to_datetime(["2023-02-28"]), [f"600{i:03d}.SSE" for i in range(100)]],
                                      names=["date", "symbol"])
    return pd.DataFrame({name: np.arange(100, dtype=float) for name in (*DIRECTIONS, BLEND)}, index=index)


def test_common_pool_within_bin_scores_do_not_select_by_outcomes():
    inputs = frame()
    inputs.iloc[0, 0] = np.nan
    score, bins = conditioned_scores(inputs)
    assert len(score) == 99 and bins.nunique() == 10
    assert set(score) == {BLEND, SIZE, CONDITIONED}
    assert score.index.equals(bins.index)
    assert score[CONDITIONED].between(0, 1).all()
    expected = inputs.loc[score.index, "turnover_pct_candidate"].groupby(bins).rank(pct=True)
    pd.testing.assert_series_equal(score[CONDITIONED], expected.rename(CONDITIONED))


def test_size_ties_stay_together_and_duplicates_fail():
    inputs = frame()
    inputs[SIZE] = 1.
    score, bins = conditioned_scores(inputs)
    assert bins.nunique() == 1 and len(score) == 100
    with pytest.raises(ValueError, match="Unique"):
        conditioned_scores(pd.concat([inputs, inputs.iloc[:1]]))
