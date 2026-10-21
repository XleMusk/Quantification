import pandas as pd

from trading.research.full_market import FIELDS, VALUATION_FIELDS, inspect_rows
from trading.research.stockdb_history_inputs import factor_inputs, pair_rows, stock_identities


def test_optional_projection_keeps_original_fields_and_unknowns():
    row = ["000001", 20230103, 13.2, 13.85, 13.05, 13.77, 100, 1377, 4.635, False, 10.785,
           .75, -6, None, 1000, 1.13]
    frame, check = inspect_rows([row], "0", "20230101", "20230131", FIELDS + VALUATION_FIELDS)
    result = factor_inputs(frame, {"000001": "000001.SZSE"})
    assert check["rows"] == 1
    assert result.earnings_to_price_candidate.iloc[0] == -1 / 6
    assert pd.isna(result.log_float_mv_candidate.iloc[0])
    assert frame.pre_close.iloc[0] == 10.785


def test_pair_flags_preserve_absence_and_expose_unit_and_price_basis():
    source = pd.DataFrame([
        {"code": "000001", "date": "2023-01-03", "open": 13.2, "high": 13.85, "low": 13.05,
         "close": 13.77, "volume": 10000, "amount": 1377, "pre_close": 10.785, "pct_chg": 4.635},
        {"code": "600001", "date": "2023-01-03", "open": 10., "high": 10., "low": 10.,
         "close": 10., "volume": 100, "amount": 1000, "pre_close": None, "pct_chg": None}])
    reference = pd.DataFrame([
        {"code": "000001", "date": "2023-01-03", "open": 13.2, "high": 13.85, "low": 13.05,
         "close": 13.77, "volume": 100, "amount": 1377, "preclose": 13.16, "pctChg": 4.635},
        {"code": "600002", "date": "2023-01-03", "open": 5., "high": 5., "low": 5.,
         "close": 5., "volume": 100, "amount": 500, "preclose": 5., "pctChg": 0}])
    frame, summary = pair_rows(source, reference)
    assert summary["source_only_rows"] == summary["reference_only_rows"] == 1
    assert summary["ohlc_pair_exact_true"] == 1
    assert summary["pre_close_pair_agrees_false"] == 1
    assert summary["volume_ratio_near_100"] == 1
    assert summary["pct_chg_matches_source_pre_close_false"] == 1
    assert pd.isna(frame.loc[frame.code.eq("600001"), "volume_pair_agrees"].iloc[0])
    assert pd.isna(frame.loc[frame.code.eq("600002"), "pct_chg_matches_source_pre_close"].iloc[0])
    assert frame.loc[frame.code.eq("000001"), "volume"].iloc[0] == 10000


def test_master_keeps_delisted_stock_but_excludes_index_and_other_products():
    master = pd.DataFrame([
        {"code": "sh.000001", "type": "2", "status": "1"},
        {"code": "sz.000001", "type": "1", "status": "0"},
        {"code": "sh.510300", "type": "5", "status": "1"},
        {"code": "sh.900901", "type": "1", "status": "1"}])
    assert stock_identities(master) == {"000001": "000001.SZSE"}
