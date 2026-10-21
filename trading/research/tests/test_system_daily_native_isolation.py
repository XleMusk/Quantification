"""Route isolation and exact saved recovery, without market/model production."""
from copy import deepcopy
from pathlib import Path
from subprocess import Popen as _REAL_POPEN
from types import SimpleNamespace

import pytest

from trading.research import daily_refresh_recipe as recipe
from trading.research import native_shortline_daily as native
from trading.research import system_daily_refresh as m
from trading.research.tests.test_system_daily_refresh import D, E, F, saved_system, write
from trading.research.tests.test_system_daily_command_recovery import _small_real_child, _unclaimed_parent


def _wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    state = SimpleNamespace(saved=saved, productions=[], executions=[], lowvol=[], openings=[],
        fail_production=set(), fail_forward=set(), real_v1=None)
    real_run_command = m._run_command

    def core_plan(profile, day, package, out):
        assert profile["last_source_day"] == (D if day == E else E)
        output = out / "core/result.json"
        return {"schema": recipe.PLAN_SCHEMA, "source_day": day,
            "execution_day": F if day == E else "2026-10-21", "package": str(package.resolve()),
            "out": str(out), "steps": [{"id": "environment", "command": ["leader-environment", "--out", str(output.parent)],
                "expected_files": [str(output)], "requires": []}],
            "next_profile": {**deepcopy(profile), "last_source_day": day,
                "source_days": [*profile["source_days"], day]}, **recipe.FLAGS}

    def next_native(profile, day, package, out):
        state.productions.append(day)
        if day in state.fail_production:
            raise ValueError("synthetic native feature source unavailable")
        assert profile["source_day"] == (D if day == E else E)
        updated = deepcopy(profile)
        checkpoint = out / "checkpoint.json"
        checkpoint_ref = write(checkpoint, {"day": day, "synthetic_only": True})
        updated.update(source_day=day, full_closes=[*profile["full_closes"], str(package.resolve())],
            checkpoint=str(checkpoint), checkpoint_ref=checkpoint_ref, last_operation=str(out / "receipt.json"))
        for key, template in updated["forward_templates"].items():
            plan = out / key / "plan.json"
            write(plan, {"source_day": day, "execution_day": F if day == E else "2026-10-21"})
            updated["latest_plans"][key] = str(plan)
            template["plan_prefix"].append(str(plan))
            template["raw_close_prefix"].append(str(package.resolve()))
            template["argv"] = m._set(template["argv"], "--plan", template["plan_prefix"])
            template["argv"] = m._set(template["argv"], "--raw-close", template["raw_close_prefix"])
            template["argv"] = m._set(template["argv"], "--through", [day])
        result = {"status": "completed_saved_native_daily_features_scores_two_policies", "source_day": day,
            "latest_checkpoint": str(checkpoint), "synthetic_only": True}
        write(out / "receipt.json", result)
        return {"result": {**result, "operation": str(out / "receipt.json")}, "next_profile": updated}

    def execute(step, directory, day):
        state.executions.append((step["id"], day))
        if state.real_v1 is not None and step["id"] == "native_forward_v1" and day == E:
            return state.real_v1(step, directory, day, real_run_command)
        if (step["id"], day) in state.fail_forward:
            raise RuntimeError("synthetic one-policy failure before a terminal receipt")
        for path in step["expected_files"]:
            payload = ({"cases": {kind: {"state": {"day": day}} for kind in ("lr", "lgbm")}}
                       if step["id"].startswith("native_forward_") else {"day": day, "synthetic_only": True})
            write(Path(path), payload)
        return {"status": "completed", "command": step["command"], "exit_code": 0}

    def lowvol(head, day, package, operation, **options):
        value = deepcopy(head["lowvol"])
        if value.get("last_processed_day") != day:
            state.lowvol.append(day)
            value["last_processed_day"] = day
            write(Path(value["run_directory"]) / "head.json", {"day": day, "synthetic_only": True})
        return {"status": "saved_exact_source_plan_reused"}, value

    def opening(head, registration, operation):
        day = head["profile"]["last_source_day"]
        state.openings.append(day)
        return {"status": "current_saved_opening_products", "source_issues": [], "routes": [
            {"id": "eight_type", "status": "current_saved_product", "saved_product": {"source_day": day}},
            {"id": "alpha158", "status": "current_saved_product", "saved_product": {"source_day": day}}]}

    monkeypatch.setattr(recipe, "next_plan", core_plan)
    monkeypatch.setattr(native, "run_profile", next_native)
    monkeypatch.setattr(m, "_run_command", execute)
    monkeypatch.setattr(m, "_lowvol", lowvol)
    monkeypatch.setattr(m, "_opening", opening)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: pytest.fail("No market producer may run"))
    state.next_native = next_native
    return state


