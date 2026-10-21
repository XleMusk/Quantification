"""One synthetic metadata-only repair contract; no market producer or Engine."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_type_router_forward as forward
from trading.research import leader_type_router_runtime_repair as m


def test_repair_keeps_original_bytes_and_clocks_rejects_source_drift_without_future_fills(tmp_path, monkeypatch):
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    day, execution = "2026-10-19", "2026-10-20"
    original = {"schema": m.paper.REGISTRATION_SCHEMA, "method_id": m.paper.METHOD_ID,
        "spec": {"frozen_synthetic_strategy": True}, "registered_at": day + "T01:00:00+08:00",
        "first_decision_day": day, "first_execution_day": execution,
        "code": [{"path": str(tmp_path / "synthetic_runtime.py"), "sha256": "old-runtime"}],
        "accounts_created": False, "engine_advanced": False, "orders_generated": False}
    original_path = save(tmp_path / "original_registration/registration.json", original)
    terminal_path = save(tmp_path / "original_failure/terminal.json", {
        "status": "failed_command_no_automatic_retry", "exit_code": 1,
        "recorded_at": day + "T18:10:00+08:00"})
    immutable = {path: path.read_bytes() for path in (original_path, terminal_path)}
    inputs = save(tmp_path / "original_sources/manifest.json", {"source_close_day": day})
    base = save(tmp_path / "original_sources/base.json", {"source_close_day": day})
    source = {"inputs": m.paper._pin(inputs), "base_publication": m.paper._pin(base)}
    runtime_path = tmp_path / "synthetic_runtime.py"
    runtime_path.write_bytes(b"synthetic runtime bytes; never executed")
    runtime = [m.paper._pin(runtime_path)]
    evidence = {"original_registration": m.paper._pin(original_path),
        "failure_terminal": m.paper._pin(terminal_path)}
    facts = {"original": original, "runtime": runtime, "source": source, "evidence": evidence,
        "failed_out": str(tmp_path / "original_failed_publication"),
        "original_run": str(tmp_path / "original_account"),
        "terminal_at": day + "T18:10:00+08:00"}

    # The origin supervisor's large saved-evidence graph is outside this small
    # fixture. The adapter's real registration, pins, loader and publish run.
    def origin(registration_path, failure_terminal, *, verify_sources=False):
        assert Path(registration_path).resolve() == original_path.resolve()
        assert Path(failure_terminal).resolve() == terminal_path.resolve()
        for reference in evidence.values():
            m._verify(reference)
        return deepcopy(facts)

    monkeypatch.setattr(m, "_origin", origin)
    monkeypatch.setattr(m.paper, "_runtime", lambda: deepcopy(runtime))
    clock = datetime.fromisoformat(day + "T18:20:00+08:00")
    monkeypatch.setattr(m, "_now", lambda: clock)

    def legacy_loader(*args, **kwargs):
        pytest.fail("The default old registration loader cannot accept the new repair")

    monkeypatch.setattr(m.paper, "registration", legacy_loader)
    monkeypatch.setattr(m.paper, "load_inputs", lambda *a, **k: pytest.fail("Registration cannot load prices"))
    monkeypatch.setattr(forward, "run", lambda *a, **k: pytest.fail("Registration/publication cannot advance Engine"))
    registration_dir, run_dir = tmp_path / "repair_registration", tmp_path / "repair_account"
    record = m.register(original_path, terminal_path, registration_dir, run_dir)
    registration_path = registration_dir / "registration.json"
    assert m.registration(registration_path) == record
    assert record["registered_at"] == clock.isoformat()
    assert record["strategy_definition_registered_at"] == original["registered_at"]
    assert record["registered_at"] != record["strategy_definition_registered_at"]
    assert record["first_decision_day"] == day and record["first_execution_day"] == execution
    assert record["accounts_created"] is record["engine_advanced"] is record["orders_generated"] is False
    assert m.paper.registration is legacy_loader and not run_dir.exists()

    calls = []

    def publish_stub(registration, selected_inputs, selected_base, out):
        loaded = m.paper.registration(registration)
        assert loaded == record
        with pytest.raises(ValueError, match="cannot load another registration"):
            m.paper.registration(original_path)
        assert Path(selected_inputs) == inputs and Path(selected_base) == base
        calls.append("D-only publication")
        publication = {"source_close_day": day, "proposed_execution_day": execution,
            "recorded_at": clock.isoformat(), "publication_at": clock.isoformat(),
            "engine_advanced": False, "fills": [], "orders_generated": False}
        save(Path(out) / "publication.json", publication)
        return publication

    monkeypatch.setattr(m.paper, "publish", publish_stub)
    wrong_inputs = save(tmp_path / "wrong_sources/manifest.json", {"source_close_day": day})
    with pytest.raises(ValueError, match="exact failed attempt's original source pair"):
        m.publish(registration_path, wrong_inputs, base, tmp_path / "wrong_path_publication")
    saved_inputs = inputs.read_bytes()
    inputs.write_bytes(saved_inputs + b"\n")
    with pytest.raises(ValueError, match="exact failed attempt's original source pair"):
        m.publish(registration_path, inputs, base, tmp_path / "wrong_hash_publication")
    inputs.write_bytes(saved_inputs)
    assert calls == [] and m.paper.registration is legacy_loader

    clock = datetime.fromisoformat(day + "T18:21:00+08:00")
    publication_dir = tmp_path / "repair_publication"
    publication = m.publish(registration_path, inputs, base, publication_dir)
    assert calls == ["D-only publication"]
    assert publication["recorded_at"] == publication["publication_at"] == clock.isoformat()
    assert publication["runtime_repair"]["identity"] == record["repair_identity"]
    assert publication["runtime_repair"]["original_D_route_succeeded"] is False
    assert publication["fills"] == [] and publication["engine_advanced"] is False
    assert m.paper._read(publication_dir / "publication.json") == publication
    assert m.paper.registration is legacy_loader and not run_dir.exists()

    def failing_publisher(registration, *args):
        assert m.paper.registration(registration) == record
        raise ValueError("synthetic publisher failure")

    monkeypatch.setattr(m.paper, "publish", failing_publisher)
    with pytest.raises(ValueError, match="synthetic publisher failure"):
        m.publish(registration_path, inputs, base, tmp_path / "failed_new_publication")
    assert m.paper.registration is legacy_loader
    assert all(path.read_bytes() == content for path, content in immutable.items())
    assert not run_dir.exists()
