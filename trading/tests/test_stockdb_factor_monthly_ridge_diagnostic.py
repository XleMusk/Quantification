import numpy as np
import pandas as pd
import pytest

from trading.research import stockdb_factor_monthly_ridge_diagnostic as module


def _panel():
    factors = [f"Alpha158/F{i:03d}" for i in range(190)]
    index = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-31", periods=8, freq="ME"), ["000001.SZSE"]],
        names=["date", "symbol"],
    )
    return pd.DataFrame(np.zeros((len(index), len(factors))), index=index, columns=factors), factors


def test_monthly_panel_identity_accepts_ordered_unique_grid():
    panel, factors = _panel()
    candidates = pd.DataFrame({"factor": factors})
    module._validate_panel(panel, candidates)


def test_monthly_panel_identity_rejects_wrong_factor_columns():
    panel, factors = _panel()
    candidates = pd.DataFrame({"factor": factors[::-1]})
    with pytest.raises(ValueError, match="factor identity"):
        module._validate_panel(panel, candidates)
