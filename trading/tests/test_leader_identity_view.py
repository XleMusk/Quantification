"""The GET reads one complete, fixed registry report without any writes."""
from copy import deepcopy
from datetime import datetime

import pytest
from fastapi import HTTPException

from trading import api
from trading.research import leader_identity_view as view
from trading.research.leader_identity_registry import FLAGS, REPORT_SCHEMA, RULE


NOW = datetime.fromisoformat("2026-10-15T14:00:00+08:00")


def fixed_path(root):
    return root / "research/leader_identity_registry" / view.COHORT_ID / "registry.json"


def fixture_report(path):
    # Keep more rows than the environment's display cap to prove no hidden
    # topN is introduced into this separate fixed-cohort endpoint.
    return {"schema": REPORT_SCHEMA, "registry": str(path.resolve()), "registry_sha256": "synthetic",
        "read_at": NOW.isoformat(), "registered_at": "2026-10-15T12:00:00+08:00",
        "source": {"day": "2026-10-14", "created_at": "2026-10-15T11:30:00+08:00"},
        "first_eligible_close_day": "2026-10-15", "member_count": 501,
        "observed_close_days": [], "next_required_close_day": "2026-10-15", "episode": None,
        "label_counts": {"trend_leader": 501}, "status_counts": {"waiting_environment_change": 501},
        "members": [{"symbol": f"{index:06d}.SZSE", "status": "waiting_environment_change"} for index in range(501)],
        "rule": RULE, "cross_regime_leader_verified": False, **FLAGS}


def test_missing_fixed_registry_never_falls_back_to_other_cohort_or_creates_files(tmp_path, monkeypatch):
    other = tmp_path / "research/leader_identity_registry/20990101_winner/registry.json"
    other.parent.mkdir(parents=True)
    other.write_text("other untouched", encoding="utf-8")
    monkeypatch.setattr(view, "report", lambda *a, **k: pytest.fail("Missing fixed cohort cannot read another report"))
    result = view.leader_identity_tracking_view(tmp_path, now=NOW)
    assert result["status"] == "unavailable_fixed_registry_not_registered" and result["tracking"] is None
    assert not fixed_path(tmp_path).parent.exists()
    assert other.read_text(encoding="utf-8") == "other untouched"


def test_helper_and_api_return_full_report_from_only_fixed_path_without_mutation(tmp_path, monkeypatch):
    path = fixed_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("fixed untouched", encoding="utf-8")
    expected = fixture_report(path)
    calls = []
    def read_only(selected, **kwargs):
        calls.append(selected)
        return deepcopy(expected)
    monkeypatch.setattr(view, "report", read_only)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    result = view.leader_identity_tracking_view(tmp_path, now=NOW)
    api_result = api.leader_identity_view()
    assert calls == [path.resolve(), path.resolve()]
    for value in (result, api_result):
        assert value["tracking"] == expected and len(value["tracking"]["members"]) == 501
        assert value["three_closes_complete_intervals"] == 2
        assert value["execution_admitted"] is value["economic_return_or_PnL"] is False
    assert path.read_text(encoding="utf-8") == "fixed untouched"


@pytest.mark.parametrize("change", ["unsafe", "truncated", "other_cohort"])
def test_api_rejects_invalid_fixed_report_without_fallback(tmp_path, monkeypatch, change):
    path = fixed_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("fixed untouched", encoding="utf-8")
    payload = fixture_report(path)
    if change == "unsafe":
        payload["execution_admitted"] = True
    elif change == "truncated":
        payload["members"] = payload["members"][:10]
    else:
        payload["registry"] = str(path.parent.parent / "other" / "registry.json")
    monkeypatch.setattr(view, "report", lambda *a, **k: deepcopy(payload))
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    with pytest.raises(HTTPException) as error:
        api.leader_identity_view()
    assert error.value.status_code == 409
    assert path.read_text(encoding="utf-8") == "fixed untouched"
