"""Synthetic saved layouts for the thin daily chain; no market or Engine run."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.research import fixed_close_to_opening as m
from trading.research import opening_daily as opening
from trading.research import stockdb_fixed_daily as fixed
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE


OLD, D, E = "2026-10-16", "2026-10-19", "2026-10-20"
NOW = datetime.fromisoformat(D + "T18:30:00+08:00")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return fixed._reference(path)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    monkeypatch.setattr(fixed, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(opening, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(m, "lock", lambda path: nullcontext())
    monkeypatch.setattr(opening, "_lock", lambda path: nullcontext())
    counts = {"load": 0, "append": 0, "fixed": 0, "produce": 0}
    failures = set()
    run, coordinator = tmp_path / "fixed-workflow", tmp_path / "opening-coordinator"
    package, operation = tmp_path / "D-close", tmp_path / "operation"
    base = tmp_path / "old-supplement" / "raw_daily.parquet"
    base.parent.mkdir(); base.write_bytes(b"synthetic preserved original string rows")
    calendar_ref = write(tmp_path / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    schedule = {"calendar_files": [{**calendar_ref, "year": 2026}]}
    config = StrategyConfig().to_dict()
    registration = {"schema": fixed.SCHEMA, "method_id": fixed.METHOD, "signal": "low_volatility_252_20",
        "config": config, "replay_start": "2024-11-01", "month_end_schedule": schedule,
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "snapshot": str(tmp_path / "snapshot"), **fixed.FLAGS}
    registration_ref = write(run / "registration.json", registration)

    def source(directory, day):
        record = {"schema": fixed.rolling.SCHEMA, "status": "completed_research_only",
            "variants": [registration["signal"]], "replay_end": day,
            **{k: registration[k] for k in ("config", "replay_start", "month_end_schedule", "full_exit_lifecycle")}}
        protocol = {**record, "snapshot": registration["snapshot"]}
        values = {"run": record, "protocol": protocol, "account": {"day": day, "cash": 9999999},
                  "replay": {"status": "completed", "last_completed_session": day}, "daily": {"date": day}}
        return record, {key: write(directory / (key + ".json"), value) for key, value in values.items()}
    record, refs = source(tmp_path / "old-source", OLD)
    head = {"schema": fixed.SCHEMA, "method_id": fixed.METHOD, "registration": registration_ref,
        "generation": 1, "last_completed_day": OLD, "source": refs, "supplement": fixed._reference(base), **fixed.FLAGS}
    write(run / "head.json", head)
    raw = pd.DataFrame([{"date": D, "code": "sh.600001", "close": "10", "adjustflag": "3"}])
    package.mkdir()
    (package / "raw_daily.csv").write_text(raw.to_csv(index=False))
    roster_raw = tmp_path / "D-roster" / "raw.csv"; roster_raw.parent.mkdir(); roster_raw.write_text("synthetic roster")
    write(roster_raw.parent / "receipt.json", {"raw_csv": str(roster_raw),
        "created_at": D+"T18:00:00+08:00", "completed_at": D+"T18:00:01+08:00"})
    plan = {"day": D, "calendar_sha256": calendar_ref["sha256"], "roster_raw_sha256": fixed._digest(roster_raw),
        "schema": "baostock_bulk_exact_dated_close_inventory_v1", "roster_folder": str(roster_raw.parent)}
    receipt = {"day": D, "schema": plan["schema"], "status": "complete_full_dated_close_inventory_not_admitted",
        "raw_daily_sha256": fixed._digest(package / "raw_daily.csv"), "full_dated_roster_inventory_complete": True,
        "pending_symbols": [], "preserved_failed_batches": [], "created_at": D+"T18:00:02+08:00",
        "updated_at": D+"T18:00:03+08:00", "completed_at": D+"T18:00:03+08:00"}
    write(package / "plan.json", plan); write(package / "receipt.json", receipt)
    metadata = {"plan": plan, "receipt": receipt, "read_only_verified": True, "source_folder": str(package)}

    def load(path):
        counts["load"] += 1
        assert Path(path) == package
        return raw.copy(), deepcopy(metadata)
    monkeypatch.setattr(m, "load_complete", load)

    def append(parent, packages, out):
        counts["append"] += 1
        assert Path(parent) == base and list(packages) == [package]
        out.mkdir()
        file = out / "raw_daily.parquet"; file.write_bytes(base.read_bytes()+b" exact-D append")
        if "append" in failures:
            raise ValueError("synthetic incomplete append after raw write")
        proof = m.source_proof(raw, metadata, plan["calendar_sha256"])
        value = {"schema": m.APPEND_SCHEMA, "status": "completed_append_only_research_supplement",
            "base_file": str(base), "base_sha256": fixed._digest(base), "raw_daily_file": str(file),
            "raw_daily_sha256": fixed._digest(file), "source_proofs": [proof],
            "original_full_row_prefix_preserved": True, "original_strings_and_nulls_preserved": True,
            "current_source_overwrites_original_keys": False,
            **{k: False for k in ("execution_admitted", "selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated")}}
        write(out / "result.json", value)
        return value
    monkeypatch.setattr(m, "append_sources", append)

    def fixed_run(directory, through, *, packages, supplemental_raw_view):
        counts["fixed"] += 1
        assert directory == run and through == D and packages == [package]
        before = fixed._read(run / "head.json")
        identity, inputs = fixed._fingerprint(before, through, packages, Path(supplemental_raw_view))
        attempt = {"schema": fixed.SCHEMA, "method_id": fixed.METHOD, "input_id": identity,
            "head_before": before, "inputs": inputs, "status": "running_replay", "execution_progress_unknown": True}
        attempt_path = run / "attempts" / (identity + ".json")
        if "fixed" in failures:
            write(attempt_path, attempt)
            raise ValueError("synthetic unresolved fixed replay")
        record, refs = source(tmp_path / "new-source", through)
        attempt.update(status="committed", output=str(tmp_path / "new-source"), result=record)
        write(attempt_path, attempt)
        updated = {**before, "generation": before["generation"]+1, "last_completed_day": through,
            "source": refs, "supplement": fixed._reference(supplemental_raw_view), "last_attempt": str(attempt_path)}
        write(run / "head.json", updated)
        return {"status": "continued_research_only"}
    monkeypatch.setattr(fixed, "run", fixed_run)

    def product(route, source_ref, refs):
        day = fixed._read(Path(route["source"]) / "head.json").get("last_completed_day", OLD)
        return {"source_head": source_ref, "source_day": day,
            "execution_day": E if day == D else D, "status": "paper_candidates_only", "refs": refs, **opening.FLAGS}
    def adopt(route, source_ref, artifacts):
        return product(route, source_ref, artifacts)
    def produce(route, source_ref, out):
        counts["produce"] += 1
        reference = write(out / "result.json", {"source_head": source_ref, "synthetic_only": True})
        return product(route, source_ref, {"result": reference})
    def validate(route, source_ref, value):
        assert value["source_head"] == source_ref
        for ref in value["refs"].values(): fixed._checked(ref)
    monkeypatch.setattr(opening, "_factory", lambda: SimpleNamespace(adopt=adopt, produce=produce, validate_product=validate))
    old_artifact = write(tmp_path / "old-opening" / "result.json", {"synthetic": True})
    other_source = tmp_path / "other-source"; write(other_source / "head.json", {"last_completed_day": OLD})
    routes = [{"id": "fixed_lowvol", "family": "fixed", "source": str(run), "signal": registration["signal"],
               "adopt": {"result": old_artifact["path"]}},
              {"id": "other", "family": "eight_type", "source": str(other_source), "adopt": {"result": old_artifact["path"]}}]
    opening.register(coordinator, routes, current=NOW)
    return SimpleNamespace(run=run, coordinator=coordinator, package=package, operation=operation,
        head=head, metadata=metadata, counts=counts, failures=failures, raw=raw, source=source)


def invoke(b, **kwargs):
    return m.run(b.run, b.coordinator, D, b.package, b.operation, current=NOW, **kwargs)


def test_future_or_missing_source_waits_without_operation_or_consumer_calls(chain):
    b = chain
    b.package.rename(b.package.with_name("not-yet-D"))
    before = {p: p.read_bytes() for p in b.run.rglob("*") if p.is_file()}
    value = m.run(b.run, b.coordinator, D, b.package, b.operation,
                  current=datetime.fromisoformat("2026-10-17T12:00:00+08:00"))
    assert value["status"] == "waiting_for_complete_source" and not b.operation.exists()
    assert b.counts == {"load": 0, "append": 0, "fixed": 0, "produce": 0}
    assert invoke(b)["status"] == "waiting_for_complete_source" and not b.operation.exists()
    assert before == {p: p.read_bytes() for p in b.run.rglob("*") if p.is_file()}


def test_one_close_uses_original_three_components_and_completed_repeat_writes_nothing(chain, monkeypatch):
    b = chain
    value = invoke(b)
    assert value["status"] == "completed" and value["fixed_path_completed"]
    assert value["fixed_consumer_called_this_call"] and value["research_engine_advanced_this_call"]
    assert value["all_system_routes_refreshed"] is False  # Other route remains D16.
    assert b.counts == {"load": 1, "append": 1, "fixed": 1, "produce": 1}
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for root in (b.operation, b.run, b.coordinator)
              for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(m, "lock", lambda path: pytest.fail("Completed repeat has no writer lock"))
    repeated = invoke(b)
    assert repeated["status"] == "completed" and not repeated["fixed_consumer_called_this_call"]
    assert repeated["research_engine_advanced_this_call"] is False
    assert b.counts == {"load": 1, "append": 1, "fixed": 1, "produce": 1}
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for root in (b.operation, b.run, b.coordinator)
                      for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("stage", ["append", "fixed"])
def test_successful_stage_is_claimed_after_receipt_interruption_without_repeating_work(chain, monkeypatch, stage):
    b = chain
    original = m._save
    interrupted = []
    def fail(path, receipt):
        point = receipt["supplement"] is not None if stage == "append" else receipt["fixed"] is not None
        if point and not interrupted:
            interrupted.append(True)
            raise OSError("synthetic operation receipt interruption")
        return original(path, receipt)
    monkeypatch.setattr(m, "_save", fail)
    with pytest.raises(OSError, match="interruption"):
        invoke(b)
    monkeypatch.setattr(m, "_save", original)
    value = invoke(b)
    assert value["status"] == "completed"
    assert b.counts == {"load": 1, "append": 1, "fixed": 1, "produce": 1}


def test_incomplete_append_is_unknown_and_same_parent_failed_fixed_cannot_change_fingerprint(chain):
    b = chain; b.failures.add("fixed")
    value = invoke(b)
    assert value["status"] == "fixed_pending" and value["execution_progress_unknown"]
    assert value["research_engine_advanced_this_call"] is None
    assert invoke(b)["status"] == "fixed_pending"
    other = b.operation.with_name("attempt-other-operation")
    value = m.run(b.run, b.coordinator, D, b.package, other, current=NOW)
    assert value["status"] == "fixed_pending" and not (other / "supplement").exists()
    assert b.counts["append"] == b.counts["fixed"] == 1 and b.counts["produce"] == 0


def test_partial_append_without_success_result_is_not_rebuilt(chain):
    b = chain; b.failures.add("append")
    assert invoke(b)["status"] == "source_complete"
    value = invoke(b)
    assert value["reason"] == "append_progress_unknown_no_successful_result_no_retry"
    assert b.counts["append"] == 1 and b.counts["fixed"] == 0


def test_opening_saved_product_manual_recovery_never_replays_fixed_or_producer(chain, monkeypatch):
    b = chain
    commit = opening._commit
    monkeypatch.setattr(opening, "_commit", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic opening commit interruption")))
    value = invoke(b)
    assert value["status"] == "opening_pending" and "opening-daily recover" in value["manual_recovery_command"]
    assert invoke(b)["status"] == "opening_pending"
    assert b.counts["fixed"] == b.counts["produce"] == 1
    monkeypatch.setattr(opening, "_commit", commit)
    row = value["opening_observation"]
    opening.recover(b.coordinator, "fixed_lowvol", row["input_id"], current=NOW)
    assert invoke(b)["status"] == "completed"
    assert b.counts["load"] == b.counts["append"] == b.counts["fixed"] == b.counts["produce"] == 1


def test_wrong_calendar_skipped_day_or_overlap_rejected_before_append(chain):
    b = chain
    with pytest.raises(ValueError, match="one next session"):
        m.run(b.run, b.coordinator, E, b.package, b.operation, current=NOW)
    with pytest.raises(ValueError, match="overlap"):
        m.run(b.run, b.coordinator, D, b.package, b.run / "bad-operation", current=NOW)
    plan = fixed._read(b.package / "plan.json"); plan["calendar_sha256"] = "bad"
    write(b.package / "plan.json", plan)
    with pytest.raises(ValueError, match="calendar"):
        invoke(b)
    assert not b.operation.exists() and b.counts["append"] == b.counts["fixed"] == 0


def test_completed_operation_cannot_claim_a_later_head_as_its_old_day(chain):
    b = chain
    invoke(b)
    head = fixed._read(b.run / "head.json")
    _, refs = b.source(b.run.parent / "later-source", E)
    head.update(generation=head["generation"]+1, last_completed_day=E, source=refs)
    write(b.run / "head.json", head)
    before = (b.operation / "receipt.json").read_bytes()
    with pytest.raises(ValueError, match="superseded"):
        invoke(b)
    assert (b.operation / "receipt.json").read_bytes() == before
    assert b.counts["fixed"] == b.counts["produce"] == 1


def test_shared_workflow_lock_covers_pending_check_and_rejects_competing_output(chain, monkeypatch):
    b = chain
    shared = b.run / "close_to_opening.lock"
    competing = b.operation.with_name("competing-operation")
    held, injected = set(), []

    @contextmanager
    def nonblocking_lock(path):
        path = Path(path)
        if path in held:
            raise BlockingIOError("synthetic shared driver lock already held")
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)

    monkeypatch.setattr(m, "lock", nonblocking_lock)
    pending = m._pending_fixed_attempts
    append, consumer = m.append_sources, fixed.run

    def check(directory, parent):
        assert shared in held
        if not injected:
            assert b.operation / "operation.lock" in held
            injected.append(True)
            # Pause at the old check-to-call race boundary. A second out must
            # fail on the shared account lock before it can append or replay.
            with pytest.raises(BlockingIOError, match="shared driver lock"):
                m.run(b.run, b.coordinator, D, b.package, competing, current=NOW)
            assert b.counts["append"] == b.counts["fixed"] == 0
        return pending(directory, parent)

    def guarded_append(*args, **kwargs):
        assert shared in held and b.operation / "operation.lock" in held
        return append(*args, **kwargs)

    def guarded_consumer(*args, **kwargs):
        assert shared in held and b.operation / "operation.lock" in held
        return consumer(*args, **kwargs)

    monkeypatch.setattr(m, "_pending_fixed_attempts", check)
    monkeypatch.setattr(m, "append_sources", guarded_append)
    monkeypatch.setattr(fixed, "run", guarded_consumer)
    b.failures.add("fixed")
    assert invoke(b)["status"] == "fixed_pending" and not held
    # After the first caller leaves an unknown attempt, the second output can
    # acquire the lock but must observe that attempt before making a new raw.
    value = m.run(b.run, b.coordinator, D, b.package, competing, current=NOW)
    assert value["status"] == "fixed_pending" and not (competing / "supplement").exists()
    assert b.counts["append"] == b.counts["fixed"] == 1 and b.counts["produce"] == 0
    assert not held
