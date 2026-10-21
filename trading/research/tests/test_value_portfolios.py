import json
import pandas as pd
import numpy as np
import pytest

from trading.research.value_portfolios import value_pool, ranked_value, financial_coverage, CASES
from trading.research.financials import VALUES


def test_signed_yield_unknowns_and_matched_pool():
    pool = [{"symbol": str(i), "momentum": float(i), "volatility": i + 1} for i in range(7)]
    raw = {str(i): {"peTTM": pe, "pbMRQ": pb} for i, (pe, pb) in enumerate(
        [(10, 1), (-10, 1), (0, 1), ("", 1), (10, 0), (10, -1), (20, 2)])}
    current, audit = value_pool(pool, raw)
    assert current.index.tolist() == ["0", "1", "6"]
    assert current.loc["1", "earnings_yield"] == -.1
    assert current.loc["0", "value_rank"] > current.loc["1", "value_rank"]
    assert audit["pe_zero_unknown"] == audit["pe_missing"] == 1
    assert audit["nonpositive_pb"] == 2
    assert {row["symbol"] for row in ranked_value(current, "matched_low_vol")} == set(current.index)
    assert {row["symbol"] for row in ranked_value(current, "ep_bp_value")} == set(current.index)
    assert ranked_value(current, "ep_bp_value")[0]["symbol"] == "0"


def test_scores_do_not_use_labels_or_pool_order():
    pool = [{"symbol": str(i), "momentum": float(i), "volatility": i + 1} for i in range(8)]
    raw = {str(i): {"peTTM": i + 2, "pbMRQ": i + 1} for i in range(8)}
    first, _ = value_pool(pool, raw)
    second, _ = value_pool(list(reversed(pool)), raw)
    second["future_return"] = np.arange(8)[::-1] * 100
    for case in CASES:
        a = [(row["symbol"], row["score"]) for row in ranked_value(first, case)]
        b = [(row["symbol"], row["score"]) for row in ranked_value(second, case)]
        assert a == b
    raw["0"]["peTTM"] = "invalid"
    with pytest.raises(ValueError, match="Invalid numeric"):
        value_pool(pool, raw)


@pytest.mark.parametrize("net_margin, joint_coverage", [(-.1, .5), (np.nan, 0)])
def test_limited_financial_coverage_never_shrinks_value_pool(tmp_path, net_margin, joint_coverage):
    (tmp_path / "result.json").write_text(json.dumps({"status": "completed_publication_lagged_provider_view",
        "execution_admitted": False, "declared_symbols": 5000, "symbols": 1}), encoding="utf-8")
    source = pd.DataFrame({"date": ["2025-01-31"], "symbol": ["0"], "source_pub_date": ["2025-01-30"],
        "source_stat_date": ["2024-12-31"], **{field + "_provider_value": [np.nan] for field in VALUES}})
    source["roeAvg_provider_value"] = .1
    source["npMargin_provider_value"] = net_margin
    source["gpMargin_provider_value"] = 0
    source.to_parquet(tmp_path / "publication_lagged.parquet", index=False)
    pools = pd.DataFrame({"date": ["2025-01-31"] * 2, "symbol": ["0", "1"]})
    coverage, summary = financial_coverage(tmp_path, pools)
    assert coverage.pool.iloc[0] == 2
    assert coverage.roe_coverage.iloc[0] == .5
    assert coverage.profitability_coverage.iloc[0] == joint_coverage
    assert summary["mean_monthly_profitability_coverage"] == joint_coverage
    assert summary["mean_monthly_field_coverage"]["gpMargin"] == .5
    assert summary["mean_monthly_field_coverage"]["netProfit"] == 0
    assert summary["mean_monthly_field_coverage"]["npMargin"] == joint_coverage
    assert summary["affects_strategy_pool"] is False
    source["source_pub_date"] = "2025-01-31"
    source.to_parquet(tmp_path / "publication_lagged.parquet", index=False)
    with pytest.raises(ValueError, match="Same-day/future"):
        financial_coverage(tmp_path, pools)
