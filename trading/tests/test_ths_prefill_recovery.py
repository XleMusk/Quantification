"""Interrupted-prefill recovery in isolated SQLite; no desktop or broker access."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from trading import settings
from trading.ths import adapter, journal


@pytest.fixture
def isolated_plan(tmp_path, monkeypatch):
    current = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "isolated_ths")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "isolated_ths" / "recovery.sqlite3")
    monkeypatch.setattr(adapter, "now", lambda: current)
    monkeypatch.setattr(journal, "now", lambda: current)
    owner = {"attempt_id": "a" * 32, "pid": 424242,
        "process_create_time": 1710000000.25, "claimed_at": current.isoformat()}
    monkeypatch.setattr(journal, "_new_prefill_owner", lambda: deepcopy(owner))
    journal.initialize()
    snapshot = {"source": "ths_user_export", "account_alias": "synthetic-isolated-only",
        "observed_at": current.isoformat(), "trading_date": "2026-10-17",
        "cash_total": 1000000, "cash_frozen": 0, "cash_available": 1000000,
        "cash_basis": "cash_balance_includes_frozen", "positions": {}}
    plan = adapter.create_plan(snapshot, "600000.SSE", "buy", "10.00", 100,
        price_basis="raw_rmb", current=current)
    journal.save_plan(plan)
    return plan, owner


def database_rows():
    with journal.connection() as db:
        return {"plans": [tuple(row) for row in db.execute("SELECT * FROM ths_plans ORDER BY id")],
            "control": [tuple(row) for row in db.execute("SELECT * FROM ths_control ORDER BY key")],
            "observations": [tuple(row) for row in db.execute("SELECT * FROM ths_observations ORDER BY id")]}


def test_dead_writer_recovers_only_to_unknown_preserves_plan_and_never_retries_or_autocloses(isolated_plan, monkeypatch):
    plan, owner = isolated_plan
    claimed = journal.claim(plan["id"])
    assert claimed["result"]["prefill_owner"] == owner and claimed["status"] == "prefilling"
    with journal.connection() as db:
        original_payload = db.execute("SELECT payload FROM ths_plans WHERE id=?", (plan["id"],)).fetchone()[0]
    journal.pause(True)
    monkeypatch.setattr(journal, "_prefill_process_state", lambda value: {"status": "owner_not_running", "pid": value["pid"]})
    monkeypatch.setattr(journal, "close_unsubmitted", lambda *args, **kwargs: pytest.fail("Recovery cannot infer that a form was never submitted"))
    recovered = journal.recover_prefill(plan["id"], confirm_attempt=owner["attempt_id"])
    result = recovered["result"]
    assert recovered["status"] == "prefill_unknown" and recovered["contract_id"] is None
    assert result["prefill_owner"] == owner and result["prefill_recovery"]["attempt_id"] == owner["attempt_id"]
    assert result["submission_state"] == "unknown" and result["form_state_known"] is False
    assert "submitted" not in result
    assert all(result[key] is False for key in ("broker_submission_performed", "client_interacted", "ledger_mutated", "execution_admitted"))
    with journal.connection() as db:
        assert db.execute("SELECT payload FROM ths_plans WHERE id=?", (plan["id"],)).fetchone()[0] == original_payload
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs'").fetchone() is None
    before = database_rows()
    assert journal.recover_prefill(plan["id"], confirm_attempt=owner["attempt_id"]) == recovered
    assert database_rows() == before
    journal.pause(False)
    with pytest.raises(ValueError, match="already attempted"):
        journal.claim(plan["id"])
    assert journal.get(plan["id"])["status"] == "prefill_unknown"


def test_alive_unknown_legacy_wrong_attempt_and_unpaused_recovery_are_refused_without_db_changes(isolated_plan, monkeypatch):
    plan, owner = isolated_plan
    journal.claim(plan["id"])
    for mode in ("alive", "pid_unverifiable", "legacy_missing_owner", "wrong_attempt", "unpaused"):
        base = {"status": "prefilling", "prefill_owner": deepcopy(owner)}
        if mode == "legacy_missing_owner":
            base.pop("prefill_owner")
        with journal.connection() as db:
            db.execute("UPDATE ths_plans SET status='prefilling',result=? WHERE id=?", (json.dumps(base), plan["id"]))
        journal.pause(mode != "unpaused")
        state = "owner_alive" if mode == "alive" else "owner_state_unknown" if mode == "pid_unverifiable" else "owner_not_running"
        monkeypatch.setattr(journal, "_prefill_process_state", lambda value, status=state: {"status": status})
        before = database_rows()
        attempt = "different-attempt" if mode == "wrong_attempt" else owner["attempt_id"]
        with pytest.raises(ValueError):
            journal.recover_prefill(plan["id"], confirm_attempt=attempt)
        assert database_rows() == before, mode


def test_late_success_and_failure_callbacks_cannot_overwrite_recovered_attempt(isolated_plan, monkeypatch):
    plan, owner = isolated_plan
    monkeypatch.setattr(journal, "_prefill_process_state", lambda value: {"status": "pid_reused"})

    class LateSuccessAdapter:
        def prefill(self, claimed, *, stop_check):
            assert claimed["result"]["prefill_owner"] == owner
            journal.pause(True)
            journal.recover_prefill(plan["id"], confirm_attempt=owner["attempt_id"])
            return {"status": "awaiting_manual_submission", "synthetic_late_success": True}

    with pytest.raises(ValueError, match="no longer owns"):
        journal.prefill(plan["id"], LateSuccessAdapter())
    recovered = journal.get(plan["id"])
    assert recovered["status"] == "prefill_unknown" and "synthetic_late_success" not in recovered["result"]
    before = database_rows()
    for callback_owner in (owner, {**owner, "attempt_id": "another-attempt"}):
        with pytest.raises(ValueError, match="no longer owns"):
            journal._finish_prefill(plan["id"], callback_owner,
                {"status": "prefill_unknown", "error": "synthetic stale failure"})
        assert database_rows() == before
