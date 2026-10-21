import json

import pandas as pd
import pytest

from trading.research import leader_historical_environment as h
from trading.research.leader_daily_signals import _physical_frame


def _raw(days):
    return pd.DataFrame([{"code": code, "date": day, "open": 10, "high": 11,
        "low": 9, "close": 10, "volume": volume, "amount": 1000,
        "pct_chg": change, "is_st": st, "pre_close": 99}
        for day, change, volume, st in days for code in ("000001", "600001")])


def test_missing_exact_day_is_unknown_and_breaks_confirmation():
    frame = _physical_frame(_raw([("2026-01-05", 1, 100, False),
                                  ("2026-01-07", 2, 100, False)]), "2026-01-07")
    result = h.summarize_days(frame, ["2026-01-05", "2026-01-06", "2026-01-07"])
    assert result[1]["phase"] == "unknown"
    assert result[1]["breadth"] is None and result[1]["median_return_pct"] is None
    assert not result[2]["diagnostic_two_close_phase_agreement"]
    assert not any(result[2]["phase_only_type_conditions"].values())
    assert result[0]["source_observed_prefix_counts"] == {"0": 1, "6": 1}


def test_zero_volume_and_ST_exclusions_are_visible_not_price_imputed():
    raw = _raw([("2026-01-05", 2, 100, False),
                ("2026-01-06", 2, 0, False),
                ("2026-01-07", 2, 100, True)])
    result = h.summarize_days(_physical_frame(raw, "2026-01-07"),
                              ["2026-01-05", "2026-01-06", "2026-01-07"])
    assert result[1]["source_observed_rows"] == 2
    assert result[1]["source_zero_volume_rows"] == 2
    assert result[1]["active_observed_non_ST_rows"] == 0
    assert result[1]["phase"] == "unknown"
    assert result[2]["source_known_ST_rows"] == 2
    assert result[2]["phase"] == "unknown"
    assert all(row["complete_dated_roster_verified"] is False for row in result)


def test_fixed_rule_applicability_is_separate_from_candidate_evidence():
    frame = _physical_frame(_raw([("2026-01-05", 2, 100, False),
                                  ("2026-01-06", 2, 100, False)]), "2026-01-06")
    second = h.summarize_days(frame, ["2026-01-05", "2026-01-06"])[1]
    assert second["phase"] == "broad_strength_snapshot"
    assert second["diagnostic_two_close_phase_agreement"] is True
    assert second["phase_only_type_conditions"]["space_height_leader"] is True
    assert second["phase_only_type_conditions"]["rebound_repair_candidate"] is False
    assert h.FLAGS["selection_enabled"] is False
    assert h.PROTOCOL["individual_types_or_candidates_computed"] is False


def test_context_selection_is_first_two_per_phase_and_does_not_skip_missing_tail():
    days = [{"day": day, "phase": "broad_strength_snapshot",
             "diagnostic_two_close_phase_agreement": i > 0,
             "source_observed_rows": 1} for i, day in enumerate(
                 ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])]
    calendar = [x["day"] for x in days] + ["2026-01-09"]
    selected = h.select_contexts(days, calendar)
    assert [x["day"] for x in selected] == ["2026-01-06", "2026-01-07"]
    assert selected[1]["slots"][-1]["day"] == "2026-01-09"
    assert selected[1]["slots"][-1]["status"] == "unknown_not_replaced"


def test_native_reader_rejects_changed_bytes_and_does_not_scale_source(tmp_path):
    file = tmp_path / "month.parquet"
    raw = _raw([("2026-01-05", 1, 100, False)])
    raw = raw.loc[raw.code.eq("000001")].copy()
    raw["date"] = 20260105
    raw.to_parquet(file, index=False)
    manifest = {"status": "complete_inventory_only", "execution_admitted": False,
        "chunks_expected": 1, "chunks": [{"prefix": "0", "lower_inclusive": "20260101",
            "upper_inclusive": "20260131", "rows": 1, "parquet_file": file.name,
            "parquet_sha256": h.digest(file)}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    kwargs = dict(start="2026-01-05", end="2026-01-05", expected_manifest_sha256=h.digest(path))
    frame, source = h.read_history(tmp_path, **kwargs)
    assert frame.iloc[0].pre_close == 99
    assert frame.iloc[0].volume == 100 and frame.iloc[0].amount == 1000
    assert source["units"] == "unknown_source_native"
    raw["close"] = 9.5
    raw.to_parquet(file, index=False)
    with pytest.raises(ValueError, match="bytes changed"):
        h.read_history(tmp_path, **kwargs)
