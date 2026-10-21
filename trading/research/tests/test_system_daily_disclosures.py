"""New disclosure coordination wiring; no providers or market replay."""
from copy import deepcopy

import pytest

from trading.research import seat_daily, seat_disclosure_daily as disclosure, system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import saved_system, write, D


def test_bound_collector_and_catalog_commands_only():
    command = ["seat-collect", "--start", D, "--end", D, "--max-pages", "3", "--execute"]
    assert daily._command(command)[-len(command):] == command
    for changed in ([*command[:-1]], [*command[:-2], "8", "--execute"],
                    ["seat-collect", "--start", D, "--end", "2026-10-19", "--max-pages", "3", "--execute"]):
        with pytest.raises(ValueError, match="one exact day"):
            daily._command(changed)
    with pytest.raises(ValueError, match="only build or append"):
        daily._command(["seat-disclosure-catalog", "register"])


def test_disclosure_wrapper_uses_saved_prior_catalog_after_partial_adoption(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = deepcopy(saved.head)
    original = write(tmp_path / "original/catalog.json", {"old": True})
    latest = write(tmp_path / "later/catalog.json", {"new": True})
    head["public_seat_attachment"] = write(tmp_path / "attachment.json", {})
    head["public_seat_profile"] = {"append_seat": {"catalog": latest}}
    operation = saved.directory / "operations/20261016"
    plan = write(operation / "seat_disclosures/plan.json", {"prior_catalog": original})
    source = {"day": D, "source_folder": str(saved.package[D])}
    calls = []

    def run(day, prior, out, *, calendar, run_step, execute, **options):
        assert day == D and prior == original["file"] and execute
        assert calendar == daily._read(saved.directory / "registration.json")["calendar"]["file"]
        run_step({"id": "seat_disclosure_catalog", "command": ["seat-disclosure-catalog", "append"],
            "expected_files": [str(out / "catalog.json")]})
        return {"daily_complete": True, "catalog": latest, "network_accessed": False}

    monkeypatch.setattr(disclosure, "run", run)
    monkeypatch.setattr(daily, "_run_command", lambda step, directory, day: calls.append(step) or {"status": "completed"})
    before = deepcopy(head)
    result = daily._seat_disclosures_refresh(head, daily._read(saved.directory / "registration.json"),
        operation, D, source)
    assert result["catalog"] == latest and head == before
    assert calls[0]["binding"] == {"kind": "seat_disclosures", "registration": head["registration"],
        "attachment": head["public_seat_attachment"], "source": source, "plan": plan}


def test_failed_new_disclosure_source_does_not_block_saved_seat_execution_or_old_routes(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    head = daily._read(saved.directory / "head.json")
    catalog = write(tmp_path / "initial/catalog.json", {})
    profile = {"last_source_day": D, "append_seat": {"catalog": catalog}}
    head.update(public_seat_profile=profile,
        public_seat_attachment=write(saved.directory / "addenda/public_seat.json", {
            "schema": "system_daily_public_seat_attachment_v1", "system_registration": head["registration"],
            "initial_profile": profile, **daily.FLAGS}))
    write(saved.directory / "head.json", head)
    monkeypatch.setattr(seat_daily, "input_directories", lambda profile: [])
    monkeypatch.setattr(daily, "_run_command", lambda *a, **k: pytest.fail("Saved old routes must not rerun"))
    monkeypatch.setattr(daily, "_seat_disclosures_refresh", lambda *a: {
        "status": "failed_or_unresolved_saved_attempt_no_retry", "daily_complete": False,
        "catalog": None, "network_accessed": None, "network_access_unknown": True})
    called = []
    monkeypatch.setattr(daily, "_public_seat_refresh", lambda *a, catalog=None:
        called.append(catalog) or {"daily_complete": True, "network_accessed": False})
    result = daily.run(saved.directory, D, saved.package[D], refresh_seat_disclosures=True)
    assert called == [None] and result["status"] == "partial_research_routes"
    assert result["network_accessed"] is None and result["network_access_unknown"] is True
    assert result["core_producers"] == "saved_exact_source_day_reused_without_reexecution"
    assert all(result["native_forward_" + key]["status"] == "exact_execution_day_already_committed"
        for key in ("v1", "qualified"))
    assert daily._read(saved.directory / "head.json")["profile"] == head["profile"]
