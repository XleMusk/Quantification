"""Saved THS reads and execution-cost arithmetic never write account state."""

from hashlib import sha256
import json

import pytest

from trading import settings as trading_settings, store as trading_store
from trading.config import StrategyConfig
from trading.ths import journal
from webapp.backend.app import execution_review as review


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(trading_settings, "DB_PATH", tmp_path / "trading" / "trading.sqlite3")


def payload(fills=None, quotes=None):
    return {"currency": "CNY", "price_basis": "raw_rmb", "quantity_unit": "share",
            "evidence_source": "broker_export", "max_reference_age_seconds": 300,
            "fills_csv": "account_alias,trading_date,execution_id,symbol,side,qty,price_rmb,executed_at,fees_rmb\n" + (fills if fills is not None else
                "SIM-01,2025-01-06,00001,600000.SSE,buy,1000,10.01,2025-01-06T10:00:00+08:00,5\n"),
            "quotes_csv": "account_alias,trading_date,execution_id,symbol,reference_at,reference_price_rmb,reference_kind,source\n" + (quotes if quotes is not None else
                "SIM-01,2025-01-06,00001,600000.SSE,2025-01-06T09:59:59+08:00,10,arrival,explicit-price-journal\n")}


def test_missing_database_is_not_created_and_unknown_views_are_not_zero():
    result = review.workspace()
    assert result["accounts"] == [] and result["plans"] == []
    assert result["calibration"]["fill_count"] is None
    assert result["client_interacted"] is False and result["ledger_mutated"] is False
    assert not trading_settings.DATA_ROOT.exists()
    with pytest.raises(ValueError, match="不存在"):
        review.workspace("missing")


def test_saved_views_keep_empty_distinct_from_absent_and_read_is_unchanged():
    trading_store.initialize()
    journal.initialize()
    journal.observe({"account_alias": "SIM-01", "kind": "account", "observed_at": "2025-01-06T10:00:04+08:00", "trading_date": "2025-01-06",
                     "source": "ths_user_export", "rows": [], "cash_available": 100000, "cash_total": 100000,
                     "cash_frozen": 0, "cash_basis": "cash_balance_includes_frozen"})
    journal.observe({"account_alias": "SIM-01", "kind": "orders", "observed_at": "2025-01-06T10:00:02+08:00", "trading_date": "2025-01-06",
                     "source": "ths_user_export", "rows": []})
    path = trading_settings.DB_PATH
    before = sha256(path.read_bytes()).hexdigest()
    result = review.workspace()
    account = result["accounts"][0]
    assert account["views"]["orders"]["row_count"] == 0
    assert account["views"]["trades"]["row_count"] is None
    assert account["views"]["trades"]["observed"] is False
    assert account["cash_available_rmb"] == 1000 and account["cash_frozen_rmb"] == 0
    assert account["views"]["orders"]["freshness"] == "historical"
    assert account["status"] == "observation_incomplete"
    assert sha256(path.read_bytes()).hexdigest() == before


def test_selected_replay_does_not_consume_broker_observations_or_simulated_fills():
    trading_store.initialize()
    journal.initialize()
    run = trading_store.create("replay", StrategyConfig().to_dict(), "2025-01-06", "2025-01-07", "execution-review-replay")
    journal.observe({"account_alias": "another", "kind": "trades", "observed_at": "2025-01-06T10:00:00+08:00", "trading_date": "2025-01-06", "source": "ths_user_export", "rows": []})
    state = trading_store.get(run["id"], full=True)["state"]
    result = review.workspace(run["id"])
    assert result["accounts"] == [] and result["plans"] == []
    assert result["issues"] and result["runs"][0]["bound"] is False
    assert trading_store.get(run["id"], full=True)["state"] == state


def test_saved_reconciliation_conflict_retains_previous_success_and_run_import_identity():
    trading_store.initialize()
    journal.initialize()
    run = trading_store.create("bound", StrategyConfig().to_dict(), "2025-01-06", "2025-01-07", "execution-review-bound", mode="paper")
    state = trading_store.get(run["id"], full=True)["state"]
    state["broker_binding"] = {"channel": "ths", "broker_account_kind": "simulated", "account_alias": "SIM-01"}
    saved_plan = {"id": "plan1", "account_alias": "SIM-01", "symbol": "600000.SSE", "side": "buy", "qty": 100}
    saved_result = {"confirmed_filled_qty": 100, "confirmed_executions": [{"execution_id": "001"}],
                    "account_reconciliation": {"status": "reconciliation_required", "reason": "cash_mismatch"},
                    "confirmed_account_reconciliation": {"status": "matched_cash_and_positions"},
                    "ledger_import": {"ledger_imported": True, "run_id": "another-run"}}
    with trading_store.connection() as db:
        db.execute("UPDATE runs SET state=?,status='paused' WHERE id=?", (json.dumps(state), run["id"]))
        db.execute("INSERT INTO ths_plans VALUES(?,?,?,?,?,?,?,?)", ("plan1", "SIM-01", "reconciliation_required", json.dumps(saved_plan), json.dumps(saved_result), "0001", "2025-01-06", "2025-01-06T10:01:00+08:00"))
    result = review.workspace(run["id"])
    plan = result["plans"][0]
    assert plan["confirmed_filled_qty"] == 100
    assert plan["account_reconciliation"]["status"] == "reconciliation_required"
    assert plan["confirmed_account_reconciliation"]["status"] == "matched_cash_and_positions"
    assert plan["ledger_imported"] is False
    assert trading_store.get(run["id"], full=True)["state"] == state


