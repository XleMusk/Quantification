import pytest
from fastapi import HTTPException

from trading import api


def test_missing_endpoint_is_optional_and_unsafe_saved_file_returns409(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    view = api.latest_leader_close_publication()
    assert view["status"] == "missing" and view["cases"] == []
    assert view["execution_admitted"] is view["strategy_run_started"] is False
    path = tmp_path / "outputs/leader_close_publication/unsafe/publication.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"recorded_at":"2026-10-15T09:00:00+08:00","execution_admitted":true}', encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        api.latest_leader_close_publication()
    assert error.value.status_code == 409
