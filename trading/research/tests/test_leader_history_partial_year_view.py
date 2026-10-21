"""The registered partial-year saved layer never becomes an annual backtest."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research import leader_eight_type_historical_native as native
from trading.research import leader_partial_year_inputs as partial
from trading.research.tests.test_leader_eight_type_history_view import save, SELECTED
from trading.research.tests.test_leader_history_case_annual_view import eight_saved_windows, durable_annual
from trading.research.tests.test_leader_history_known_scope_view import saved_continuation


def partial_source(tmp_path):
    path, source, registration_path, progress = durable_annual(tmp_path / "partial")
    calendar_path = tmp_path / "calendar_2023.json"
    calendar = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2023,
        "closed_ranges": ["2023-01-01/2023-01-02", "2023-01-21/2023-01-27",
            "2023-04-05/2023-04-05", "2023-04-29/2023-05-03", "2023-06-22/2023-06-24",
            "2023-09-29/2023-10-06"], "open_sessions": []}
    save(calendar_path, calendar)
    calendar_ref = view._read(calendar_path)[1]
    sessions, _, _ = native._calendar_view(calendar, "2023-01-01", "2023-12-31", [])
    _, warmup, days = partial._schedule(calendar, sessions)
    mapped_days = [days[0], days[1], *days[-7:]]
    remap = dict(zip(source["source_days"], mapped_days))

    def dated(value):
        if isinstance(value, dict):
            return {remap.get(key, key): dated(item) for key, item in value.items()}
        if isinstance(value, list):
            return [dated(item) for item in value]
        return remap.get(value, value) if isinstance(value, str) else value

    source = dated(source)
    source.update(start=partial.START, end=partial.END, source_days=days)
    old_environment = next(iter(source["environment_decisions"].values()))
    source["environment_decisions"] = {day: {**deepcopy(old_environment), "day": day} for day in days}
    registration = view._read(registration_path)[0]
    source_registration = tmp_path / "source_registration/registration.json"
    source_record = {"schema": partial.METHOD_ID + "_registration", "method_id": partial.METHOD_ID,
        "registered_at": "2026-10-18T20:00:00+08:00", "spec": deepcopy(partial.SPEC),
        "protocol": deepcopy(view.PROTOCOL), "case_ids": list(view.CASE_IDS),
        "config": source["config"], "source_days": days, "warmup_source_days": warmup,
        "all_official_year_sessions": sessions,
        "source_pins": {"calendar": calendar_ref, "action_event": registration["action_event_view"],
            "action_reference": registration["action_reference_view"]},
        "action_event_view": str(Path(registration["action_event_view"]["path"]).parent),
        "action_reference_view": str(Path(registration["action_reference_view"]["path"]).parent), **native.FLAGS}
    save(source_registration, source_record)
    manifest_path = Path(registration["inputs"]["path"])
    manifest = view._read(manifest_path)[0]
    manifest.update(start=partial.START, end=partial.END, through=partial.THROUGH, source_days=days,
        calendar=calendar_ref["path"], calendar_sha256=calendar_ref["sha256"],
        created_at="2026-10-18T20:01:00+08:00", warmup_source_days=warmup,
        partial_year_source_registration=view._read(source_registration)[1],
        partial_year_source_method=partial.METHOD_ID, partial_year_semantics=deepcopy(partial.SPEC))
    save(manifest_path, manifest)
    manifest_ref = view._read(manifest_path)[1]
    registration.update(inputs=manifest_ref, registered_at="2026-10-18T20:02:00+08:00")
    registration["input_payload_pins"].update(calendar=calendar_ref["path"], calendar_sha256=calendar_ref["sha256"])
    save(registration_path, registration)
    registration_ref = view._read(registration_path)[1]
    preparation_path = Path(source["preparation"]["path"])
    save(preparation_path, {"registration": registration_ref,
        "environment_decisions": source["environment_decisions"]})
    preparation_ref = view._read(preparation_path)[1]
    source.update(registration=registration_ref, preparation=preparation_ref)
    source["source"].update(manifest=manifest_ref["path"], manifest_sha256=manifest_ref["sha256"],
        input_record=deepcopy(manifest))
    save(path, source)
    progress.update(registration=registration_ref, result=view._read(path)[1], preparation=preparation_ref)
    save(path.parent / "progress.json", progress)
    return path, source_registration, source, progress


def eleven_saved_windows(tmp_path):
    previous, data = eight_saved_windows(tmp_path)
    annual_path, _, _, _ = durable_annual(tmp_path / "existing_2024")
    annual_view = previous.parent / "with_2024"
    view.build_case_annual(annual_path, previous, annual_view)
    paths = [saved_continuation(tmp_path / "known", month)[0] for month in ("2025-01", "2026-04")]
    final = previous.parent / "with_known_periods"
    view.build_known_scope_continuations(paths, annual_view, final)
    return final, data


def test_partial_cli_appends_32_to_11_windows_preserving_312_detail_bytes_and_nulls(tmp_path, monkeypatch, capsys):
    previous, data = eleven_saved_windows(tmp_path)
    old = {path.relative_to(previous): path.read_bytes() for path in (previous / "windows").rglob("*.json")}
    assert len(old) == 312
    path, registration, source, _ = partial_source(tmp_path / "new_partial")
    output = previous.parent / "with_2023_partial"
    original_read = view._read

    def no_market(file):
        target = Path(file).resolve()
        assert target.suffix == ".json" and "never_read" not in target.parts
        assert not target.is_relative_to((tmp_path / "sources").resolve())
        return original_read(file)

    monkeypatch.setattr(view, "_read", no_market)
    assert view.main(["--case-partial-year-result", str(path), "--partial-year-registration", str(registration),
        "--reuse-projection", str(previous), "--out", str(output), "--publish", "--data-root", str(data)]) == 0
    assert "saved_durable_partial_year_history_projection_built" in capsys.readouterr().out
    assert all((output / relative).read_bytes() == payload for relative, payload in old.items())
    result = view.read_history(data, "partial_year32_2023-04-06_2023-12-21", SELECTED)
    assert result["status"] == "saved_history_projection" and len(result["windows"]) == 12
    assert sum(len(window["case_rows"]) for window in result["windows"]) == 344
    window = next(row for row in result["windows"] if row["id"].startswith("partial_year32_"))
    assert window["period_kind"] == "independent_partial_year" and "非全年" in window["label"]
    assert window["full_year_claimed"] is False and window["warmup_sessions"] == 61
    assert len(window["decision_days"]) == 175 and len(window["tail_days"]) == 6
    assert window["source_sessions"] == 181 and window["equity_series_concatenated"] is False
    original = next(case for case in source["cases"] if case["case_id"] == SELECTED)
    assert result["detail"]["summary"]["metrics"] is None
    assert result["detail"]["summary"]["blocking_evidence"] == original["blocking_evidence"]
    assert result["detail"]["summary"]["residual_positions"] == original["residual_positions"]
    assert all(result[key] is False for key in ("engine_advanced", "profits_recomputed", "returns_reportable"))


def test_partial_source_pin_and_registered_period_cannot_be_rehashed_into_a_different_study(tmp_path):
    path, registration, _, _ = partial_source(tmp_path)
    payload = view._read(registration)[0]
    payload["spec"]["warmup_sessions"] = 60
    save(registration, payload)
    output = tmp_path / "not_created"
    with pytest.raises(ValueError, match="partial-year spec"):
        view.build_case_partial_year(path, registration, tmp_path / "unread_previous", output)
    assert not output.exists()


def test_partial_pending_never_publishes_and_annual_validation_is_not_relaxed(tmp_path):
    path, registration, _, progress = partial_source(tmp_path)
    output = tmp_path / "pending"
    progress["status"] = "running"
    save(path.parent / "progress.json", progress)
    assert view.main(["--case-partial-year-result", str(path), "--partial-year-registration", str(registration),
        "--reuse-projection", str(tmp_path / "unread_previous"), "--out", str(output),
        "--publish", "--data-root", str(tmp_path / "data")]) == 0
    assert not output.exists() and not (tmp_path / "data/research/leader_eight_type_history_views/current.json").exists()
    progress["status"] = "completed"
    save(path.parent / "progress.json", progress)
    with pytest.raises(ValueError, match="Durable annual source"):
        view.build_case_annual(path, tmp_path / "unread_previous", tmp_path / "not_annual")
