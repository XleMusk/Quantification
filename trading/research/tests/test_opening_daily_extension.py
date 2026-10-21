"""Append-only route registration with synthetic heads and a bounded fake factory."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
import json
import sys
from types import SimpleNamespace

import pytest

from trading.research import opening_daily as m


NOW = datetime.fromisoformat("2026-10-17T05:30:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(m, "_lock", lambda path: nullcontext())
    dependencies = {}
    monkeypatch.setitem(sys.modules, "trading.research.native_saved_opening", SimpleNamespace(
        source_dependencies_current=lambda path: dependencies.get(str(path), True)))
    calls = {"adopt": [], "produce": [], "validate": []}

    def product(source, refs):
        return {"source_head": deepcopy(source), "source_day": "2026-10-16",
            "execution_day": "2026-10-19", "status": "synthetic_saved_opening_product",
            "refs": refs, **m.FLAGS}

    def adopt(route, source, refs):
        calls["adopt"].append(route["id"])
        return product(source, refs)

    def produce(route, source, out):
        calls["produce"].append(route["id"])
        output = out / "result.json"
        write(output, {"synthetic_source": source})
        return product(source, {"result": m._ref(output)})

    def validate(route, source, value):
        calls["validate"].append(route["id"])
        assert value["source_head"] == source
        assert all(m._ref(ref["path"]) == ref for ref in value["refs"].values())

    factory = SimpleNamespace(adopt=adopt, produce=produce, validate_product=validate)
    monkeypatch.setattr(m, "_factory", lambda: factory)
    routes = []
    for route_id, family in (("fixed_lowvol", "fixed"), ("eight_type", "eight_type"),
                             ("leader20", "leader20"), ("alpha158", "alpha158"),
                             ("native_original", "native_preopen"), ("native_qualified", "native_preopen")):
        source, artifact = tmp_path / (route_id + "_source"), tmp_path / (route_id + "_existing") / "result.json"
        write(source / "head.json", {"synthetic_only": True, "generation": 0,
            "schema": "native_saved_opening_source_v1" if family == "native_preopen" else "synthetic_legacy_head"})
        write(artifact, {"synthetic_saved_product": True})
        route = {"id": route_id, "family": family, "source": str(source), "adopt": {"result": str(artifact)}}
        if family == "fixed":
            route["signal"] = "low_volatility_252_20"
        routes.append(route)
    directory = tmp_path / "coordinator"
    m.register(directory, routes[:4], current=NOW)
    return SimpleNamespace(directory=directory, legacy=routes[:4], additions=routes[4:],
        calls=calls, factory=factory, dependencies=dependencies)


def test_adopt_two_native_routes_preserves_base_and_legacy_attempt_bytes(saved, monkeypatch):
    directory = saved.directory
    registration_bytes = (directory / "registration.json").read_bytes()
    head_before, _ = m._load(directory)
    attempts = {p: p.read_bytes() for p in (directory / "attempts").rglob("*.json")}
    result = m.extend(directory, saved.additions, current=NOW)
    head, effective = m._load(directory)
    assert result["newly_registered_routes"] == result["newly_committed_routes"] == [r["id"] for r in saved.additions]
    assert result["old_routes_reproduced"] is False
    assert (directory / "registration.json").read_bytes() == registration_bytes
    assert head["registration"] == head_before["registration"]
    assert all(head["routes"][key] == value for key, value in head_before["routes"].items())
    assert all(path.read_bytes() == data for path, data in attempts.items())
    assert [route["id"] for route in effective["routes"]] == [r["id"] for r in saved.legacy + saved.additions]
    assert saved.calls["adopt"] == [r["id"] for r in saved.legacy + saved.additions]
    assert saved.calls["produce"] == []
    for route in saved.legacy:
        source = m._source(route)
        assert m._registration_for(head, route["id"]) == head_before["registration"]
        assert m._attempt(directory, head, route, source)[0] == head_before["routes"][route["id"]]["input_id"]
    for route in saved.additions:
        anchor = m._registration_for(head, route["id"])
        assert anchor == head["registration_addenda"][0]
        attempt = m._read(m.Path(head["routes"][route["id"]]["attempt"]))
        assert attempt["registration"] == anchor
        assert attempt["input_id"] == m._input_id(anchor, route["id"], attempt["source_head"])
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    monkeypatch.setattr(m, "_factory", lambda: pytest.fail("unchanged status/run must not import a producer"))
    monkeypatch.setattr(m, "_lock", lambda path: pytest.fail("unchanged run must not acquire a writer"))
    assert m.status(directory)["status"] == "current_saved_opening_products"
    assert m.run(directory)["status"] == "no_changed_source"
    assert before == {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_later_addendum_keeps_first_added_anchor_and_new_source_runs_only_its_route(saved):
    first, second = saved.additions
    m.extend(saved.directory, [first], current=NOW)
    head_before, _ = m._load(saved.directory)
    anchor = deepcopy(m._registration_for(head_before, first["id"]))
    m.extend(saved.directory, [second], current=NOW)
    head, _ = m._load(saved.directory)
    assert len(head["registration_addenda"]) == 2
    assert m._registration_for(head, first["id"]) == anchor
    assert head["routes"][first["id"]] == head_before["routes"][first["id"]]
    newest = m._read(head["registration_addenda"][-1]["path"])
    assert newest["previous_addenda"] == [anchor]
    untouched = {key: deepcopy(value) for key, value in head["routes"].items() if key != first["id"]}
    write(m.Path(first["source"]) / "head.json", {"synthetic_only": True, "generation": 1})
    result = m.run(saved.directory, current=NOW)
    assert result["factory_called_routes"] == result["newly_committed_routes"] == [first["id"]]
    assert saved.calls["produce"] == [first["id"]]
    updated, _ = m._load(saved.directory)
    assert all(updated["routes"][key] == value for key, value in untouched.items())
    assert m._registration_for(updated, first["id"]) == anchor


@pytest.mark.parametrize("mutation", ["bytes", "chain", "anchor", "old_anchor", "route_order"])
def test_bad_addenda_or_anchor_mapping_is_rejected_without_factory(saved, monkeypatch, mutation):
    m.extend(saved.directory, saved.additions, current=NOW)
    head = m._read(saved.directory / "head.json")
    reference = head["registration_addenda"][0]
    path = m.Path(reference["path"])
    if mutation == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "chain":
        addendum = m._read(path)
        addendum["previous_addenda"] = [reference]
        write(path, addendum)
        replacement = m._ref(path)
        head["registration_addenda"] = [replacement]
        head["route_registrations"] = {key: replacement for key in head["route_registrations"]}
    elif mutation == "anchor":
        head["route_registrations"][saved.additions[0]["id"]] = head["registration"]
    elif mutation == "old_anchor":
        head["route_registrations"][saved.legacy[0]["id"]] = head["registration"]
    else:
        original = next(iter(head["routes"]))
        head["routes"][original] = head["routes"].pop(original)
    write(saved.directory / "head.json", head)
    monkeypatch.setattr(m, "_factory", lambda: pytest.fail("invalid registration must not import a factory"))
    with pytest.raises(ValueError):
        m.status(saved.directory)


def test_repeat_ids_and_source_overlap_cannot_replace_existing_routes(saved):
    m.extend(saved.directory, [saved.additions[0]], current=NOW)
    before = {p: p.read_bytes() for p in saved.directory.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Unique route IDs"):
        m.extend(saved.directory, [saved.additions[0]], current=NOW)
    route = deepcopy(saved.additions[1])
    route["source"] = str(saved.directory / "overlap")
    with pytest.raises(ValueError, match="overlap"):
        m.extend(saved.directory, [route], current=NOW)
    assert before == {p: p.read_bytes() for p in saved.directory.rglob("*") if p.is_file()}


def test_source_change_during_addendum_write_leaves_old_head_current(saved, monkeypatch):
    route = saved.additions[0]
    before = (saved.directory / "head.json").read_bytes()
    original_ref = m._ref

    def change_after_seal(path):
        reference = original_ref(path)
        if m.Path(path).parent.name == "registration_addenda":
            write(m.Path(route["source"]) / "head.json", {"synthetic_only": True, "generation": 1})
        return reference

    monkeypatch.setattr(m, "_ref", change_after_seal)
    with pytest.raises(ValueError, match="addition source changed"):
        m.extend(saved.directory, [route], current=NOW)
    assert (saved.directory / "head.json").read_bytes() == before
    assert saved.calls["adopt"] == [r["id"] for r in saved.legacy]
    assert len(list((saved.directory / "registration_addenda").glob("*.json"))) == 1


def test_native_saved_adoption_recovers_without_reproducing_old_or_new_routes(saved, monkeypatch):
    route = saved.additions[0]
    commit = m._commit
    monkeypatch.setattr(m, "_commit", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic publish interruption")))
    result = m.extend(saved.directory, [route], current=NOW)
    pending = next(row for row in result["routes"] if row["id"] == route["id"])
    assert pending["status"] == "recoverable_saved_product"
    calls = deepcopy(saved.calls)
    assert m.run(saved.directory)["factory_called"] is False
    assert saved.calls == calls
    monkeypatch.setattr(m, "_commit", commit)
    recovered = m.recover(saved.directory, route["id"], pending["input_id"], current=NOW)
    assert recovered["recovery_status"] == "recovered_saved_product"
    assert saved.calls["adopt"] == [r["id"] for r in saved.legacy] + [route["id"]]
    assert saved.calls["produce"] == []
    assert m.recover(saved.directory, route["id"], pending["input_id"])["recovery_status"] == "already_committed"


def test_changed_dependencies_and_wrong_new_attempt_anchor_never_call_factory(saved, monkeypatch):
    route = saved.additions[0]
    m.extend(saved.directory, [route], current=NOW)
    saved.dependencies[str(m.Path(route["source"]) / "head.json")] = False
    result = m.status(saved.directory)
    row = next(row for row in result["routes"] if row["id"] == route["id"])
    assert row["status"] == "source_unavailable" and row["stale"] is True
    assert row["source_head"] is None and row["saved_product"] is not None
    saved.dependencies.clear()
    write(m.Path(route["source"]) / "head.json", {"synthetic_only": True, "generation": 1})
    head, _ = m._load(saved.directory)
    source = m._source(route)
    identity, path, _ = m._attempt(saved.directory, head, route, source)
    write(path, {"schema": m.SCHEMA, "method_id": m.METHOD, "registration": head["registration"],
        "route_id": route["id"], "input_id": identity, "source_head": source, "status": "started"})
    monkeypatch.setattr(m, "_factory", lambda: pytest.fail("wrong attempt registration must never be adopted"))
    result = m.run(saved.directory)
    row = next(row for row in result["routes"] if row["id"] == route["id"])
    assert result["factory_called"] is False and row["attempt_status"] == "invalid_saved_attempt"


def test_extend_cli_accepts_explicit_routes_file_without_registering_again(tmp_path, monkeypatch, capsys):
    config = tmp_path / "routes.json"
    routes = [{"id": "native_original", "family": "native_preopen"}]
    write(config, {"routes": routes})
    called = []
    monkeypatch.setattr(m, "extend", lambda directory, additions: called.append((directory, additions)) or {"synthetic": True})
    assert m.main(["extend", "--directory", str(tmp_path / "existing"), "--routes", str(config)]) == 0
    assert called == [(tmp_path / "existing", routes)]
    assert json.loads(capsys.readouterr().out) == {"synthetic": True}
