from copy import deepcopy
from datetime import datetime
import gzip
import json
from pathlib import Path

import pytest

from trading.research import seat_disclosure_daily as daily


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now = datetime.fromisoformat("2026-10-19T19:00:00+08:00")
    monkeypatch.setattr(daily, "_now", lambda: now)
    monkeypatch.setattr(daily.metadata, "_now", lambda: now)
    calendar = save(tmp_path / "fixed/calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    entries = []
    for day in ("2026-10-15", "2026-10-16"):
        batch = save(tmp_path / "seed_batches" / day / "batch.json", {
            "schema": "public_seat_disclosure_batch_v1", "event_day": day, "batch_id": day,
            "status": "completed_nonempty", "created_at": "2026-10-16T23:00:00+08:00",
            "source_available_at": "2026-10-16T22:00:00+08:00"})
        entries.append({"event_day": day, "batch_id": day, "reference": daily._ref(batch)})
    prior = save(tmp_path / "catalog/catalog16.json", {"schema": "public_seat_disclosure_catalog_v1",
        "created_at": "2026-10-16T23:00:00+08:00", "parent_ref": None, "batches": entries})
    monkeypatch.setattr(daily, "CATALOG_ANCHOR", prior)
    return tmp_path, calendar, prior


def supervisor(day, *, fail=False):
    calls = []
    def run(step):
        calls.append(step["id"])
        args = step["command"]
        arg = lambda name: args[args.index(name) + 1]
        if step["id"] == "seat_disclosure_collect":
            root = Path(arg("--out"))
            manifest = {"query": {"start": day, "end": day, "codes": [], "source": "Eastmoney", "pagination_version": 2},
                "publication_timestamps_available": False, "completeness": "provider_pages_only"}
            if fail:
                save(root / "manifest.json", {**manifest, "status": "failed", "error": "Empty page is not a verified no-event day"})
                return {"status": "failed", "returncode": 1}
            for kind in ("summary", "buy", "sell"):
                root.mkdir(parents=True, exist_ok=True)
                with gzip.open(root / f"{kind}_0001.json.gz", "wt", encoding="utf-8") as stream:
                    json.dump({"request": {"synthetic": True}, "response": {"success": True},
                        "retrieved_at": day + "T18:31:00+08:00"}, stream)
            save(root / "manifest.json", {**manifest, "status": "ready", "completed_at": day + "T18:32:00+08:00",
                "reports": {kind: {"pages": 1, "rows": 1} for kind in ("summary", "buy", "sell")}})
            save(root / "normalization.json", {"raw_future_performance_fields_used": False,
                "publication_timestamps_available": False})
            (root / "events.parquet").write_bytes(b"synthetic artifact presence only; not market data")
        elif step["id"] == "seat_disclosure_batch":
            save(Path(arg("--out")) / "batch.json", {"schema": "public_seat_disclosure_batch_v1",
                "event_day": day, "batch_id": day, "source_root": arg("--source"), "status": "completed_nonempty",
                "created_at": day + "T19:00:00+08:00", "source_available_at": day + "T18:32:00+08:00"})
        else:
            parent = daily._read(arg("--parent"))
            batch = Path(arg("--batch"))
            save(arg("--out"), {"schema": "public_seat_disclosure_catalog_v1", "created_at": day + "T19:00:00+08:00",
                "parent_ref": daily._ref(arg("--parent")), "batches": [*parent["batches"],
                    {"event_day": day, "batch_id": day, "reference": daily._ref(batch)}]})
        return {"status": "completed"}
    return calls, run


def test_plan_is_readonly_bounded_exactD_and_real_supplier_window(setup, monkeypatch):
    root, calendar, prior = setup
    out = root / "plan_only"
    value = daily.run("2026-10-19", prior, out, calendar=calendar,
        run_step=lambda _: pytest.fail("planning cannot execute"))
    assert not out.exists() and value["network_accessed"] is False
    assert [s["id"] for s in value["steps"]] == ["seat_disclosure_collect", "seat_disclosure_batch", "seat_disclosure_catalog"]
    collect = value["steps"][0]["command"]
    assert collect[collect.index("--start") + 1] == collect[collect.index("--end") + 1] == "2026-10-19"
    assert collect[collect.index("--max-pages") + 1] == "3" and "--codes" not in collect
    monkeypatch.setattr(daily, "_now", lambda: datetime.fromisoformat("2026-10-18T19:00:00+08:00"))
    with pytest.raises(ValueError, match="actual D"):
        daily.run("2026-10-19", prior, out, calendar=calendar, execute=True, run_step=lambda _: pytest.fail("future request"))
    assert not out.exists()


def test_three_steps_then_exact_saved_source_batch_catalog_reuse_no_second_collection(setup):
    root, calendar, prior = setup
    calls, run = supervisor("2026-10-19")
    out = root / "operation"
    result = daily.run("2026-10-19", prior, out, calendar=calendar, execute=True, run_step=run)
    assert len(calls) == 3 and result["daily_complete"] is True
    assert result["network_accessed"] is True and result["network_access_unknown"] is False
    assert result["official_market_coverage_verified"] is False
    plan = daily._read(out / "plan.json")
    assert daily.validate_plan(plan, "2026-10-19", prior, out, calendar=calendar) == plan
    assert daily.run("2026-10-19", prior, out, calendar=calendar, execute=True,
        run_step=lambda _: pytest.fail("completed source cannot repeat")) == result
    reused = daily.run("2026-10-19", prior, root / "reuse", calendar=calendar, execute=True,
        source=result["source"], batch=result["batch"], saved_catalog=result["catalog"]["file"],
        run_step=lambda _: pytest.fail("saved successful artifacts must be reused"))
    assert reused["daily_complete"] is True and reused["catalog"] == result["catalog"]
    assert reused["network_accessed"] is False and reused["steps"] == {}
    partial = root / "partial_batch"
    (partial / "artifacts/batch/snapshot").mkdir(parents=True)
    stopped = daily.run("2026-10-19", prior, partial, calendar=calendar, execute=True,
        source=result["source"], run_step=lambda _: pytest.fail("partial batch generation must not be retried"))
    assert stopped["daily_complete"] is False and stopped["steps"] == {}
    assert stopped["status"] == "source_failed_or_unknown_no_retry"
    changed = deepcopy(plan)
    changed["steps"][0]["command"][2] = "2026-10-16"
    with pytest.raises(ValueError, match="cannot change"):
        daily.validate_plan(changed, "2026-10-19", prior, out, calendar=calendar)


def test_empty_failed_response_stays_unknown_and_status_never_retries(setup, monkeypatch):
    root, calendar, prior = setup
    calls, run = supervisor("2026-10-19", fail=True)
    out = root / "failed"
    result = daily.run("2026-10-19", prior, out, calendar=calendar, execute=True, run_step=run)
    assert calls == ["seat_disclosure_collect"] and result["daily_complete"] is False
    assert result["catalog"] is None and result["network_accessed"] is None
    assert result["network_access_unknown"] is True
    assert "Empty page" in daily._read(Path(result["source"]) / "manifest.json")["error"]
    assert not (out / "artifacts/batch/batch.json").exists()
    assert daily.run("2026-10-19", prior, out, calendar=calendar, execute=True,
        run_step=lambda _: pytest.fail("failed generation must not retry")) == result
    monkeypatch.setattr(daily, "next_plan", lambda *a, **k: pytest.fail("status must only read"))
    assert daily.status(out) == result


def test_explicit_verified_saved_child_recovery_adopts_without_recollecting(setup):
    root, calendar, prior = setup
    calls, supervised = supervisor("2026-10-19")
    def interrupted(step):
        result = supervised(step)
        if step["id"] == "seat_disclosure_collect":
            raise RuntimeError("parent observation interrupted after saved child terminal success")
        return result
    out = root / "recover"
    first = daily.run("2026-10-19", prior, out, calendar=calendar, execute=True, run_step=interrupted)
    assert first["daily_complete"] is False and calls == ["seat_disclosure_collect"]
    assert daily.run("2026-10-19", prior, out, calendar=calendar, execute=True,
        run_step=lambda _: pytest.fail("default must not retry")) == first
    assert daily.run("2026-10-19", prior, out, calendar=calendar, execute=True,
        resume_saved_recovery=True, recover_step=lambda _: None,
        run_step=lambda _: pytest.fail("unverified recovery must not continue")) == first
    recovered = []
    def exact_saved_recovery(step):
        recovered.append(step["id"])
        return {"status": "completed", "recovered_saved_child": True, "producer_repeated": False,
            "recovery_ref": {"synthetic": "exact supervised terminal0 and request binding"}}
    final = daily.run("2026-10-19", prior, out, calendar=calendar, execute=True,
        resume_saved_recovery=True, recover_step=exact_saved_recovery, run_step=supervised)
    assert final["daily_complete"] is True and recovered == ["seat_disclosure_collect"]
    assert calls == ["seat_disclosure_collect", "seat_disclosure_batch", "seat_disclosure_catalog"]
    history = final["recovery_history"][0]
    assert daily._read(history["before_result"]["file"]) == first
    assert history["producer_repeated"] is False and final["network_accessed"] is True
