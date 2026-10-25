import pytest

from trading.research.query_dependencies import QueryDependency, decision_impact, fact_reopens_query


def dependency(state="MISS"):
    return QueryDependency("issuer-1", "parent_net_profit", "2025-01-01", "2025-12-31",
                           "2026-04-30T00:00:00+08:00", "PIT", "coverage-v1", state)


def test_miss_query_has_stable_identity_and_later_fact_reopens_it():
    first = dependency()
    second = QueryDependency(**first.to_dict())
    assert first.dependency_id == second.dependency_id
    assert fact_reopens_query(first, {"issuer_id": "issuer-1", "metric_or_notice_type": "parent_net_profit",
                                      "period_start": "2025-07-01", "period_end": "2025-09-30",
                                      "available_at": "2026-04-29T10:00:00+08:00"})


def test_unrelated_or_after_cutoff_fact_does_not_reopen():
    first = dependency("UNKNOWN")
    base = {"issuer_id": "issuer-1", "metric_or_notice_type": "parent_net_profit",
            "period_start": "2025-07-01", "period_end": "2025-09-30"}
    assert not fact_reopens_query(first, {**base, "available_at": "2026-05-01T00:00:00+08:00"})
    assert not fact_reopens_query(first, {**base, "issuer_id": "issuer-2", "available_at": "2026-04-01T00:00:00+08:00"})
    assert not fact_reopens_query(first, {**base, "metric_or_notice_type": "total_assets", "available_at": "2026-04-01T00:00:00+08:00"})


@pytest.mark.parametrize("old,new,expected", [
    ({"diagnostics": 1}, {"diagnostics": 2}, "RECOMPUTE_EXPLANATION"),
    ({"selected_symbols": ["A"]}, {"selected_symbols": ["B"]}, "REPLAY_ACCOUNT"),
    ({"coverage": "v1"}, {"coverage": "v1"}, "NO_IMPACT"),
])
def test_decision_impact_distinguishes_explanation_and_economic_changes(old, new, expected):
    assert decision_impact(old, new) == expected
