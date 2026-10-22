from datetime import datetime
import json

import pytest

from trading import settings as trading_settings, store as trading_store
from trading.config import StrategyConfig
from webapp.backend.app import data, operations, settings, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(trading_settings, "DB_PATH", tmp_path / "trading/accounts.sqlite3")
    for module in (settings, store, data):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "web")
    store.initialize()
    trading_store.initialize()


def save(relative, value):
    path = trading_settings.DATA_ROOT / "research" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_missing_calendar_source_and_workers_remain_independent():
    value = operations.workspace(datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    assert value["clock"]["calendar_status"] == "unknown"
    assert value["progress"]["data_status"] == "calendar_unknown"
    assert value["workers"]["account"]["online"] is False
    assert value["evidence"]["system_head"]["status"] == "missing"
    assert value["accounts"] == [] and value["ledger_mutated"] is False


def test_latest_failed_attempt_never_falls_back_to_older_success():
    save("calendar/cn_a_share_2026_sse_v1.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "test", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": [],
    })
    receipt = {"day": "2026-10-21", "status": "completed_bulk_close_cycle_research_only",
               "source_status": "complete_full_dated_close_inventory_not_admitted",
               "full_dated_roster_inventory_complete": True, "full_close_package": "fixed/source"}
    save("bulk_close_cycles/20261021_v9/receipt.json", receipt)
    newest = save("bulk_close_cycles/20261021_v10/receipt.json", {"day": "2026-10-21", "status": "failed"})
    save("system_daily/20261020_v1/head.json", {"profile": {"last_source_day": "2026-10-20"},
         "native_profile": {"source_day": "2026-10-21", "missing_publication_day": "2026-10-20"}})
    value = operations.workspace(datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    assert value["clock"]["latest_due_day"] == "2026-10-21"
    assert value["progress"]["data_status"] == "due_close_failed"
    assert value["evidence"]["due_close"]["file"] == str(newest)
    native = next(row for row in value["profiles"] if row["id"] == "native_profile")
    assert native["source_day"] == "2026-10-21" and native["publication_day"] is None
    assert value["progress"]["system_source_day"] == "2026-10-20"
    newest.write_text("broken", encoding="utf-8")
    value = operations.workspace(datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    assert value["evidence"]["due_close"]["status"] == "unreadable"
    assert value["progress"]["due_source_status"] == "unreadable_or_unknown"


def test_old_paper_rows_remain_unbound_and_account_is_not_advanced():
    run = trading_store.create("old paper", StrategyConfig().to_dict(), "2026-10-20", "2026-10-22",
                               "operations-test-paper", mode="paper")
    with trading_store.connection() as db:
        db.execute("INSERT INTO daily VALUES(?,?,?)", (run["id"], "2026-10-20", json.dumps({"date": "2026-10-20"})))
    before = trading_store.get(run["id"], full=True)
    value = operations.workspace(datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    assert value["accounts"][0]["market_version_status"] == "legacy_unbound"
    assert value["accounts"][0]["market_version"] is None
    assert trading_store.get(run["id"], full=True) == before


def test_selected_system_never_adopts_another_generations_success_or_recovery():
    save("calendar/cn_a_share_2026_sse_v1.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "test", "year": 2026,
        "closed_ranges": [], "open_sessions": [],
    })
    save("bulk_close_cycles/20261021_v1/receipt.json", {
        "day": "2026-10-21", "status": "completed_bulk_close_cycle_research_only",
        "source_status": "complete_full_dated_close_inventory_not_admitted",
        "full_dated_roster_inventory_complete": True, "full_close_package": "fixed/source",
    })
    older = "system_daily/20261020_v1"
    chosen = "system_daily/20261021_v1"
    save(older + "/head.json", {"profile": {"last_source_day": "2026-10-21"}})
    save(older + "/operations/20261021/receipt.json", {"day": "2026-10-21", "status": "completed"})
    save(older + "/settlements/20261021/receipt.json", {
        "schema": "system_daily_native_settlement_result_v1", "day": "2026-10-21",
        "status": "completed_native_settlement", "policies": {"base": {"status": "completed"}},
    })
    save(older + "/operations/20261021/recovery/receipt.json", {"day": "2026-10-21", "status": "completed"})
    selected_head = save(chosen + "/head.json", {"profile": {"last_source_day": "2026-10-21"}})
    current = datetime.fromisoformat("2026-10-21T19:00:00+08:00")
    value = operations.workspace(current)
    assert value["system_directory"] == str(selected_head.parent)
    assert value["progress"]["data_status"] == "due_close_complete_research_snapshot"
    assert value["progress"]["paper_status"] == "daily_receipt_missing_or_stale"
    assert value["evidence"]["operation"]["status"] == "missing"
    assert value["evidence"]["settlement"]["status"] == "missing"
    assert value["settlement"] is None and value["recoveries"] == []
    # An unreadable chosen head still cannot borrow the older generation's success.
    selected_head.write_text("broken", encoding="utf-8")
    value = operations.workspace(current)
    assert value["evidence"]["system_head"]["status"] == "unreadable"
    assert value["progress"]["paper_status"] == "system_source_cursor_unknown"
    assert value["evidence"]["operation"]["file"] is None and value["profiles"] == []


def test_chosen_generation_operations_and_actual_eight_type_publication_fields():
    root = "system_daily/20261021_v1"
    save(root + "/head.json", {
        "profile": {"last_source_day": "2026-10-19"},
        "eight_type_profile": {"source_day": "2026-10-21", "publication_source_day": "2026-10-19",
                               "missing_publication_days": ["2026-10-20", "2026-10-21"]},
        "native_profile": {"source_day": "2026-10-20", "last_publication_day": "2026-10-19",
                           "missing_publication_day": "2026-10-20"},
        "legacy_profile": {"source_day": "2026-10-19", "publication_day": "2026-10-19"},
    })
    operation = save(root + "/operations/20261021/receipt.json", {
        "day": "2026-10-21", "status": "partial_research_routes",
        "steps": {"eight_source": {"status": "completed"}, "eight_publication": {"status": "missing_actual_publication"}},
    })
    recovery = save(root + "/operations/20261021/recovery/receipt.json", {
        "day": "2026-10-21", "status": "saved_success_reused",
    })
    value = operations.workspace(datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    profiles = {row["id"]: row for row in value["profiles"]}
    assert profiles["eight_type_profile"]["source_day"] == "2026-10-21"
    assert profiles["eight_type_profile"]["publication_day"] == "2026-10-19"
    assert profiles["eight_type_profile"]["missing_publication_day"] == "2026-10-20, 2026-10-21"
    assert profiles["native_profile"]["publication_day"] == "2026-10-19"
    assert profiles["native_profile"]["missing_publication_day"] == "2026-10-20"
    assert profiles["legacy_profile"]["publication_day"] == "2026-10-19"
    assert value["progress"]["system_source_day"] == "2026-10-19"
    assert value["evidence"]["operation"]["file"] == str(operation)
    assert {row["id"]: row["status"] for row in value["steps"]} == {
        "eight_source": "completed", "eight_publication": "missing_actual_publication"}
    assert value["recoveries"][0]["file"] == str(recovery)
    assert value["signals_published"] is False and value["ledger_mutated"] is False
