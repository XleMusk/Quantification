"""New router generation adopts saved evidence and joins the existing driver."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading.research import leader_router_repair_daily as daily
from trading.research import system_daily_refresh as system
from trading.research.tests.test_system_daily_native_isolation import _wired, _head
from trading.research.tests.test_system_daily_refresh import E, F, write


def test_adopt_repair_and_next_saved_core_day_uses_new_identity_without_changing_original(tmp_path, monkeypatch):
    state = _wired(tmp_path, monkeypatch)
    system.run(state.saved.directory, E, state.saved.package[E])
    head = _head(state)
    original_reg = tmp_path / "original_registration/reg.json"
    original_ref = write(original_reg, {"synthetic_original": True})
    old_profile = {"registration": original_ref, "last_source_day": "2026-10-16", "first_decision_day": E}
    old_attachment = state.saved.directory / "addenda/leader_type_router.json"
    write(old_attachment, {"schema": "system_daily_leader_type_router_attachment_v1",
        "system_registration": head["registration"], "initial_profile": old_profile, **system.FLAGS})
    head.update(leader_type_router_profile=old_profile, leader_type_router_attachment=system._ref(old_attachment))
    write(state.saved.directory / "head.json", head)
    registration_bytes = (state.saved.directory / "registration.json").read_bytes()
    old_bytes = old_attachment.read_bytes()
    new_reg = tmp_path / "new_registration/reg.json"
    case_ids = ["case1", "case2", "case3", "case4"]
    record = {"schema": daily.repair.SCHEMA, "repair_identity": "synthetic-repair",
        "repair_run_directory": str(tmp_path / "repaired_books"), "case_ids": case_ids,
        "first_decision_day": E, "first_execution_day": F,
        "repair_evidence": {"original_registration": {"path": original_ref["file"], "sha256": original_ref["sha256"]}}}
    new_ref = write(new_reg, record)
    panel19, pub19 = tmp_path / "input19/manifest.json", tmp_path / "publication19/publication.json"
    write(panel19, {"synthetic_day": E}); write(pub19, {"synthetic_day": E})
    observation = tmp_path / "observations/first.json"
    observation_ref = write(observation, {"synthetic_saved_consumer": True})
    profile = {"schema": daily.SCHEMA, "registration": new_ref,
        "fixed_refs": [new_ref, head["profile"]["calendar"] | {"file": head["profile"]["calendar"]["path"]}],
        "calendar": {"file": head["profile"]["calendar"]["path"], "sha256": head["profile"]["calendar"]["sha256"]},
        "repair_identity": record["repair_identity"], "original_registration": record["repair_evidence"]["original_registration"],
        "first_decision_day": E, "first_execution_day": F, "case_ids": case_ids,
        "run_directory": record["repair_run_directory"], "last_source_day": E,
        "panels": [str(panel19)], "publications": [str(pub19)], "base_publications": [str(pub19)],
        "operations": [], "consumer_result": observation_ref, **daily.FLAGS}
    profile["fixed_refs"][1].pop("path")
    monkeypatch.setattr(daily, "bootstrap_profile", lambda **kwargs: deepcopy(profile))
    opening_calls = []
    monkeypatch.setattr(system, "_registered_opening", lambda *args: opening_calls.append(args[0]["router_repair_profile"]) or [])
    adopted = system.adopt_router_repair(state.saved.directory, registration=new_reg,
        publication=pub19, panel=panel19, consumer_observation=observation)
    assert adopted["commands_executed"] is False and len(opening_calls) == 1
    assert _head(state)["leader_type_router_profile"] == old_profile
    assert system.adopt_router_repair(state.saved.directory, registration=new_reg,
        publication=pub19, panel=panel19, consumer_observation=observation)["status"] == "existing_router_repair_attachment_reused"
    monkeypatch.setattr(system, "_leader_type_router_refresh", lambda *a, **k: pytest.fail("Do not retry the superseded failed route"))
    monkeypatch.setattr(system._attached_recipe("leader_type_router"), "input_directories",
        lambda profile: [str(original_reg.parent)])  # The retired source is a deliberately small fixture.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat("2026-10-20T18:00:00+08:00")
    monkeypatch.setattr(daily, "datetime", Clock)
    panel20, base20 = tmp_path / "input20/manifest.json", tmp_path / "base20/paper.json"
    panel_ref = write(panel20, {"synthetic_day": F})
    write(base20, {"source_close_day": F, "proposed_execution_day": "2026-10-21",
        "recorded_at": "2026-10-20T15:10:00+08:00", "source": {
            "input_manifest": str(panel20), "input_manifest_sha256": panel_ref["sha256"]}})
    saved = _head(state)
    saved["last_source_day"] = saved["profile"]["last_source_day"] = F
    saved["profile"]["output_paths"] = {"eight_inputs": {"manifest": str(panel20)}, "paper32": {"publication": str(base20)}}
    write(state.saved.directory / "head.json", saved)
    prior_execute = system._run_command
    produced = []
    def execute(step, directory, day):
        if not step["id"].startswith("router_repair_"):
            return prior_execute(step, directory, day)
        receipt = directory / (step["id"] + ".json")
        if receipt.exists():
            return system._read(receipt)
        produced.append(step["id"])
        assert step["command"][0] == "leader-type-router-runtime-repair"
        assert system._values(step["command"], "--registration") == [str(new_reg)]
        if step["id"] == "router_repair_forward":
            assert system._values(step["command"], "--publication")[0] == str(pub19)
            value = {"case_count": 4, "cases": [{"case_id": key, "last_processed_day": day} for key in case_ids]}
        else:
            value = {"synthetic_new_identity_publication": True}
        write(Path(step["expected_files"][0]), value)
        result = {"status": "completed", "command": step["command"]}
        write(receipt, result)
        return result
    monkeypatch.setattr(system, "_run_command", execute)
    first = system.run(state.saved.directory, F, state.saved.package[F])
    assert first["router_repair"]["daily_complete"] is True
    assert _head(state)["router_repair_profile"]["last_source_day"] == F
    system.run(state.saved.directory, F, state.saved.package[F])
    assert produced == ["router_repair_publication", "router_repair_forward"]
    assert _head(state)["leader_type_router_profile"] == old_profile
    assert old_attachment.read_bytes() == old_bytes
    assert (state.saved.directory / "registration.json").read_bytes() == registration_bytes
    with pytest.raises(ValueError, match="exact next"):
        daily.next_plan(_head(state)["router_repair_profile"], "2026-10-22", panel20, base20, tmp_path / "skip")
