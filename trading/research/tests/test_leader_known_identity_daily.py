from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_known_identity_daily as daily
from trading.research import leader_known_identity_paper as paper


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_first19_only_publishes_then20_keeps_previous_and_full_consumer_prefix(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-20T19:00:00+08:00")
    monkeypatch.setattr(daily, "datetime", Clock)
    fixed = tmp_path / "fixed"
    calendar = save(fixed / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    ipo, master, reg = [save(fixed / name, {}) for name in ("ipo.json", "master.json", "registration.json")]
    anchor = save(tmp_path / "anchor/manifest.json", {"source_days": ["2026-10-16"],
        "calendar_sha256": daily._ref(calendar)["sha256"]})
    anchor_pub = save(tmp_path / "anchor_pub/paper_plan.json", {"source_close_day": "2026-10-16"})
    profile = {"schema": daily.SCHEMA, "paper_registration": daily._ref(reg),
        "fixed_refs": [daily._ref(p) for p in (reg, calendar, anchor, anchor_pub, ipo, master)],
        "anchor_input": daily._ref(anchor), "anchor_base_publication": daily._ref(anchor_pub),
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
        "last_source_day": "2026-10-16", "calendar": daily._ref(calendar),
        "ipo_evidence": str(ipo), "research_master_run": str(master),
        "run_directory": str(tmp_path / "books"), "case_ids": list(paper.CASE_IDS),
        "panels": [], "base_publications": [], "publications": [], "operations": [], **daily.FLAGS}
    def close(day):
        days = ["2026-10-16", "2026-10-19"] + (["2026-10-20"] if day == "2026-10-20" else [])
        panel = save(tmp_path / ("source_" + day) / "input/manifest.json",
            {"source_days": days, "calendar_sha256": profile["calendar"]["sha256"]})
        execution = "2026-10-20" if day == "2026-10-19" else "2026-10-21"
        pub = save(tmp_path / ("source_" + day) / "paper/paper_plan.json", {
            "source_close_day": day, "proposed_execution_day": execution,
            "recorded_at": day + "T18:00:00+08:00", "source": {
                "input_manifest": str(panel), "input_manifest_sha256": daily._ref(panel)["sha256"]}})
        return panel, pub
    calls = []
    finished = {}
    def run_step(step):
        key = step["expected_files"][0]
        if key not in finished:
            calls.append(deepcopy(step))
            value = {"synthetic": True}
            if step["id"] == "known_identity_forward":
                value = {"case_count": 4, "status": "advanced_research_only", "cases": [
                    {"case_id": key, "last_processed_day": "2026-10-20", "error": None,
                     "blocking_evidence": None} for key in paper.CASE_IDS]}
            save(key, value)
            finished[key] = {"status": "completed"}
        return finished[key]
    panel19, pub19 = close("2026-10-19")
    first = daily.run_profile(profile, "2026-10-19", panel19, pub19, tmp_path / "operations/19", run_step=run_step)
    assert first["result"]["consumer_required"] is False and len(calls) == 1
    assert "--previous-publication" not in calls[0]["command"]
    assert not Path(profile["run_directory"]).exists()
    panel20, pub20 = close("2026-10-20")
    second = daily.run_profile(first["next_profile"], "2026-10-20", panel20, pub20,
        tmp_path / "operations/20", run_step=run_step)
    assert len(calls) == 3 and second["result"]["consumer_complete"] is True
    pub_command, run_command = calls[1]["command"], calls[2]["command"]
    assert pub_command[pub_command.index("--previous-publication") + 1] == first["next_profile"]["publications"][-1]
    assert [run_command[i + 1] for i, arg in enumerate(run_command) if arg == "--publication"] == second["next_profile"]["publications"]
    recovered = daily.run_profile(second["next_profile"], "2026-10-20", panel20, pub20,
        tmp_path / "operations/20", run_step=run_step)
    assert len(calls) == 3 and recovered["next_profile"] == second["next_profile"]
    assert profile["last_source_day"] == "2026-10-16"


def test_forward_cli_uses_protected_keyword_export_and_does_not_replace_source(tmp_path, monkeypatch):
    files = [save(tmp_path / "sources" / name, {}) for name in ("panel.json", "ipo.json", "master.json", "registration.json")]
    panel, ipo, master, reg = files
    run, output = tmp_path / "books", tmp_path / "observations/result.json"
    calls = []
    def forward(*args, **kwargs):
        calls.append((args, kwargs))
        return {"schema": paper.RESULT_SCHEMA, "status": "synthetic_cli_dispatch", "case_count": 4}
    monkeypatch.setattr(paper, "forward", forward)
    assert paper.main(["forward", "--panel", str(panel), "--ipo-evidence", str(ipo),
        "--research-master-run", str(master), "--registration", str(reg), "--run-dir", str(run),
        "--through", "2026-10-20", "--result-json", str(output)]) == 0
    assert len(calls) == 1 and json.loads(output.read_text())["case_count"] == 4
    with pytest.raises(ValueError, match="new external JSON"):
        paper.main(["forward", "--panel", str(panel), "--ipo-evidence", str(ipo),
            "--research-master-run", str(master), "--registration", str(reg), "--run-dir", str(run),
            "--result-json", str(panel)])
    assert len(calls) == 1 and not run.exists()
