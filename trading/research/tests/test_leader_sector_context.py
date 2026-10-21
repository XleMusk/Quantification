"""Context provenance and denominator tests; fixtures are not market evidence."""
import json

import pandas as pd
import pytest

from trading.research.leader_sector_context import sector_context


def snapshot(tmp_path, *, retrieved="2026-10-04T03:41:51+08:00"):
    path = tmp_path / "2026-09-30.parquet"
    pd.DataFrame({"date": ["2026-09-30"] * 2, "symbol": ["000001.SZSE", "600000.SSE"],
                  "updateDate": ["2026-09-28"] * 2, "industry": ["bank", "bank"],
                  "industryClassification": ["CSRC"] * 2}).to_parquet(path)
    receipt = {"source": "BaoStock.query_stock_industry(date=decision_day)", "execution_admitted": False,
               "entries": [{"date": "2026-09-30", "rows": 2, "file": path.name, "retrieved_at": retrieved}]}
    (tmp_path / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
    return path


def current():
    return pd.DataFrame({"date": ["2026-10-14"] * 3,
                         "symbol": ["000001.SZSE", "600000.SSE", "000002.SZSE"],
                         "amount": [100., 100., 800.], "pct_chg": [2., -1., 4.]})


def test_unmapped_turnover_stays_in_denominator_and_source_retrieval_is_explicit(tmp_path):
    frame = current()
    original = frame.copy(deep=True)
    result = sector_context(frame, snapshot(tmp_path), decision_day="2026-10-14",
                            observed_at="2026-10-15T09:50:00+08:00")
    assert result["snapshot_staleness_calendar_days"] == 14
    assert result["known_at"] == "2026-10-04T03:41:51+08:00"
    assert result["observed_mapping_name_coverage"] == pytest.approx(2 / 3)
    assert result["observed_mapping_amount_coverage"] == .2 and result["unmapped_amount_cny"] == 800
    sector = result["sectors"][0]
    assert sector["amount_share_of_observed_cross_section"] == .2
    assert sector["median_return_pct"] == .5 and sector["breadth"] == 0
    assert result["current_source_complete"] is False and result["selection_enabled"] is False
    assert result["execution_admitted"] is False and result["net_inflow_identified"] is False
    pd.testing.assert_frame_equal(frame, original)


def test_retrieval_after_decision_and_future_mapping_are_rejected(tmp_path):
    path = snapshot(tmp_path, retrieved="2026-10-15T08:00:00+08:00")
    with pytest.raises(ValueError, match="collected after"):
        sector_context(current(), path, decision_day="2026-10-14", observed_at="2026-10-15T09:50:00+08:00")
    with pytest.raises(ValueError, match="Future industry"):
        sector_context(current(), path, decision_day="2026-09-29", observed_at="2026-10-15T09:50:00+08:00")


def test_duplicate_or_mixed_day_cannot_invent_cross_section(tmp_path):
    path = snapshot(tmp_path)
    frame = current()
    frame.loc[2, "date"] = "2026-10-15"
    with pytest.raises(ValueError, match="exact nonempty"):
        sector_context(frame, path, decision_day="2026-10-14", observed_at="2026-10-15T09:50:00+08:00")
    with pytest.raises(ValueError, match="exact nonempty"):
        sector_context(pd.concat([current(), current().iloc[:1]]), path,
                       decision_day="2026-10-14", observed_at="2026-10-15T09:50:00+08:00")