def _head(state):
    return m._read(state.saved.directory / "head.json")


def _account_day(state, key):
    path = Path(state.saved.head["native_profile"]["forward_templates"][key]["run_dir"]) / "head.json"
    return {case["state"]["day"] for case in m._read(path)["cases"].values()}


def test_native_producer_failure_keeps_other_routes_durable_and_does_not_skip_native_next_day(tmp_path, monkeypatch):
    state = _wired(tmp_path, monkeypatch)
    registration = (state.saved.directory / "registration.json").read_bytes()
    state.fail_production.add(E)
    first = m.run(state.saved.directory, E, state.saved.package[E])
    assert first["status"] == "partial_research_routes"
    assert first["native_production"]["status"] == "failed_native_production_no_automatic_retry"
    assert _head(state)["last_source_day"] == _head(state)["profile"]["last_source_day"] == E
    assert _head(state)["native_profile"]["source_day"] == D
    assert _head(state)["lowvol"]["last_processed_day"] == E
    assert state.lowvol == [E] and state.openings == [E]
    assert _account_day(state, "v1") == _account_day(state, "qualified") == {E}
    original_attempt = deepcopy(first["native_production_attempt"])
    m.run(state.saved.directory, E, state.saved.package[E])
    assert state.productions == [E] and state.lowvol == [E]
    assert m._read(state.saved.directory / "operations/20261019/receipt.json")["native_production_attempt"] == original_attempt
    later = m.run(state.saved.directory, F, state.saved.package[F])
    assert later["native_production"]["status"] == "blocked_by_missing_exact_native_source_day"
    assert state.productions == [E]  # No F call may silently bridge missing E.
    assert _head(state)["last_source_day"] == _head(state)["profile"]["last_source_day"] == F
    assert _head(state)["native_profile"]["source_day"] == D
    assert state.lowvol == [E, F] and state.openings[-1] == F
    assert all(later["native_forward_" + key]["status"] == "blocked_by_missing_original_source_plan"
               for key in ("v1", "qualified"))
    assert _account_day(state, "v1") == _account_day(state, "qualified") == {E}
    assert (state.saved.directory / "registration.json").read_bytes() == registration


def test_one_policy_failure_is_not_retried_or_bridged_while_other_policy_and_core_continue(tmp_path, monkeypatch):
    state = _wired(tmp_path, monkeypatch)
    state.fail_forward.add(("native_forward_v1", E))
    first = m.run(state.saved.directory, E, state.saved.package[E])
    assert first["native_forward_v1"]["status"] == "failed_native_forward_no_automatic_retry"
    assert first["native_forward_qualified"]["status"] == "completed"
    assert _account_day(state, "v1") == {D} and _account_day(state, "qualified") == {E}
    m.run(state.saved.directory, E, state.saved.package[E])
    assert state.executions.count(("native_forward_v1", E)) == 1
    later = m.run(state.saved.directory, F, state.saved.package[F])
    assert later["native_forward_v1"]["status"] == "blocked_by_unresolved_prior_native_forward"
    assert later["native_forward_v1"]["blocking_day"] == E
    assert later["native_forward_qualified"]["status"] == "completed"
    assert state.productions == [E, F] and state.lowvol == [E, F] and state.openings[-1] == F
    assert _account_day(state, "v1") == {D} and _account_day(state, "qualified") == {F}
    assert ("native_forward_v1", F) not in state.executions
    assert _head(state)["native_forward_progress"]["v1"]["day"] == E


