"""Saved synthetic receipts are not native client or account verification."""
from copy import deepcopy
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from trading import store
from trading.api import router
from trading.ths import journal
from trading.ths.broker import THSAdapter
from trading.tests.test_ths_signals import case


@pytest.fixture
def client(case):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as value:
        yield value


def saved(case):
    run_id, request, snapshot, current = case
    plan = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_signal(
        run_id, request, current=current)["next_plan"]
    return journal.save_plan(plan)


def test_read_receipt_preserves_conflicting_evidence_and_does_not_import(case, client):
    run_id, _, _, current = case
    plan = saved(case)
    fill = {"execution_id": "0000007", "order_id": "0000012", "symbol": plan["symbol"],
            "side": plan["side"], "qty": 300, "price_cents": 2000, "notional_cents": 600000}
    proof = {"status": "matched_cash_and_positions", "fees_cents": 517,
             "account_observed_at": current.isoformat(), "cashflows_observed_at": current.isoformat()}
    result = {"status": "reconciliation_required", "reason": "previously_confirmed_execution_changed_or_missing",
              "executions": [], "confirmed_executions": [fill], "confirmed_filled_qty": 300,
              "account_reconciliation": {"status": "reconciliation_required", "reason": "account_or_actual_cashflow_missing"},
              "confirmed_account_reconciliation": proof}
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET status=?,contract_id=?,result=? WHERE id=?",
                   (result["status"], "0000012", json.dumps(result), plan["id"]))
        before = [tuple(row) for row in db.execute("SELECT * FROM ths_plans")]
    opening = deepcopy(store.get(run_id, full=True)["state"])
    for _ in range(2):
        response = client.get(f"/api/trading/runs/{run_id}/broker/history")
        assert response.status_code == 200, response.text
        body = response.json()
        item = body["plans"][0]
        assert body["total"] == 1 and not body["client_interacted"] and not body["submission_enabled"]
        assert item["contract_id"] == "0000012" and item["confirmed_filled_qty"] == 300
        assert item["confirmed_executions"] == [fill]
        assert item["confirmed_account_reconciliation"] == proof
        assert item["account_reconciliation"]["status"] == "reconciliation_required"
        assert not item["ledger_imported"] and "snapshot" not in item and "strategy_signal" not in item
    assert store.get(run_id, full=True)["state"] == opening
    with store.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM ths_plans")] == before
        assert db.execute("SELECT count(*) FROM daily").fetchone()[0] == 0


def test_paging_is_account_scoped_and_unknown_is_not_zero(case, client):
    run_id, _, snapshot, _ = case
    plan = saved(case)
    with store.connection() as db:
        for alias in [snapshot["account_alias"], snapshot["account_alias"], "other-synthetic-account"]:
            item = {**plan, "id": uuid.uuid4().hex, "account_alias": alias}
            db.execute("INSERT INTO ths_plans(id,account_alias,status,payload,updated_at) VALUES(?,?,?,?,?)",
                       (item["id"], alias, "planned", json.dumps(item), item["updated_at"]))
    ids = []
    for offset in range(3):
        response = client.get(f"/api/trading/runs/{run_id}/broker/history?limit=1&offset={offset}&account_alias=other-synthetic-account")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 3 and body["account_alias"] == snapshot["account_alias"]
        item = body["plans"][0]
        ids.append(item["id"])
        assert item["confirmed_filled_qty"] is None and item["confirmed_executions"] is None
        assert item["confirmed_account_reconciliation"] is None and not item["ledger_imported"]
    assert len(set(ids)) == 3


def test_import_status_requires_receipt_for_this_run(case, client):
    run_id, _, _, _ = case
    plan = saved(case)
    with store.connection() as db:
        for receipt_run, expected in [("another-run", False), (run_id, True)]:
            db.execute("UPDATE ths_plans SET result=? WHERE id=?", (json.dumps({"ledger_import": {
                "run_id": receipt_run, "ledger_imported": True}}), plan["id"]))
            db.commit()
            result = client.get(f"/api/trading/runs/{run_id}/broker/history").json()
            assert result["plans"][0]["ledger_imported"] is expected


def test_missing_tables_do_not_get_initialized_by_read(case, client):
    run_id = case[0]
    with store.connection() as db:
        db.execute("DROP TABLE ths_plans")
        db.execute("DROP TABLE ths_control")
    response = client.get(f"/api/trading/runs/{run_id}/broker/history")
    assert response.status_code == 200 and response.json()["plans"] == []
    assert response.json()["paused"] is None
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='ths_plans'").fetchone()


def test_reject_unbound_missing_and_invalid_page(case, client):
    run_id = case[0]
    assert client.get("/api/trading/runs/missing/broker/history").status_code == 404
    assert client.get(f"/api/trading/runs/{run_id}/broker/history?limit=201").status_code == 422
    assert client.get(f"/api/trading/runs/{run_id}/broker/history?offset=-1").status_code == 422
    with store.connection() as db:
        state = json.loads(db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        state.pop("broker_binding")
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))
    assert client.get(f"/api/trading/runs/{run_id}/broker/history").status_code == 403


def test_bad_saved_payload_is_not_reported_as_a_missing_run(case, client):
    run_id = case[0]
    plan = saved(case)
    with store.connection() as db:
        payload = {**plan}
        payload.pop("snapshot")
        db.execute("UPDATE ths_plans SET payload=? WHERE id=?", (json.dumps(payload), plan["id"]))
    response = client.get(f"/api/trading/runs/{run_id}/broker/history")
    assert response.status_code == 409 and "保存回读尚不可读取" in response.text
