"""One incomplete execution source blocks only dependent new-generation cases."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from trading.research import leader_eight_type_forward_run as forward
from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_paper as paper
from trading.research import system_daily_refresh as daily
from trading.research.tests.test_leader_eight_type_forward_run import evidence, RELAY, SYMBOLS
from trading.research.tests.test_leader_eight_type_forward_registration import setup_registration


def split_fixture_targets(evidence):
    # Relay requires five different stocks; other types require only stock six.
    # This is synthetic source construction, not filtering real failed trades.
    for value in evidence["loaded"].values():
        for panel in value["panel_by_day"].values():
            for item in panel["types"]:
                item["candidates"] = item["candidates"][:5] if item["id"] == "relay_continuation_leader" else item["candidates"][-1:]
        value["raw"].loc[value["raw"].date.eq("2026-10-16") & value["raw"].code.eq("sz.000001"), "preclose"] = "9"
    for path, day in zip(evidence["pubs"], ("2026-10-15", "2026-10-16", "2026-10-19")):
        value = evidence["loaded"][str(evidence["panels"][day])]
        observed = datetime.fromisoformat(day + "T18:30:00+08:00")
        source_day, execution, environment, prepared = paper._prepare(value, observed)
        record = paper._finalize(source_day, execution, environment, prepared, value["_bindings"],
            datetime.fromisoformat(day + "T18:45:00+08:00"))
        path.write_text(json.dumps(record), encoding="utf8")


def consume(evidence, options, directory=None):
    return forward.run(evidence["pubs"][:2], evidence["panels"]["2026-10-16"],
        options["ipo_evidence"], options["research_master_run"], directory or options["run_dir"],
        registration=options["out"])


def test_partial_first_execution_preserves_failed_case_and_healthy_engine_books(evidence, monkeypatch):
    original_registration = evidence["registration"].read_bytes()
    split_fixture_targets(evidence)
    options = setup_registration(evidence, monkeypatch)
    registrar.register(**options)
    declaration = json.loads(options["out"].read_text(encoding="utf8"))
    legacy = deepcopy(declaration)
    for field in ("generation", "generation_method_id", "initial_quality_scope", "registered_run_dir"):
        legacy.pop(field, None)
    old_registration = options["out"].parent / "synthetic_legacy_startup.json"
    old_registration.write_text(json.dumps(legacy), encoding="utf8")
    old_run = options["out"].parent / "synthetic_legacy_books"
    waiting = forward.run(evidence["pubs"][:2], evidence["panels"]["2026-10-16"],
        options["ipo_evidence"], options["research_master_run"], old_run, registration=old_registration)
    assert waiting["status"] == "waiting_for_required_execution_quality_evidence"
    assert not old_run.exists()  # The old registration's original startup semantics remain.

    result = consume(evidence, options)
    assert result["status"] == "advanced_research_only_with_blocked_cases"
    assert 0 < result["newly_committed_case_sessions"] < 64
    path = options["run_dir"] / "head.json"
    saved = json.loads(path.read_text(encoding="utf8"))
    assert len(saved["cases"]) == 32
    failed = saved["cases"][RELAY]
    assert failed["state"]["day"] is None and not failed["state"]["orders"]
    assert failed["blocking_evidence"] and failed["daily"] == []
    healthy = saved["cases"]["space_height_leader__unguarded__uncapped"]
    assert healthy["state"]["day"] == "2026-10-16"
    assert healthy["state"]["orders"] and healthy["state"]["fills"]
    assert {fill["symbol"] for fill in healthy["state"]["fills"].values()} == {SYMBOLS[-1]}
    assert result["metrics"] is None
    step = {"command": ["leader-eight-type-forward", "--registration", str(options["out"]),
        "--run-dir", str(options["run_dir"])], "expected_files": [str(path)]}
    assert daily._outputs(step, "2026-10-16")[0]["file"] == str(path)
    preserved = path.read_bytes()
    assert consume(evidence, options)["newly_committed_case_sessions"] == 0
    assert path.read_bytes() == preserved
    saved["cases"][RELAY]["blocking_evidence"] = None
    path.write_text(json.dumps(saved), encoding="utf8")
    with pytest.raises(ValueError, match="partial-case evidence"):
        daily._outputs(step, "2026-10-16")
    assert evidence["registration"].read_bytes() == original_registration
    assert not evidence["run"].exists()


@pytest.mark.parametrize("fault", ["another_directory", "changed_scope"])
def test_explicit_generation_cannot_move_directory_or_change_startup_scope(evidence, monkeypatch, fault):
    options = setup_registration(evidence, monkeypatch)
    registrar.register(**options)
    directory = options["run_dir"].with_name("another_generation")
    if fault == "changed_scope":
        directory = options["run_dir"]
        value = json.loads(options["out"].read_text(encoding="utf8"))
        value["initial_quality_scope"] = "shared"
        options["out"].write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="New generation"):
        consume(evidence, options, directory)
    assert not directory.exists() and not options["run_dir"].exists()
