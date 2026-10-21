from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.research import annual_quality_value_inputs as module
from trading.research.low_risk import portfolio
from trading.research.profitability_portfolios import rank_z


ASOF = "2026-10-05T06:10:26+08:00"


def frames(days, n=40):
    calendar = pd.bdate_range("2023-01-03", "2026-09-30").strftime("%Y-%m-%d").tolist()
    symbols = [f"{600000+i:06}.SSE" for i in range(n)]
    original = pd.DataFrame([{"date": day, "symbol": s, "momentum": (i-20)/100,
        "volatility": .2+i/1000, "median_amount": 5e7, "peTTM": -10 if i == 0 else i+1,
        "pbMRQ": i+1., "earnings_yield": -.1 if i == 0 else 1/(i+1),
        "book_yield": 1/(i+1), "value_rank": (i+1)/n} for day in days for i,s in enumerate(symbols)])
    annual = []
    for year in (2023, 2024, 2025):
        for i, symbol in enumerate(symbols):
            row = {"symbol": symbol, "report_day": f"{year}-12-31", "asof": ASOF,
                "pit_verified": False, "units_verified": False, "execution_admitted": False,
                "revenue_cost_spread_assets": (i-10)/100, "operating_cashflow_assets": (i-20)/200}
            for field in module.QUALITY_FIELDS:
                row[field+"_status"] = "available_numeric"
                row[field+"_received_at"] = pd.Timestamp("2026-10-04T22:00:00+08:00")
            for statement in module.STATEMENTS:
                row[statement+"_statement_observed"] = True
                row[statement+"_notice_at"] = f"{year+1}-04-30T00:00:00+08:00"
                row[statement+"_retrieved_at"] = "2026-10-04T21:00:00+08:00"
            annual.append(row)
    meta = {"decision_days": days, "calendar": calendar, "quality_asof": ASOF,
            "quality_periods": ["2023-12-31", "2024-12-31", "2025-12-31"],
            "source_clocks": {"quality_asof": ASOF, "actual_received_at_columns_preserved": True}}
    return original, pd.DataFrame(annual), meta


def test_exact_june_monthend_year_no_fallback_and_same_four_case_pool():
    days = ["2024-05-31", "2024-06-28", "2025-05-30", "2025-06-30"]
    original, annual, meta = frames(days)
    result = module.score_monthly_pool(original, annual, meta)
    assert [r["required_fiscal_year"] for r in result["coverage"]] == [2022, 2023, 2023, 2024]
    assert result["status_by_day"][days[0]]["status"] == "NO_CHANGE"
    assert result["status_by_day"][days[0]]["reason"] == "exact_required_fiscal_year_missing"
    assert result["coverage"][0]["joint_pool_members"] == 0
    assert len(result["full_pool"]) == len(original)
    for case in module.CASES:
        ranked = result["ranks"][case]
        assert days[0] not in set(ranked.date)
        assert ranked.groupby("date").size().tolist() == [30, 30, 30]
    current = result["pool"].loc[lambda r: r.date.eq(days[-1])].set_index("symbol")
    pd.testing.assert_series_equal(current.value_score, rank_z(current.value_rank), check_names=False)
    expected_q = sum(rank_z(current[f]) for f in module.QUALITY_FIELDS) / 2
    np.testing.assert_allclose(current.quality_score, expected_q)
    np.testing.assert_allclose(current.quality_value_half_half_score, .5*expected_q+.5*current.value_score)
    assert current.revenue_cost_spread_assets.lt(0).any()
    assert current.revenue_cost_spread_assets.eq(0).any()
    assert current.operating_cashflow_assets.lt(0).any()
    assert current.peTTM.lt(0).any()
    value = result["ranks"]["value_only"].loc[lambda r: r.date.eq(days[-1])]
    assert value.symbol.iloc[0] == "600039.SSE"
    assert result["full_pool"].loc[lambda r: r.date.eq(days[1]), "derived_assumed_historical_effective"].eq(days[1]).all()
    assert result["pit_verified"] is False
    assert "asset_growth" not in result["pool"]
    bad = original.copy()
    bad.loc[bad.date.eq(days[1]), "date"] = "2024-06-14"
    bad_meta = {**meta, "decision_days": [days[0], "2024-06-14", *days[2:]]}
    with pytest.raises(ValueError, match="month end"):
        module.score_monthly_pool(bad, annual, bad_meta)


