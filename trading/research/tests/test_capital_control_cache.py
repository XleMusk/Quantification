from copy import deepcopy
from pathlib import Path

import pytest

from trading.research.capital_allocation_replay import (
    PROFITABILITY_CASES, PROFITABILITY_FAMILY, source_layout, validate_control_cache,
)


def fixture():
    source = {"start": "2025-01-02", "end": "2026-09-30", "sessions": 424, "symbols": 5368,
              "scope": "fixed", "dataset": "prices", "rankings": "ranks",
              "status": "completed_profitability_diagnostics", "research_family": PROFITABILITY_FAMILY,
              "protocol": {"id": PROFITABILITY_FAMILY, "fresh_holdout_claimed": False,
                           "signals": list(PROFITABILITY_CASES), "financial_receipt": {"frozen": True},
                           "cohort_policy": {"quarter": "fixed"}, "formulas": {"profit": "fixed"},
                           "signal_portfolio_configs": {case: {} for case in PROFITABILITY_CASES}}}
    path = Path("original/run.json")
    cache = {**source, "status": "failed_capital_allocation_diagnostics",
             "research_family": "capital_lot_fee_implementation", "source_run": str(path),
             "protocol": {**source["protocol"], "source_rankings": source["rankings"],
                          "signal_source_family": PROFITABILITY_FAMILY},
             "controls": [{"signal": "profitability_only", "status": "completed"}]}
    return source, path, cache


def test_open_arm_is_fixed_stock_only_and_cache_is_independent_of_failed_arm():
    source, path, cache = fixture()
    assert source_layout(source, "open_lots") == (PROFITABILITY_CASES, ("open_lots",), True)
    assert validate_control_cache(cache, path, source)["profitability_only"]["status"] == "completed"
    legacy = {**source, "status": "completed_family_portfolio_diagnostics", "research_family": "fixed_family_incremental"}
    with pytest.raises(ValueError, match="stock profitability"):
        source_layout(legacy, "open_lots")


@pytest.mark.parametrize("field", ["source_run", "rankings", "formula", "receipt", "duplicate"])
def test_control_cache_cannot_change_sample_ranks_or_economics(field):
    source, path, cache = fixture()
    cache = deepcopy(cache)
    if field == "source_run":
        cache["source_run"] = "other"
    elif field == "rankings":
        cache["protocol"]["source_rankings"] = "other"
    elif field == "formula":
        cache["protocol"]["formulas"] = {"profit": "reversed"}
    elif field == "receipt":
        cache["protocol"]["financial_receipt"] = {"frozen": False}
    else:
        cache["controls"] *= 2
    with pytest.raises(ValueError):
        validate_control_cache(cache, path, source)
