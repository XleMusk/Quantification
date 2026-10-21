"""Leader production/source recovery with synthetic files and no account run."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import leader_close_daily as leader
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_system_daily_refresh import saved_system, write


D, E, F, PREFIX = "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-16"


def set_clock(monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(leader, "datetime", Clock)


def source_proof(folder):
    folder = Path(folder).resolve()
    record = daily._read(folder / "plan.json")
    receipt = daily._read(folder / "receipt.json")
    return {"day": record["day"], "source_folder": str(folder),
        "plan_sha256": daily._ref(folder / "plan.json")["sha256"],
        "receipt_sha256": daily._ref(folder / "receipt.json")["sha256"],
        "raw_sha256": receipt["raw_daily_sha256"],
        "roster_raw_sha256": record["roster_raw_sha256"]}


def publication(folder, day, execution, packages, calendar_ref, cohort_file, *, status=None, wrong_day=None):
    folder = Path(folder)
    raw = folder / "input_bundle/raw.csv"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("date,symbol,close\n" + day + ",000001.SZSE,10\n", encoding="utf-8")
    rows = []
    for package in packages:
        proof = source_proof(package)
        rows.append({"day": proof["day"], "folder": proof["source_folder"],
            "plan_sha256": proof["plan_sha256"], "receipt_sha256": proof["receipt_sha256"],
            "raw_daily_sha256": proof["raw_sha256"], "roster_raw_sha256": proof["roster_raw_sha256"]})
    own = folder / "input_bundle/bundle.json"
    recorded = day + "T18:00:00+08:00"
    proof = write(own, {"recorded_at": recorded, "raw_bars_file": str(raw),
        "raw_bars_sha256": daily._ref(raw)["sha256"],
        "bundle": {"calendar_sha256": calendar_ref["sha256"], "source_packages": rows}})
    record = {"schema": "leader_close_target_publication_v1",
        "status": status or "close_research_target_publication_only",
        "source_close_day": wrong_day or day, "proposed_execution_day": execution,
        "recorded_at": day + "T18:01:00+08:00",
        "publication_input_bundle": {**proof, "recorded_at": recorded},
        "frozen_cohort_registration": daily._ref(cohort_file),
        "execution_binding_pending": False, "execution_day_rolled_forward": False,
        "publication_time_backfilled": False, "ledger_checkpoint": None, "case_count": 20,
        "candidate_panel_only": True,
        **{key: False for key in ("execution_admitted", "selection_enabled", "strategy_validated",
            "orders_generated", "account_created")}}
    write(folder / "publication.json", record)
    return folder / "publication.json"


def wired(tmp_path, monkeypatch):
    saved = saved_system(tmp_path, monkeypatch)
    packages = dict(saved.package)
    packages[F] = saved.directory.parent / "prices" / F
    for day, folder in packages.items():
        write(folder / "plan.json", {"day": day, "roster_raw_sha256": "roster-" + day})
        write(folder / "receipt.json", {"raw_daily_sha256": "raw-" + day})
    head = daily._read(saved.directory / "head.json")
    registration = daily._read(saved.directory / "registration.json")
    calendar_ref = registration["calendar"]
    protocol = saved.directory.parent / "protocol/protocol.json"
    cohort = saved.directory.parent / "cohort/registration.json"
    write(protocol, {"fixed_rules": True})
    write(cohort, {"fixed_cohort": "original_14_anchor"})
    old = publication(saved.directory / "operations/20261019/artifacts/pub20", D, E,
        [packages[PREFIX], packages[D]], calendar_ref, cohort)
    command = ["leader-close-publication", "--package", str(packages[PREFIX]), "--package", str(packages[D]),
        "--history-snapshot", str(saved.directory.parent / "frozen_history"),
        "--calendar", calendar_ref["file"], "--protocol", str(protocol),
        "--cohort-registration", str(cohort), "--decision-day", D, "--out", str(old.parent)]
    head["profile"].update(last_source_day=D, source_days=[PREFIX, D],
        templates={"pub20": command, "forward20": ["leader-published-forward", "--through", D]},
        output_paths={"pub20": {"publication": str(old), "bundle": str(old.parent / "input_bundle/bundle.json")}})
    head["last_source_day"] = D
    registration["initial_profile"] = deepcopy(head["profile"])
    head["registration"] = write(saved.directory / "registration.json", registration)
    write(saved.directory / "head.json", head)
    calls, behavior = [], {"status": "completed", "late": False, "wrong_day": False}

    def execute(step, commands, day):
        assert step["id"] == "pub20" and step["command"][0] == "leader-close-publication"
        calls.append(deepcopy(step))
        publication_file = Path(step["expected_files"][0])
        if behavior["status"] == "completed":
            publication(publication_file.parent, day, "2026-10-22",
                daily._values(step["command"], "--package"), calendar_ref, cohort,
                status="late_context_only" if behavior["late"] else None,
                wrong_day=E if behavior["wrong_day"] else None)
        record = {"status": behavior["status"], "command": step["command"], "exit_code": 0}
        write(Path(commands) / "pub20.json", record)
        return record

    def forbidden(*args, **kwargs):
        pytest.fail("Independent leader production cannot run accounts, opening, other routes or a real child")

    monkeypatch.setattr(daily, "_source", lambda folder, *args: {"proof": source_proof(folder)})
    monkeypatch.setattr(daily, "_run_command", execute)
    monkeypatch.setattr(daily, "_terminal", lambda *args: (None, None, {"exit_code": 0}, []))
    for name in ("_opening", "_lowvol", "_recent_actions", "_native_forward_refresh"):
        monkeypatch.setattr(daily, name, forbidden)
    monkeypatch.setattr(daily.subprocess, "Popen", forbidden)
    set_clock(monkeypatch, "2026-10-21T13:00:00+08:00")
    accounts = [Path(t["run_dir"]) / "head.json" for t in head["native_profile"]["forward_templates"].values()]
    frozen = {path: path.read_bytes() for path in [saved.directory / "registration.json", old,
        *accounts, *map(Path, head["native_profile"]["latest_plans"].values())]}
    return SimpleNamespace(saved=saved, packages=packages, head=head, calls=calls,
        behavior=behavior, frozen=frozen, command=command, calendar_ref=calendar_ref, cohort=cohort, old=old)


def assert_old_state(e):
    head = daily._read(e.saved.directory / "head.json")
    assert {key: value for key, value in head.items() if key != leader.PROFILE_KEY} == e.head
    assert all(path.read_bytes() == value for path, value in e.frozen.items())
    return head


def test_19_to_20_source_only_changes_only_production_profile_without_any_producer(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    result = leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert result["status"] == "completed_leader20_source_context"
    assert not result["producer_called"] and not result["signals_published"] and not e.calls
    head = assert_old_state(e)
    profile = head[leader.PROFILE_KEY]
    assert profile["source_days"] == [PREFIX, D, E]
    assert profile["publication_source_day"] == D
    assert profile["missing_publication_days"] == [E]
    assert profile["latest_publication"] == daily._ref(e.old)
    assert head["profile"]["templates"]["forward20"] == e.head["profile"]["templates"]["forward20"]


def test_identical_context_is_byte_preserving_and_does_not_produce(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    operation = e.saved.directory / "leader20_context/20261020"
    paths = [e.saved.directory / "head.json", operation / "plan.json", operation / "receipt.json"]
    before = {path: path.read_bytes() for path in paths}
    again = leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert again["reused_saved_result"] and not again["producer_called"] and not e.calls
    assert all(path.read_bytes() == value for path, value in before.items())


def test_completed_context_receipt_recovers_interrupted_head_write_without_production(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    atomic = daily._atomic
    def interrupted(path, value):
        if Path(path) == e.saved.directory / "head.json":
            raise OSError("synthetic parent head interruption")
        return atomic(path, value)
    monkeypatch.setattr(daily, "_atomic", interrupted)
    with pytest.raises(OSError, match="head interruption"):
        leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    receipt_file = e.saved.directory / "leader20_context/20261020/receipt.json"
    raw = receipt_file.read_bytes()
    assert leader.PROFILE_KEY not in daily._read(e.saved.directory / "head.json")
    monkeypatch.setattr(daily, "_atomic", atomic)
    recovered = leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert recovered["reused_saved_result"] and not recovered["producer_called"] and not e.calls
    assert receipt_file.read_bytes() == raw
    assert assert_old_state(e)[leader.PROFILE_KEY]["source_day"] == E


def test_real_current21_publication_retains_source_gap_and_all_original_accounts(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    result = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "completed_leader20_publication" and result["signals_published"]
    assert len(e.calls) == 1
    command = e.calls[0]["command"]
    assert leader._static(command) == leader._static(e.command)
    assert daily._values(command, "--package") == [str(e.packages[d]) for d in (PREFIX, D, E, F)]
    assert daily._values(command, "--decision-day") == [F]
    assert "--ledger-checkpoint" not in command and "--publication-at" not in command
    head = assert_old_state(e)
    profile = head[leader.PROFILE_KEY]
    assert profile["source_day"] == profile["publication_source_day"] == F
    assert profile["missing_publication_days"] == [E]
    assert head["last_source_day"] == head["profile"]["last_source_day"] == D
    before = (e.saved.directory / "head.json").read_bytes()
    repeated = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert repeated["reused_saved_result"] and not repeated["producer_called"]
    assert len(e.calls) == 1 and (e.saved.directory / "head.json").read_bytes() == before


def test_source_package_replacement_and_changed_original_source_are_rejected(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    replacement = e.saved.directory.parent / "replacement20"
    for name in ("plan.json", "receipt.json"):
        write(replacement / name, daily._read(e.packages[E] / name))
    before = (e.saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="change or skip"):
        leader.advance(e.saved.directory, E, replacement, source_only=True)
    write(e.packages[D] / "receipt.json", {"raw_daily_sha256": "changed"})
    with pytest.raises(ValueError, match="source receipt changed"):
        leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert not e.calls and (e.saved.directory / "head.json").read_bytes() == before


def test_skipping_source_day_or_switching_mode_cannot_fill_old_publication(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    with pytest.raises(ValueError, match="change or skip"):
        leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    set_clock(monkeypatch, "2026-10-21T13:00:00+08:00")
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    with pytest.raises(ValueError, match="Missed windows"):
        leader.advance(e.saved.directory, E, e.packages[E], source_only=False)
    assert not e.calls


def test_changed_static_method_is_rejected_without_publishing(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    head_file = e.saved.directory / "head.json"
    head = daily._read(head_file)
    head[leader.PROFILE_KEY]["command"].extend(["--ledger-checkpoint", "another-account.json"])
    write(head_file, head)
    before = head_file.read_bytes()
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    with pytest.raises(ValueError, match="registered method"):
        leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert not e.calls and head_file.read_bytes() == before


@pytest.mark.parametrize("fault", ["late", "wrong_day"])
def test_exit_zero_publisher_with_late_or_wrong_day_output_is_not_adopted(tmp_path, monkeypatch, fault):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    e.behavior[fault] = True
    before = (e.saved.directory / "head.json").read_bytes()
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    result = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "partial_leader20_production_no_retry"
    assert not result["signals_published"] and "same-day" in result["error"]
    assert (e.saved.directory / "head.json").read_bytes() == before
    leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert len(e.calls) == 1 and (e.saved.directory / "head.json").read_bytes() == before


def test_failed_saved_command_is_not_produced_again(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    e.behavior["status"] = "failed_no_automatic_retry"
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    before = (e.saved.directory / "head.json").read_bytes()
    first = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    second = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert first["status"] == second["status"] == "partial_leader20_production_no_retry"
    assert not second["producer_called"] and len(e.calls) == 1
    assert (e.saved.directory / "head.json").read_bytes() == before


@pytest.mark.parametrize("parent_receipt_missing", [False, True])
def test_parent_receipt_is_saved_before_child_and_saved_terminal_recovers_after_day_changes(
        tmp_path, monkeypatch, parent_receipt_missing):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    operation = e.saved.directory / "leader20_production/20261021"
    parent_file = operation / "receipt.json"
    execute = daily._run_command
    observations = []

    def completed_child_but_parent_interrupted(step, commands, day):
        # Terminal mechanics are already isolated by wired(). This test covers
        # the parent's durable claim and adoption of the original saved child.
        parent = daily._read(parent_file)
        assert parent["day"] == F and parent["source_only"] is False
        assert parent["plan_ref"] == daily._ref(operation / "plan.json")
        observations.append("parent_saved_before_child")
        execute(step, commands, day)
        write(Path(commands) / "pub20.json", {"status": "started", "command": step["command"]})
        files = daily.command_child.paths(commands, "pub20")
        for key in ("request", "started", "terminal"):
            write(files[key], {"synthetic_saved_child": key, "day": F})
        raise OSError("synthetic interruption after saved child terminal")

    monkeypatch.setattr(daily, "_run_command", completed_child_but_parent_interrupted)
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    before = (e.saved.directory / "head.json").read_bytes()
    with pytest.raises(OSError, match="saved child terminal"):
        leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert observations == ["parent_saved_before_child"] and len(e.calls) == 1
    assert (e.saved.directory / "head.json").read_bytes() == before
    if parent_receipt_missing:
        parent_file.unlink()
    monkeypatch.setattr(daily, "_run_command", execute)
    set_clock(monkeypatch, "2026-10-22T10:00:00+08:00")
    result = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "completed_leader20_publication"
    assert not result["producer_called"] and len(e.calls) == 1
    assert result["command_result"]["saved_success_adopted_without_production"] is True
    profile = assert_old_state(e)[leader.PROFILE_KEY]
    assert profile["source_day"] == profile["publication_source_day"] == F
    assert profile["missing_publication_days"] == [E]


@pytest.mark.parametrize("claim", ["request", "started", "terminal", "recovery"])
def test_original_command_claim_without_parent_receipt_rejects_a_new_path(tmp_path, monkeypatch, claim):
    e = wired(tmp_path, monkeypatch)
    original = e.saved.directory / "operations/20261020/commands"
    write(daily.command_child.paths(original, "pub20")[claim], {"synthetic_original_claim": claim})
    before = (e.saved.directory / "head.json").read_bytes()
    with pytest.raises(ValueError, match="original producer operation"):
        leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    assert not e.calls and (e.saved.directory / "head.json").read_bytes() == before
    assert not (e.saved.directory / "leader20_context/20261020").exists()


def test_overdue_parent_without_child_claim_cannot_launch_a_late_producer(tmp_path, monkeypatch):
    e = wired(tmp_path, monkeypatch)
    leader.advance(e.saved.directory, E, e.packages[E], source_only=True)
    operation = e.saved.directory / "leader20_production/20261021"
    execute = daily._run_command
    def stop_before_child(step, commands, day):
        assert (operation / "receipt.json").exists()
        raise OSError("synthetic interruption before child claim")
    monkeypatch.setattr(daily, "_run_command", stop_before_child)
    set_clock(monkeypatch, "2026-10-21T18:45:00+08:00")
    before = (e.saved.directory / "head.json").read_bytes()
    with pytest.raises(OSError, match="before child claim"):
        leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert not e.calls and (operation / "receipt.json").exists()
    monkeypatch.setattr(daily, "_run_command", execute)
    set_clock(monkeypatch, "2026-10-22T10:00:00+08:00")
    result = leader.advance(e.saved.directory, F, e.packages[F], source_only=False)
    assert result["status"] == "partial_leader20_production_no_retry"
    assert result["command_result"]["status"] == "missed_publication_window_no_producer_started"
    assert not result["producer_called"] and not result["signals_published"] and not e.calls
    assert not (operation / "commands/pub20.json").exists()
    assert (e.saved.directory / "head.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["clock_before_close", "own_input_late", "raw_bytes_changed"])
def test_real_publication_small_json_checks_clock_and_own_immutable_input(tmp_path, monkeypatch, fault):
    e = wired(tmp_path, monkeypatch)
    now = datetime.fromisoformat("2026-10-19T19:00:00+08:00")
    value, own = leader._publication(e.old, D, E, e.calendar_ref, e.command, now)
    assert value["source_close_day"] == D and own["recorded_at"].startswith(D)
    if fault == "clock_before_close":
        value["recorded_at"] = D + "T15:04:59+08:00"
        write(e.old, value)
    elif fault == "own_input_late":
        bundle_file = Path(value["publication_input_bundle"]["file"])
        own["recorded_at"] = D + "T18:02:00+08:00"
        proof = write(bundle_file, own)
        value["publication_input_bundle"] = {**proof, "recorded_at": own["recorded_at"]}
        write(e.old, value)
    else:
        Path(own["raw_bars_file"]).write_text("changed-raw\n", encoding="utf-8")
    with pytest.raises(ValueError, match="same-day|immutable input"):
        leader._publication(e.old, D, E, e.calendar_ref, e.command, now)
