"""Only the new daily recipe and CLI export; no source, model or replay runs."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_dated_catchup_daily as daily
from trading.research import leader_dated_catchup_forward as forward


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def inputs(tmp_path):
    frozen = tmp_path / "frozen"
    calendar = save(frozen / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    registry, ipo, master = [save(frozen / name, {}) for name in ("registry.json", "ipo.json", "master.json")]
    catalog = save(tmp_path / "catalog16/catalog.json", {"entries": [{"source_day": "2026-10-16"}]})
    base = save(tmp_path / "old_panel/manifest.json", {})
    candidate = save(frozen / "candidate_registration.json", {"base_panel": daily._ref(base)})
    paper = save(frozen / "paper_registration.json", {})
    profile = {"schema": daily.SCHEMA, "paper_registration": daily._ref(paper),
        "candidate_registration": daily._ref(candidate),
        "fixed_refs": [daily._ref(p) for p in (calendar, registry, ipo, master, candidate, paper)],
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20",
        "last_source_day": "2026-10-16", "calendar": daily._ref(calendar),
        "registry": str(registry), "catalog": daily._ref(catalog),
        "ipo_evidence": str(ipo), "research_master_run": str(master),
        "run_directory": str(tmp_path / "books"), "candidate_inputs": [], "publications": [],
        "case_ids": ["case" + str(i) for i in range(4)], "operations": [], **daily.FLAGS}
    def source(day):
        days = ["2026-10-16", "2026-10-19"] + (["2026-10-20"] if day == "2026-10-20" else [])
        panel = save(tmp_path / ("core" + day) / "panel/manifest.json",
            {"source_days": days, "calendar_sha256": profile["calendar"]["sha256"]})
        analysis = save(tmp_path / ("core" + day) / "environment/result.json", {"day": day})
        return panel, analysis
    return profile, source


def runner(profile):
    calls, records = [], {}
    blocked = {"value": True}
    def run(step):
        key = step["id"] + ":" + step["expected_files"][0]
        if key not in records:
            calls.append(key)
            for filename in step["expected_files"]:
                payload = {"written_by": step["command"][0]}
                if step["id"] == "catchup_forward":
                    payload = {"case_count": 4, "status": "blocked_or_waiting_research_only",
                        "cases": [{"case_id": key, "last_processed_day": "2026-10-19" if blocked["value"] else "2026-10-20",
                            "error": None, "blocking_evidence": {"reason": "reference_unknown"} if blocked["value"] else None}
                            for key in profile["case_ids"]], "metrics": None}
                save(filename, payload)
            records[key] = {"status": "completed"}
        return records[key]
    return run, calls, blocked


def test_exact_new_day_prefix_first_publication_no_waiting_and_same_day_recovery(tmp_path):
    original, source = inputs(tmp_path)
    before = deepcopy(original)
    run, calls, blocked = runner(original)
    p19, a19 = source("2026-10-19")
    out19 = tmp_path / "operations/19"
    result19 = daily.run_profile(original, "2026-10-19", p19, a19, out19, run_step=run)
    profile19, read19 = result19["next_profile"], result19["result"]
    assert original == before and read19["producer_complete"] is True
    assert read19["consumer_required"] is False and read19["consumer_complete"] is False
    assert read19["consumer_called"] is False and not Path(original["run_directory"]).exists()
    assert len(calls) == 3 and not (out19 / "forward_result.json").exists()
    p20, a20 = source("2026-10-20")
    out20 = tmp_path / "operations/20"
    planned = daily.next_plan(profile19, "2026-10-20", p20, a20, out20)
    pub = next(s for s in planned["steps"] if s["id"] == "catchup_publication")["command"]
    values = [pub[i + 1] for i, v in enumerate(pub[:-1]) if v == "--candidate-input"]
    assert values == [profile19["candidate_inputs"][0], str(out20 / "artifacts/candidate/input.json")]
    result20 = daily.run_profile(profile19, "2026-10-20", p20, a20, out20, run_step=run)
    assert result20["next_profile"]["last_source_day"] == "2026-10-20"
    assert result20["result"]["consumer_required"] is True
    assert result20["result"]["consumer_complete"] is False
    assert result20["result"]["status"] == "catchup_publication_saved_consumer_blocked_or_pending"
    count = len(calls)
    # A recovered terminal/result becomes readable even after producer adoption.
    status = json.loads((out20 / "forward_result.json").read_text())
    for row in status["cases"]:
        row.update(last_processed_day="2026-10-20", blocking_evidence=None)
    save(out20 / "forward_result.json", status)
    recovered = daily.run_profile(result20["next_profile"], "2026-10-20", p20, a20, out20, run_step=run)
    assert len(calls) == count and recovered["result"]["consumer_complete"] is True
    assert recovered["next_profile"] == result20["next_profile"]
    assert daily.validate_plan(json.loads((out20 / "plan.json").read_text()),
        recovered["next_profile"], "2026-10-20", p20, a20, out20)


def test_failed_producer_keeps_cursor_and_changed_plan_is_not_retried(tmp_path):
    profile, source = inputs(tmp_path)
    panel, analysis = source("2026-10-19")
    calls = []
    def failed(step):
        calls.append(step["id"])
        return {"status": "failed_no_automatic_retry"}
    out = tmp_path / "operation"
    result = daily.run_profile(profile, "2026-10-19", panel, analysis, out, run_step=failed)
    assert result["next_profile"] is None and profile["last_source_day"] == "2026-10-16"
    assert calls == ["catchup_input"] and result["result"]["producer_complete"] is False
    plan = json.loads((out / "plan.json").read_text())
    plan["steps"][0]["command"].extend(["--registration", "different"])
    save(out / "plan.json", plan)
    with pytest.raises(ValueError, match="cannot change"):
        daily.run_profile(profile, "2026-10-19", panel, analysis, out, run_step=failed)
    assert calls == ["catchup_input"]


def test_skipped_day_wrong_source_and_frozen_ref_reject_before_output(tmp_path):
    profile, source = inputs(tmp_path)
    panel, analysis = source("2026-10-19")
    out = tmp_path / "unwritten"
    with pytest.raises(ValueError, match="exact next"):
        daily.next_plan(profile, "2026-10-20", panel, analysis, out)
    save(analysis, {"day": "2026-10-16"})
    with pytest.raises(ValueError, match="Exact current"):
        daily.next_plan(profile, "2026-10-19", panel, analysis, out)
    save(profile["ipo_evidence"], {"changed": True})
    with pytest.raises(ValueError, match="input or catalog changed"):
        daily.next_plan(profile, "2026-10-19", panel, analysis, out)
    assert not out.exists()


def test_cli_saves_same_single_run_result_and_preserves_stdout(tmp_path, monkeypatch, capsys):
    profile, _ = inputs(tmp_path)
    panel = save(tmp_path / "panel/manifest.json", {})
    result_file = tmp_path / "observations/result.json"
    calls = []
    result = {"status": "blocked_or_waiting_research_only", "case_count": 4, "metrics": None,
        "research_checkpoint_created": False, "execution_admitted": False}
    monkeypatch.setattr(forward, "run", lambda *a, **kw: calls.append((a, kw)) or result)
    args = ["--panel", str(panel), "--ipo-evidence", profile["ipo_evidence"],
        "--research-master-run", profile["research_master_run"],
        "--registration", profile["paper_registration"]["file"], "--run-dir", profile["run_directory"],
        "--through", "2026-10-20", "--result-json", str(result_file)]
    assert forward.main(args) == 0
    assert len(calls) == 1 and json.loads(capsys.readouterr().out) == result
    assert json.loads(result_file.read_text()) == result
    with pytest.raises(ValueError, match="new external"):
        forward.main(args)
    assert len(calls) == 1 and not Path(profile["run_directory"]).exists()


def test_cli_protects_indirect_source_packages_before_account_call(tmp_path, monkeypatch):
    profile, _ = inputs(tmp_path)
    raw = save(tmp_path / "indirect/raw.json", {})
    candidate = save(tmp_path / "candidate/input.json", {"source": {"proof": {"file": str(raw)}}})
    publication = save(tmp_path / "publication/paper.json", {"candidate_inputs": [{"file": str(candidate)}]})
    panel = save(tmp_path / "panel/manifest.json", {})
    calls = []
    monkeypatch.setattr(forward, "run", lambda *a, **kw: calls.append(True))
    with pytest.raises(ValueError, match="all declared source"):
        forward.main(["--publication", str(publication), "--panel", str(panel),
            "--ipo-evidence", profile["ipo_evidence"], "--research-master-run", profile["research_master_run"],
            "--registration", profile["paper_registration"]["file"], "--run-dir", profile["run_directory"],
            "--result-json", str(raw.parent / "must_not_write.json")])
    assert not calls and not (raw.parent / "must_not_write.json").exists()


def test_bootstrap_reads_existing_registration_without_creating_books_and_rejects_late_attach(tmp_path, monkeypatch):
    from trading.research import leader_dated_catchup_paper as paper
    profile, _ = inputs(tmp_path)
    catalog_file = Path(profile["catalog"]["file"])
    save(catalog_file, {"schema": "leader_dated_identity_catalog_v1", "catalog_file": str(catalog_file),
        "entries": [{"source_day": "2026-10-16"}],
        "anchor": {"calendar": profile["calendar"]["file"], "registry_file": profile["registry"]}})
    save(catalog_file.parent / "receipt.json", {"catalog_sha256": daily._ref(catalog_file)["sha256"],
        "persisted_at": "2026-10-17T00:10:00+08:00"})
    candidate_file = Path(profile["candidate_registration"]["file"])
    candidate = json.loads(candidate_file.read_text())
    candidate["catalog_anchor"] = daily._ref(catalog_file)
    save(candidate_file, candidate)
    registered = {"candidate_registration": daily._ref(candidate_file),
        "first_decision_day": "2026-10-19", "first_execution_day": "2026-10-20", "case_ids": profile["case_ids"]}
    monkeypatch.setattr(paper, "_registered", lambda path, now: (registered, daily._ref(path)))
    class Clock(datetime):
        value = "2026-10-18T12:00:00+08:00"
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(cls.value)
    monkeypatch.setattr(daily, "datetime", Clock)
    kwargs = {"paper_registration": profile["paper_registration"]["file"],
        "ipo_evidence": profile["ipo_evidence"], "research_master_run": profile["research_master_run"],
        "run_dir": profile["run_directory"]}
    result = daily.bootstrap_profile(**kwargs)
    assert result["last_source_day"] == "2026-10-16" and result["publications"] == []
    assert result["candidate_inputs"] == [] and not Path(result["run_directory"]).exists()
    assert str(Path(profile["run_directory"]).resolve()) in daily.input_directories(result)
    Clock.value = "2026-10-19T15:00:00+08:00"
    with pytest.raises(ValueError, match="precede the original"):
        daily.bootstrap_profile(**kwargs)


def test_recipe_catalog_uses_actual_project_launcher_and_existing_cli_contract(tmp_path, monkeypatch, capsys):
    import run_committee
    from trading.research import leader_dated_identity_catalog as catalog
    profile, source = inputs(tmp_path)
    panel, analysis = source("2026-10-19")
    plan = daily.next_plan(profile, "2026-10-19", panel, analysis, tmp_path / "operation")
    command = next(s["command"] for s in plan["steps"] if s["id"] == "catchup_catalog")
    observed = []
    def build(registry, analyses, out, *, parent=None):
        observed.append((registry, analyses, out, parent))
        return {"catalog_file": str(Path(out) / "catalog.json"),
            "entries": [{"source_day": "2026-10-19"}], "anchor": {"member_count": 590}}
    monkeypatch.setattr(catalog, "build_catalog", build)
    assert run_committee.main(command) == 0
    assert observed == [(profile["registry"], [str(analysis)],
        str(tmp_path / "operation/artifacts/catalog"), profile["catalog"]["file"])]
    assert json.loads(capsys.readouterr().out)["source_days"] == ["2026-10-19"]
    assert not (tmp_path / "operation").exists()
