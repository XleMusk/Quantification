"""Thin generation coordination; real registrar, synthetic child outcomes.

The registrar's separate focused test already exercises the unchanged Engine.
These tests do not duplicate that account replay.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import eight32_paper_generation as generation
from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_forward_run as forward
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_leader_eight_type_forward_run import evidence
from trading.research.tests.test_system_daily_refresh import saved_system, write


def clock(monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value=datetime.fromisoformat(stamp)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(generation,"datetime",Clock)
    monkeypatch.setattr(registrar,"_now",lambda:datetime.fromisoformat(stamp))


def wired(tmp_path,monkeypatch,evidence):
    saved=saved_system(tmp_path/"coordinator",monkeypatch)
    monkeypatch.setattr(daily,"DATA_ROOT",saved.directory.parent)
    monkeypatch.setattr(generation.producer,"_validate",lambda *args:None)
    ipo,master=tmp_path/"ipo.json",tmp_path/"master.json"
    for file in (ipo,master):
        write(file,{"synthetic":True})
    profile={"source_day":"2026-10-15","publication_source_day":"2026-10-15",
        "latest_publication":daily._ref(evidence["pubs"][0]),
        "publication_manifest":daily._ref(evidence["panels"]["2026-10-15"]),
        "latest_manifest":daily._ref(evidence["panels"]["2026-10-15"]),
        "commands":{"paper32":["leader-eight-type-paper","--ipo-evidence",str(ipo),"--research-master-run",str(master)]}}
    head=daily._read(saved.directory/"head.json")
    head[generation.producer.PROFILE_KEY]=profile
    write(saved.directory/"head.json",head)
    calls,behavior=[],{"fail":False}
    def child(step,operation,day,**kwargs):
        receipt=operation/"commands"/(step["id"]+".json")
        if receipt.exists():
            return daily._read(receipt)
        if kwargs.get("allow_start") is False:
            return {"status":"missed_publication_window_no_producer_started"}
        calls.append(step["id"])
        if behavior["fail"]:
            result={"status":"failed_no_automatic_retry","command":step["command"]}
        elif step["id"] == "register_generation":
            command=step["command"]
            registrar.register(**{name:daily._values(command,"--"+flag)[0] for name,flag in
                (("publication","publication"),("input_manifest","input-manifest"),("ipo_evidence","ipo-evidence"),
                 ("research_master_run","research-master-run"),("out","out"),("run_dir","run-dir"))})
            result={"status":"completed","command":command}
        else:
            cases={key:{"state":{"day":day},"blocking_evidence":None,"error":None} for key in forward.CASE_IDS}
            first=next(iter(cases))
            cases[first].update(state={"day":"2026-10-15"},blocking_evidence={"field":"reference", "session_not_committed":True})
            account=Path(step["expected_files"][0])
            write(account,{"cases":cases})
            write(operation/"commands/consume_generation.stdout.txt",{
                "status":"advanced_research_only_with_blocked_cases","case_count":32,
                "checkpoint":str(account),"execution_admitted":False})
            result={"status":"completed","command":step["command"]}
        write(receipt,result)
        return result
    monkeypatch.setattr(generation,"_saved_command",child)
    clock(monkeypatch,"2026-10-15T22:00:00+08:00")
    return saved,head,calls,behavior


def next_source(saved,evidence):
    head=daily._read(saved.directory/"head.json")
    profile=head[generation.producer.PROFILE_KEY]
    profile.update(source_day="2026-10-16",publication_source_day="2026-10-16",
        latest_publication=daily._ref(evidence["pubs"][1]),
        publication_manifest=daily._ref(evidence["panels"]["2026-10-16"]),
        latest_manifest=daily._ref(evidence["panels"]["2026-10-16"]))
    write(saved.directory/"head.json",head)


def test_start_and_no_E_source_never_create_book_or_change_old_fields(tmp_path,monkeypatch,evidence):
    saved,original,calls,_=wired(tmp_path,monkeypatch,evidence)
    result=generation.start(saved.directory,"synthetic_v1")
    assert result["status"] == "registered_eight32_paper_generation"
    value=result["generation"]
    assert not Path(value["run_directory"]).exists()
    head=daily._read(saved.directory/"head.json")
    assert {k:v for k,v in head.items() if k != generation.KEY} == original
    waiting=generation.advance(saved.directory,"2026-10-15")
    assert waiting["status"] == "awaiting_exact_first_execution_close" and not waiting["command_called"]
    assert calls == ["register_generation"] and not Path(value["run_directory"]).exists()
    with pytest.raises(ValueError,match="cannot be replaced"):
        generation.start(saved.directory,"another_generation")
    assert calls == ["register_generation"]


def test_partial_child_result_retains_each_case_and_cannot_imply_whole_completion(tmp_path,monkeypatch,evidence):
    saved,_,calls,_=wired(tmp_path,monkeypatch,evidence)
    generation.start(saved.directory,"synthetic_v1")
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    result=generation.advance(saved.directory,"2026-10-16")
    state=result["generation"]
    assert state["account_count"] == 32 and state["current_case_count"] == 31
    assert state["blocked_case_count"] == 1 and state["error_case_count"] == 0 and state["metrics"] is None
    assert [row["day"] for row in state["publications"]] == ["2026-10-15","2026-10-16"]
    assert state["consumer_status"] == "advanced_research_only_with_blocked_cases"
    before=(saved.directory/"head.json").read_bytes()
    again=generation.advance(saved.directory,"2026-10-16")
    assert again["status"] == "already_saved_eight32_generation_day" and not again["command_called"]
    assert (saved.directory/"head.json").read_bytes() == before
    assert calls == ["register_generation","consume_generation"]


def test_saved_success_recovers_start_head_without_registering_again(tmp_path,monkeypatch,evidence):
    saved,_,calls,_=wired(tmp_path,monkeypatch,evidence)
    atomic=daily._atomic
    def interrupted(file,value):
        if Path(file) == saved.directory/"head.json":
            raise OSError("head write interrupted")
        return atomic(file,value)
    monkeypatch.setattr(daily,"_atomic",interrupted)
    with pytest.raises(OSError,match="head write interrupted"):
        generation.start(saved.directory,"synthetic_v1")
    receipt=saved.directory/"eight32_generations/synthetic_v1/start/receipt.json"
    before=receipt.read_bytes()
    monkeypatch.setattr(daily,"_atomic",atomic)
    result=generation.start(saved.directory,"synthetic_v1")
    assert result["status"] == "registered_eight32_paper_generation" and calls == ["register_generation"]
    assert receipt.read_bytes() == before and not Path(result["generation"]["run_directory"]).exists()


def test_failed_command_remains_failed_without_child_retry(tmp_path,monkeypatch,evidence):
    saved,original,calls,behavior=wired(tmp_path,monkeypatch,evidence)
    behavior["fail"]=True
    first=generation.start(saved.directory,"synthetic_v1")
    assert first["status"] == "partial_generation_command_no_retry"
    again=generation.start(saved.directory,"synthetic_v1")
    assert again["status"] == first["status"] and calls == ["register_generation"]
    assert daily._read(saved.directory/"head.json") == original


def test_saved_unstarted_registration_plan_cannot_launch_after_first_open(tmp_path,monkeypatch,evidence):
    saved,original,calls,_=wired(tmp_path,monkeypatch,evidence)
    child=generation._saved_command
    monkeypatch.setattr(generation,"_saved_command",lambda *a,**k: (_ for _ in ()).throw(OSError("parent interrupted")))
    with pytest.raises(OSError,match="parent interrupted"):
        generation.start(saved.directory,"synthetic_v1")
    monkeypatch.setattr(generation,"_saved_command",child)
    clock(monkeypatch,"2026-10-16T09:31:00+08:00")
    result=generation.start(saved.directory,"synthetic_v1")
    assert result["status"] == "partial_generation_command_no_retry" and not calls
    assert daily._read(saved.directory/"head.json") == original


def test_saved_partial_consumer_success_recovers_head_without_replaying_child(tmp_path,monkeypatch,evidence):
    saved,_,calls,_=wired(tmp_path,monkeypatch,evidence)
    generation.start(saved.directory,"synthetic_v1")
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    atomic=daily._atomic
    def interrupted(file,value):
        if Path(file) == saved.directory/"head.json":
            raise OSError("head write interrupted")
        return atomic(file,value)
    monkeypatch.setattr(daily,"_atomic",interrupted)
    with pytest.raises(OSError,match="head write interrupted"):
        generation.advance(saved.directory,"2026-10-16")
    receipt=saved.directory/"eight32_generations/synthetic_v1/advance/20261016/receipt.json"
    raw=receipt.read_bytes()
    monkeypatch.setattr(daily,"_atomic",atomic)
    # Production may advance independently while the successful original
    # consumer result still awaits adoption by the coordination head.
    head=daily._read(saved.directory/"head.json")
    head[generation.producer.PROFILE_KEY].update(source_day="2026-10-19",publication_source_day="2026-10-19",
        latest_publication=daily._ref(evidence["pubs"][2]),
        publication_manifest=daily._ref(evidence["panels"]["2026-10-19"]),
        latest_manifest=daily._ref(evidence["panels"]["2026-10-19"]))
    write(saved.directory/"head.json",head)
    result=generation.advance(saved.directory,"2026-10-16")
    assert result["generation"]["current_case_count"] == 31
    assert calls == ["register_generation","consume_generation"] and receipt.read_bytes() == raw
    current=daily._read(saved.directory/"head.json")
    assert current[generation.producer.PROFILE_KEY]["source_day"] == "2026-10-19"
    assert current[generation.KEY]["last_consumer_day"] == "2026-10-16"


def test_already_saved_day_checks_account_pin_before_claiming_success(tmp_path,monkeypatch,evidence):
    saved,_,calls,_=wired(tmp_path,monkeypatch,evidence)
    generation.start(saved.directory,"synthetic_v1")
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    result=generation.advance(saved.directory,"2026-10-16")
    account=Path(result["generation"]["account_head"]["file"])
    write(account,{"corrupt_after_original_success":True})
    with pytest.raises(ValueError,match="registration/publication/input changed"):
        generation.advance(saved.directory,"2026-10-16")
    assert calls == ["register_generation","consume_generation"]


@pytest.mark.parametrize("fault",["row_manifest","qualification"])
def test_original_manifest_and_qualification_refs_cannot_be_replaced(tmp_path,monkeypatch,evidence,fault):
    saved,_,calls,_=wired(tmp_path,monkeypatch,evidence)
    generation.start(saved.directory,"synthetic_v1")
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    generation.advance(saved.directory,"2026-10-16")
    file=saved.directory/"head.json"
    head=daily._read(file)
    if fault == "row_manifest":
        own=evidence["panels"]["2026-10-15"]
        own.write_text('{"changed_own_manifest":true}',encoding="utf-8")
    else:
        replacement=tmp_path/"another_qualification.json"
        write(replacement,{"different_qualification":True})
        head[generation.KEY]["ipo_evidence"]=daily._ref(replacement)
        write(file,head)
    with pytest.raises(ValueError):
        generation.advance(saved.directory,"2026-10-16")
    assert calls == ["register_generation","consume_generation"]


@pytest.mark.parametrize("outcome",["unstarted","failed"])
def test_old_saved_plan_without_success_never_launches_after_producer_moves(tmp_path,monkeypatch,evidence,outcome):
    saved,_,calls,behavior=wired(tmp_path,monkeypatch,evidence)
    generation.start(saved.directory,"synthetic_v1")
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    child=generation._saved_command
    if outcome == "unstarted":
        monkeypatch.setattr(generation,"_saved_command",lambda *a,**k: (_ for _ in ()).throw(OSError("parent interrupted")))
        with pytest.raises(OSError,match="parent interrupted"):
            generation.advance(saved.directory,"2026-10-16")
        monkeypatch.setattr(generation,"_saved_command",child)
    else:
        behavior["fail"]=True
        assert generation.advance(saved.directory,"2026-10-16")["status"] == "partial_generation_command_no_retry"
    original=list(calls)
    head=daily._read(saved.directory/"head.json")
    head[generation.producer.PROFILE_KEY].update(source_day="2026-10-19",publication_source_day="2026-10-19",
        latest_publication=daily._ref(evidence["pubs"][2]),
        publication_manifest=daily._ref(evidence["panels"]["2026-10-19"]),
        latest_manifest=daily._ref(evidence["panels"]["2026-10-19"]))
    write(saved.directory/"head.json",head)
    result=generation.advance(saved.directory,"2026-10-16")
    assert result["status"] == "partial_generation_command_no_retry" and calls == original
    assert daily._read(saved.directory/"head.json")[generation.KEY]["last_consumer_day"] is None
