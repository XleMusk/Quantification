"""New forward registrations from synthetic actual publications; no market IO."""
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_forward_run as consumer
from trading.research.tests.test_leader_eight_type_forward_run import (
    evidence, head, RELAY,
)


def setup_registration(evidence, monkeypatch, stamp="2026-10-15T22:00:00+08:00"):
    publication = evidence["pubs"][0]
    own = evidence["panels"]["2026-10-15"]
    root = own.parent
    ipo, master = root / "ipo_proof.json", root / "master_run.json"
    for path in (ipo, master):
        path.write_text(json.dumps({"synthetic_input_only": True}), encoding="utf-8")
    out = root / "new_forward_registration.json"
    run = root / "independent_new_books"
    now = datetime.fromisoformat(stamp)
    monkeypatch.setattr(registrar, "_now", lambda: now)
    return {"publication": publication, "input_manifest":own,
        "ipo_evidence":ipo, "research_master_run":master, "out":out, "run_dir":run}


def test_new_registration_is_readable_by_original_consumer_and_creates_no_account(evidence, monkeypatch):
    options = setup_registration(evidence, monkeypatch)
    frozen = {p:p.read_bytes() for p in (options["publication"], options["input_manifest"], evidence["registration"])}
    result = registrar.register(**options)
    value = json.loads(options["out"].read_text(encoding="utf-8"))
    assert result["status"] == "registered_independent_eight_type_forward_only"
    assert Path(result["registration_file"]) == options["out"].resolve()
    assert value["first_decision_day"] == "2026-10-15"
    assert value["first_execution_day"] == "2026-10-16"
    assert value["case_ids"] == list(consumer.CASE_IDS) and len(value["case_ids"]) == 32
    assert value["config"] == consumer._config(100000, 5).to_dict()
    assert all(value[k] is False for k in ("execution_admitted", "selection_enabled", "strategy_validated"))
    now = datetime.fromisoformat("2026-10-15T22:00:00+08:00")
    first = consumer._publication(options["publication"], options["ipo_evidence"], options["research_master_run"], now, {})
    checked = consumer._registration(options["out"], first, consumer._config(100000, 5), now)
    assert checked["file"] == str(options["out"].resolve())
    assert not options["run_dir"].exists() and not evidence["run"].exists()
    assert all(p.read_bytes() == content for p,content in frozen.items())


@pytest.mark.parametrize("fault", ["schema", "own_hash", "another_manifest"])
def test_bad_or_mismatched_publication_is_rejected_before_registration(evidence, monkeypatch, fault):
    options = setup_registration(evidence, monkeypatch)
    if fault == "schema":
        record = json.loads(options["publication"].read_text(encoding="utf-8"))
        record["schema"] = "another_publication_contract"
        options["publication"].write_text(json.dumps(record), encoding="utf-8")
    elif fault == "own_hash":
        options["input_manifest"].write_text('{"different_bytes":true}', encoding="utf-8")
    else:
        options["input_manifest"] = evidence["panels"]["2026-10-16"]
    with pytest.raises(ValueError):
        registrar.register(**options)
    assert not options["out"].exists() and not options["run_dir"].exists()


@pytest.mark.parametrize("stamp", ["2026-10-15T18:44:00+08:00", "2026-10-16T09:30:00+08:00"])
def test_registration_cannot_precede_publication_or_miss_first_open(evidence, monkeypatch, stamp):
    options = setup_registration(evidence, monkeypatch, stamp)
    with pytest.raises(ValueError):
        registrar.register(**options)
    assert not options["out"].exists() and not options["run_dir"].exists()


@pytest.mark.parametrize("collision", ["out", "run_dir"])
def test_existing_registration_or_book_is_never_overwritten(evidence, monkeypatch, collision):
    options = setup_registration(evidence, monkeypatch)
    if collision == "out":
        options["out"].write_text("preserve_original_registration", encoding="utf-8")
        old = options["out"].read_bytes()
    else:
        options["run_dir"].mkdir()
        (options["run_dir"] / "head.json").write_text("preserve_original_book", encoding="utf-8")
        old = (options["run_dir"] / "head.json").read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        registrar.register(**options)
    if collision == "out":
        assert options["out"].read_bytes() == old and not options["run_dir"].exists()
    else:
        assert (options["run_dir"] / "head.json").read_bytes() == old and not options["out"].exists()


def test_registered_new_generation_consumes_original_engine_path_and_resumes(evidence, monkeypatch):
    options = setup_registration(evidence, monkeypatch)
    registrar.register(**options)
    assert not options["run_dir"].exists()
    result = consumer.run(evidence["pubs"][:2], evidence["panels"]["2026-10-16"],
        options["ipo_evidence"], options["research_master_run"], options["run_dir"],
        registration=options["out"])
    assert result["newly_committed_case_sessions"] == 64
    book = {**evidence, "run": options["run_dir"]}
    saved = head(book)
    assert len(saved["cases"]) == 32
    relay = saved["cases"][RELAY]
    assert [row["date"] for row in relay["daily"]] == ["2026-10-15", "2026-10-16"]
    assert len(relay["state"]["orders"]) == 5
    assert {row["day"] for row in relay["state"]["fills"].values()} == {"2026-10-16"}
    before = (options["run_dir"] / "head.json").read_bytes()
    again = consumer.run(evidence["pubs"][:2], evidence["panels"]["2026-10-16"],
        options["ipo_evidence"], options["research_master_run"], options["run_dir"], registration=options["out"])
    assert again["newly_committed_case_sessions"] == 0
    assert (options["run_dir"] / "head.json").read_bytes() == before
    assert not evidence["run"].exists()


def test_new_method_binds_per_case_quality_and_allows_coordinator_artifact_parent(evidence, monkeypatch):
    options = setup_registration(evidence, monkeypatch)
    # A saved system coordinator is not a consumer account tree. Its unrelated
    # head is unchanged when a new registration artifact is written below it.
    coordinator = options["out"].parent / "head.json"
    coordinator.write_text(json.dumps({"schema": "system_daily_saved_coordinator_v1",
        "profile": {"last_source_day": "2026-10-15"}}), encoding="utf-8")
    before = coordinator.read_bytes()
    result = registrar.register(**options)
    record = json.loads(options["out"].read_text(encoding="utf-8"))
    assert record["generation_method_id"] == "leader_eight_type_explicit_new_generation_per_case_start_v1"
    assert record["initial_quality_scope"] == "per_case"
    assert record["registered_run_dir"] == str(options["run_dir"].resolve())
    assert result["generation_method_id"] == record["generation_method_id"]
    assert coordinator.read_bytes() == before and not options["run_dir"].exists()
