import hashlib
import json

import pandas as pd

from trading.research.adjusted_prices import ADJUSTMENT_FIELDS
from trading.research import extend_derived_factor_view as source


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _factor_view(path, frame, result, plan):
    path.mkdir(parents=True)
    frame.to_parquet(path / "adjustment_events.parquet", index=False)
    plan_hash = _write(path / "plan.json", plan)
    result = dict(result, panel_file="adjustment_events.parquet", rows=len(frame),
                  symbols=int(frame.code.nunique()),
                  panel_sha256=hashlib.sha256((path / "adjustment_events.parquet").read_bytes()).hexdigest(),
                  plan_sha256=plan_hash)
    _write(path / "result.json", result)


def test_extends_derived_rows_and_marks_new_events_provider(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    raw_plan_hash = _write(raw / "plan.json", {"requested_start": "2023-01-01", "requested_end": "2026-10-13"})
    _write(raw / "result.json", {"kind": "stock", "status": "source_cache_complete",
                                  "raw_daily_sha256": "raw-sha", "plan_sha256": raw_plan_hash})
    base_frame = pd.DataFrame([
        ["sh.600000", "2020-01-01", "0.9", "1", "1", "provider", "sh.600000", "2020-01-01"],
        ["sh.600000", "2026-01-01", "1", "1", "1", "derived_ipo_continuity", "sh.600000", "2026-01-01"],
    ], columns=[*ADJUSTMENT_FIELDS, "origin", "source_code", "source_factor_date"])
    base_plan = {"kind": "adjustment", "requested_start": "2023-01-01", "requested_end": "2026-09-30",
                 "rename": {"old": "sz.300114", "new": "sz.302132"}}
    base = tmp_path / "base"
    _factor_view(base, base_frame, {"kind": "adjustment", "status": "derived_research_levels",
                                    "original_provider_rows": 1, "derived_initial_level_count": 1,
                                    "derived_initial_price_rows": 1, "derived_code_change_level_count": 0,
                                    "rename": base_plan["rename"]}, base_plan)
    (base / "implementation.py").write_text("# reviewed\n", encoding="utf-8")
    extension_frame = base_frame.iloc[:1, :5].copy()
    extension_frame["dividOperateDate"] = "2026-10-08"
    extension = tmp_path / "extension"
    _factor_view(extension, extension_frame, {"kind": "adjustment", "status": "source_cache_complete"},
                 {"kind": "adjustment", "requested_start": "2026-10-08", "requested_end": "2026-10-13"})
    folder, result = source.extend(base, extension, raw, tmp_path / "out")
    panel = pd.read_parquet(folder / "adjustment_events.parquet")
    assert result["status"] == "derived_research_levels" and result["rows"] == 3
    assert result["original_provider_rows"] == 2
    assert panel.loc[panel.dividOperateDate.eq("2026-10-08"), "origin"].tolist() == ["provider"]
