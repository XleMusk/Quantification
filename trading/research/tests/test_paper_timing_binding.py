import json

import pytest

from trading.research.paper_timing_binding import bind_timing, write_binding


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _timing(path):
    return _write(path, {
        "schema": "stockdb_post_close_timing_contract_receipt_v1",
        "execution_admitted": False,
    })


def _task(path, **changes):
    task = {
        "schema": "stockdb_post_close_d0_d1_orchestration_v1",
        "status": "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1",
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "close_date": "2026-10-23",
        "execution_day": "2026-10-26",
        "settlement_day": "2026-10-27",
        "input_fingerprint": "fingerprint",
        "snapshot_coverage": {"status": "COMPLETE"},
        "d0_d1": {
            "task_measurement": {
                "schema": "local_task_measurement_v1",
                "started_at": "2026-10-26T17:10:38+00:00",
                "finished_at": "2026-10-26T17:10:39+00:00",
                "wall_seconds": 0.9,
                "clock_basis": "local_monotonic_wall_clock",
                "external_provider_cost_cny": None,
                "execution_admitted": False,
            },
            "fixed": {"target_count": 20, "paper_order_count": 20},
            "joint_paper": {"paper_order_count": 22},
        },
    }
    task.update(changes)
    return _write(path, task)


def test_bind_timing_to_completed_non_live_paper_task(tmp_path):
    timing = _timing(tmp_path / "timing.json")
    task = _task(tmp_path / "task.json")
    result = bind_timing(timing, task)
    assert result["status"] == "VERIFIED_LOCAL_PAPER_TASK_TIMING"
    assert result["timing"]["wall_seconds"] == 0.9
    assert result["task"]["joint_paper_order_count"] == 22
    assert result["expense"] == {
        "provider_cost_cny": None,
        "status": "UNMEASURED_EXTERNAL_PROVIDER_EXPENSE",
    }
    assert result["execution_admitted"] is False


@pytest.mark.parametrize("changes", [
    {"execution_admitted": True},
    {"d0_d1": {"task_measurement": {"schema": "local_task_measurement_v1", "wall_seconds": 1, "execution_admitted": True}}},
    {"d0_d1": {"task_measurement": {"schema": "local_task_measurement_v1", "wall_seconds": 1, "external_provider_cost_cny": 1.0, "execution_admitted": False}}},
])
def test_binding_rejects_untrusted_task_boundary(tmp_path, changes):
    timing = _timing(tmp_path / "timing.json")
    task = _task(tmp_path / "task.json", **changes)
    with pytest.raises(ValueError):
        bind_timing(timing, task)


def test_write_binding_persists_hash_bound_receipt(tmp_path):
    timing = _timing(tmp_path / "timing.json")
    task = _task(tmp_path / "task.json")
    output = tmp_path / "binding.json"
    write_binding(output, timing, task)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["timing_contract"]["path"] == str(timing.resolve())
    assert len(payload["timing_contract"]["sha256"]) == 64
