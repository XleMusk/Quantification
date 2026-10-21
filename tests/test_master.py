import numpy as np
import pandas as pd
import pytest

from trading.research.factors import LABEL
from trading.research.master import Sequences, architecture, fit_scaler, predict_all, scale, train, training_targets


def test_scaler_fits_only_training_dates_without_labels():
    days = pd.bdate_range("2024-01-01", periods=5)
    index = pd.MultiIndex.from_product([days, ["A", "B"]], names=["date", "symbol"])
    original = pd.DataFrame({"x": np.arange(10, dtype=float), "y": [np.nan] * 10}, index=index)
    changed = original.copy()
    changed.loc[days[3]:, "x"] = -999999
    first = fit_scaler(original, str(days[0].date()), str(days[2].date()))
    second = fit_scaler(changed, str(days[0].date()), str(days[2].date()))
    assert np.allclose(first.mean_train, second.mean_train, equal_nan=True)
    assert np.allclose(first.std_train, second.std_train, equal_nan=True)
    pd.testing.assert_frame_equal(scale(original.loc[:days[2]], first), scale(changed.loc[:days[2]], second))
    assert scale(original, first).y.eq(0).all()
    assert scale(original, first).x.abs().max() <= 3


def sample():
    days = pd.bdate_range("2024-01-01", periods=10)
    index = pd.MultiIndex.from_product([days, ["A", "B"]], names=["date", "symbol"])
    stocks = pd.DataFrame({"x": np.repeat(np.arange(10), 2)}, index=index, dtype=float)
    # A historical missing stock observation must not cause an older day to be selected.
    stocks.loc[(days[5], "A"), "x"] = np.nan
    market = pd.DataFrame({"m": np.arange(10) + 100}, index=days, dtype=float)
    eligible = index[index.get_level_values("date") >= days[7]]
    return days, Sequences(stocks, market, eligible, days, ["A", "B"])


def test_market_scaler_uses_single_observation_per_train_date():
    days = pd.bdate_range("2024-01-01", periods=5)
    market = pd.DataFrame({"x": [1., 2., 3., 900., 900.]}, index=days)
    processor = fit_scaler(market, str(days[0].date()), str(days[2].date()))
    assert processor.mean_train.tolist() == [2.]
    assert scale(market, processor).iloc[-1, 0] == 3


def test_exact_calendar_window_and_full_current_cross_section():
    days, data = sample()
    values, index = data.batch(days[-1])
    assert values.shape == (2, 8, 2)
    assert index.tolist() == [(days[-1], "A"), (days[-1], "B")]
    assert values[0, :, 0].tolist() == [2, 3, 4, 0, 6, 7, 8, 9]
    assert values[1, :, 0].tolist() == list(range(2, 10))
    assert values[0, :, 1].tolist() == list(range(102, 110))
    assert data.batch(days[7])[0][1, :, 0].tolist() == list(range(8))


def test_training_purges_future_labels_and_handles_less_than_40_stocks():
    day = pd.Timestamp("2024-12-20")
    index = pd.MultiIndex.from_product([[day], ["A", "B", "C", "D"]], names=["date", "symbol"])
    labels = pd.DataFrame({LABEL: [0, .1, np.nan, 1], "exit_date": pd.to_datetime(
        ["2024-12-30", "2024-12-30", "2024-12-30", "2025-01-02"])}, index=index)
    mask, targets = training_targets(index, labels, "2024-12-31")
    assert mask.tolist() == [True, True, False, False]
    assert targets.mean() == pytest.approx(0)
    assert targets.std(ddof=1) == pytest.approx(1)
    labels[LABEL] = 1
    assert training_targets(index, labels, "2024-12-31") is None


def test_training_trim_applies_only_to_targets_and_preserves_sorted_row_alignment():
    index = pd.MultiIndex.from_product([[pd.Timestamp("2024-01-01")], range(80)], names=["date", "symbol"])
    labels = pd.DataFrame({LABEL: np.arange(80), "exit_date": pd.Timestamp("2024-01-10")}, index=index)
    mask, targets = training_targets(index, labels, "2024-12-31")
    assert np.flatnonzero(mask).tolist() == list(range(2, 78))
    assert targets.shape == (76,)


def test_inference_does_not_receive_or_filter_labels():
    import torch
    days, data = sample()
    class Model(torch.nn.Module):
        def forward(self, x):
            assert x.shape[0] == 2
            return x[:, -1, 0]
    scores = predict_all(Model(), data, [days[7], days[9]], torch.device("cpu"))
    assert len(scores) == 4
    assert scores.index.get_level_values("symbol").tolist() == ["A", "B", "A", "B"]


def test_architecture_refuses_unreviewed_source(tmp_path):
    path = tmp_path / "master.py"
    path.write_text("raise AssertionError('must not run')")
    with pytest.raises(ValueError, match="reviewed commit"):
        architecture(path)


def test_training_limit_preserves_unmet_criterion_instead_of_selecting_checkpoint(tmp_path):
    import torch
    days, data = sample()
    _, index = data.batch(days[-1])
    labels = pd.DataFrame({LABEL: [0, .1], "exit_date": days[-1]}, index=index)
    class Constant(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.value = torch.nn.Parameter(torch.zeros(1))

        def forward(self, x):
            return self.value.expand(len(x))
    config = {"seed": 17, "segments": {"train": [str(days[-1].date()), str(days[-1].date())]},
              "training": {"max_epochs": 1, "learning_rate": 1e-5, "wall_seconds": 100,
                           "stop_train_loss": .1}}
    result = train(Constant(), data, labels, config, tmp_path, torch.device("cpu"))
    assert result["status"] == "epoch_limit_criterion_unmet"
    assert result["epochs_attempted"] == 1
    assert not result["validation_checkpoint_selection"]
    assert not result["test_feedback_used"]
    assert (tmp_path / "weights.pt").is_file()
