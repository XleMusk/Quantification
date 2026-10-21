import numpy as np
import pandas as pd

from trading.research.factor_families import rank_group_means
from trading.research.factor_library_extension import alpha360_catalog, diagnostics_batch, qualify, score360
from trading.research.factors import candidate_batches, candidates, catalog, daily_ic


def test_multihorizon_statistics_match_existing_pairwise_engine():
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2024-01-02", periods=3)
    index = pd.MultiIndex.from_product([dates, range(17)], names=["date", "symbol"])
    x = pd.DataFrame(rng.integers(0, 10, (len(index), 4)).astype(float), index=index, columns=list("ABCD"))
    x.iloc[[0, 18, 32], 0] = np.nan
    x.iloc[[3, 29, 44], 1] = np.nan
    x["C"] = 1.
    labels = {h: pd.Series(rng.normal(size=len(index)), index=index) for h in (1, 5)}
    labels[1].iloc[[3, 19, 42]] = np.nan
    labels[5].iloc[17:34] = np.nan
    labels[5].iloc[[1, 7, 49]] = np.nan
    actual = diagnostics_batch(x, labels, dict.fromkeys(dates, "train"), min_pairs=5)
    for horizon, label in labels.items():
        ic = daily_ic(x, label, min_pairs=5).rename_axis(columns="factor").stack(future_stack=True).rename("rank_ic").reset_index()
        expected = rank_group_means(x, label, min_pairs=5).merge(ic, on=["date", "factor"])
        columns = expected.columns.tolist()
        pd.testing.assert_frame_equal(actual.loc[actual.horizon.eq(horizon), columns].reset_index(drop=True), expected,
                                      check_dtype=False, check_exact=True)


def test_template_stream_preserves_names_formulas_and_sign_mirrors():
    index = pd.MultiIndex.from_product([pd.bdate_range("2024-01-02", periods=2), range(7)], names=["date", "symbol"])
    rng = np.random.default_rng(9)
    x = pd.DataFrame({f"{family}{w}": rng.uniform(.1, 2, len(index))
                      for family in ("ROC", "VMA", "RETVOL", "RSV", "BETA", "CORR") for w in (5, 10)}, index=index)
    x.iloc[0, 0] = np.nan
    batches = list(candidate_batches(x, [5, 10]))
    assert [len(values.columns) for values, _ in batches] == [8, 8, 8, 8]
    values, defs = candidates(x, [5, 10])
    assert defs.name.tolist() == [f"T{i:03d}" for i in range(32)]
    rank = lambda series: series.groupby(level="date").rank(pct=True)
    m5, m10 = rank(1 / x.ROC5 - 1), rank(1 / x.ROC10 - 1)
    v5 = rank(1 / x.VMA5)
    pd.testing.assert_series_equal(values.T000, m5 * v5, check_names=False)
    pd.testing.assert_series_equal(values.T010, m5 - m10, check_names=False)
    pd.testing.assert_series_equal(values.T018, -values.T010, check_names=False)
    assert values.T002.dropna().eq(0).all()
    assert values.T026.dropna().eq(0).all()
    assert defs.iloc[-1].family == "quiet_reversal"


def test_library_names_are_unique_and_only_exact_formulas_reused():
    native = catalog()
    alpha = alpha360_catalog(native)
    assert len(alpha) == 360 and alpha.reused_from.count() == 8
    assert len(set(qualify(native, "Alpha158").name) | set(alpha.name)) == 518
    for item in alpha.loc[alpha.reused_from.notna()].itertuples(index=False):
        assert item.formula == native.set_index("name").loc[item.reused_from, "formula"]


def test_lagged_translation_keeps_full_calendar_and_float32():
    dates = pd.bdate_range("2024-01-02", periods=4)
    source = pd.DataFrame({"A": np.array([2., np.nan, 8., 16.], dtype="float32")}, index=dates)
    definitions = alpha360_catalog(catalog())
    item = next(definitions.loc[definitions.original_name.eq("CLOSE1")].itertuples(index=False))
    result = score360(source, source, source, item)
    assert result.A.dtype == np.float32
    assert pd.isna(result.A.iloc[2])
    assert result.A.iloc[3] == .5
