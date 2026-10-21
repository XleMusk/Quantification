"""Only the new saved daily attachment; no strategy, source or Engine run."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_type_router_daily as daily
from trading.research import leader_type_router_paper as paper
from trading.research import system_daily_refresh as system
from trading.research.tests.test_system_daily_refresh import saved_system


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")
    return path


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime.fromisoformat("2026-10-19T03:00:00+08:00")
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(daily, "datetime", Clock)
    fixed = tmp_path / "fixed"
    calendar = save(fixed / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    anchor = save(fixed / "anchor/manifest.json", {"source_days": ["2026-10-16"],
        "calendar_sha256": daily._ref(calendar)["sha256"]})
    base = save(fixed / "base/paper_plan.json", {"source_close_day": "2026-10-16",
        "recorded_at": "2026-10-16T18:00:00+08:00"})
    ipo, master = [save(fixed / name, {}) for name in ("ipo.json", "master.json")]
    reg = save(fixed / "reg/registration.json", {"schema": paper.REGISTRATION_SCHEMA,
        "method_id": paper.METHOD_ID, "case_ids": list(paper.CASE_IDS),
        "registered_at": "2026-10-19T02:00:00+08:00", "first_decision_day": "2026-10-19",
        "first_execution_day": "2026-10-20", "references": {name: {
            "path": str(path), "sha256": daily._ref(path)["sha256"]} for name,path in (
                ("calendar", calendar), ("anchor_inputs", anchor), ("anchor_publication", base),
                ("ipo_evidence", ipo), ("research_master_run", master))}, **paper.FLAGS})
    # The real registration does not carry the daily adapter's network flag.
    assert "network_accessed" not in json.loads(reg.read_text())
    profile = daily.bootstrap_profile(registration=reg, run_dir=tmp_path / "books")
    return profile, Clock


def test_firstD_publishes_and_consumes_D_without_E_raw_preserving_partial_then_exact_recovery(evidence, tmp_path):
    profile, clock = evidence
    clock.current = datetime.fromisoformat("2026-10-19T20:00:00+08:00")
    panel = save(tmp_path / "Dsource/manifest.json", {"source_days": ["2026-10-16", "2026-10-19"],
        "calendar_sha256": profile["calendar"]["sha256"]})
    base = save(tmp_path / "Dpub/paper_plan.json", {"source_close_day": "2026-10-19",
        "proposed_execution_day": "2026-10-20", "recorded_at": "2026-10-19T18:30:00+08:00"})
    calls, outcomes = [], {}
    def run_step(step):
        calls.append(deepcopy(step))
        path = step["expected_files"][0]
        if step["id"] == "leader_type_router_publication":
            save(path, {"synthetic_saved_publication": True})
        else:
            save(path, {"case_count": 4, "status": "advanced_research_only_with_blocked_cases", "cases": [
                {"case_id": key, "last_processed_day": "2026-10-19" if i else None,
                    "error": None if i else "exact_D_failed", "blocking_evidence": None}
                for i,key in enumerate(paper.CASE_IDS)]})
        outcomes[step["id"]] = {"status": "completed"}
        return outcomes[step["id"]]
    out = tmp_path / "operations/19"
    first = daily.run_profile(profile, "2026-10-19", panel, base, out, run_step=run_step)
    assert [s["id"] for s in calls] == ["leader_type_router_publication", "leader_type_router_forward"]
    command = calls[1]["command"]
    assert command[command.index("--through")+1] == "2026-10-19"
    assert command[command.index("--panel")+1] == str(panel)
    assert first["result"]["consumer_required"] and not first["result"]["daily_complete"]
    assert first["next_profile"]["last_source_day"] == "2026-10-19"
    assert not Path(profile["run_directory"]).exists()  # Callback never creates a fake account.
    # A supervisor's exact saved recovery can be adopted without rerunning it.
    fixed = json.loads((out / "forward_result.json").read_text())
    fixed["cases"][0].update(last_processed_day="2026-10-19", error=None)
    save(out / "forward_result.json", fixed)
    recovered = daily.run_profile(first["next_profile"], "2026-10-19", panel, base, out,
        run_step=lambda s: outcomes[s["id"]])
    assert recovered["result"]["daily_complete"] and recovered["next_profile"] == first["next_profile"]
    assert len(calls) == 2 and profile["last_source_day"] == "2026-10-16"


def test_attach_is_metadata_only_and_router_commands_do_not_allow_registration(tmp_path, monkeypatch, evidence):
    saved = saved_system(tmp_path / "system_fixture", monkeypatch)
    profile, _ = evidence
    monkeypatch.setattr(daily, "bootstrap_profile", lambda **kwargs: deepcopy(profile))
    monkeypatch.setattr(system, "_run_command", lambda *a, **k: pytest.fail("attach cannot execute"))
    original = system._read(saved.directory / "head.json")
    result = system.attach_leader_type_router(saved.directory,
        registration=profile["registration"]["file"], forward_run_dir=profile["run_directory"])
    after = system._read(saved.directory / "head.json")
    assert not result["commands_executed"]
    assert all(after[k] == v for k,v in original.items())
    assert not Path(profile["run_directory"]).exists()
    before = (saved.directory / "head.json").read_bytes()
    assert system.attach_leader_type_router(saved.directory)["commands_executed"] is False
    assert (saved.directory / "head.json").read_bytes() == before
    for command in (["leader-type-router-paper", "publish"], ["leader-type-router-forward", "run"]):
        assert system._command(command)[-2:] == command
    with pytest.raises(ValueError, match="existing registration"):
        system._command(["leader-type-router-paper", "register"])
    blocked = system._leader_type_router_refresh(after, {}, tmp_path / "unused", "2026-10-19", {}, None)
    assert blocked["consumer_required"] is True and blocked["daily_complete"] is False
