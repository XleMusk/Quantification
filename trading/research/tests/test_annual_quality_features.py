import numpy as np
import pandas as pd

from trading.research.annual_quality_features import compute


def test_per_feature_availability_denominators_and_signed_values():
    frame = pd.DataFrame({"symbol": ["000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE"],
        "report_day": ["2025-12-31"] * 4,
        "total_assets": [100., 0., 100., 100.], "revenue": [10., 10., 0., 10.],
        "operating_cost": [20., 0., 5., np.nan], "operating_cashflow": [0., 5., -5., 10.]})
    for field in ("total_assets", "revenue", "operating_cost", "operating_cashflow"):
        frame[field + "_status"] = np.where(frame[field].notna(), "observed_numeric", "source_null")
    for report in ("balance", "income", "cashflow"):
        frame[report + "_notice_at"] = "2026-03-01"
        frame[report + "_retrieved_at"] = "2026-10-04T13:00:00+00:00"
    current = compute(frame, "2026-10-04T22:00:00+08:00").set_index("symbol")
    assert current.loc["000001.SZSE", "revenue_cost_spread_assets"] == -.1
    assert current.loc["000001.SZSE", "operating_cashflow_assets"] == 0
    assert current.loc["000003.SZSE", "operating_cashflow_assets"] == -.05
    assert current.loc["000002.SZSE", "revenue_cost_spread_assets_status"] == "nonpositive_denominator"
    assert current.loc["000003.SZSE", "revenue_cost_spread_margin_status"] == "nonpositive_denominator"
    assert pd.isna(current.loc["000004.SZSE", "revenue_cost_spread_assets"])
    assert current.loc["000004.SZSE", "operating_cashflow_assets"] == .1
    old = compute(frame, "2026-09-30T15:00:00+08:00")
    assert old[["revenue_cost_spread_assets", "operating_cashflow_assets", "revenue_cost_spread_margin"]].isna().all().all()
    assert old.operating_cashflow_assets_not_received_by_asof.all()


def test_investment_exact_previous_year_identity_and_both_receipt_times():
    rows = [("A", "2023-12-31", 50.), ("A", "2025-12-31", 150.),
            ("B", "2024-12-31", 100.), ("B", "2025-12-31", 120.),
            ("C", "2024-12-31", 100.), ("C", "2025-12-31", 80.),
            ("D", "2024-12-31", 0.), ("D", "2025-12-31", 100.),
            ("E", "2024-12-31", 100.), ("E", "2025-12-31", 100.)]
    panel = pd.DataFrame(rows, columns=["symbol", "report_day", "total_assets"])
    panel["revenue"], panel["operating_cost"], panel["operating_cashflow"] = 10., 5., 2.
    for field in ("total_assets", "revenue", "operating_cost", "operating_cashflow"):
        panel[field + "_status"] = "observed_numeric"
    for report in ("balance", "income", "cashflow"):
        panel[report + "_notice_at"] = "2026-03-01"
        panel[report + "_retrieved_at"] = "2026-10-04T13:00:00+00:00"
    # A known current year cannot make the late prior-year version available.
    panel.loc[panel.symbol.eq("B") & panel.report_day.eq("2024-12-31"), "balance_retrieved_at"] = "2026-10-05T01:00:00+00:00"
    current = compute(panel.sample(frac=1, random_state=0), "2026-10-04T22:00:00+08:00",
                      include_investment=True)
    year = current.loc[current.report_day.eq("2025-12-31")].set_index("symbol")
    assert year.at["A", "asset_growth_status"] == "missing_prior_period"
    assert pd.isna(year.at["A", "asset_growth"])
    assert year.at["B", "asset_growth_status"] == "unavailable_asof"
    assert year.at["B", "asset_growth_not_received_by_asof"]
    assert np.isclose(year.at["C", "asset_growth"], -.2)
    assert year.at["D", "asset_growth_status"] == "nonpositive_denominator"
    assert year.at["E", "asset_growth"] == 0.
    assert (year.asset_growth_prior_period == "2024-12-31").all()
    later = compute(panel, "2026-10-05T10:00:00+08:00", include_investment=True)
    b = later.loc[later.symbol.eq("B") & later.report_day.eq("2025-12-31")].iloc[0]
    assert np.isclose(b.asset_growth, .2)
    assert b.asset_growth_received_at == pd.Timestamp("2026-10-05T01:00:00+00:00")
    assert "asset_growth" not in compute(panel, "2026-10-05T10:00:00+08:00")
