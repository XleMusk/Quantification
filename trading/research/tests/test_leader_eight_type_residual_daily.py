"""Recipe-only checks; all child publications/results below are synthetic."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from trading.research import leader_eight_type_residual_daily as daily


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path, monkeypatch):
    class Clock(datetime):
        value = "2026-10-20T18:10:00+08:00"
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(cls.value)
    monkeypatch.setattr(daily, "datetime", Clock)
    frozen = tmp_path / "fixed"
    calendar = save(frozen / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    ipo, master, reg = [save(frozen / name, {}) for name in ("ipo.json", "master.json", "registration.json")]
    raw_dir = tmp_path / "exact_raw_source"
    raw_dir.mkdir()
    anchor = save(tmp_path / "anchor/manifest.json", {"source_days": ["2026-10-16"],
        "calendar_sha256": daily._ref(calendar)["sha256"], "source_proofs": [{"source_folder": str(raw_dir)}]})
    anchor_pub = save(tmp_path / "anchor_pub/paper_plan.json", {"source_close_day": "2026-10-16"})
    profile = {"schema": daily.SCHEMA, "paper_registration": daily._ref(reg),
        "fixed_refs": [daily._ref(p) for p in (reg, calendar, anchor, anchor_pub, ipo, master)],
        "anchor_input": daily._ref(anchor), "anchor_base_publication": daily._ref(anchor_pub),
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
        "last_source_day": "2026-10-16", "calendar": daily._ref(calendar),
        "ipo_evidence": str(ipo), "research_master_run": str(master),
        "run_directory": str(tmp_path / "books"), "case_ids": ["case" + str(i) for i in range(32)],
        "panels": [], "base_publications": [], "publications": [], "operations": [], **daily.FLAGS}
    def close(day):
        days = ["2026-10-16", "2026-10-19"] + (["2026-10-20"] if day == "2026-10-20" else [])
        panel = save(tmp_path / ("core" + day) / "inputs/manifest.json",
            {"source_days": days, "calendar_sha256": profile["calendar"]["sha256"]})
        execution = "2026-10-20" if day == "2026-10-19" else "2026-10-21"
        pub = save(tmp_path / ("core" + day) / "paper/paper_plan.json", {
            "source_close_day": day, "proposed_execution_day": execution,
            "recorded_at": day + "T18:00:00+08:00", "source": {
                "input_manifest": str(panel), "input_manifest_sha256": daily._ref(panel)["sha256"]}})
        return panel, pub
    return profile, close, Clock


def runner(profile):
    calls, saved = [], {}
    def execute(step):
        key = step["expected_files"][0]
        if key not in saved:
            calls.append(deepcopy(step))
            for path in step["expected_files"]:
                payload = {"synthetic": step["id"]}
                if step["id"] == "residual_forward":
                    payload = {"case_count": 32, "status": "blocked_or_waiting_research_only",
                        "cases": [{"case_id": key, "last_processed_day": "2026-10-19", "error": None,
                            "blocking_evidence": {"reason": "reference_unknown"}} for key in profile["case_ids"]]}
                save(path, payload)
            saved[key] = {"status": "completed"}
        return saved[key]
    return execute, calls


def test_first_decision_no_account_then_full_prefix_and_same_day_consumer_recovery(source, tmp_path):
    profile, close, _ = source
    original = deepcopy(profile)
    execute, calls = runner(profile)
    panel19, pub19 = close("2026-10-19")
    result19 = daily.run_profile(profile, "2026-10-19", panel19, pub19,
        tmp_path / "operations/19", run_step=execute)
    read19, profile19 = result19["result"], result19["next_profile"]
    assert profile == original and read19["producer_complete"] is True
    assert read19["consumer_required"] is False and read19["consumer_complete"] is False
    assert read19["consumer_called"] is False and read19["account_count"] is None
    assert len(calls) == 1 and not Path(profile["run_directory"]).exists()
    assert calls[0]["command"][:2] == ["leader-eight-type-residual", "publish"]
    panel20, pub20 = close("2026-10-20")
    out20 = tmp_path / "operations/20"
    result20 = daily.run_profile(profile19, "2026-10-20", panel20, pub20, out20, run_step=execute)
    assert result20["next_profile"]["last_source_day"] == "2026-10-20"
    assert result20["result"]["consumer_required"] is True
    assert result20["result"]["consumer_complete"] is False
    command = calls[-1]["command"]
    assert command[:2] == ["leader-eight-type-residual", "forward"]
    assert [command[i + 1] for i, v in enumerate(command[:-1]) if v == "--publication"] == [
        *profile19["publications"], str(out20 / "artifacts/publication/paper_plan.json")]
    assert len(calls) == 3
    saved = json.loads((out20 / "forward_result.json").read_text())
    for row in saved["cases"]:
        row.update(last_processed_day="2026-10-20", blocking_evidence=None)
    duplicate = deepcopy(saved)
    duplicate["cases"].append(deepcopy(duplicate["cases"][0]))
    save(out20 / "forward_result.json", duplicate)
    malformed = daily.run_profile(result20["next_profile"], "2026-10-20", panel20, pub20, out20, run_step=execute)
    assert malformed["result"]["consumer_complete"] is False
    save(out20 / "forward_result.json", saved)
    recovered = daily.run_profile(result20["next_profile"], "2026-10-20", panel20, pub20, out20, run_step=execute)
    assert len(calls) == 3 and recovered["result"]["consumer_complete"] is True
    assert recovered["next_profile"] == result20["next_profile"]
    assert daily.validate_plan(json.loads((out20 / "plan.json").read_text()),
        recovered["next_profile"], "2026-10-20", panel20, pub20, out20)


def test_failed_wrapper_keeps_cursor_and_plan_tampering_does_not_invoke_child(source, tmp_path):
    profile, close, _ = source
    panel, pub = close("2026-10-19")
    calls = []
    def failed(step):
        calls.append(step["id"])
        return {"status": "failed_no_automatic_retry"}
    out = tmp_path / "operation"
    result = daily.run_profile(profile, "2026-10-19", panel, pub, out, run_step=failed)
    assert result["next_profile"] is None and profile["last_source_day"] == "2026-10-16"
    assert calls == ["residual_publication"] and result["result"]["producer_complete"] is False
    plan = json.loads((out / "plan.json").read_text())
    plan["steps"][0]["command"].append("different")
    save(out / "plan.json", plan)
    with pytest.raises(ValueError, match="cannot change"):
        daily.run_profile(profile, "2026-10-19", panel, pub, out, run_step=failed)
    assert calls == ["residual_publication"]


def test_exact_day_source_binding_actual_clock_and_indirect_input_separation(source, tmp_path):
    profile, close, _ = source
    panel, pub = close("2026-10-19")
    out = tmp_path / "unwritten"
    with pytest.raises(ValueError, match="exact next"):
        daily.next_plan(profile, "2026-10-20", panel, pub, out)
    payload = json.loads(pub.read_text())
    payload["source"]["input_manifest_sha256"] = "wrong"
    save(pub, payload)
    with pytest.raises(ValueError, match="Exact current"):
        daily.next_plan(profile, "2026-10-19", panel, pub, out)
    payload["source"]["input_manifest_sha256"] = daily._ref(panel)["sha256"]
    payload["recorded_at"] = "2026-10-20T00:01:00+08:00"
    save(pub, payload)
    with pytest.raises(ValueError, match="real same-day"):
        daily.next_plan(profile, "2026-10-19", panel, pub, out)
    payload["recorded_at"] = "2026-10-19T18:00:00+08:00"
    save(pub, payload)
    root = Path(profile["anchor_input"]["file"]).parent.parent / "exact_raw_source"
    assert str(root) in daily.input_directories(profile)
    with pytest.raises(ValueError, match="separate"):
        daily.next_plan(profile, "2026-10-19", panel, pub, root / "new_output")
    save(profile["ipo_evidence"], {"changed": True})
    with pytest.raises(ValueError, match="Registered residual-exit input changed"):
        daily.next_plan(profile, "2026-10-19", panel, pub, out)
    assert not out.exists()


def test_bootstrap_reads_original_registration_and_anchor_without_producers(source, tmp_path, monkeypatch):
    profile, _, clock = source
    clock.value = "2026-10-18T19:00:00+08:00"
    registration = {"anchor_base_publication": profile["anchor_base_publication"],
        "anchor_input": profile["anchor_input"], "calendar": profile["calendar"],
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
        "registered_at": "2026-10-18T18:50:00+08:00", "case_ids": profile["case_ids"]}
    module = SimpleNamespace(_registered=lambda path, now: (registration, daily._ref(path)))
    monkeypatch.setitem(sys.modules, "trading.research.leader_eight_type_residual_paper", module)
    import trading.research as package
    monkeypatch.setattr(package, "leader_eight_type_residual_paper", module, raising=False)
    kwargs = {"paper_registration": profile["paper_registration"]["file"],
        "ipo_evidence": profile["ipo_evidence"], "research_master_run": profile["research_master_run"],
        "run_dir": profile["run_directory"]}
    actual = daily.bootstrap_profile(**kwargs)
    assert actual["last_source_day"] == "2026-10-16"
    assert actual["publications"] == [] and actual["base_publications"] == [] and actual["panels"] == []
    assert not Path(profile["run_directory"]).exists()
    clock.value = "2026-10-19T15:00:00+08:00"
    with pytest.raises(ValueError, match="precede the original"):
        daily.bootstrap_profile(**kwargs)
