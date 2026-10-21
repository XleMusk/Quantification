"""A same-D disclosure append never replaces an already frozen seat plan."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import seat_daily as seat
from trading.research import system_daily_refresh as system
from trading.research.tests.test_seat_daily import setup, runner, save


def test_same_day_new_catalog_preserves_frozen_plan_and_cached_execution(setup, monkeypatch):
    profile, close, catalog16, root = setup
    day = "2026-10-19"
    operation = root / "system/operations/20261019"
    out = operation / "public_seat"
    package = close(day)
    calls, produce = runner(profile, day)

    def initial(step):
        produce(step)  # Only the existing synthetic saved-file fixture runs.
        if step["id"].endswith("forward"):
            file = Path(step["expected_files"][0])
            save(file, {**seat._read(file), **system.FLAGS})
        receipt = {"status": "completed", "command": step["command"],
            "output_refs": system._outputs(step, day), "synthetic_only": True}
        save(operation / "commands" / (step["id"] + ".json"), receipt)
        return receipt

    first = seat.run_profile(profile, day, package, out, run_step=initial)
    head = {"public_seat_profile": first["next_profile"],
        "registration": {"synthetic_only": True}, "public_seat_attachment": {"synthetic_only": True}}
    plan_before = (out / "plan.json").read_bytes()
    plan = seat._read(out / "plan.json")
    publications_before = {key: Path(file).read_bytes() for key, file in plan["publication_files"].items()}
    calls_before = list(calls)
    batch19 = save(root / "catalog_sources/2026-10-19/batch.json", {
        "schema": "public_seat_disclosure_batch_v1", "batch_id": day, "event_day": day,
        "status": "completed_empty", "created_at": "2026-10-19T18:40:00+08:00",
        "source_available_at": "2026-10-19T18:39:00+08:00"})
    record19 = {"schema": "public_seat_disclosure_catalog_v1",
        "created_at": "2026-10-19T18:41:00+08:00", "parent_ref": seat._ref(catalog16),
        "batches": [*seat._read(catalog16)["batches"],
            {"batch_id": day, "event_day": day, "reference": seat._ref(batch19)}]}
    catalog19 = save(root / "catalog/catalog19.json", record19)
    monkeypatch.setattr(system.subprocess, "Popen", lambda *a, **k: pytest.fail("No new publisher or Engine child"))
    repeated = system._public_seat_refresh(head, {}, operation, day, {"day": day}, package,
        catalog=catalog19)
    assert repeated["daily_complete"] is True
    assert calls == calls_before  # No new synthetic producer/Engine invocation.
    assert (out / "plan.json").read_bytes() == plan_before
    assert {key: Path(file).read_bytes() for key, file in plan["publication_files"].items()} == publications_before
    assert head["public_seat_profile"] == first["next_profile"]
    assert seat._read(out / "plan.json")["catalog"] == str(catalog16)

    # The actual later event catalog is only consumed by a new source day.
    next_plan = seat.next_plan(head["public_seat_profile"], "2026-10-20", close("2026-10-20"),
        root / "system/operations/20261020/public_seat", catalog=catalog19)
    assert next_plan["catalog"] == str(catalog19) and next_plan["missing_event_days"] == []
    assert next_plan["before_profile"]["append_seat"]["publications"] == first["next_profile"]["append_seat"]["publications"]

    invalid = deepcopy(record19)
    invalid["batches"] = invalid["batches"][-1:]  # Illegal replacement of the exact original prefix.
    bad_catalog = save(root / "catalog/invalid_append.json", invalid)
    monkeypatch.setattr(system, "_run_command", lambda *a, **k: pytest.fail("Invalid catalog must fail before saved steps"))
    with pytest.raises(ValueError, match="removed or replaced"):
        system._public_seat_refresh(head, {}, operation, day, {"day": day}, package, catalog=bad_catalog)
