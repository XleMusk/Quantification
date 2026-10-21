"""Synthetic two-route coordinator tests; artifact factory and writer lock mocked."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from trading.research import opening_daily as m


NOW = datetime.fromisoformat("2026-10-17T13:00:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(m, "_lock", lambda path: nullcontext())
    calls = {"adopt": [], "produce": [], "validate": []}
    failures = set()
    routes = []
    for key, family in (("fixed", "fixed"), ("leader", "eight_type")):
        source = tmp_path / (key + "_source")
        artifact = tmp_path / (key + "_existing") / "result.json"
        write(source / "head.json", {"synthetic_generation": 0})
        write(artifact, {"synthetic_existing_product": True})
        routes.append({"id": key, "family": family, "source": str(source),
                       "signal": "low_volatility_252_20" if family == "fixed" else None,
                       "adopt": {"result": str(artifact)}})

    def product(source_ref, refs):
        return {"source_head": deepcopy(source_ref), "source_day": "2026-10-16",
                "execution_day": "2026-10-19", "status": "synthetic_saved_research_product",
                "refs": refs, **m.FLAGS}

    def adopt(route, source_ref, artifacts):
        calls["adopt"].append(route["id"])
        return product(source_ref, artifacts)

    def produce(route, source_ref, out):
        calls["produce"].append(route["id"])
        if route["id"] in failures:
            raise RuntimeError("synthetic concrete factory failure")
        path = out / "result.json"
        write(path, {"synthetic_source_sha": source_ref["sha256"]})
        return product(source_ref, {"result": m._ref(path)})

    def validate(route, source_ref, value):
        calls["validate"].append(route["id"])
        assert value["source_head"] == source_ref
        for reference in value["refs"].values():
            assert m._ref(reference["path"]) == reference

    factory = SimpleNamespace(adopt=adopt, produce=produce, validate_product=validate)
    monkeypatch.setattr(m, "_factory", lambda: factory)
    directory = tmp_path / "opening_coordinator"
    result = m.register(directory, routes, current=NOW)
    assert result["status"] == "current_saved_opening_products"
    assert calls["adopt"] == ["fixed", "leader"] and calls["produce"] == []
    return SimpleNamespace(directory=directory, routes=routes, calls=calls,
                           failures=failures, factory=factory)


def change(setup, key, generation=1):
    route = next(row for row in setup.routes if row["id"] == key)
    write(m.Path(route["source"]) / "head.json", {"synthetic_generation": generation})


def test_unchanged_run_and_status_read_only_never_import_or_validate_factory(setup, monkeypatch):
    directory = setup.directory
    files = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    monkeypatch.setattr(m, "_factory", lambda: pytest.fail("No factory import for status/same head"))
    monkeypatch.setattr(m, "_lock", lambda path: pytest.fail("No writer machinery for unchanged head"))
    for _ in range(2):
        assert m.status(directory)["status"] == "current_saved_opening_products"
        result = m.run(directory, current=NOW)
        assert result["status"] == "no_changed_source" and result["factory_called"] is False
    assert files == {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    assert setup.calls["validate"] == ["fixed", "leader"]


def test_new_source_produces_only_its_own_route(setup):
    before = m._read(setup.directory / "head.json")["routes"]["leader"]
    change(setup, "fixed")
    result = m.run(setup.directory, current=NOW)
    assert result["newly_committed_routes"] == ["fixed"]
    assert result["factory_called_routes"] == ["fixed"] and setup.calls["produce"] == ["fixed"]
    assert m._read(setup.directory / "head.json")["routes"]["leader"] == before
    assert m.run(setup.directory)["status"] == "no_changed_source"
    assert setup.calls["produce"] == ["fixed"]


def test_failure_keeps_stale_refs_and_does_not_retry_or_stop_other_route(setup):
    change(setup, "fixed")
    change(setup, "leader")
    setup.failures.add("fixed")
    result = m.run(setup.directory, current=NOW)
    assert result["newly_committed_routes"] == ["leader"]
    failed = result["routes"][0]
    assert failed["status"] == "failed_same_input_no_retry" and failed["stale"] is True
    assert failed["saved_product"] is not None and failed["error"]
    calls = deepcopy(setup.calls)
    repeated = m.run(setup.directory, current=NOW)
    assert setup.calls == calls and repeated["factory_called"] is False
    assert repeated["routes"][1]["status"] == "current_saved_product"


def test_successful_saved_product_recovers_head_commit_without_repeating_producer(setup, monkeypatch):
    change(setup, "fixed")
    real_commit = m._commit
    monkeypatch.setattr(m, "_commit", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic head commit interruption")))
    result = m.run(setup.directory, current=NOW)
    row = result["routes"][0]
    assert row["status"] == "recoverable_saved_product" and row["stale"] is True
    assert setup.calls["produce"] == ["fixed"]
    m.run(setup.directory, current=NOW)
    assert setup.calls["produce"] == ["fixed"]
    monkeypatch.setattr(m, "_commit", real_commit)
    recovered = m.recover(setup.directory, "fixed", row["input_id"], current=NOW)
    assert recovered["recovery_status"] == "recovered_saved_product"
    assert setup.calls["produce"] == ["fixed"]
    assert recovered["routes"][0]["status"] == "current_saved_product"
    assert m.recover(setup.directory, "fixed", row["input_id"], current=NOW)["recovery_status"] == "already_committed"


def test_interrupted_attempt_without_product_is_unknown_not_a_live_job_or_retry(setup, monkeypatch):
    change(setup, "fixed")
    head, registration = m._load(setup.directory)
    route = registration["routes"][0]
    source = m._source(route)
    identity, path, _ = m._attempt(setup.directory, head, route, source)
    write(path, {"schema": m.SCHEMA, "method_id": m.METHOD, "registration": head["registration"],
        "route_id": "fixed", "input_id": identity, "source_head": source, "status": "started",
        "product": None, "product_validated": False, "execution_progress_unknown": True})
    monkeypatch.setattr(m, "_factory", lambda: pytest.fail("Unknown attempts do not invoke a factory"))
    result = m.run(setup.directory, current=NOW)
    assert result["routes"][0]["status"] == "unresolved_same_input_no_retry"
    result = m.recover(setup.directory, "fixed", identity, current=NOW)
    assert result["recovery_status"] == "unknown_no_successful_saved_product"
    assert result["execution_progress_unknown"] is True and setup.calls["produce"] == []


def test_registration_binding_and_path_overlap_cannot_be_silently_changed(setup, tmp_path):
    registration = setup.directory / "registration.json"
    registration.write_bytes(registration.read_bytes() + b" ")
    with pytest.raises(ValueError, match="references changed"):
        m.status(setup.directory)
    route = deepcopy(setup.routes[0])
    with pytest.raises(ValueError, match="overlap"):
        m.register(m.Path(route["source"]) / "coordinator_inside_source", [route], current=NOW)
    route["id"] = "../outside"
    with pytest.raises(ValueError, match="safe"):
        m.register(tmp_path / "fresh_coordinator", [route], current=NOW)
