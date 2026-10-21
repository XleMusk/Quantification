"""Independent Alpha producer wiring using saved synthetic evidence, never Qlib."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import recent_alpha158_daily as alpha
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import saved_system, write


P, D, E, F = "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"
CHECK_FLAGS = {k: False for k in ("model_refit", "model_trained", "future_labels_used_for_score",
    "execution_admitted", "selection_enabled", "strategy_validated")}


def clock(monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(alpha, "datetime", Clock)


def source(folder):
    folder = Path(folder).resolve()
    return {"day": daily._read(folder / "plan.json")["day"], "source_folder": str(folder),
        "raw_sha256": "raw_" + folder.name, "roster_raw_sha256": "roster_" + folder.name}


def source_rows(packages, calendar):
    return [{"day": source(p)["day"], "source_folder": str(Path(p).resolve()),
        "raw_daily_sha256": source(p)["raw_sha256"], "roster_raw_sha256": source(p)["roster_raw_sha256"],
        "calendar_sha256": calendar["sha256"], "source_completed_at": source(p)["day"] + "T15:10:00+08:00"}
        for p in packages]


def provider(folder, packages, calendar, recorded):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "raw.parquet"
    raw.write_bytes(b"synthetic saved raw proof, not market data")
    return write(folder / "result.json", {"schema": "a_share_recent_complete_source_research_provider_v1",
        "status": "research_provider_completed", "provider_method_id": alpha.PROVIDER_METHOD,
        "created_at": recorded, "recent_source_days": [source(p)["day"] for p in packages],
        "source_proofs": source_rows(packages, calendar), "calendar_sha256": calendar["sha256"],
        "recent_raw_observations_file": str(raw), "recent_raw_observations_sha256": daily._ref(raw)["sha256"],
        "recent_source_inventory_complete": True, "raw_prices_imputed": False,
        "recent_raw_original_fields_unchanged": True, **CHECK_FLAGS})


def batch(folder, provider_ref, dates, model_file, recorded):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    provider_record = daily._read(provider_ref["file"])
    scores = folder / "dated_scores.parquet"
    scores.write_bytes(b"synthetic saved scores proof, not a fitted model")
    return write(folder / "result.json", {"schema": "a_share_recent_source_alpha158_frozen_score_batch_v1",
        "status": "completed_research_only_dated_score_batch", "method_id": alpha.SCORE_METHOD,
        "provider_view": str(Path(provider_ref["file"]).parent), "provider_result_sha256": provider_ref["sha256"],
        "source_proofs": provider_record["source_proofs"], "actual_computed_at": recorded,
        "score_dates": dates, "score_path": str(scores), "model_run": str(model_file),
        "model_run_sha256": daily._ref(model_file)["sha256"],
        "raw_observations": provider_record["recent_raw_observations_file"],
        "raw_observations_sha256": provider_record["recent_raw_observations_sha256"], **CHECK_FLAGS})


def model_contract(score):
    ref = daily._ref(score["model_run"])
    if ref["sha256"] != score["model_run_sha256"]:
        raise ValueError("Synthetic fixed model changed")
    return {"fixed_model": ref}


def publication(folder, batch_ref, prefixes, day, execution, recorded, calendar):
    score = daily._read(batch_ref["file"])
    current_provider = daily._read(Path(score["provider_view"]) / "result.json")
    return write(Path(folder) / "result.json", {"schema": "recent_alpha158_dated_target_publication_v1",
        "status": "close_research_target_publication_only", "source_close_day": day,
        "proposed_execution_day": execution, "recorded_at": recorded, "execution_binding_pending": False,
        "publication_time_backfilled": False, "execution_day_rolled_forward": False,
        "case_count": 2, "candidate_panel_only": True, "frozen_model_contract": model_contract(score),
        "cases": [{"case": item["id"]} for item in alpha.PROTOCOL["cases"]],
        "protocol": deepcopy(alpha.PROTOCOL), "config": alpha._config(100000,5).to_dict(),
        "case_selection_performed": False,
        "score_computed_at": score["actual_computed_at"], "provider_ready_at": current_provider["created_at"],
        "orders_generated": False, "account_created": False, "persistent_account_used": False,
        "engine_ledger_replay_completed": False, "source": {"batch_run": batch_ref["file"],
            "batch_sha256": batch_ref["sha256"], "source_proofs": current_provider["source_proofs"],
            "score_sha256": daily._ref(score["score_path"])["sha256"],
            "provider_result_sha256": score["provider_result_sha256"],
            "score_computed_at": score["actual_computed_at"], "source_available_at": current_provider["created_at"]},
        "capacity_prefix": {"old_scores_recomputed": False, "capacity_reset": False,
            "score_dates": current_provider["recent_source_days"][:-1],
            "batches": [{"batch_run": r["file"], "batch_sha256": r["sha256"],
                "score_sha256": daily._ref(daily._read(r["file"])["score_path"])["sha256"]} for r in prefixes]},
        **CHECK_FLAGS})


def wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    packages = dict(saved.package)
    packages[F] = saved.directory.parent / "prices" / F
    for day, folder in packages.items():
        write(folder / "plan.json", {"day": day})
        write(folder / "receipt.json", {"complete": True})
    head = daily._read(saved.directory / "head.json")
    registration = daily._read(saved.directory / "registration.json")
    calendar = registration["calendar"]
    model = saved.directory.parent / "alpha_model/run.json"
    history = saved.directory.parent / "alpha_history"
    ipo = saved.directory.parent / "alpha_ipo/evidence.json"
    master = saved.directory.parent / "alpha_master/run.json"
    for file in (model, history / "manifest.json", ipo, master):
        write(file, {"frozen_synthetic_input": str(file)})
    old_provider = provider(saved.directory.parent / "old_provider", [packages[P]], calendar, P + "T18:00:00+08:00")
    old_batch = batch(saved.directory.parent / "old_score", old_provider, [P], model, P + "T18:01:00+08:00")
    current_provider = provider(saved.directory / "operations/20261019/artifacts/alpha_provider",
        [packages[P], packages[D]], calendar, D + "T18:00:00+08:00")
    current_batch = batch(saved.directory / "operations/20261019/artifacts/alpha_score", current_provider,
        [D], model, D + "T18:01:00+08:00")
    current_pub = publication(saved.directory / "operations/20261019/artifacts/alpha_publication",
        current_batch, [old_batch], D, E, D + "T18:02:00+08:00", calendar)
    commands = {"alpha_provider": ["recent-source-qlib-provider", "--package", str(packages[P]),
            "--package", str(packages[D]), "--history-snapshot", str(history), "--calendar", calendar["file"],
            "--out", str(Path(current_provider["file"]).parent)],
        "alpha_score": ["recent-source-alpha158", "--provider-view", str(Path(current_provider["file"]).parent),
            "--model-run", str(model), "--start", D, "--end", D, "--out", str(Path(current_batch["file"]).parent)],
        "alpha_publication": ["recent-source-alpha158-target-publication", "--batch-run", current_batch["file"],
            "--prefix-batch-run", old_batch["file"], "--calendar", calendar["file"], "--ipo-evidence", str(ipo),
            "--research-master-run", str(master), "--out", str(Path(current_pub["file"]).parent)]}
    head["profile"].update(last_source_day=D, source_days=[P,D], templates=commands,
        output_paths={"alpha_provider": {"result": current_provider["file"]},
            "alpha_score": {"result": current_batch["file"], "scores": str(Path(current_batch["file"]).parent / "dated_scores.parquet")},
            "alpha_publication": {"result": current_pub["file"]}})
    head["last_source_day"] = D
    registration["initial_profile"] = deepcopy(head["profile"])
    head["registration"] = write(saved.directory / "registration.json", registration)
    write(saved.directory / "head.json", head)
    calls, behavior = [], {"failed_stage": None, "status": "failed_no_automatic_retry", "publication_day": None,
        "stamp": F + "T18:00:00+08:00"}

    def execute(step, commands_folder, day):
        calls.append(deepcopy(step))
        if step["id"] != behavior["failed_stage"]:
            command, out = step["command"], Path(alpha._one(step["command"], "--out"))
            if step["id"] == "alpha_provider":
                provider(out, daily._values(command, "--package"), calendar, behavior["stamp"])
            elif step["id"] == "alpha_score":
                batch(out, daily._ref(Path(alpha._one(command, "--provider-view")) / "result.json"),
                    [alpha._one(command, "--start")], model, behavior["stamp"])
            else:
                publication(out, daily._ref(alpha._one(command, "--batch-run")),
                    [daily._ref(p) for p in daily._values(command, "--prefix-batch-run")],
                    day, "2026-10-22", behavior["publication_day"] or behavior["stamp"], calendar)
            status = "completed"
        else:
            status = behavior["status"]
        record = {"command": step["command"], "status": status, "exit_code": 0 if status == "completed" else 2}
        if status == "completed":
            record["output_refs"] = [daily._ref(p) for p in step["expected_files"]]
        write(Path(commands_folder) / (step["id"] + ".json"), record)
        return record

    def terminal(step, commands_folder, day, receipt):
        # Saved at the synthetic child exit, never regenerated as expectations
        # after a later payload edit.
        outputs = receipt["output_refs"]
        if outputs != [daily._ref(p) for p in step["expected_files"]]:
            raise ValueError("Saved child terminal output bytes changed")
        return None, None, {"exit_code": 0}, outputs

    def forbidden(*args, **kwargs):
        pytest.fail("Independent Alpha producer cannot call accounts, other routes or real children")

    monkeypatch.setattr(alpha, "_model_contract", model_contract)
    monkeypatch.setattr(daily, "_source", lambda folder, *args: {"proof": source(folder)})
    monkeypatch.setattr(daily, "_run_command", execute)
    monkeypatch.setattr(daily, "_terminal", terminal)
    monkeypatch.setattr(daily.subprocess, "Popen", forbidden)
    for name in ("_opening", "_lowvol", "_recent_actions", "_native_forward_refresh"):
        monkeypatch.setattr(daily, name, forbidden)
    clock(monkeypatch, F + "T18:05:00+08:00")
    accounts = [Path(t["run_dir"]) / "head.json" for t in head["native_profile"]["forward_templates"].values()]
    frozen = {p: p.read_bytes() for p in [saved.directory / "registration.json", Path(current_pub["file"]),
        Path(current_batch["file"]), *accounts]}
    return SimpleNamespace(saved=saved, packages=packages, head=head, calls=calls, behavior=behavior,
        frozen=frozen, model=model, batches=[old_batch,current_batch], calendar=calendar)


def old_unchanged(e):
    head = daily._read(e.saved.directory / "head.json")
    assert {k:v for k,v in head.items() if k != alpha.PROFILE_KEY} == e.head
    assert all(p.read_bytes() == contents for p,contents in e.frozen.items())
    return head


def test_20_own_context_then_21_publication_has_complete_fixed_prefix_without_account_advance(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    result = alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert result["status"] == "completed_alpha158_source_context" and not result["signals_published"]
    assert [s["id"] for s in e.calls] == ["alpha_provider", "alpha_score"]
    head = old_unchanged(e)
    profile = head[alpha.PROFILE_KEY]
    assert "score_artifacts" not in profile
    assert profile["source_day"] == E and profile["missing_publication_days"] == [E]
    assert profile["publication_source_day"] == D
    score20 = daily._read(profile["score_batches"][-1]["file"])
    assert daily._read(Path(score20["provider_view"]) / "result.json")["recent_source_days"] == [P,D,E]
    assert score20["score_dates"] == [E]
    assert score20["actual_computed_at"].startswith(F)
    assert daily._read(profile["latest_publication"]["file"])["recorded_at"].startswith(D)
    result = alpha.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "completed_alpha158_publication" and result["signals_published"]
    profile = old_unchanged(e)[alpha.PROFILE_KEY]
    assert profile["source_days"] == [P,D,E,F] and profile["publication_source_day"] == F
    assert profile["missing_publication_days"] == [E]
    assert len(profile["score_batches"]) == 4
    command = e.calls[-1]["command"]
    assert daily._values(command, "--prefix-batch-run") == [r["file"] for r in profile["score_batches"][:-1]]
    assert [s["id"] for s in e.calls] == ["alpha_provider", "alpha_score", "alpha_provider", "alpha_score", "alpha_publication"]


@pytest.mark.parametrize("source_only", [True,False])
def test_own_complete_receipt_recovers_head_interruption_then_reuses_bytes_without_production(tmp_path, monkeypatch, source_only):
    e = wired(tmp_path, monkeypatch)
    if not source_only:
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    day = E if source_only else F
    atomic = daily._atomic
    def interrupted(path, value):
        if Path(path) == e.saved.directory / "head.json":
            raise OSError("synthetic head interruption")
        return atomic(path, value)
    monkeypatch.setattr(daily, "_atomic", interrupted)
    with pytest.raises(OSError, match="head interruption"):
        alpha.advance(e.saved.directory, day, e.packages[day], source_only=source_only)
    operation = e.saved.directory / ("alpha158_context" if source_only else "alpha158_production") / day.replace("-", "")
    evidence = (operation / "receipt.json").read_bytes()
    count = len(e.calls)
    monkeypatch.setattr(daily, "_atomic", atomic)
    recovered = alpha.advance(e.saved.directory, day, e.packages[day], source_only=source_only)
    assert recovered["reused_saved_result"] and not recovered["producer_called"] and len(e.calls) == count
    assert (operation / "receipt.json").read_bytes() == evidence
    old_unchanged(e)
    head = (e.saved.directory / "head.json").read_bytes()
    alpha.advance(e.saved.directory, day, e.packages[day], source_only=source_only)
    assert (e.saved.directory / "head.json").read_bytes() == head and len(e.calls) == count


@pytest.mark.parametrize("status", ["started", "failed_no_automatic_retry"])
def test_failed_or_pending_stage_has_no_reproduction_or_mode_switch(tmp_path, monkeypatch, status):
    e = wired(tmp_path, monkeypatch)
    e.behavior.update(failed_stage="alpha_score", status=status)
    first = alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert first["status"] == "partial_alpha158_production_no_retry"
    assert alpha.PROFILE_KEY not in old_unchanged(e)
    count = len(e.calls)
    second = alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert second["status"] == first["status"] and len(e.calls) == count
    with pytest.raises(ValueError, match="mode-based retry"):
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=False)


def test_saved_successful_child_terminal_is_adopted_without_running_again(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    e.behavior.update(failed_stage="alpha_score", status="started")
    alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    operation = e.saved.directory / "alpha158_context/20261020"
    score_step = e.calls[-1]
    command = score_step["command"]
    batch(alpha._one(command, "--out"), daily._ref(Path(alpha._one(command, "--provider-view")) / "result.json"),
        [E], e.model, e.behavior["stamp"])
    for file in daily.command_child.paths(operation / "commands", "alpha_score").values():
        write(file, {"synthetic_terminal_fixture": True})
    saved = daily._read(operation / "commands/alpha_score.json")
    saved["output_refs"] = [daily._ref(p) for p in score_step["expected_files"]]
    write(operation / "commands/alpha_score.json", saved)
    before = len(e.calls)
    result = alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert result["status"] == "completed_alpha158_source_context" and len(e.calls) == before
    assert result["steps"]["alpha_score"]["saved_success_adopted_without_production"]


def test_model_and_contiguous_day_drift_reject_before_new_compute(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="skip"):
        alpha.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert not e.calls
    write(e.model, {"changed": True})
    with pytest.raises(ValueError, match="model changed"):
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert not e.calls and alpha.PROFILE_KEY not in old_unchanged(e)


def test_source_only_before_missed_window_and_historical_publication_rejected(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    clock(monkeypatch, E + "T18:05:00+08:00")
    with pytest.raises(ValueError, match="Missed days"):
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    clock(monkeypatch, F + "T18:05:00+08:00")
    with pytest.raises(ValueError, match="real source day"):
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=False)
    assert not e.calls


def test_future_or_rolled_recording_is_not_adopted_as_current_publication(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    e.behavior["publication_day"] = "2026-10-22T00:01:00+08:00"
    result = alpha.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "partial_alpha158_production_no_retry"
    assert old_unchanged(e)[alpha.PROFILE_KEY]["source_day"] == E
    before = len(e.calls)
    alpha.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert len(e.calls) == before


def test_original_parent_operation_claim_prevents_duplicate_independent_producer(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    write(e.saved.directory / "operations/20261020/commands/alpha_score.json", {"status": "failed"})
    with pytest.raises(ValueError, match="original Alpha158 attempt"):
        alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert not e.calls


@pytest.mark.parametrize("status,expected", [
    ("partial_alpha158_production_no_retry", 1), ("completed_alpha158_source_context", 0)])
def test_cli_exit_code_distinguishes_partial_from_complete_without_a_producer(tmp_path, monkeypatch, status, expected):
    calls = []
    def saved_result(run_dir, day, package, *, source_only):
        calls.append((day, source_only))
        return {"status": status}
    monkeypatch.setattr(alpha, "advance", saved_result)
    assert daily.main(["advance-alpha158", "--run-dir", str(tmp_path / "run"),
        "--day", E, "--package", str(tmp_path / "source"), "--source-only"]) == expected
    assert calls == [(E, True)]


def test_unpublished_context_score_bytes_cannot_drift_before_next_publication(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    alpha.advance(e.saved.directory, E, e.packages[E], source_only=True)
    head = daily._read(e.saved.directory / "head.json")
    assert "score_artifacts" not in head[alpha.PROFILE_KEY]
    score = daily._read(head[alpha.PROFILE_KEY]["score_batches"][-1]["file"])
    scores = Path(score["score_path"])
    scores.write_bytes(b"changed unpublished score payload without changing its result.json")
    before = len(e.calls)
    with pytest.raises(ValueError, match="terminal output bytes"):
        alpha.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert len(e.calls) == before
