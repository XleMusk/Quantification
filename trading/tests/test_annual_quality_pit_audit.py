import pandas as pd

from trading.research.annual_quality_pit_audit import audit


def test_annual_quality_timing_audit_counts_strict_rows(tmp_path):
    rows = {
        "symbol": ["000001.SZSE", "000002.SZSE"],
        "report_day": ["2025-12-31", "2025-12-31"],
        "asof": ["2026-10-05T06:10:26+08:00"] * 2,
        "balance_notice_at": ["2026-03-01 00:00:00", None],
        "income_notice_at": ["2026-03-01 00:00:00"] * 2,
        "cashflow_notice_at": ["2026-03-01 00:00:00"] * 2,
        "balance_retrieved_at": ["2026-10-04T22:00:00+08:00"] * 2,
        "income_retrieved_at": ["2026-10-04T22:00:00+08:00"] * 2,
        "cashflow_retrieved_at": ["2026-10-04T22:00:00+08:00"] * 2,
        "pit_verified": [False, False],
        "units_verified": [False, False],
    }
    source = tmp_path / "features.parquet"
    pd.DataFrame(rows).to_parquet(source, index=False)
    result_path, result = audit(source, tmp_path / "out")
    assert result_path.is_file()
    assert result["strict_timing_rows"] == 1
    assert result["pit_verified_source_flag"] is False
    assert result["execution_admitted"] is False

