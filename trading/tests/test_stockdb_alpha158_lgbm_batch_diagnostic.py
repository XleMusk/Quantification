import pandas as pd

from trading.research import stockdb_alpha158_lgbm_batch_diagnostic as module


def test_calendar_parses_provider_yyyymmdd_values(tmp_path):
    (tmp_path / "result.json").write_text(
        '{"calendar": [20260901, 20260902, 20261009]}', encoding="utf-8"
    )
    days = module._calendar({"provider_view": str(tmp_path)})
    assert [day.strftime("%Y-%m-%d") for day in days] == [
        "2026-09-01", "2026-09-02", "2026-10-09"
    ]


def test_read_range_filters_dates(tmp_path):
    index = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2026-09-01"), "000001.SZSE"),
        (pd.Timestamp("2026-09-02"), "000001.SZSE"),
    ], names=["date", "symbol"])
    frame = pd.DataFrame({"f0": [1.0, 2.0]}, index=index)
    frame.to_parquet(tmp_path / "part.parquet")
    result = module._read_range(
        tmp_path, {"partitions": [{"file": "part.parquet"}]},
        {pd.Timestamp("2026-09-02")}, ["f0"],
    )
    assert result.index.tolist() == [(pd.Timestamp("2026-09-02"), "000001.SZSE")]


def test_batch_module_declares_research_only_scope():
    assert "without refit" in module.__doc__
