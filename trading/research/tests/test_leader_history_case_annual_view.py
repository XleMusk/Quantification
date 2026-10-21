"""Durable annual saved-result adapter only; no market inputs or Engine."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_eight_type_historical_native as native
from trading.research.leader_eight_type_case_replay import METHOD
from trading.research.tests.test_leader_eight_type_history_view import SELECTED, save
from trading.research.tests.test_leader_history_annual_view import annual_fixture, TREND
from trading.research.tests.test_leader_history_new_layers_view import (
    six_windows, saved_identity_layer, saved_secondary_layer,
)


def eight_saved_windows(tmp_path):
    fixture = six_windows(tmp_path)
    identity_path, _, _, _ = saved_identity_layer(tmp_path, fixture)
    identity_view = fixture.previous.parent / "with_identity"
    view.build_identity_overlay(identity_path, fixture.previous, identity_view)
    secondary_path, _, _, _ = saved_secondary_layer(tmp_path, fixture)
    output = fixture.previous.parent / "with_secondary"
    view.build_secondary_supplement(secondary_path, identity_view, output)
    return output, fixture.data


def durable_annual(tmp_path):
    path, record = annual_fixture(tmp_path / "durable")

    def year(value):
        if isinstance(value, dict):
            return {year(key): year(item) for key, item in value.items()}
        if isinstance(value, list):
            return [year(item) for item in value]
        return value.replace("2025-", "2024-") if isinstance(value, str) else value

    record = year(record)
    inputs_path = tmp_path / "durable_inputs" / "manifest.json"
    payload_keys = ("raw_file", "raw_sha256", "panels_file", "panels_sha256",
                    "context_raw_file", "context_raw_sha256", "calendar", "calendar_sha256")
    manifest = {"schema": native.INPUT_SCHEMA, "method_id": native.METHOD_ID,
        "fixed_protocol": deepcopy(view.PROTOCOL),
        **{key: deepcopy(record[key]) for key in ("start", "end", "source_days", "scope_members")},
        "through": record["source_days"][-1],
        **{key: "a" * 64 if key.endswith("sha256") else str(tmp_path / "never_read" / key)
           for key in payload_keys}, **native.FLAGS}
    save(inputs_path, manifest)
    manifest_ref = view._read(inputs_path)[1]
    event_path, reference_path = [tmp_path / name / "result.json" for name in ("events", "references")]
    save(event_path, {"status": "research_action_events_projected", "execution_admitted": False})
    save(reference_path, {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event_path.parent.resolve())})
    registration_path = tmp_path / "registration" / "registration.json"
    registration = {"schema": METHOD + "_registration", "method_id": METHOD,
        "protocol": deepcopy(view.PROTOCOL), "config": deepcopy(record["config"]),
        "case_ids": list(view.CASE_IDS), "check_entry_readiness": True,
        "same_case_runner": "leader_eight_type_panel_replay._case", "session_checkpoint_supported": False,
        "case_selection_by_return": False, "inputs": manifest_ref,
        "input_payload_pins": {key: manifest[key] for key in payload_keys},
        "action_event_view": view._read(event_path)[1],
        "action_reference_view": view._read(reference_path)[1], **native.FLAGS}
    save(registration_path, registration)
    registration_ref = view._read(registration_path)[1]
    preparation_path = path.parent / "preparation.json"
    save(preparation_path, {"registration": registration_ref,
        "environment_decisions": deepcopy(record["environment_decisions"])})
    preparation_ref = view._read(preparation_path)[1]
    for case in record["cases"]:
        case["historical_execution_policy"] = {"accounting": {
            "event_view": str(event_path.parent.resolve()),
            "reference_view": str(reference_path.parent.resolve())}}
        if case["case_id"] == TREND:
            case["metrics"] = {"net_return": -.17, "max_drawdown": -.22}
        if case["case_id"] == SELECTED:
            case["blocking_evidence"] = {"day": record["source_days"][1], "reason": "saved_unknown_action"}
    record.update(schema=METHOD + "_result", method_id=METHOD, registration=registration_ref,
        preparation=preparation_ref, single_case_session_checkpoint_supported=False,
        source={"method_id": native.METHOD_ID, "historical_input_schema": native.INPUT_SCHEMA,
            "manifest": manifest_ref["path"], "manifest_sha256": manifest_ref["sha256"],
            "input_record": deepcopy(manifest)}, **native.FLAGS)
    save(path, record)
    progress = {"schema": METHOD + "_progress", "method_id": METHOD, "status": "completed",
        "registration": registration_ref, "result": view._read(path)[1], "preparation": preparation_ref,
        "run_directory": str(path.parent.resolve()), "active_case": None,
        "case_ids": list(view.CASE_IDS), "completed_cases": [{"case_id": case_id} for case_id in view.CASE_IDS],
        "errors": []}
    save(path.parent / "progress.json", progress)
    return path, record, registration_path, progress


def test_durable_year_appends_32_and_preserves_all_272_saved_details_without_market_reads(tmp_path, monkeypatch):
    previous, data = eight_saved_windows(tmp_path)
    path, source, _, _ = durable_annual(tmp_path)
    old_index, _ = view._read(previous / "index.json")
    old_details = {file.relative_to(previous): file.read_bytes()
        for file in (previous / "windows").rglob("*.json")}
    assert len(old_index["windows"]) == 8 and len(old_details) == 272
    output, original_read = previous.parent / "with_2024", view._read

    def no_market_or_old_sources(file):
        resolved = Path(file).resolve()
        assert resolved.suffix == ".json" and "never_read" not in resolved.parts
        assert not resolved.is_relative_to((tmp_path / "sources").resolve())
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", no_market_or_old_sources)
        built = view.build_case_annual(path, previous, output)
    assert built["case_count"] == 304
    assert all((output / relative).read_bytes() == content for relative, content in old_details.items())
    new_index, _ = view._read(output / "index.json")
    old_windows = {window["id"]: window for window in old_index["windows"]}
    for window in new_index["windows"]:
        if window["id"] in old_windows:
            assert window == old_windows[window["id"]]
    view.publish(output, data)

    def projection_only(file):
        assert Path(file).resolve().is_relative_to(data.resolve())
        return original_read(file)

    with monkeypatch.context() as guarded:
        guarded.setattr(view, "_read", projection_only)
        readback = view.read_history(data, "annual32_2024", SELECTED)
    assert readback["status"] == "saved_history_projection"
    window = next(row for row in readback["windows"] if row["id"] == "annual32_2024")
    assert len(readback["windows"]) == 9 and window["case_count"] == 32
    assert window["independent_account"] is True and window["equity_series_concatenated"] is False
    assert window["source_scope_count"] == 2 and len(window["tail_days"]) == 6
    rows = {row["case_id"]: row for row in window["case_rows"]}
    assert rows[TREND]["metrics"] == {"net_return": -.17, "max_drawdown": -.22}
    assert rows[SELECTED]["metrics"] is None
    assert rows[SELECTED]["blocking_evidence"] == source["cases"][view.CASE_IDS.index(SELECTED)]["blocking_evidence"]
    assert rows[SELECTED]["residual_positions"] == ["002491.SZSE"]
    assert any(day["order_blocks"] for day in readback["detail"]["days"])
    assert all(readback[key] is False for key in ("engine_advanced", "ledger_mutated", "profits_recomputed",
        "provider_contacted", "execution_admitted", "returns_reportable", "pit_verified"))


def test_durable_pending_and_rehashed_bad_source_or_protocol_never_publish(tmp_path):
    path, record, registration_path, progress = durable_annual(tmp_path)
    previous, data = tmp_path / "unread_previous", tmp_path / "data"
    missing, output = tmp_path / "missing" / "result.json", data / "pending"
    assert view.build_case_annual(missing, previous, output)["status"] == "pending"
    running = deepcopy(progress)
    running["status"] = "running"
    save(path.parent / "progress.json", running)
    assert view.main(["--case-annual-result", str(path), "--reuse-projection", str(previous),
        "--out", str(output), "--publish", "--data-root", str(data)]) == 0
    assert not output.exists() and not (data / "research/leader_eight_type_history_views/current.json").exists()
    save(path.parent / "progress.json", progress)

    registration = view._read(registration_path)[0]
    registration["inputs"]["sha256"] = "0" * 64
    save(registration_path, registration)
    with pytest.raises(ValueError, match="source pin changed"):
        view.build_case_annual(path, previous, data / "bad_pin")
    # Even consistent top-level result/progress hashes cannot legitimize changed costs/protocol.
    record["registration"] = view._read(registration_path)[1]
    record["protocol"] = {**record["protocol"], "unregistered_change": True}
    save(path, record)
    progress.update(registration=record["registration"], result=view._read(path)[1])
    save(path.parent / "progress.json", progress)
    with pytest.raises(ValueError, match="exact terminal registered"):
        view.build_case_annual(path, previous, data / "bad_protocol")
    assert not (data / "bad_pin").exists() and not (data / "bad_protocol").exists()
