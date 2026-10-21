from pathlib import Path

import pandas as pd

from trading.research.stockdb_semantic_policy import diagnose


def _panel(*, legacy: bool) -> pd.DataFrame:
    # The legacy panel deliberately keeps raw OHLC/pct_chg while carrying a
    # different pre_close basis.  Amount and pct_chg only differ by the
    # expected storage precision.
    return pd.DataFrame(
        {
            "code": ["000001"] * 3,
            "date": ["2023-01-03", "2023-01-04", "2023-01-05"],
            "open": [10.0, 11.0, 12.0],
            "high": [11.0, 12.0, 13.2],
            "low": [9.5, 10.8, 11.8],
            "close": [11.0, 12.0, 13.2],
            "pre_close": [10.0, 11.0, 12.0] if not legacy else [8.0, 9.0, 10.0],
            "volume": [100000, 200000, 300000],
            "amount": [1000000.0, 20000000.0, 300000000.0],
            "pct_chg": [10.0, 9.0909, 10.0] if not legacy else [10.0, 9.091, 10.0],
        }
    )


def test_semantic_policy_accepts_precision_but_rejects_mixed_preclose(tmp_path: Path):
    extension = tmp_path / "extension.parquet"
    legacy = tmp_path / "legacy.parquet"
    _panel(legacy=False).to_parquet(extension, index=False)
    _panel(legacy=True).to_parquet(legacy, index=False)
    result_path, result = diagnose(extension, legacy, tmp_path / "report", tmp_path / "artifact")

    assert result_path.is_file()
    assert result["amount"]["acceptance_pass"] is True
    assert result["pct_chg"]["acceptance_pass"] is True
    assert result["pre_close"]["extension_formula_consistent_rows_tolerance_0_02"] == 3
    assert result["pre_close"]["legacy_formula_consistent_rows_tolerance_0_02"] == 0
    assert result["pre_close"]["cross_source_acceptance_pass"] is False
    assert result["promotion"] is False
    assert result["execution_admitted"] is False
