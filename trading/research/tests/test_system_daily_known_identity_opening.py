"""Only the new optional route's saved-opening registration is exercised."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_opening as published
from trading.research import native_lowvol_saved_opening as lowvol
from trading.research import native_saved_opening as native
from trading.research import system_daily_refresh as daily


@pytest.fixture
def saved_routes(tmp_path, monkeypatch):
    routes = [{"id": key, "family": key, "source": str(tmp_path / key)}
        for key in ("native_preopen", "native_ipoqualified", "native_lowvol")]
    original_routes = deepcopy(routes)
    configured = {"routes": routes}
    coordinator = tmp_path / "coordinator"
    registration = {"opening_run_directory": str(coordinator)}
    account = tmp_path / "known_identity_books"
    case_ids = ["case" + str(i) for i in range(4)]
    head = {"native_profile": {"forward_templates": {
        key: {"run_dir": str(tmp_path / key)} for key in ("v1", "qualified")},
        "latest_plans": {}}, "lowvol": {"source_directory": str(tmp_path / "native_lowvol")},
        "known_identity_profile": {"run_directory": str(account), "case_ids": case_ids,
            "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
            "last_source_day": "2026-10-20"}}
    monkeypatch.setattr(native, "source_dependencies_current", lambda source: True)
    monkeypatch.setattr(lowvol, "source_dependencies_current", lambda source: True)
    monkeypatch.setattr(daily.opening_daily, "_load", lambda source: ({}, configured))
    monkeypatch.setattr(daily.opening_daily, "run", lambda source: {"status": "saved_only"})
    monkeypatch.setattr(daily.opening_daily, "status", lambda source:
        {"status": "saved_only", "routes": configured["routes"]})
    extended = []
    def extend(source, incoming):
        assert source == coordinator
        extended.extend(deepcopy(incoming))
        configured["routes"].extend(deepcopy(incoming))
    monkeypatch.setattr(daily.opening_daily, "extend", extend)
    return head, registration, account, case_ids, routes, original_routes, extended


def test_no_head_is_only_pending_registration_and_cannot_produce_opening_or_account(saved_routes, tmp_path, monkeypatch):
    head, registration, account, _, routes, original, extended = saved_routes
    monkeypatch.setattr(published, "build", lambda *a, **k: pytest.fail("A registration is not a saved account"))
    result = daily._opening(head, registration, tmp_path / "operations/20261019")
    assert result["source_issues"] == [] and not extended and routes == original
    assert result["pending_registered_routes"] == [{"id": "known_identity", "case_count": 4,
        "status": "registered_without_saved_account", "account_count": None,
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20", "metrics": None}]
    assert not account.exists()


def test_all_four_exact_day_accounts_get_next_session_opening_once_without_rebuilding_old_routes(saved_routes, tmp_path, monkeypatch):
    head, registration, account, case_ids, routes, original, extended = saved_routes
    operation = tmp_path / "operations/20261020"
    calendar = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []}
    account_head = {"binding": {"calendar": calendar}, "cases": {
        key: {"state": {"day": "2026-10-19"}} for key in case_ids}}
    daily._atomic(account / "head.json", account_head)
    calls = []
    def build(source, execution, out, *, source_family):
        calls.append((source, execution, out, source_family))
        daily._atomic(out / "result.json", {"opening_pack": str(out / "opening_pack.json")})
    monkeypatch.setattr(published, "build", build)
    stale = daily._opening(head, registration, operation)
    assert stale["source_issues"][0]["route"] == "known_identity" and not calls and not extended
    account_head["cases"][case_ids[0]]["state"]["day"] = "2026-10-20"
    daily._atomic(account / "head.json", account_head)
    mixed = daily._opening(head, registration, operation)
    assert mixed["source_issues"] and not calls and not extended
    for case in account_head["cases"].values():
        case["state"]["day"] = "2026-10-20"
    daily._atomic(account / "head.json", account_head)
    first = daily._opening(head, registration, operation)
    destination = operation / "artifacts/known_identity_opening"
    assert first["source_issues"] == [] and first["pending_registered_routes"] == []
    assert calls == [(account, "2026-10-21", destination, "known_identity")]
    assert extended == [{"id": "known_identity", "family": "known_identity",
        "source": str(account), "adopt": {"result": str(destination / "result.json"),
            "opening_pack": str(destination / "opening_pack.json")}}]
    repeated = daily._opening(head, registration, operation)
    assert repeated["source_issues"] == [] and len(calls) == 1 and len(extended) == 1
    assert routes[:len(original)] == original