def test_late_exact_successful_child_recovery_unlocks_only_original_policy_without_another_child(tmp_path, monkeypatch):
    state = _wired(tmp_path, monkeypatch)
    # Only the supervisor helper's tiny synthetic Python child runs. It writes
    # an account-shaped fixture, never imports a market/model/Engine producer.
    initial = state.saved.head["native_profile"]
    operation = state.saved.directory / "operations/20261019"
    updated = state.next_native(initial, E, state.saved.package[E], operation / "native")
    state.productions.clear()
    monkeypatch.setattr(native, "run_profile", lambda *a, **k: deepcopy(updated))
    profile = updated["next_profile"]
    step = m._native_forward_step(profile, "v1", E, state.saved.package[E], operation)
    account = Path(step["expected_files"][0])
    monkeypatch.setattr(m.subprocess, "Popen", _REAL_POPEN)
    marker = _small_real_child(monkeypatch, account,
        payload={"cases": {kind: {"state": {"day": E}} for kind in ("lr", "lgbm")}},
        logical_command=step["command"])
    saved_attempt = {}

    def actual_saved_child(spec, directory, day, real_run):
        result = real_run(spec, directory, day)
        if not saved_attempt:
            assert result["status"] == "completed"
            path, original = _unclaimed_parent(operation, spec)
            saved_attempt.update(path=path, original=original)
        return m._read(saved_attempt["path"])

    state.real_v1 = actual_saved_child
    first = m.run(state.saved.directory, E, state.saved.package[E])
    assert first["native_forward_v1"]["status"] == "started"
    monkeypatch.setattr(native, "run_profile", state.next_native)
    later = m.run(state.saved.directory, F, state.saved.package[F])
    assert later["native_forward_v1"]["status"] == "blocked_by_unresolved_prior_native_forward"
    assert _head(state)["last_source_day"] == F
    recovered = m.recover_step(state.saved.directory, E, "native_forward_v1")
    assert recovered["status"] == "recovered_saved_command"
    # Let the original supervisor expose its exact recovery, not the stale
    # parent status returned above to simulate the interrupted first caller.
    state.real_v1 = lambda spec, directory, day, real_run: real_run(spec, directory, day)
    final = m.run(state.saved.directory, F, state.saved.package[F])
    assert final["native_forward_v1"]["status"] == "completed"
    assert _account_day(state, "v1") == _account_day(state, "qualified") == {F}
    assert marker.read_text(encoding="utf-8").splitlines() == ["called"]
    assert saved_attempt["path"].read_bytes() == saved_attempt["original"]


def test_native_profile_restores_only_exact_success_receipt_without_reproducing(tmp_path, monkeypatch):
    state = _wired(tmp_path, monkeypatch)
    m.run(state.saved.directory, E, state.saved.package[E])
    saved = _head(state)
    saved["native_profile"] = deepcopy(state.saved.head["native_profile"])
    write(state.saved.directory / "head.json", saved)
    monkeypatch.setattr(native, "run_profile", lambda *a, **k: pytest.fail("Saved success must not re-produce"))
    recovered = m.run(state.saved.directory, E, state.saved.package[E])
    assert recovered["native_production"]["status"] == "completed_saved_native_daily_features_scores_two_policies"
    assert _head(state)["native_profile"]["source_day"] == E and state.productions == [E]
    saved = _head(state)
    saved["native_profile"] = deepcopy(state.saved.head["native_profile"])
    write(state.saved.directory / "head.json", saved)
    producer = state.saved.directory / "operations/20261019/native/receipt.json"
    producer.write_bytes(producer.read_bytes() + b" ")
    refused = m.run(state.saved.directory, E, state.saved.package[E])
    assert refused["native_production_observation_error"]["status"] == "native_production_evidence_unavailable_no_retry"
    assert _head(state)["native_profile"]["source_day"] == D
    assert _head(state)["profile"]["last_source_day"] == _head(state)["lowvol"]["last_processed_day"] == E
    assert state.productions == [E]
