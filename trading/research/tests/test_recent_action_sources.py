"""Synthetic scoped source preparation/collection; no market or SDK session."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.research import recent_action_sources as sources


D = "2026-10-16"
A, B, C, X, Y, Z = [f"60000{i}.SSE" for i in range(1, 7)]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def cached_task(root, symbol, *, retrieved="2026-10-17T09:00:00+08:00"):
    payload = root / "actions" / symbol / "2026.csv"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("code,symbol,dividOperateDate,dividRegistDate,dividCashPsBeforeTax,dividStocksPs,dividReserveToStockPs\n"
        + f"sh.{symbol[:6]},{symbol},2026-07-02,2026-07-01,.05,0,\n", encoding="utf-8")
    task = {"path": f"actions/{symbol}/2026.csv", "retrieved_at": retrieved,
        "quality": {"rows": 1}, "sha256": sha256(payload.read_bytes()).hexdigest(),
        "source_error_code": "0", "source_sdk": {"distribution_version": "0.9.4", "sha256": sources.WHEEL_SHA256}}
    save(root / "receipts" / f"{symbol}.json", {"symbol": symbol, "tasks": {"actions_2026": task}})
    return payload, task


def setup_sources(tmp_path, monkeypatch, *, selected=(A,), cached=(A,)):
    package = tmp_path / "complete_close"
    package.mkdir()
    calendar = tmp_path / "calendar" / "2026.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"]})
    now = datetime.fromisoformat("2026-10-17T10:00:00+08:00")
    monkeypatch.setattr(sources, "_now", lambda: now)
    monkeypatch.setattr(sources, "_source", lambda *args: {"proof": {"day": D,
        "source_folder": str(package), "read_only_verified": True, "full_dated_roster_inventory_complete": True}})
    publication = tmp_path / "publication" / "publication.json"
    payload = {"source_day": D, "published_at": "2026-10-17T09:30:00+08:00",
        "calendar_ref": {"file": str(calendar), "sha256": sha256(calendar.read_bytes()).hexdigest()},
        "weights": {s: .04 for s in selected}}
    save(publication, payload)
    monkeypatch.setattr(sources, "load_publication", lambda path, **kwargs: json.loads(Path(path).read_text()))
    reuse = tmp_path / "prior_cache"
    reuse.mkdir()
    save(reuse / "collection_plan.json", {"end": D})
    for symbol in cached:
        cached_task(reuse, symbol)
    return {"package": package, "calendar": calendar, "publication": publication, "reuse": reuse,
        "out": tmp_path / "new_source"}


def prepare(env, **kwargs):
    return sources.prepare(D, env["package"], env["calendar"], env["out"],
        publications=[env["publication"]], reuse_roots=[env["reuse"]], **kwargs)


def test_scope_union_cache_bytes_and_clocks_do_not_start_sdk(tmp_path, monkeypatch):
    env = setup_sources(tmp_path, monkeypatch, cached=(A, B, C, X, Y, Z))
    head = tmp_path / "account" / "head.json"
    save(head, {"schema": "native_lowvol_preopen_checkpoint_v1", "method_id": sources.METHOD_ID,
        "execution_admitted": False, "context": RunContext("synthetic", TradingMode.REPLAY).to_dict(),
        "action_scope": [Z], "state": {"day": D, "positions": {B: [{"qty": 100}], A: []},
            "pending_targets": {C: .2}, "full_exit_lifecycle": {"remaining_full_exits": {X: {"remaining_qty": 100}}},
            "entitlements": {Y + ":2026-10-19": 100, A + ":2026-10-15": 0}, "receivables": {}}})
    manifest = prepare(env, account_heads=[head])
    assert manifest["scope_symbols"] == sorted([A, B, C, X, Y, Z])
    assert manifest["reused_query_count"] == 6 and manifest["pending_query_count"] == 0
    assert manifest["source_complete"] is True
    provider = Path(manifest["provider_root"])
    for item in manifest["reused_queries"]:
        original = Path(item["payload"]["path"])
        task = json.loads((provider / "receipts" / f"{item['symbol']}.json").read_text())["tasks"]["actions_2026"]
        assert (provider / task["path"]).read_bytes() == original.read_bytes()
        assert task["retrieved_at"] == item["retrieved_at"] == "2026-10-17T09:00:00+08:00"
        assert "actions_2025" not in json.loads((provider / "receipts" / f"{item['symbol']}.json").read_text())["tasks"]
    monkeypatch.setattr(sources, "select_sdk", lambda *args: pytest.fail("all-cache must not load SDK"))
    monkeypatch.setattr(sources, "collect", lambda *args, **kwargs: pytest.fail("all-cache must not collect"))
    result = sources.collect_prepared(env["out"], None)
    assert result["status"] == "scoped_action_sources_complete"
    assert result["network_accessed"] is False and result["new_query_count"] == 0
    assert sources.collect_prepared(env["out"], None) == result


def test_stale_success_can_be_new_query_but_failed_same_day_is_not_retried(tmp_path, monkeypatch):
    env = setup_sources(tmp_path, monkeypatch, cached=())
    cached_task(env["reuse"], A, retrieved=D + "T14:00:00+08:00")
    save(env["reuse"] / "collection_runs" / "failed.json", {"status": "failed", "stage": "actions",
        "failed_query": {"symbol": A, "task": "actions_2026", "year": 2026}})
    manifest = prepare(env)
    assert manifest["pending_symbols"] == [A] and manifest["blocked_failed_queries"]
    monkeypatch.setattr(sources, "select_sdk", lambda *args: pytest.fail("failed same-day task must not retry"))
    result = sources.collect_prepared(env["out"], "unused")
    assert result["status"] == "scoped_action_sources_blocked_existing_failed_query"
    assert result["new_query_count"] == 0


def test_pending_queries_use_one_explicit_collect_call_and_failed_call_is_cached(tmp_path, monkeypatch):
    env = setup_sources(tmp_path, monkeypatch, selected=(A, B), cached=(A,))
    manifest = prepare(env)
    assert manifest["pending_symbols"] == [B] and manifest["reused_query_count"] == 1
    calls = []
    monkeypatch.setattr(sources, "select_sdk", lambda wheel: {"synthetic_sdk": True})
    def fail_collect(root, **kwargs):
        calls.append((root, kwargs))
        raise ValueError("synthetic first provider failure")
    monkeypatch.setattr(sources, "collect", fail_collect)
    first = sources.collect_prepared(env["out"], "synthetic-wheel")
    assert first["status"] == "scoped_action_collection_failed_no_retry"
    assert first["new_query_count"] is None and first["source_complete"] is False
    assert len(calls) == 1 and calls[0][1]["stage"] == "actions"
    assert sources.collect_prepared(env["out"], "synthetic-wheel") == first and len(calls) == 1


def test_failed_session_before_any_query_cannot_retry_under_a_new_root(tmp_path, monkeypatch):
    env = setup_sources(tmp_path, monkeypatch, cached=())
    save(env["reuse"] / "collection_runs" / "failed_login.json", {"status": "failed", "stage": "actions",
        "failed_query": None, "error": "synthetic socket failure before first query"})
    manifest = prepare(env)
    assert manifest["pending_symbols"] == [A] and manifest["blocked_failed_queries"]
    monkeypatch.setattr(sources, "select_sdk", lambda *args: pytest.fail("failed session must not retry"))
    assert sources.collect_prepared(env["out"], "unused")["status"] == "scoped_action_sources_blocked_existing_failed_query"
