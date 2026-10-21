"""First-observation eligibility only; no historical replay or future filtering."""
from copy import deepcopy

import pandas as pd
import pytest

from trading.research.leader_trend_eligible_scope import select_anchor_scope


DAY = "2026-02-02"
SYMBOLS = [f"60000{i}.SSE" for i in range(1, 9)]


def fixture_anchor():
    scope = list(SYMBOLS)
    risk = {symbol: {"close": 11., "sma20": 12., "sma60": 13., "ipo_feature_support": True}
        for symbol in scope}
    panel = {"day": DAY, "types": [{"id": "trend_leader", "unresolved_symbols": []}],
        "risk_features": risk}
    diagnostic = {"first_decision_day": DAY, "first_day_scope_members": list(scope),
        "final_valid_count": {symbol: 61 for symbol in scope},
        "outcomes": {symbol: "saved_first_day_history" for symbol in scope}}
    # Low amount is deliberately still valid for scope eligibility. The
    # original daily trend rule, rather than this pool, applies the 20m rule.
    raw = pd.DataFrame([{"date": DAY, "code": "sh." + symbol[:6], "symbol": symbol,
        "open": "10", "high": "11", "low": "9", "close": "11", "preclose": "10",
        "volume": "100", "amount": "1000", "pctChg": "10", "tradestatus": "1", "isST": "0"}
        for symbol in scope])
    return scope, panel, diagnostic, raw


def test_only_known_first_day_61_observation_active_qualified_features_enter_scope():
    scope, panel, diagnostic, raw = fixture_anchor()
    good, sixty, count_unknown, ipo_unknown, membership_unknown, row_missing, feature_unknown, halted = scope
    diagnostic["final_valid_count"][sixty] = 60
    diagnostic["final_valid_count"].pop(count_unknown)
    panel["risk_features"][ipo_unknown].pop("ipo_feature_support")
    panel["types"][0]["unresolved_symbols"] = [membership_unknown]
    raw = raw.loc[raw.symbol.ne(row_missing)].copy()
    panel["risk_features"][feature_unknown]["sma60"] = float("nan")
    raw.loc[raw.symbol.eq(halted), ["tradestatus", "volume"]] = ["0", "0"]
    before = deepcopy((scope, panel, diagnostic)), raw.copy(deep=True)
    selected = select_anchor_scope(scope, panel, diagnostic, raw)
    assert selected["scope_members"] == [good] and selected["eligible_count"] == 1
    excluded = {row["symbol"]: row for row in selected["excluded"]}
    assert selected["excluded_count"] == 7
    assert "insufficient_or_unknown_valid_observations" in excluded[sixty]["reasons"]
    assert excluded[sixty]["valid_observation_count"] == 60
    assert "insufficient_or_unknown_valid_observations" in excluded[count_unknown]["reasons"]
    assert excluded[count_unknown]["valid_observation_count"] is None
    assert "anchor_IPO_feature_not_supported" in excluded[ipo_unknown]["reasons"]
    assert "anchor_trend_membership_unresolved" in excluded[membership_unknown]["reasons"]
    assert "anchor_native_row_missing" in excluded[row_missing]["reasons"]
    assert "anchor_risk_feature_missing_or_nonfinite" in excluded[feature_unknown]["reasons"]
    assert "anchor_native_not_active_non_st" in excluded[halted]["reasons"]
    assert scope == before[0][0] and diagnostic == before[0][2]
    assert "ipo_feature_support" not in panel["risk_features"][ipo_unknown]
    pd.testing.assert_frame_equal(raw, before[1])


def test_pool_ignores_unrelated_future_diagnostics_and_rejects_nonanchor_raw_rows():
    scope, panel, diagnostic, raw = fixture_anchor()
    diagnostic["final_valid_count"][scope[1]] = 60
    original = select_anchor_scope(scope, panel, diagnostic, raw)
    later = deepcopy(diagnostic)
    later.update(future_final_valid_count={symbol: 999 for symbol in scope},
        later_session="2026-03-02", later_returns={scope[1]: 10.},
        unrelated_future_scope_members=[scope[1]])
    selected = select_anchor_scope(scope, panel, later, raw)
    assert selected["scope_members"] == original["scope_members"]
    assert scope[1] not in selected["scope_members"]
    wrong_day = raw.copy()
    wrong_day["date"] = "2026-02-03"
    with pytest.raises(ValueError):
        select_anchor_scope(scope, panel, diagnostic, wrong_day)
    mixed = pd.concat([raw, wrong_day.iloc[:1]], ignore_index=True)
    with pytest.raises(ValueError):
        select_anchor_scope(scope, panel, diagnostic, mixed)
