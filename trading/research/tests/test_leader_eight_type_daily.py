"""Independent eight-type producer contracts; synthetic files, no Engine."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import leader_eight_type_daily as eight
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import saved_system, write

D, E, F = "2026-10-19", "2026-10-20", "2026-10-21"


def clock(monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(eight, "datetime", Clock)


def manifest(folder, days, packages):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "raw_daily.parquet"
    raw.write_text("synthetic_raw_prefix_" + ",".join(days), encoding="utf-8")
    return write(folder / "manifest.json", {"source_days": days,
        "source_proofs": [{"day": day, "source_folder": str(package)} for day,package in zip(days,packages)],
        "raw_file": str(raw), "raw_sha256": daily._ref(raw)["sha256"]})


def publication(file, source, day, execution, *, late=False):
    value = {"schema": "leader_eight_type_unbound_close_paper_plan_v1",
        "status": "late_context_only" if late else "unbound_current_close_paper_research",
        "source_close_day": day, "proposed_execution_day": execution, "execution_day_bound": execution,
        "recorded_at": day + "T18:00:00+08:00", "case_count": 32, "candidate_panel_only": True,
        "source": {"input_manifest": source["file"], "input_manifest_sha256": source["sha256"],
            "input_recorded_at": day + "T17:59:00+08:00"},
        **{k:False for k in ("execution_admitted", "selection_enabled", "strategy_validated", "engine_advanced",
            "orders_generated", "account_created", "publication_time_backfilled", "execution_day_rolled_forward")}}
    return write(file, value)


def wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    packages = dict(saved.package)
    packages[F] = saved.directory.parent / "prices" / F
    for day, folder in packages.items():
        write(folder / "plan.json", {"day": day}); write(folder / "receipt.json", {"day": day})
    head, registration = daily._load(saved.directory)
    registry = saved.directory.parent / "identities/registry.json"
    write(registry, {"immutable_members": ["000001"]})
    old_manifest = manifest(saved.directory.parent / "old_inputs", [D], [packages[D]])
    old_pub = publication(saved.directory.parent / "old_paper/paper_plan.json", old_manifest, D, E)
    commands = {
        "extras": ["baostock-bulk-extras", "--package", str(packages[D]), "--out", "old/extras"],
        "preview": ["leader-recent-source-preview", "--package", str(packages[D]),
            "--calendar", registration["calendar"]["file"], "--out", "old/preview"],
        "environment": ["leader-environment", "--preview", "old/preview/20261019", "--bulk-extras", "old/extras/result.json", "--out", "old/environment"],
        "observe": ["leader-identity", "observe", "--registry", str(registry), "--analysis", "old/environment/result.json"],
        "eight_inputs": ["leader-eight-type-inputs", "--append-from", old_manifest["file"],
            "--preview", "old/preview/20261019", "--turnover-source", "old/extras/result.json", "--out", "old/inputs"],
        "paper32": ["leader-eight-type-paper", "--panel", old_manifest["file"], "--out", "old/paper"]}
    head["profile"].update(last_source_day=D, source_days=[D], templates=commands,
        output_paths={"eight_inputs": {"manifest":old_manifest["file"]}, "paper32":{"publication":old_pub["file"]}})
    head["last_source_day"] = D
    registration["initial_profile"] = deepcopy(head["profile"])
    head["registration"] = write(saved.directory / "registration.json", registration)
    write(saved.directory / "head.json", head)
    calls, behavior = [], {"fail": None, "late":False}
    def execute(step, directory, day):
        calls.append((day, step["id"]))
        state = "failed_no_automatic_retry" if behavior["fail"] == step["id"] else "completed"
        if state == "completed":
            file = Path(step["expected_files"][0])
            if step["id"] == "eight_inputs":
                parent = daily._read(eight._one(step["command"], "--append-from"))
                parent_packages = [r["source_folder"] for r in parent["source_proofs"]]
                manifest(file.parent, [*parent["source_days"],day], [*parent_packages,str(packages[day])])
            elif step["id"] == "paper32":
                publication(file, daily._ref(eight._one(step["command"], "--panel")), day,
                    {E:F,F:"2026-10-22"}[day], late=behavior["late"])
            else:
                for output in step["expected_files"]:
                    write(output, {"day": day, "stage": step["id"]})
        result = {"command": step["command"], "status":state}
        write(Path(directory) / (step["id"] + ".json"), result)
        return result
    monkeypatch.setattr(daily, "_run_command", execute)
    monkeypatch.setattr(daily, "_terminal", lambda *args: (None, None, {"exit_code":0}, []))
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("No real child/account"))
    clock(monkeypatch, F + "T13:00:00+08:00")
    return SimpleNamespace(saved=saved, packages=packages, head=head, calls=calls, behavior=behavior)


def test_context20_then_actual21_keeps_gap_and_original_accounts(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    context = eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert context["status"] == "completed_eight32_source_context" and not context["signals_published"]
    assert e.calls == [(E,k) for k in eight.STAGES[:-1]]
    clock(monkeypatch, F + "T18:45:00+08:00")
    result = eight.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "completed_eight32_publication" and result["signals_published"]
    h = daily._read(e.saved.directory / "head.json")
    assert {k:v for k,v in h.items() if k != eight.PROFILE_KEY} == e.head
    p = h[eight.PROFILE_KEY]
    assert p["source_days"] == [D,E,F] and p["missing_publication_days"] == [E]
    assert p["publication_source_day"] == F
    assert daily._values(p["commands"]["preview"], "--package") == [str(e.packages[d]) for d in [D,E,F]]
    assert not any("forward" in k for _,k in e.calls)
    for k in eight.STAGES:
        assert eight._static(k,p["commands"][k]) == eight._static(k,e.head["profile"]["templates"][k])


def test_changed_source_or_skipped_day_never_runs_and_missing_pub_not_backfilled(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    clock(monkeypatch, F + "T18:45:00+08:00")
    with pytest.raises(ValueError, match="change or skip"):
        eight.advance(e.saved.directory, F, e.packages[F], source_only=False)
    clock(monkeypatch, F + "T13:00:00+08:00")
    eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    with pytest.raises(ValueError, match="Missed windows"):
        eight.advance(e.saved.directory, E, e.packages[E], source_only=False)
    before = len(e.calls)
    write(e.packages[D] / "receipt.json", {"changed":True})
    clock(monkeypatch, F + "T18:45:00+08:00")
    with pytest.raises(ValueError, match="input or successful operation changed"):
        eight.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert len(e.calls) == before


def test_failed_or_late_production_does_not_advance_and_is_not_reproduced(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    e.behavior["fail"] = "environment"
    result = eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert result["status"] == "partial_eight32_production_no_retry"
    before = list(e.calls)
    again = eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert not again["signals_published"] and e.calls == before
    assert daily._read(e.saved.directory / "head.json") == e.head


def test_successful_receipt_adopts_interrupted_head_without_reproduction(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    atomic = daily._atomic
    def interrupted(file, value):
        if Path(file) == e.saved.directory / "head.json":
            raise OSError("head interrupted")
        return atomic(file, value)
    monkeypatch.setattr(daily, "_atomic", interrupted)
    with pytest.raises(OSError, match="head interrupted"):
        eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    calls = list(e.calls)
    receipt = e.saved.directory / "eight32_context/20261020/receipt.json"
    original = receipt.read_bytes()
    monkeypatch.setattr(daily, "_atomic", atomic)
    result = eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert result["reused_saved_result"] and not result["producer_called"]
    assert calls == e.calls and receipt.read_bytes() == original
    before = (e.saved.directory / "head.json").read_bytes()
    eight.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert calls == e.calls and (e.saved.directory / "head.json").read_bytes() == before
