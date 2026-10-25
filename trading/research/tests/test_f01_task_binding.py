import json

from trading import store
from trading.research.f01_task_binding import bind_f01_task


IDENTITY = {
    "study_id": "f01-task", "code_version": "test", "snapshot_id": "snap-1",
    "calendar_id": "cal-1", "rules_id": "rules-1", "benchmark_config_id": "bench-1",
    "initial_account_ref": "paper-1", "study_config": {"delay": 1},
}


def test_blocked_readiness_never_creates_a_task(tmp_path, monkeypatch):
    from trading import settings
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "trading" / "trading.sqlite3")
    store.initialize()
    readiness = {"schema": "f01_input_readiness_audit_v1", "status": "BLOCKED_DATA",
                 "input_run": "missing", "blockers": ["single_quarter_inputs_missing"],
                 "execution_admitted": False}
    result = bind_f01_task(readiness, IDENTITY, persistent_store=store)
    assert result["status"] == "BLOCKED_DATA" and result["task_created"] is False
    assert store.research_attempt(result["attempt_key"]) is None


def test_ready_readiness_binds_to_existing_store(tmp_path, monkeypatch):
    from trading import settings
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "trading" / "trading.sqlite3")
    store.initialize()
    readiness = {"schema": "f01_input_readiness_audit_v1", "status": "READY_FOR_F01_FACT_ASSEMBLY",
                 "input_run": "snapshot-1", "blockers": [], "execution_admitted": False}
    result = bind_f01_task(readiness, IDENTITY, persistent_store=store)
    assert result["status"] == "BOUND_PAPER_RESEARCH_TASK" and result["task_created"] is True
    attempt = store.research_attempt(result["attempt_key"])
    assert attempt["status"] == "REQUESTED"
    assert attempt["request"]["readiness_sha256"] == result["readiness_sha256"]
