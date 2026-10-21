from fastapi import HTTPException
import pytest

from trading import api


def test_optional_fixed_batch_endpoint_reads_missing_and_rejects_unsafe_without_running(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    view = api.latest_recent_alpha158()
    assert view["status"] == "unavailable" and view["score_run_started"] is False
    assert view["execution_admitted"] is view["provider_contacted"] is False
    assert not list(tmp_path.iterdir())
    path = tmp_path / "research/recent_source_alpha158/20261015_1012_1014_v1/result.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"execution_admitted":true}', encoding="utf8")
    with pytest.raises(HTTPException) as error:
        api.latest_recent_alpha158()
    assert error.value.status_code == 409
    assert any(route.path == "/api/trading/research/recent-alpha158" and route.methods == {"GET"} for route in api.router.routes)