def test_notice_uses_shanghai_day_and_actual_receipt_clock_is_preserved():
    day = "2025-06-30"
    original, annual, meta = frames([day])
    mask = annual.report_day.eq("2024-12-31")
    index = annual.index[mask]
    annual.loc[index[0], "income_notice_at"] = "2025-06-30T00:00:00+08:00"
    annual.loc[index[1], "cashflow_notice_at"] = "2025-06-29T16:00:00Z"  # Same Shanghai day.
    annual.loc[index[2], "balance_notice_at"] = ""
    annual.loc[index[3], "operating_cashflow_assets_status"] = "unavailable_asof"
    result = module.score_monthly_pool(original, annual, meta)
    current = result["pool"]
    assert len(current) == 36
    assert not set(current.symbol) & {f"60000{i}.SSE" for i in range(4)}
    assert result["coverage"][0]["reported_notice_not_strictly_before_D_members"] == 2
    assert result["coverage"][0]["reported_notice_unknown_members"] == 1
    evidence = result["full_pool"].set_index("symbol")
    assert evidence.at["600001.SSE", "cashflow_derived_notice_day"] == day
    assert evidence.at["600001.SSE", "cashflow_notice_at"] == "2025-06-29T16:00:00Z"
    field = module.QUALITY_FIELDS[0]
    assert evidence.at["600010.SSE", field+"_received_at"] == pd.Timestamp("2026-10-04T22:00:00+08:00")
    assert evidence.at["600010.SSE", field+"_actual_received_after_D"]
    assert evidence.at["600010.SSE", "asof"] == ASOF
    assert result["status_by_day"][day]["status"] == "FULL_TARGET"


def test_metadata_inspection_without_data_read_then_saved_payload_pin_and_inventory(tmp_path, monkeypatch):
    days = ["2025-01-31", "2025-02-28"]
    original, annual, common = frames(days, n=3)
    event, old, quality = [tmp_path / name for name in ("events", "old", "quality")]
    for folder in (event, old / "predictions", quality):
        folder.mkdir(parents=True)
    pd.DataFrame({"date": common["calendar"]}).to_csv(event / "source_input_calendar.csv", index=False)
    original.to_parquet(old / "monthly_value_pool.parquet", index=False)
    annual.to_parquet(quality / "annual_quality_features.parquet", index=False)
    replay = [d for d in common["calendar"] if "2025-01-02" <= d <= days[-1]]
    value = {"research_family": module.ORIGINAL_METHOD, "status": "completed_economic_value_diagnostics",
        "start": "2025-01-02", "end": days[-1], "sessions": len(replay), "rankings": str(old / "predictions"),
        "pool_summary": {"common_rows": len(original), "distinct_pool_symbols": 3, "decision_months": 2},
        "execution_admitted": False, "strategy_validated": False,
        "protocol": {"signals": list(module.ORIGINAL_CASES), "portfolio": portfolio().to_dict(),
                     "stock_inputs": {"event_view": str(event)}},
        "results": [{"signal": case, "status": "completed", "completed_sessions": len(replay)}
                    for case in module.ORIGINAL_CASES]}
    quality_meta = {"status": "completed_annual_quality_features", "execution_admitted": False,
        "pit_verified": False, "definitions": {f: {} for f in module.QUALITY_FIELDS}, "asof": ASOF,
        "rows": len(annual), "securities": 3,
        "profiles": [{"report_day": p} for p in common["quality_periods"]]}
    run, quality_result = old / "run.json", quality / "result.json"
    run.write_text(json.dumps(value), encoding="utf-8")
    quality_result.write_text(json.dumps(quality_meta), encoding="utf-8")
    actual_reader = pd.read_parquet
    reads = []

    def reader(*args, **kwargs):
        reads.append(Path(args[0]))
        return actual_reader(*args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", reader)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    meta = module.inspect_saved_inputs(run, quality_result)
    assert not reads
    built = module.build_inputs(run, quality_result, expected_meta=meta)
    assert reads == [old / "monthly_value_pool.parquet", quality / "annual_quality_features.parquet"]
    assert len(built["full_pool"]) == len(original)
    assert all(status["status"] == "NO_CHANGE" for status in built["status_by_day"].values())
    assert all(frame.empty for frame in built["ranks"].values())
    assert {p: p.read_bytes() for p in before} == before
    assert set(p for p in tmp_path.rglob("*") if p.is_file()) == set(before)
    with (old / "monthly_value_pool.parquet").open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="generation changed"):
        module.build_inputs(run, quality_result, expected_meta=meta)
