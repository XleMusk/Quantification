"""HTTP preview consumes isolated synthetic accounts; no client or plan writes."""
from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from trading.api import router
from trading import store
from trading.tests.test_ths_signals import case


@pytest.fixture
def client(case):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as value:
        yield value


def test_http_signal_preview_uses_existing_consumer_without_saving(case, client):
    run_id, request, snapshot, current = case
    before = store.get(run_id, full=True)["state"]
    response = client.post(f"/api/trading/runs/{run_id}/broker/signal-preview",
                           json={"signal_request": request})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["next_plan"]["qty"] == 900
    assert result["execution_config"] == result["strategy_signal"]["selector_config"]
    assert not result["saved"] and not result["client_interacted"] and not result["execution_admitted"]
    assert store.get(run_id, full=True)["state"] == before
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM daily").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["stale", "other_account", "missing_frame"])
def test_http_signal_preview_rejects_bad_input_without_a_plan(case, client, change):
    run_id, request, snapshot, current = case
    request = deepcopy(request)
    if change == "stale":
        request["generated_at"] = "2026-09-30T15:30:00+08:00"
    elif change == "other_account":
        request["account_alias"] = "other"
    else:
        request.pop("signal_frame")
    response = client.post(f"/api/trading/runs/{run_id}/broker/signal-preview",
                           json={"signal_request": request})
    assert response.status_code in {403, 409}, response.text
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0
