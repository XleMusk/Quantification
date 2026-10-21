"""Synthetic CLI command wiring drives the actual shared Engine and journals."""
from copy import deepcopy
from pathlib import Path

from trading.research import eight32_paper_generation as generation
from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_split_forward as consumer
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_eight32_paper_generation import wired, next_source, clock
from trading.research.tests.test_leader_eight_type_forward_run import evidence
from trading.research.tests.test_system_daily_refresh import write


def test_coordinator_registration_source_only_settlement_and_later_pub_use_actual_engine(tmp_path, monkeypatch, evidence):
    saved, original, calls, _ = wired(tmp_path, monkeypatch, evidence)
    def child(step, operation, day, **options):
        receipt = operation / "commands" / (step["id"] + ".json")
        if receipt.exists():
            return daily._read(receipt)
        assert options.get("allow_start") is not False
        command = step["command"]
        calls.append(command[0])
        args = {flag: daily._values(command, "--" + flag.replace("_", "-"))[0]
            for flag in ("ipo_evidence", "research_master_run", "run_dir")}
        if command[0] == "leader-eight-type-forward-register":
            registrar.register(**args, **{flag: daily._values(command, "--" + flag.replace("_", "-"))[0]
                for flag in ("publication", "input_manifest", "out", "session_method")})
        else:
            result = consumer.run(daily._values(command, "--publication"),
                daily._values(command, "--panel")[0], **args,
                through=daily._values(command, "--through")[0],
                registration=daily._values(command, "--registration")[0])
            write(operation / "commands/consume_generation.stdout.txt", result)
            daily._outputs(step, day)
        result = {"status": "completed", "command": command}
        write(receipt, result)
        return result
    monkeypatch.setattr(generation, "_saved_command", child)
    initial = generation.start(saved.directory, "synthetic_real_engine_split", session_method="split-settlement-v1")
    run = Path(initial["generation"]["run_directory"])
    assert not run.exists()
    seed = generation.advance(saved.directory, "2026-10-15")["generation"]
    assert seed["current_case_count"] == 32
    assert all(not case["state"]["orders"] for case in daily._read(run / "head.json")["cases"].values())
    head = daily._read(saved.directory / "head.json")
    head[generation.producer.PROFILE_KEY].update(source_day="2026-10-16",
        latest_manifest=daily._ref(evidence["panels"]["2026-10-16"]))
    write(saved.directory / "head.json", head)
    clock(monkeypatch, "2026-10-16T18:00:00+08:00")
    settled = generation.advance(saved.directory, "2026-10-16")["generation"]
    assert settled["current_case_count"] == 32 and settled["missing_publication_days"] == ["2026-10-16"]
    before = daily._read(run / "head.json")
    assert any(case["state"]["fills"] for case in before["cases"].values())
    next_source(saved, evidence)
    attached = generation.advance(saved.directory, "2026-10-16")["generation"]
    assert attached["current_case_count"] == 32 and attached["missing_publication_days"] == []
    report = daily._read(attached["consumer_result"]["file"])
    assert report["new_order_count"] == report["new_fill_count"] == 0
    assert report["orders_generated"] is False and report["cumulative_orders_present"] is True
    after = daily._read(run / "head.json")
    for key, case in after["cases"].items():
        for name in ("cash", "positions", "orders", "fills", "prices", "receivables"):
            assert case["state"][name] == before["cases"][key]["state"][name]
        assert len(case["attachments"]) == 1
    count = len(calls)
    assert generation.advance(saved.directory, "2026-10-16")["command_called"] is False
    assert len(calls) == count
    system = daily._read(saved.directory / "head.json")
    assert {k: v for k, v in system.items() if k not in {generation.KEY, generation.producer.PROFILE_KEY}} == {
        k: v for k, v in original.items() if k != generation.producer.PROFILE_KEY}
