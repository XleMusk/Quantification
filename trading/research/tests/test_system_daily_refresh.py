"""Daily wiring and no-repeat contracts; no providers, models or Engine runs."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import daily_refresh_recipe as recipe
from trading.research import native_shortline_daily as native
from trading.research import system_daily_refresh as m


D, E, F = "2026-10-16", "2026-10-19", "2026-10-20"


def write(path, value):
    m._atomic(path, value)
    return m._ref(path)


def saved_system(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(m.opening_daily, "DATA_ROOT", data)
    monkeypatch.setattr(m, "_lock", lambda path: nullcontext())
    calendar = data / "calendar" / "calendar.json"
    calendar_ref = write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    package = {}
    for day in (D, E, F):
        package[day] = data / "prices" / day
        write(package[day] / "plan.json", {"day": day})
    checkpoint = data / "native_input" / "checkpoint.json"
    checkpoint_ref = write(checkpoint, {"synthetic_checkpoint": True})
    ipo = data / "ipo" / "bundle.json"
    master = data / "master" / "run.json"
    model = data / "model" / "manifest.json"
    for path in (ipo, master, model):
        write(path, {"synthetic_input": True})
    templates, plans = {}, {}
    for key in ("v1", "qualified"):
        run = data / "accounts" / key
        write(run / "head.json", {"cases": {kind: {"state": {"day": D}} for kind in ("lr", "lgbm")}})
        plan = data / "plans" / key / "plan.json"
        write(plan, {"source_day": D, "execution_day": E})
        plans[key] = str(plan)
        argv = ["shortline-native-preopen-forward", "--run", str(run), "--plan", str(plan),
            "--raw-close", str(package[D]), "--through", D,
            "--result-json", str(data / "old_results" / (key + ".json"))]
        templates[key] = {"argv": argv, "run_dir": str(run), "plan_prefix": [str(plan)],
            "raw_close_prefix": [str(package[D])], "result_path_must_be_replaced_before_call": True}
    profile = {"schema": recipe.SCHEMA, "last_source_day": D, "source_days": [D],
        "calendar": {"path": calendar_ref["file"], "sha256": calendar_ref["sha256"]},
        "templates": {}, "output_paths": {}, **recipe.FLAGS}
    native_profile = {"schema": "native_shortline_daily_profile_v1", "method_id": native.METHOD_ID,
        "source_day": D, "checkpoint": str(checkpoint), "checkpoint_ref": checkpoint_ref,
        "base_view": str(data / "native_base"), "extension_views": [],
        "full_closes": [str(package[D])], "calendar": str(calendar), "model": str(model),
        "ipo_evidence": str(ipo), "master_run": str(master), "latest_plans": plans,
        "forward_templates": templates, "chain_has_missing_plan": False,
        "missing_plan_days": [], **native.FLAGS}
    lowvol = {"run_directory": str(data / "accounts" / "lowvol"),
        "source_directory": str(data / "sources" / "lowvol"), "publications": [],
        "raw_closes": [str(package[D])], "action_event_view": str(data / "action_events"),
        "action_reference_view": str(data / "action_refs")}
    directory = data / "system_refresh"
    registration = {"schema": m.SCHEMA, "run_directory": str(directory.resolve()),
        "calendar": calendar_ref, "opening_run_directory": str(data / "opening_coordinator"),
        "initial_profile": profile, "initial_native_profile": native_profile, "lowvol": lowvol,
        "original_routes": [], **m.FLAGS}
    registration_ref = write(directory / "registration.json", registration)
    head = {"schema": m.SCHEMA, "registration": registration_ref, "profile": profile,
        "native_profile": native_profile, "lowvol": lowvol, "last_source_day": D,
        "operations": [], **m.FLAGS}
    write(directory / "head.json", head)

    def source(path, calendar_ref, sessions, now):
        return {"proof": {"day": m._read(Path(path) / "plan.json")["day"],
            "source_folder": str(Path(path).resolve()), "synthetic_complete_close": True}}
    monkeypatch.setattr(m, "_source", source)
    monkeypatch.setattr(m, "_lowvol", lambda head, day, package, operation, **options:
        ({"status": "saved_exact_source_plan_reused"}, deepcopy(head["lowvol"])))
    openings = []

    def opening(head, registration, operation):
        day = head["native_profile"]["source_day"]
        openings.append(day)
        return {"status": "current_saved_opening_products", "source_issues": [], "routes": [
            {"id": route_id, "status": "current_saved_product", "saved_product": {"source_day": day}}
            for route_id in ("fixed_lowvol", "eight_type", "leader20", "alpha158",
                             "native_preopen", "native_ipoqualified", "native_lowvol")]}
    monkeypatch.setattr(m, "_opening", opening)
    return SimpleNamespace(directory=directory, package=package, head=head, openings=openings)


def test_same_exact_day_reuses_all_existing_routes_without_any_producer_or_forward(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("The already saved exact source day must not rerun a producer or forward")
    monkeypatch.setattr(recipe, "next_plan", forbidden)
    monkeypatch.setattr(native, "run_profile", forbidden)
    monkeypatch.setattr(m, "_run_command", forbidden)
    monkeypatch.setattr(m.subprocess, "Popen", forbidden)
    for _ in range(2):
        result = m.run(saved.directory, D, saved.package[D])
        assert result["status"] == "completed_saved_research_refresh"
        assert result["core_producers"] == "saved_exact_source_day_reused_without_reexecution"
        assert result["native_production"]["status"] == "saved_exact_source_day_reused_without_rescoring"
        assert all(result["native_forward_" + key]["status"] == "exact_execution_day_already_committed"
                   for key in ("v1", "qualified"))
    assert saved.openings == [D, D]


def test_started_or_failed_command_receipt_never_launches_another_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("No retry child"))
    for status in ("started", "failed_no_automatic_retry"):
        directory = tmp_path / status
        step = {"id": "environment", "command": ["leader-environment", "--out", str(tmp_path / "artifact")],
            "expected_files": [str(tmp_path / "uncreated.json")]}
        receipt = {"command": step["command"], "status": status, "automatic_retry": False}
        write(directory / "environment.json", receipt)
        before = (directory / "environment.json").read_bytes()
        assert m._run_command(step, directory, E) == receipt
        assert (directory / "environment.json").read_bytes() == before


def test_exit_zero_native_forward_with_old_head_is_failed_and_not_retried(tmp_path, monkeypatch):
    account = tmp_path / "native_account" / "head.json"
    write(account, {"cases": {kind: {"state": {"day": D}} for kind in ("lr", "lgbm")}})
    calls = []
    def popen(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(pid=12345, wait=lambda: 0)
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    step = {"id": "native_forward_v1", "command": ["shortline-native-preopen-forward", "--run", str(account.parent)],
        "expected_files": [str(account)]}
    result = m._run_command(step, tmp_path / "commands", E)
    assert result["status"] == "failed_or_interrupted_no_automatic_retry" and result["exit_code"] == 0
    assert "exact requested" in result["error"]
    assert m._run_command(step, tmp_path / "commands", E) == result and len(calls) == 1


def test_state_only_can_execute_prior_plan_at_current_open_but_cannot_skip_missing_next_plan(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    productions, forwards = [], []

    def next_plan(profile, day, package, out):
        return {"steps": [], "next_profile": {**deepcopy(profile), "last_source_day": day,
            "source_days": [*profile["source_days"], day]}}

    def state_only(profile, day, package, out):
        productions.append(day)
        updated = deepcopy(profile)
        updated.update(source_day=day, full_closes=[*profile["full_closes"], str(package)],
            chain_has_missing_plan=True, missing_plan_days=[*profile["missing_plan_days"], day])
        # Exactly the producer wrapper's real state-only shape: the old plans
        # and forward argv remain unchanged; the top-level runner adds E raw.
        return {"result": {"status": "saved_state_without_scorable_input_no_plan",
            "no_change_manufactured": False, "empty_target_manufactured": False}, "next_profile": updated}

    def forward(step, directory, day):
        assert day == E
        command = step["command"]
        assert str(saved.package[E]) in m._values(command, "--raw-close")
        assert m._values(command, "--plan") == m._values(
            saved.head["native_profile"]["forward_templates"][step["id"].removeprefix("native_forward_")]["argv"], "--plan")
        forwards.append((step["id"], day))
        write(Path(step["expected_files"][0]), {"cases": {kind: {"state": {"day": day}} for kind in ("lr", "lgbm")}})
        return {"status": "completed", "command": command, "exit_code": 0}

    monkeypatch.setattr(recipe, "next_plan", next_plan)
    monkeypatch.setattr(native, "run_profile", state_only)
    monkeypatch.setattr(m, "_run_command", forward)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("No real child in wiring test"))
    first = m.run(saved.directory, E, saved.package[E])
    assert first["status"] == "partial_research_routes"
    assert len(forwards) == 2 and productions == [E]
    assert first["native_production"]["status"] == "saved_state_without_scorable_input_no_plan"
    repeated = m.run(saved.directory, E, saved.package[E])
    assert repeated["status"] == "partial_research_routes" and len(forwards) == 2 and productions == [E]
    later = m.run(saved.directory, F, saved.package[F])
    assert later["status"] == "partial_research_routes" and len(forwards) == 2
    assert productions == [E, F]
    for key in ("v1", "qualified"):
        assert later["native_forward_" + key]["status"] == "blocked_by_missing_original_source_plan"
        account = Path(saved.head["native_profile"]["forward_templates"][key]["run_dir"]) / "head.json"
        assert all(case["state"]["day"] == E for case in m._read(account)["cases"].values())
