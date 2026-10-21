"""Saved input-context adoption uses local sources and actual receipt validators."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_daily as producer
from trading.research import system_daily_refresh as daily
from trading.research.tests import test_leader_eight_type_daily as fixtures
from trading.research.tests.test_eight32_split_generation_wiring import saved_input_context
from trading.research.tests.test_system_daily_refresh import write


def failed_context(tmp_path,monkeypatch):
    e,head,registration,sessions,now,operation=saved_input_context(tmp_path,monkeypatch)
    receipt=daily._read(operation/"receipt.json")
    plan=daily._read(operation/"plan.json")
    parent=head[producer.PROFILE_KEY]
    _,steps=producer._steps(parent,fixtures.E,e.packages[fixtures.E],operation,False)
    step=steps[-1]
    spec=daily._step_spec(step)
    paths=daily.command_child.paths(operation/"commands","paper32")
    argv=daily._command(step["command"])
    request_ref=write(paths["request"],{"schema":daily.command_child.REQUEST_SCHEMA,"day":fixtures.E,
        "step":spec,"argv":argv,"cwd":str(daily.ROOT),"binding":{"kind":"eight32_independent_production",
            "registration":head["registration"],"source":plan["source"],"plan":daily._ref(operation/"plan.json")},
        **daily.FLAGS})
    started_ref=write(paths["started"],{"schema":daily.command_child.START_SCHEMA,
        "request_ref":request_ref,"supervisor_pid":12345})
    terminal_ref=write(paths["terminal"],{"schema":daily.command_child.TERMINAL_SCHEMA,
        "request_ref":request_ref,"started_ref":started_ref,"day":fixtures.E,"step":spec,"argv":argv,
        "supervisor_pid":12345,"status":"failed_command_no_automatic_retry","exit_code":7,
        "execution_progress_unknown":False,"output_refs":None,**daily.FLAGS})
    saved={"command":step["command"],"status":"failed_no_automatic_retry","day":fixtures.E,"step":"paper32",
        "argv":argv,"expected_files":spec["expected_files"],"child_request_ref":request_ref,
        "child_terminal_ref":terminal_ref,"child_pid":12345}
    write(operation/"commands/paper32.json",saved)
    receipt["steps"]["paper32"]=saved
    write(operation/"receipt.json",receipt)
    return e,head,registration,sessions,now,operation


def test_adopt_keeps_failure_and_allows_next_source_append_without_pub_backfill(tmp_path,monkeypatch):
    e,head,registration,sessions,now,operation=failed_context(tmp_path,monkeypatch)
    before_receipt=(operation/"receipt.json").read_bytes()
    before_calls=list(e.calls)
    result=producer.recover_context(e.saved.directory,fixtures.E)
    assert result["status"] == "adopted_eight32_failed_publication_context" and not result["producer_called"]
    updated=daily._read(e.saved.directory/"head.json")
    profile=updated[producer.PROFILE_KEY]
    producer._validate(profile,updated,registration,sessions,now)
    assert profile["source_days"] == [fixtures.D,fixtures.E]
    assert profile["missing_publication_days"] == [fixtures.E]
    for key in ("publication_source_day","latest_publication","publication_manifest"):
        assert profile[key] == head[producer.PROFILE_KEY][key]
    assert profile["latest_manifest"] == daily._ref(operation/"artifacts/eight_inputs/manifest.json")
    assert {k:v for k,v in updated.items() if k != producer.PROFILE_KEY} == {k:v for k,v in head.items() if k != producer.PROFILE_KEY}
    assert (operation/"receipt.json").read_bytes() == before_receipt and e.calls == before_calls
    # Only this explicitly requested next production uses the synthetic child.
    e.behavior["fail"]=None
    fixtures.clock(monkeypatch,fixtures.F+"T18:45:00+08:00")
    following=producer.advance(e.saved.directory,fixtures.F,e.packages[fixtures.F],source_only=False)
    assert following["status"] == "completed_eight32_publication"
    current=daily._read(e.saved.directory/"head.json")[producer.PROFILE_KEY]
    assert current["source_days"] == [fixtures.D,fixtures.E,fixtures.F]
    assert current["missing_publication_days"] == [fixtures.E] and current["publication_source_day"] == fixtures.F
    assert daily._values(current["commands"]["eight_inputs"],"--append-from") == [profile["latest_manifest"]["file"]]
    forwarded=(e.saved.directory/"head.json").read_bytes()
    with pytest.raises(ValueError,match="roll back"):
        producer.recover_context(e.saved.directory,fixtures.E)
    assert (e.saved.directory/"head.json").read_bytes() == forwarded


def test_adoption_receipt_survives_parent_interruption_and_is_idempotent(tmp_path,monkeypatch):
    e,head,registration,sessions,now,operation=failed_context(tmp_path,monkeypatch)
    atomic=daily._atomic
    def interrupted(file,value):
        if Path(file) == e.saved.directory/"head.json":
            raise OSError("adoption head interrupted")
        return atomic(file,value)
    monkeypatch.setattr(daily,"_atomic",interrupted)
    with pytest.raises(OSError,match="adoption head interrupted"):
        producer.recover_context(e.saved.directory,fixtures.E)
    adoption=operation/"context_adoption.json"
    recorded=adoption.read_bytes()
    assert daily._read(e.saved.directory/"head.json") == head
    monkeypatch.setattr(daily,"_atomic",atomic)
    calls=list(e.calls)
    assert producer.recover_context(e.saved.directory,fixtures.E)["status"] == "adopted_eight32_failed_publication_context"
    saved_head=(e.saved.directory/"head.json").read_bytes()
    assert producer.recover_context(e.saved.directory,fixtures.E)["status"] == "already_adopted_eight32_failed_publication_context"
    assert adoption.read_bytes() == recorded and (e.saved.directory/"head.json").read_bytes() == saved_head
    assert e.calls == calls


@pytest.mark.parametrize("fault",["pending_paper","altered_input"])
def test_pending_or_altered_evidence_cannot_be_adopted(tmp_path,monkeypatch,fault):
    e,head,registration,sessions,now,operation=failed_context(tmp_path,monkeypatch)
    if fault == "pending_paper":
        paths=daily.command_child.paths(operation/"commands","paper32")
        terminal=daily._read(paths["terminal"])
        terminal.update(status="started",exit_code=None,execution_progress_unknown=True)
        ref=write(paths["terminal"],terminal)
        receipt=daily._read(operation/"receipt.json")
        receipt["steps"]["paper32"]["child_terminal_ref"]=ref
        write(operation/"receipt.json",receipt)
    else:
        manifest=daily._read(operation/"artifacts/eight_inputs/manifest.json")
        Path(manifest["raw_file"]).write_text("altered_successful_raw",encoding="utf-8")
    before=(e.saved.directory/"head.json").read_bytes()
    calls=list(e.calls)
    with pytest.raises(ValueError):
        producer.recover_context(e.saved.directory,fixtures.E)
    assert not (operation/"context_adoption.json").exists()
    assert (e.saved.directory/"head.json").read_bytes() == before and e.calls == calls


@pytest.mark.parametrize("valid",[True,False])
def test_existing_paper_artifact_is_preserved_and_valid_pub_never_discarded(tmp_path,monkeypatch,valid):
    e,head,registration,sessions,now,operation=failed_context(tmp_path,monkeypatch)
    paper=operation/"artifacts/paper32/paper_plan.json"
    if valid:
        fixtures.publication(paper,daily._ref(operation/"artifacts/eight_inputs/manifest.json"),fixtures.E,fixtures.F)
    else:
        write(paper,{"incomplete_failed_command_artifact":True})
    before=paper.read_bytes()
    if valid:
        with pytest.raises(ValueError,match="genuine saved publication exists"):
            producer.recover_context(e.saved.directory,fixtures.E)
        assert not (operation/"context_adoption.json").exists()
    else:
        producer.recover_context(e.saved.directory,fixtures.E)
        adoption=daily._read(operation/"context_adoption.json")
        artifact=adoption["failed_paper_evidence"]["paper_artifact"]
        assert artifact["reference"] == daily._ref(paper)
        assert artifact["status"] == "unverified_failed_command_artifact" and artifact["validation_error"]
    assert paper.read_bytes() == before


def test_orphan_failed_child_adopts_without_parent_terminal_adoption_or_receipt_mutation(tmp_path,monkeypatch):
    e,head,registration,sessions,now,operation=failed_context(tmp_path,monkeypatch)
    producer_receipt=operation/"receipt.json"
    receipt=daily._read(producer_receipt)
    del receipt["steps"]["paper32"]
    receipt.pop("status",None)
    write(producer_receipt,receipt)
    command_receipt=operation/"commands/paper32.json"
    command=daily._read(command_receipt)
    command.pop("child_terminal_ref")
    command.update(status="started",execution_progress_unknown=True)
    write(command_receipt,command)
    originals={file:file.read_bytes() for file in (producer_receipt,command_receipt)}
    calls=list(e.calls)
    result=producer.recover_context(e.saved.directory,fixtures.E)
    assert result["status"] == "adopted_eight32_failed_publication_context"
    saved=(e.saved.directory/"head.json").read_bytes()
    again=producer.recover_context(e.saved.directory,fixtures.E)
    assert again["status"] == "already_adopted_eight32_failed_publication_context"
    assert (e.saved.directory/"head.json").read_bytes() == saved and e.calls == calls
    assert all(file.read_bytes() == before for file,before in originals.items())
