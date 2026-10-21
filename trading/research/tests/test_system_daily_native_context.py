"""Native-only orchestration: synthetic files, no providers/models/accounts."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading.research import native_shortline_daily as native
from trading.research import system_daily_refresh as m
from trading.research.tests.test_system_daily_refresh import D, E, F, saved_system, write


def wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    calls, behavior = [], {"fail": False, "bad_continuation": False}

    def produce(profile, day, package, out, *, source_only=False):
        calls.append((day, source_only))
        if behavior["fail"]:
            raise ValueError("synthetic feature failure")
        checkpoint = out / "features/checkpoint.json"
        pin = write(checkpoint, {"day": day})
        result = {"status": "saved_native_source_context_without_publication" if source_only else
            "completed_saved_native_daily_features_scores_two_policies", "source_day": day,
            "latest_checkpoint": str(checkpoint)}
        updated = deepcopy(profile)
        updated.update(source_day=day, full_closes=[*profile["full_closes"], str(package)],
            checkpoint=str(checkpoint), checkpoint_ref=pin, last_operation=str(out / "receipt.json"))
        if source_only:
            result.update(source_only=True, signals_published=False, missing_plan_day=day)
            feature_csv = out / "features/fixture.csv"
            feature_csv.write_text("synthetic-feature\n", encoding="utf-8")
            result["stages"] = {"features": {"status":"completed",
                "artifact":{"refs":[pin, m._ref(feature_csv)]}}}
            updated.update(chain_has_missing_plan=True, missing_plan_days=[*profile["missing_plan_days"], day])
        else:
            for key, template in updated["forward_templates"].items():
                plan = out / key / "plan.json"
                write(plan, {"source_day": day})
                updated["latest_plans"][key] = str(plan)
                template["plan_prefix"].append(str(plan))
                template["raw_close_prefix"].append(str(package))
                template["argv"] = m._set(template["argv"], "--plan", template["plan_prefix"])
                template["argv"] = m._set(template["argv"], "--raw-close", template["raw_close_prefix"])
            result["plans"] = dict(updated["latest_plans"])
        if behavior["bad_continuation"]:
            updated["latest_plans"]["v1"] = "fabricated_plan.json"
        write(out / "receipt.json", result)
        return {"result": {**result, "operation": str(out / "receipt.json")}, "next_profile": updated}

    def forbidden(*args, **kwargs):
        pytest.fail("Native-only production cannot execute an account, core producer, opening, or action refresh")

    monkeypatch.setattr(native, "run_profile", produce)
    for name in ("_run_command", "_opening", "_lowvol", "_recent_actions"):
        monkeypatch.setattr(m, name, forbidden)
    return saved, calls, behavior


def set_clock(monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(m, "datetime", Clock)


def test_source_only_changes_native_context_but_never_core_accounts_or_original_plans(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    before = m._read(saved.directory / "head.json")
    accounts = {Path(t["run_dir"]) / "head.json" for t in before["native_profile"]["forward_templates"].values()}
    frozen = {p:p.read_bytes() for p in [saved.directory / "registration.json", *accounts,
        *map(Path, before["native_profile"]["latest_plans"].values())]}
    result = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert result["status"] == "completed_native_only_production" and result["producer_called"]
    after = m._read(saved.directory / "head.json")
    assert {k:v for k,v in before.items() if k != "native_profile"} == {k:v for k,v in after.items() if k != "native_profile"}
    assert after["native_profile"]["source_day"] == E
    assert after["native_profile"]["missing_plan_days"] == [E]
    assert after["native_profile"]["forward_templates"] == before["native_profile"]["forward_templates"]
    assert all(p.read_bytes() == value for p,value in frozen.items())
    operation = saved.directory / "native_context" / E.replace("-", "")
    unchanged = {p:p.read_bytes() for p in [operation / "receipt.json", operation / "plan.json", saved.directory / "head.json"]}
    again = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert again["status"] == "already_completed_native_only_production" and not again["producer_called"]
    assert calls == [(E, True)]
    assert all(p.read_bytes() == value for p,value in unchanged.items())


def test_current_production_continues_after_gap_without_reopening_old_account_chain(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    set_clock(monkeypatch, "2026-10-20T13:00:00+08:00")
    m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    set_clock(monkeypatch, "2026-10-20T18:45:00+08:00")
    result = m.advance_native(saved.directory, F, saved.package[F], source_only=False)
    head = m._read(saved.directory / "head.json")
    assert result["signals_published"] is True and calls == [(E, True), (F, False)]
    assert head["last_source_day"] == head["profile"]["last_source_day"] == D
    assert head["native_profile"]["source_day"] == F
    assert head["native_profile"]["missing_plan_days"] == [E]
    for template in head["native_profile"]["forward_templates"].values():
        assert {c["state"]["day"] for c in m._read(Path(template["run_dir"]) / "head.json")["cases"].values()} == {D}
    source = m._source(saved.package[F], {}, [], None)["proof"]
    blocked = m._native_forward_refresh(head, m._read(saved.directory / "registration.json"),
        saved.directory, saved.directory / "operations/20261020", F, saved.package[F], source, E,
        saved.head["native_profile"], "v1")
    assert blocked["status"] == "blocked_by_missing_original_source_plan"
    assert blocked["missing_plan_days"] == [E]


def test_failed_producer_is_not_retried_and_head_is_not_written(tmp_path, monkeypatch):
    saved, calls, behavior = wired(tmp_path, monkeypatch)
    behavior["fail"] = True
    before = (saved.directory / "head.json").read_bytes()
    result = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert result["status"] == "partial_native_only_production_no_retry"
    receipt = saved.directory / "native_context" / E.replace("-", "") / "receipt.json"
    raw = receipt.read_bytes()
    behavior["fail"] = False
    again = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert not again["producer_called"] and calls == [(E, True)]
    assert receipt.read_bytes() == raw and (saved.directory / "head.json").read_bytes() == before


def test_successful_saved_continuation_is_adopted_after_parent_write_interruption(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    original = m._atomic
    def interrupted(path, value):
        if Path(path) == saved.directory / "head.json":
            raise OSError("synthetic parent adoption interruption")
        return original(path, value)
    monkeypatch.setattr(m, "_atomic", interrupted)
    with pytest.raises(OSError, match="adoption interruption"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert m._read(saved.directory / "head.json")["native_profile"]["source_day"] == D
    monkeypatch.setattr(m, "_atomic", original)
    result = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert result["native_profile_adopted"] is True and not result["producer_called"]
    assert calls == [(E, True)]


@pytest.mark.parametrize("source_only,stamp", [(True,"2026-10-19T18:00:00+08:00"),
    (False,"2026-10-20T09:30:00+08:00"), (False,"2026-10-19T14:59:59+08:00")])
def test_wrong_window_rejected_before_creating_attempt(tmp_path, monkeypatch, source_only, stamp):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    set_clock(monkeypatch, stamp)
    with pytest.raises(ValueError, match="publication windows"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=source_only)
    assert not calls and not (saved.directory / "native_context").exists()
    assert not (saved.directory / "native_production").exists()


def test_mode_change_cannot_retry_an_original_daily_attempt(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    write(saved.directory / "operations" / E.replace("-", "") / "receipt.json",
        {"native_production_attempt": {"status": "failed"}})
    with pytest.raises(ValueError, match="mode-based retry"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert not calls


def test_source_only_cannot_adopt_a_changed_plan_in_its_returned_profile(tmp_path, monkeypatch):
    saved, calls, behavior = wired(tmp_path, monkeypatch)
    behavior["bad_continuation"] = True
    raw = (saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="preserve plans"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert calls == [(E, True)] and (saved.directory / "head.json").read_bytes() == raw


def test_cli_requires_explicit_mode_and_dispatches_only_native(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "advance_native", lambda *args, **kwargs: calls.append((args,kwargs)) or {"status":"ok"})
    args = ["advance-native","--run-dir",str(tmp_path),"--day",E,"--package",str(tmp_path / "raw")]
    with pytest.raises(SystemExit):
        m.main(args)
    assert not calls
    assert m.main([*args,"--source-only"]) == 0 and calls[0][1] == {"source_only":True}


def test_early_saved_success_before_parent_continuation_is_adopted_without_producing(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    completed = []
    produce = native.run_profile
    def capture(*args, **kwargs):
        value = produce(*args, **kwargs)
        completed.append(deepcopy(value))
        return value
    monkeypatch.setattr(native, "run_profile", capture)
    operation = saved.directory / "native_context" / E.replace("-", "")
    original = m._atomic
    def interrupted(path, value):
        if Path(path) == operation / "receipt.json" and "native_production_continuation" in value:
            raise OSError("synthetic interruption before parent continuation persisted")
        return original(path, value)
    monkeypatch.setattr(m, "_atomic", interrupted)
    with pytest.raises(OSError, match="before parent continuation"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert m._read(operation / "receipt.json")["native_production_attempt"]["status"] == "started"
    assert (operation / "native/receipt.json").exists() and len(completed) == 1
    recovered = []
    def adopt(profile, day, package, out, *, source_only):
        assert profile == saved.head["native_profile"] and day == E and out == operation / "native"
        assert source_only and m._read(out / "receipt.json")["status"] == "saved_native_source_context_without_publication"
        recovered.append(day)
        return completed[0]
    monkeypatch.setattr(native, "recover_saved_profile", adopt)
    monkeypatch.setattr(m, "_atomic", original)
    result = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert result["native_saved_result_adopted_without_production"] and result["native_profile_adopted"]
    assert not result["producer_called"] and calls == [(E, True)] and recovered == [E]


def test_missing_original_success_never_restarts_started_producer(tmp_path, monkeypatch):
    saved, calls, behavior = wired(tmp_path, monkeypatch)
    behavior["fail"] = True
    operation = saved.directory / "native_context" / E.replace("-", "")
    original = m._atomic
    def interrupted(path, value):
        if (Path(path) == operation / "receipt.json" and value.get("native_production_attempt", {}).get("status") == "failed"):
            raise OSError("synthetic interruption before parent failure persisted")
        return original(path, value)
    monkeypatch.setattr(m, "_atomic", interrupted)
    before = (saved.directory / "head.json").read_bytes()
    with pytest.raises(OSError, match="failure persisted"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert not (operation / "native/receipt.json").exists()
    monkeypatch.setattr(m, "_atomic", original)
    behavior["fail"] = False
    result = m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert result["status"] == "partial_native_only_production_no_retry" and not result["producer_called"]
    assert result["saved_producer_recovery_error"]
    assert calls == [(E, True)] and (saved.directory / "head.json").read_bytes() == before


def test_changed_saved_feature_bytes_reject_success_reuse_without_producing(tmp_path, monkeypatch):
    saved, calls, _ = wired(tmp_path, monkeypatch)
    m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    feature = saved.directory / "native_context" / E.replace("-", "") / "native/features/fixture.csv"
    feature.write_text("changed-feature\n",encoding="utf-8")
    with pytest.raises(ValueError, match="stage artifacts changed"):
        m.advance_native(saved.directory, E, saved.package[E], source_only=True)
    assert calls == [(E, True)]
