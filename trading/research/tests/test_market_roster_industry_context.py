"""Saved lagged classifications never narrow a verified intraday roster."""
from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd

from trading.research import market_roster_industry_context as m


def setup(tmp_path):
    folder = tmp_path / m.MAPPING_RELATIVE
    folder.mkdir(parents=True)
    symbols = ["600000.SSE", "000001.SZSE", "600293.SSE", "600004.SSE", "600005.SSE"]
    frame = pd.DataFrame([
        {"date": m.MAPPING_DAY, "symbol": symbols[0], "industryClassification": "B", "industry": "银行", "updateDate": "2026-09-28"},
        {"date": m.MAPPING_DAY, "symbol": symbols[1], "industryClassification": "A", "industry": "制造", "updateDate": "2026-09-28"},
        {"date": m.MAPPING_DAY, "symbol": symbols[2], "industryClassification": "A", "industry": "制造", "updateDate": "2026-09-28"},
        {"date": m.MAPPING_DAY, "symbol": symbols[3], "industryClassification": "A", "industry": "", "updateDate": "2026-09-28"},
    ])
    frame.to_parquet(folder / (m.MAPPING_DAY + ".parquet"), index=False)
    receipt = {"source": "BaoStock.query_stock_industry(date=decision_day)", "execution_admitted": False,
               "status": "completed_requested_industry_history", "entries": [{"date": m.MAPPING_DAY,
               "file": m.MAPPING_DAY + ".parquet", "rows": len(frame), "retrieved_at": "2026-10-04T03:41:51.825234+08:00"}]}
    (folder / "result.json").write_text(json.dumps(receipt), encoding="utf-8")
    targets = [{"symbol": symbol, "source_day": "2026-10-14", "in_fixed_590": index < 2}
               for index, symbol in enumerate(symbols)]
    read_rows = [{"symbol": symbol, "current_at_read": index in (0, 1, 4),
                  "day_change_percent": [9., -2., None, None, 1.][index],
                  "source_raw_amount": "not-normalized"} for index, symbol in enumerate(symbols)]
    saved_rows = [{**row, "current_at_read": index != 3, "day_change_percent": [9., -2., 3., None, 1.][index]}
                  for index, row in enumerate(read_rows)]
    kwargs = {"read_rows": read_rows, "saved_rows": saved_rows, "targets": targets, "source_day": "2026-10-14",
              "source_available_at": "2026-10-15T09:50:42+08:00", "observation_began_at": "2026-10-15T13:10:00+08:00",
              "read_at": "2026-10-15T13:11:00+08:00", "current_valid": True, "data_root": tmp_path}
    return folder, receipt, kwargs


def test_dictionary_order_full_denominators_unknown_bucket_and_actual_proof(tmp_path):
    folder, _, kwargs = setup(tmp_path)
    before = deepcopy(kwargs)
    result = m.industry_context(**kwargs)
    assert result["status"] == "observed_lagged_industry_price_context_only"
    assert kwargs == before
    assert (result["member_count"], result["mapped_target_count"], result["unknown_mapping_count"], result["mapping_coverage"]) == (5, 3, 2, .6)
    assert [(row["industryClassification"], row["industry"]) for row in result["groups"]] == [("A", "制造"), ("B", "银行")]
    first, second = result["groups"]
    assert first["member_count"] == first["industry_mapping_covered_count"] == 2 and first["fixed_590_count"] == 1
    assert first["current_summary"]["known_count"] == first["current_summary"]["unknown_count"] == 1
    assert first["current_summary"]["full_cohort_breadth"] is None
    assert first["current_summary"]["breadth_bounds"] == [-1., 0.]
    assert first["current_summary"]["conditional_median_change_percent"] == -2.
    assert first["observation_summary"]["conditional_median_change_percent"] == .5
    assert second["current_summary"]["conditional_median_change_percent"] == 9.  # Not ranked first by return.
    unknown = result["unknown_mapping"]
    assert unknown["member_count"] == 2 and unknown["industry_mapping_covered_count"] == 0
    assert unknown["current_summary"]["known_count"] == 1 and unknown["current_summary"]["breadth_bounds"] == [0., 1.]
    proof = result["mapping"]
    assert proof["sha256"] == sha256((folder / (m.MAPPING_DAY + ".parquet")).read_bytes()).hexdigest()
    assert proof["receipt_sha256"] == sha256((folder / "result.json").read_bytes()).hexdigest()
    assert proof["known_at"] == "2026-10-04T03:41:51.825234+08:00"
    assert proof["snapshot_staleness_calendar_days_at_source"] == 14 and proof["snapshot_staleness_calendar_days_at_read"] == 15
    assert result["net_inflow_identified"] is result["amount_aggregation_performed"] is result["execution_admitted"] is False


def test_stale_or_outside_slot_caller_never_gets_current_industry_summary(tmp_path):
    _, _, kwargs = setup(tmp_path)
    kwargs.update(current_valid=False, read_at="2026-10-15T15:30:00+08:00")
    result = m.industry_context(**kwargs)
    assert result["status"] == "observed_lagged_industry_price_context_only"
    assert all(group["current_summary"] is None for group in [*result["groups"], result["unknown_mapping"]])
    assert result["groups"][0]["observation_summary"]["known_count"] == 2


def test_mapping_known_after_observation_or_dated_after_roster_is_unavailable(tmp_path):
    folder, receipt, kwargs = setup(tmp_path)
    receipt["entries"][0]["retrieved_at"] = "2026-10-15T13:10:30+08:00"
    (folder / "result.json").write_text(json.dumps(receipt), encoding="utf8")
    result = m.industry_context(**kwargs)
    assert result["status"] == "unavailable_industry_context" and result["groups"] == []
    receipt["entries"][0]["retrieved_at"] = "2026-09-29T09:00:00+08:00"
    (folder / "result.json").write_text(json.dumps(receipt), encoding="utf8")
    kwargs["source_day"] = "2026-09-29"
    for target in kwargs["targets"]:
        target["source_day"] = kwargs["source_day"]
    result = m.industry_context(**kwargs)
    assert result["status"] == "unavailable_industry_context" and "Future" in result["error"]


def test_missing_mapping_fails_only_context_and_keeps_prices_untouched(tmp_path):
    folder, _, kwargs = setup(tmp_path)
    before = deepcopy(kwargs)
    (folder / (m.MAPPING_DAY + ".parquet")).unlink()
    result = m.industry_context(**kwargs)
    assert result["status"] == "unavailable_industry_context" and result["mapping"] is None
    assert result["groups"] == [] and result["unknown_mapping"] is None and kwargs == before


def test_partial_or_reordered_input_cannot_hide_missing_targets(tmp_path):
    _, _, kwargs = setup(tmp_path)
    kwargs["read_rows"] = kwargs["read_rows"][:-1]
    result = m.industry_context(**kwargs)
    assert result["status"] == "unavailable_industry_context" and "every exact ordered" in result["error"]
    kwargs["read_rows"] = list(reversed(kwargs["saved_rows"]))
    result = m.industry_context(**kwargs)
    assert result["status"] == "unavailable_industry_context" and result["groups"] == []
