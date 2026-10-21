import json

import pytest

from trading.research.close_workbench import close_workbench, process_identity


def setup(tmp_path, *, status="running"):
    root = tmp_path / "data" / "research" / "full_close"
    folder = root / "generation2"
    folder.mkdir(parents=True)
    out = tmp_path / "outputs" / "leader_close_preview" / "dated"
    out.mkdir(parents=True)
    locator = {"schema": "full_close_active_job_locator_v1", "folder": str(folder),
               "watcher_receipt": str(folder / "watcher.json"), "execution_admitted": False,
               "collector_pid": 123, "collector_process_started_at": "2026-10-15T01:00:00Z"}
    source = {"schema": "baostock_full_dated_close_inventory_v1", "day": "2026-10-14", "status": status,
              "ordinary_symbols": 5224, "completed_symbols": 5224 if status.startswith("complete_") else 20,
              "full_dated_roster_inventory_complete": status.startswith("complete_"), "raw_daily_sha256": "same",
              **{key: False for key in ("selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated", "execution_admitted")}}
    preview = {"schema": "leader_complete_close_preview_v1", "day": source["day"], "source_inventory_complete": True,
               "source": {"source_folder": str(folder), "receipt": {"raw_daily_sha256": "same"}, "read_only_verified": True},
               "candidate_panel_only": True, "tape": {"advance_count": 1}, "families": [],
               **{key: False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "orders_generated", "ledger_mutated")}}
    watcher = {"execution_admitted": False, "ledger_mutated": False, "preview_exit_code": 0, "preview_output": str(out)}
    payloads = {root / "active_job.json": locator, folder / "receipt.json": source,
                folder / "watcher.json": watcher, out / "result.json": preview}
    for path, value in payloads.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    return payloads


def test_partial_receipt_never_displays_even_a_saved_preview_and_never_writes(tmp_path):
    payloads = setup(tmp_path)
    before = {p: p.read_bytes() for p in payloads}
    result = close_workbench(tmp_path / "data", tmp_path, inspect_process=lambda *_: {"status": "verified_alive"})
    assert result["status"] == "running" and result["preview"] is None
    assert result["progress"]["completed_symbols"] == 20
    assert result["job_started"] is result["provider_contacted"] is result["ledger_mutated"] is False
    assert before == {p: p.read_bytes() for p in payloads}


def test_reused_or_dead_pid_cannot_appear_running(tmp_path):
    setup(tmp_path)
    for state in ("pid_reused", "not_running", "identity_unverified"):
        result = close_workbench(tmp_path / "data", tmp_path, inspect_process=lambda *_, state=state: {"status": state})
        assert result["status"] == "collector_not_verified" and result["source_status"] == "running"


def test_complete_preview_requires_exact_source_identity_and_safe_flags(tmp_path):
    payloads = setup(tmp_path, status="complete_full_dated_close_inventory_not_admitted")
    result = close_workbench(tmp_path / "data", tmp_path, inspect_process=lambda *_: {"status": "not_running"})
    assert result["preview"]["day"] == "2026-10-14"
    assert result["source_bytes_revalidated_this_call"] is False
    path = next(p for p in payloads if p.name == "result.json")
    for key, value in (("day", "2026-10-13"), ("execution_admitted", True), ("orders_generated", True)):
        bad = dict(payloads[path], **{key: value})
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            close_workbench(tmp_path / "data", tmp_path, inspect_process=lambda *_: {"status": "not_running"})
    bad = dict(payloads[path])
    bad["source"]["receipt"]["raw_daily_sha256"] = "different"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="exact completed source"):
        close_workbench(tmp_path / "data", tmp_path, inspect_process=lambda *_: {"status": "not_running"})


def test_locator_cannot_read_outside_full_close_root(tmp_path):
    payloads = setup(tmp_path)
    path = next(p for p in payloads if p.name == "active_job.json")
    payloads[path]["folder"] = str(tmp_path)
    path.write_text(json.dumps(payloads[path]), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        close_workbench(tmp_path / "data", tmp_path)


def test_dotnet_seven_digit_start_timestamp_matches_current_process():
    from datetime import datetime, timezone
    import os
    import psutil
    timestamp = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), timezone.utc)
    text = timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
    assert process_identity(os.getpid(), text)["status"] == "verified_alive"