def test_buy_sell_slippage_and_actual_fees_match_hand_calculation():
    result = review.calibrate(payload())
    assert result["status"] == "matched" and result["matched_count"] == 1
    group = result["groups"][0]
    assert group["reference_notional_rmb"] == 10000
    assert group["slippage_cost_rmb"] == 10 and group["slippage_bps"] == 10
    assert group["fees_rmb"] == 5 and group["all_in_cost_bps"] == 15
    result = review.calibrate(payload(
        fills="SIM-01,2025-01-06,00001,600000.SSE,sell,1000,9.99,2025-01-06T10:00:00+08:00,\n"))
    assert result["groups"][0]["slippage_bps"] == 10
    assert result["groups"][0]["fees_rmb"] is None and result["groups"][0]["all_in_cost_bps"] is None
    assert result["rows"][0]["all_in_cost_rmb"] is None


@pytest.mark.parametrize("quote_line,status", [
    ("SIM-02,2025-01-06,00001,600000.SSE,2025-01-06T09:59:59+08:00,10,arrival,actual\n", "reference_missing"),
    ("SIM-01,2025-01-06,00001,600001.SSE,2025-01-06T09:59:59+08:00,10,arrival,actual\n", "reference_missing"),
    ("SIM-01,2025-01-06,00001,600000.SSE,2025-01-06T10:00:01+08:00,10,arrival,actual\n", "reference_after_fill"),
    ("SIM-01,2025-01-06,00001,600000.SSE,2025-01-06T09:54:59+08:00,10,arrival,actual\n", "reference_too_old"),
])
def test_no_wrong_account_instrument_or_future_reference_is_matched(quote_line, status):
    result = review.calibrate(payload(quotes=quote_line))
    assert result["matched_count"] == 0 and result["groups"] == []
    assert result["rows"][0]["status"] == status
    assert result["rows"][0]["slippage_bps"] is None


def test_quote_types_remain_separate_and_missing_fees_do_not_become_zero():
    data = payload(
        fills="SIM-01,2025-01-06,00001,600000.SSE,buy,100,10.01,2025-01-06T10:00:00+08:00,1\nSIM-01,2025-01-06,00002,600000.SSE,buy,100,10.02,2025-01-06T10:00:00+08:00,\n",
        quotes="SIM-01,2025-01-06,00001,600000.SSE,2025-01-06T09:59:59+08:00,10,arrival,actual\nSIM-01,2025-01-06,00002,600000.SSE,2025-01-06T09:59:59+08:00,10,decision,actual\n")
    result = review.calibrate(data)
    assert len(result["groups"]) == 2
    assert result["groups"][1]["all_in_cost_bps"] is None
    assert result["input_fingerprint"] == review.calibrate(data)["input_fingerprint"]


def test_bad_units_duplicates_and_nan_are_rejected_without_file_writes():
    data = payload()
    with pytest.raises(ValueError, match="单位"):
        review.calibrate({**data, "price_basis": "forward"})
    with pytest.raises(ValueError, match="成交号重复"):
        review.calibrate(payload(fills=data["fills_csv"].splitlines(keepends=True)[1] * 2))
    with pytest.raises(ValueError, match="有限数字"):
        review.calibrate(payload(fills="SIM-01,2025-01-06,00001,600000.SSE,buy,100,NaN,2025-01-06T10:00:00+08:00,1\n"))
    with pytest.raises(ValueError, match="时区"):
        review.calibrate(payload(fills="SIM-01,2025-01-06,00001,600000.SSE,buy,100,10,2025-01-06T10:00:00,1\n"))
    assert not trading_settings.DATA_ROOT.exists()


def test_http_cost_preview_uses_local_write_guard_and_does_not_initialize_accounts():
    from fastapi.testclient import TestClient
    from webapp.backend.app.main import app

    # No lifespan is entered: these read/preview consumers must not need DB setup.
    client = TestClient(app)
    assert client.get("/api/execution-review").json()["status"] == "no_saved_observations"
    assert client.post("/api/execution-review/calibration", json=payload()).status_code == 403
    response = client.post("/api/execution-review/calibration", json=payload(), headers={"X-Local-Request": "1"})
    assert response.status_code == 200
    assert response.json()["groups"][0]["all_in_cost_bps"] == 15
    assert response.json()["ledger_mutated"] is False
    assert not trading_settings.DATA_ROOT.exists()
