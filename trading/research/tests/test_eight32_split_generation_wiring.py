"""Explicit split coordination uses saved synthetic child outcomes, no market IO.

Engine phase behaviour is covered by its own tests; these check method selection,
exact input operations and recovery without repeating child execution.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from trading.research import eight32_paper_generation as generation
from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_forward_run as forward
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_eight32_paper_generation import wired, next_source, clock
from trading.research.tests.test_leader_eight_type_forward_run import evidence
from trading.research.tests.test_system_daily_refresh import write


def split_wired(tmp_path, monkeypatch, evidence):
    saved, original, calls, behavior = wired(tmp_path, monkeypatch, evidence)
    def child(step, operation, day, **kwargs):
        receipt = operation / "commands" / (step["id"] + ".json")
        if receipt.exists():
            return daily._read(receipt)
        if kwargs.get("allow_start") is False:
            return {"status":"missed_publication_window_no_producer_started"}
        calls.append(step["id"])
        command=step["command"]
        if behavior["fail"]:
            result={"status":"failed_no_automatic_retry","command":command}
        elif step["id"] == "register_generation":
            registrar.register(**{name:daily._values(command,"--"+flag)[0] for name,flag in
                (("publication","publication"),("input_manifest","input-manifest"),("ipo_evidence","ipo-evidence"),
                 ("research_master_run","research-master-run"),("out","out"),("run_dir","run-dir"))},
                session_method=daily._values(command,"--session-method")[0])
            result={"status":"completed","command":command}
        else:
            registration_file=daily._values(command,"--registration")[0]
            record=daily._read(registration_file)
            directory=Path(daily._values(command,"--run-dir")[0]).resolve()
            pubs=[daily._read(file) for file in daily._values(command,"--publication")]
            last=pubs[-1]["source_close_day"]
            cases={key:{"state":{"day":day},"blocking_evidence":None,"error":None,
                "last_publication_day":last,"missing_publication_days":[] if last==day else [day]}
                for key in forward.CASE_IDS}
            first=next(iter(cases))
            cases[first].update(state={"day":None},blocking_evidence={"field":"reference","session_not_committed":True})
            binding={"run_directory":str(directory),"config":record["config"],
                "forward_method_registration":daily._ref(registration_file),
                "explicit_new_generation":{"method_id":registrar.SPLIT_GENERATION_METHOD_ID,
                    "session_method_id":registrar.SPLIT_SESSION_METHOD_ID,"initial_quality_scope":"per_case",
                    "registered_run_dir":str(directory)}}
            account=directory/"head.json"
            write(account,{"schema":"leader_eight_type_split_forward_checkpoint_v1", "binding":binding,
                "cases":cases,**forward.FLAGS})
            write(operation/"commands/consume_generation.stdout.txt",{
                "schema":"leader_eight_type_split_forward_result_v1","status":"split_advanced_research_only",
                "case_count":32,"checkpoint":str(account),**forward.FLAGS})
            result={"status":"completed","command":command}
        write(receipt,result)
        return result
    monkeypatch.setattr(generation,"_saved_command",child)
    return saved, original, calls, behavior


def start(saved):
    return generation.start(saved.directory,"synthetic_split_v1",session_method="split-settlement-v1")


def test_split_is_explicit_new_registration_seed_without_future_raw(tmp_path,monkeypatch,evidence):
    saved, original, calls, _ = split_wired(tmp_path,monkeypatch,evidence)
    registered=start(saved)["generation"]
    record=daily._read(registered["registration"]["file"])
    assert record["generation_method_id"] == registrar.SPLIT_GENERATION_METHOD_ID
    assert record["session_method_id"] == registrar.SPLIT_SESSION_METHOD_ID
    assert not Path(registered["run_directory"]).exists()
    result=generation.advance(saved.directory,"2026-10-15")["generation"]
    assert result["last_consumer_day"] == "2026-10-15" and result["current_case_count"] == 31
    assert result["blocked_case_count"] == 1 and result["metrics"] is None
    current=daily._read(saved.directory/"head.json")
    assert {k:v for k,v in current.items() if k != generation.KEY} == original
    assert calls == ["register_generation","consume_generation"]
    with pytest.raises(ValueError,match="replaced or reset"):
        generation.start(saved.directory,"synthetic_split_v1")


def test_context_E_settles_then_actual_E_publication_gets_distinct_operation(tmp_path,monkeypatch,evidence):
    saved, _, calls, _ = split_wired(tmp_path,monkeypatch,evidence)
    start(saved)
    generation.advance(saved.directory,"2026-10-15")
    head=daily._read(saved.directory/"head.json")
    profile=head[generation.producer.PROFILE_KEY]
    profile.update(source_day="2026-10-16", latest_manifest=daily._ref(evidence["panels"]["2026-10-16"]))
    write(saved.directory/"head.json",head)
    clock(monkeypatch,"2026-10-16T18:00:00+08:00")
    result=generation.advance(saved.directory,"2026-10-16")["generation"]
    assert result["last_consumer_day"] == "2026-10-16"
    assert result["missing_publication_days"] == ["2026-10-16"]
    assert [r["day"] for r in result["publications"]] == ["2026-10-15"]
    count=len(calls)
    assert generation.advance(saved.directory,"2026-10-16")["command_called"] is False
    assert len(calls) == count
    next_source(saved,evidence)
    attached=generation.advance(saved.directory,"2026-10-16")["generation"]
    assert attached["missing_publication_days"] == []
    assert [r["day"] for r in attached["publications"]] == ["2026-10-15","2026-10-16"]
    assert len(calls) == count+1
    operations=list((saved.directory/"eight32_generations/synthetic_split_v1/advance_split/20261016").glob("*/plan.json"))
    assert len(operations) == 2
    commands=[daily._read(file)["command"] for file in operations]
    assert all(c[0] == "leader-eight-type-split-forward" for c in commands)
    assert sorted(len(daily._values(c,"--publication")) for c in commands) == [1,2]


def test_successful_split_child_recovers_parent_after_producer_moves(tmp_path,monkeypatch,evidence):
    saved, _, calls, _ = split_wired(tmp_path,monkeypatch,evidence)
    start(saved)
    next_source(saved,evidence)
    clock(monkeypatch,"2026-10-16T20:00:00+08:00")
    atomic=daily._atomic
    def interrupted(file,value):
        if Path(file) == saved.directory/"head.json":
            raise OSError("parent interrupted")
        return atomic(file,value)
    monkeypatch.setattr(daily,"_atomic",interrupted)
    with pytest.raises(OSError,match="parent interrupted"):
        generation.advance(saved.directory,"2026-10-16")
    monkeypatch.setattr(daily,"_atomic",atomic)
    head=daily._read(saved.directory/"head.json")
    head[generation.producer.PROFILE_KEY].update(source_day="2026-10-19", publication_source_day="2026-10-19",
        latest_publication=daily._ref(evidence["pubs"][2]), latest_manifest=daily._ref(evidence["panels"]["2026-10-19"]),
        publication_manifest=daily._ref(evidence["panels"]["2026-10-19"]))
    write(saved.directory/"head.json",head)
    before=list(calls)
    recovered=generation.advance(saved.directory,"2026-10-16")
    assert recovered["generation"]["last_consumer_day"] == "2026-10-16" and calls == before
    assert daily._read(saved.directory/"head.json")[generation.producer.PROFILE_KEY]["source_day"] == "2026-10-19"


def test_split_outputs_reject_unexplained_lag_and_wrong_method(tmp_path,monkeypatch,evidence):
    saved, _, _, _=split_wired(tmp_path,monkeypatch,evidence)
    start(saved)
    result=generation.advance(saved.directory,"2026-10-15")["generation"]
    file=Path(result["account_head"]["file"])
    operation=Path(result["last_operation"]).parent
    plan=daily._read(operation/"plan.json")
    step={"command":plan["command"],"expected_files":[str(file)]}
    original=daily._read(file)
    first=next(iter(original["cases"]))
    bad=deepcopy(original)
    bad["cases"][first]["blocking_evidence"]=None
    write(file,bad)
    with pytest.raises(ValueError,match="case failures"):
        daily._outputs(step,"2026-10-15")
    bad=deepcopy(original)
    bad["binding"]["explicit_new_generation"]["session_method_id"]="other_method"
    write(file,bad)
    with pytest.raises(ValueError,match="case failures"):
        daily._outputs(step,"2026-10-15")


def test_failed_split_operation_cannot_be_retried_with_new_publication_input(tmp_path,monkeypatch,evidence):
    saved, _, calls, behavior=split_wired(tmp_path,monkeypatch,evidence)
    start(saved)
    head=daily._read(saved.directory/"head.json")
    head[generation.producer.PROFILE_KEY].update(source_day="2026-10-16",
        latest_manifest=daily._ref(evidence["panels"]["2026-10-16"]))
    write(saved.directory/"head.json",head)
    clock(monkeypatch,"2026-10-16T18:00:00+08:00")
    behavior["fail"]=True
    assert generation.advance(saved.directory,"2026-10-16")["status"] == "partial_generation_command_no_retry"
    original=list(calls)
    next_source(saved,evidence)
    behavior["fail"]=False
    again=generation.advance(saved.directory,"2026-10-16")
    assert again["status"] == "partial_generation_command_no_retry" and calls == original
    assert daily._read(saved.directory/"head.json")[generation.KEY]["last_consumer_day"] is None


def saved_input_context(tmp_path, monkeypatch):
    # Producer fixtures create only local synthetic stage artifacts. Restore the
    # real terminal verifier below, so a JSON "completed" label is insufficient.
    from trading.research.tests import test_leader_eight_type_daily as fixtures
    verify_terminal = daily._terminal
    e = fixtures.wired(tmp_path,monkeypatch)
    fixtures.clock(monkeypatch,fixtures.E+"T18:45:00+08:00")
    head,registration=daily._load(e.saved.directory)
    sessions,_=generation._calendar(registration)
    now=datetime.fromisoformat(fixtures.E+"T18:45:00+08:00")
    parent=generation.producer._bootstrap(head,registration,sessions,now)
    head[generation.producer.PROFILE_KEY]=parent
    write(e.saved.directory/"head.json",head)
    e.behavior["fail"]="paper32"
    result=generation.producer.advance(e.saved.directory,fixtures.E,e.packages[fixtures.E],source_only=False)
    assert result["status"] == "partial_eight32_production_no_retry"
    operation=e.saved.directory/"eight32_production"/fixtures.E.replace("-","")
    plan_file=operation/"plan.json"
    plan=daily._read(plan_file)
    receipt=daily._read(operation/"receipt.json")
    _,steps=generation.producer._steps(parent,fixtures.E,e.packages[fixtures.E],operation,False)
    for step in steps[:-1]:
        bound={**step,"binding":{"kind":"eight32_independent_production","registration":head["registration"],
            "source":plan["source"],"plan":daily._ref(plan_file)}}
        spec=daily._step_spec(bound)
        paths=daily.command_child.paths(operation/"commands",step["id"])
        argv=daily._command(step["command"])
        request={"schema":daily.command_child.REQUEST_SCHEMA,"day":fixtures.E,"step":spec,"argv":argv,
            "cwd":str(daily.ROOT),"binding":bound["binding"],**daily.FLAGS}
        request_ref=write(paths["request"],request)
        started_ref=write(paths["started"],{"schema":daily.command_child.START_SCHEMA,
            "request_ref":request_ref,"supervisor_pid":12345})
        terminal_ref=write(paths["terminal"],{"schema":daily.command_child.TERMINAL_SCHEMA,
            "request_ref":request_ref,"started_ref":started_ref,"day":fixtures.E,"step":spec,"argv":argv,
            "supervisor_pid":12345,"status":"completed","exit_code":0,"execution_progress_unknown":False,
            "output_refs":daily._outputs(bound,fixtures.E),**daily.FLAGS})
        saved={"command":step["command"],"status":"completed","day":fixtures.E,"step":step["id"],
            "argv":argv,"expected_files":spec["expected_files"],"child_request_ref":request_ref,
            "child_terminal_ref":terminal_ref,"child_pid":12345}
        write(operation/"commands"/(step["id"]+".json"),saved)
        receipt["steps"][step["id"]]=saved
    write(operation/"receipt.json",receipt)
    monkeypatch.setattr(daily,"_terminal",verify_terminal)
    return e,head,registration,sessions,now,operation


@pytest.mark.parametrize("terminal_present",[True,False])
def test_split_reads_only_exact_successful_inputs_when_original_paper_failed(tmp_path,monkeypatch,terminal_present):
    e,head,registration,sessions,now,operation=saved_input_context(tmp_path,monkeypatch)
    terminal=daily.command_child.paths(operation/"commands","eight_inputs")["terminal"]
    if not terminal_present:
        terminal.unlink()
    original_head=(e.saved.directory/"head.json").read_bytes()
    original_receipt=(operation/"receipt.json").read_bytes()
    original_calls=list(e.calls)
    if terminal_present:
        context=generation._split_source_profile(e.saved.directory,head,registration,sessions,now,"2026-10-20")
        assert context["source_day"] == "2026-10-20"
        assert context["publication_source_day"] == "2026-10-19"
        assert context["latest_publication"] == head[generation.producer.PROFILE_KEY]["latest_publication"]
        assert context["latest_manifest"] == daily._ref(operation/"artifacts/eight_inputs/manifest.json")
    else:
        with pytest.raises(FileNotFoundError):
            generation._split_source_profile(e.saved.directory,head,registration,sessions,now,"2026-10-20")
    assert (e.saved.directory/"head.json").read_bytes() == original_head
    assert (operation/"receipt.json").read_bytes() == original_receipt
    assert e.calls == original_calls
