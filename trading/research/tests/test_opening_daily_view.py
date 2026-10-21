"""Synthetic small saved coordinator records; GET cannot invoke any producer."""
from copy import deepcopy
from datetime import datetime
import json

from fastapi import HTTPException
import pytest

from trading import api
from trading.research import opening_daily as core
from trading.research import opening_daily_view as view


NOW = datetime.fromisoformat("2026-10-17T15:00:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    directory = tmp_path / view.RUN_RELATIVE
    routes, committed = [], {}
    for key, family in (("fixed", "fixed"), ("alpha", "alpha158")):
        source = tmp_path / (key + "_source")
        write(source / "head.json", {"synthetic_generation": 1})
        source_ref = core._ref(source / "head.json")
        routes.append({"id": key, "family": family, "source": str(source)})
        product = {"source_head": source_ref, "source_day": "2026-10-16", "execution_day": "2026-10-19",
            "status": "paper_candidates_only", "case_count": 1 if key == "fixed" else 2,
            "ready_case_count": 1 if key == "fixed" else None,
            "not_ready_case_count": 0 if key == "fixed" else None,
            "candidate_order_count": 0 if key == "fixed" else None,
            # No actual payload file exists. A status projection must never
            # open pack/intent/raw files to recompute or validate these counts.
            "refs": {"opening_pack": {"path": str(tmp_path / key / "unopened_pack.json"), "sha256": "a" * 64}},
            **core.FLAGS}
        committed[key] = {"input_id": ("b" if key == "fixed" else "c") * 64,
            "source_head": source_ref, "product": product,
            "attempt": str(directory / "attempts" / key / "unopened_attempt.json"),
            "committed_at": "2026-10-17T14:30:00+08:00", "adopted": True}
    registration = {"schema": core.SCHEMA, "method_id": core.METHOD,
        "run_directory": str(directory.resolve()), "routes": routes, "adopt_refs": {}, **core.FLAGS}
    write(directory / "registration.json", registration)
    head = {"schema": core.SCHEMA, "method_id": core.METHOD,
        "registration": core._ref(directory / "registration.json"), "routes": committed, **core.FLAGS}
    write(directory / "head.json", head)
    return tmp_path, directory, head, routes


def test_get_projects_existing_status_only_unknown_counts_and_zero_mutations(saved, monkeypatch):
    data_root, directory, _, _ = saved
    calls = []
    actual_status = core.status
    monkeypatch.setattr(core, "status", lambda path: (calls.append(str(path)), actual_status(path))[1])
    for name in ("run", "register", "recover", "_factory", "_lock"):
        monkeypatch.setattr(core, name, lambda *a, **kw: pytest.fail("GET invoked coordinator write machinery"))
    before = {path: path.read_bytes() for path in data_root.rglob("*") if path.is_file()}
    result = view.opening_daily_view(data_root, now=NOW)
    assert calls == [str(directory.resolve())]
    assert result["read_at"] == NOW.isoformat() and result["read_at_is_source_time"] is False
    fixed, alpha = result["routes"]
    assert fixed["candidate_order_count"] == 0 and fixed["ready_case_count"] == 1
    assert alpha["candidate_order_count"] is None and alpha["ready_case_count"] is None
    assert alpha["not_ready_case_count"] is None and alpha["case_count"] == 2
    assert fixed["committed_at"] == "2026-10-17T14:30:00+08:00"
    assert fixed["refs"]["opening_pack"]["sha256"] == "a" * 64
    assert result["readonly"] is True and result["execution_admitted"] is False
    assert result["factory_called"] is False and result["saved_artifacts_revalidated"] is False
    assert before == {path: path.read_bytes() for path in data_root.rglob("*") if path.is_file()}
    route = next(route for route in api.router.routes if route.path == "/api/trading/research/opening-daily")
    assert route.methods == {"GET"}
    assert api.saved_opening_daily_status()["schema"] == view.SCHEMA


def test_missing_coordinator_returns_unavailable_without_status_or_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "status", lambda *a: pytest.fail("Missing coordinator must not initialize or inspect state"))
    result = view.opening_daily_view(tmp_path, now=NOW)
    assert result["status"] == "unavailable" and result["routes"] == []
    assert not (tmp_path / view.RUN_RELATIVE).exists()


def test_corrupt_saved_registration_returns_409_without_old_success(saved):
    data_root, directory, _, _ = saved
    registration = directory / "registration.json"
    registration.write_bytes(registration.read_bytes() + b" ")
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_status()
    assert caught.value.status_code == 409
    assert "每日纸面开盘方案" in caught.value.detail


def test_changed_source_keeps_old_product_dates_and_refs_visibly_stale(saved):
    data_root, _, head, routes = saved
    write(core.Path(routes[0]["source"]) / "head.json", {"synthetic_generation": 2, "day": "2026-10-19"})
    result = view.opening_daily_view(data_root, now=NOW)
    stale = result["routes"][0]
    assert stale["source_changed"] is True and stale["stale"] is True
    assert stale["source_head"] != stale["product_source_head"]
    assert stale["source_day"] == "2026-10-16" and stale["execution_day"] == "2026-10-19"
    assert stale["refs"] == head["routes"]["fixed"]["product"]["refs"]
    assert stale["date_basis"] == "saved_product_source_head_not_current_source_head"
