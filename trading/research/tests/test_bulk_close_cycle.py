"""Closed-clock gates, exact cache reuse and one-pass failure semantics."""
from datetime import datetime
import json
from pathlib import Path
import shutil

import pytest

from trading.research import bulk_close_cycle as cycle
from trading.research.tests.test_baostock_bulk_close import sources as bulk_sources, DAY, NOW


@pytest.fixture
def sources(bulk_sources, tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "DATA_ROOT", tmp_path / "data")
    return bulk_sources


def wheel(sources):
    return Path(json.loads((sources[0] / "receipt.json").read_text())["sdk"]["wheel"])


def invoke(sources, out, **kwargs):
    return cycle.run(DAY, sources[2], wheel(sources), out, _now=NOW, **kwargs)


def fake_provider_stages(sources, monkeypatch, *, failure=False):
    probe, roster, _, _ = sources
    calls = []
    sdk = json.loads((probe / "receipt.json").read_text())["sdk"]
    def select(path):
        calls.append("sdk")
        return sdk
    def collect(days, kinds, sdk_info, destination):
        assert days == [DAY] and kinds == ["stock"] and sdk_info is sdk
        calls.append("bulk")
        if failure:
            destination.mkdir()
            answer = {"status": "failed_probe", "failed_query": {"date": DAY}}
            (destination / "receipt.json").write_text(json.dumps(answer))
            return destination, answer
        shutil.copytree(probe, destination)
        return destination, json.loads((destination / "receipt.json").read_text())
    def query(day, sdk_path, destination, *, _sdk_info=None):
        assert day == DAY and _sdk_info is sdk
        calls.append("roster")
        shutil.copytree(roster, destination)
        receipt_path = destination / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["raw_csv"] = str(destination / Path(receipt["raw_csv"]).name)
        receipt_path.write_text(json.dumps(receipt))
        return receipt
    monkeypatch.setattr(cycle.daily, "select_sdk", select)
    monkeypatch.setattr(cycle.daily, "collect", collect)
    monkeypatch.setattr(cycle.roster_api, "probe", query)
    return calls


def test_before_close_does_not_import_sdk_inspect_process_lock_or_create_directory(sources, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No SDK/provider/process operation before the actual close")
    monkeypatch.setattr(cycle.daily, "select_sdk", forbidden)
    monkeypatch.setattr(cycle.daily, "collect", forbidden)
    monkeypatch.setattr(cycle.roster_api, "probe", forbidden)
    out = tmp_path / "nested/new_cycle"
    result = cycle.run("2026-10-15", sources[2], wheel(sources), out,
        inspect_process=forbidden, _now=datetime.fromisoformat("2026-10-15T14:59:59+08:00"))
    assert result["status"] == "waiting_for_market_close"
    assert result["provider_contacted"] is result["output_directory_created"] is False
    assert not out.parent.exists() and not cycle.DATA_ROOT.exists()


@pytest.mark.parametrize("when", ["2026-10-15T15:00:00+08:00", "2026-10-15T17:29:59+08:00", "2026-10-15T09:29:59+00:00"])
@pytest.mark.parametrize("cached", ["none", "bulk", "roster"])
def test_today_before_provider_update_has_no_sdk_process_lock_or_output(sources, tmp_path, monkeypatch, when, cached):
    def forbidden(*args, **kwargs):
        pytest.fail("Before provider update no new SDK, process inspection, cache import or lock is allowed")
    for module, name in ((cycle.daily, "select_sdk"), (cycle.daily, "collect"), (cycle.roster_api, "probe"),
                         (cycle, "_active_collector"), (cycle.importer, "import_bulk_close")):
        monkeypatch.setattr(module, name, forbidden)
    options = {"bulk_probe": sources[0]} if cached == "bulk" else {"roster": sources[1]} if cached == "roster" else {}
    out = tmp_path / "not_created/new_cycle"
    answer = cycle.run("2026-10-15", sources[2], wheel(sources), out,
                       inspect_process=forbidden, _now=datetime.fromisoformat(when), **options)
    assert answer["status"] == "waiting_for_provider_daily_update"
    assert answer["earliest_expected_at"] == "2026-10-15T17:30:00+08:00"
    assert answer["source_url"] == cycle.PROVIDER_UPDATE_SOURCE_URL
    assert answer["provider_contacted"] is answer["output_directory_created"] is answer["job_started"] is False
    assert answer["provider_readiness_verified"] is answer["scheduled_time_is_quality_admission"] is False
    assert answer["checks"]["market_session_actually_closed"] is True
    assert not out.parent.exists() and not cycle.DATA_ROOT.exists()


def _same_day_cache_clocks(sources):
    """Synthetic already-successful sources, not actual pre-update acquisition."""
    bulk_receipt, roster_receipt = sources[0] / "receipt.json", sources[1] / "receipt.json"
    bulk = json.loads(bulk_receipt.read_text())
    bulk.update(created_at=DAY + "T15:10:00+08:00", finished_at=DAY + "T15:11:00+08:00")
    bulk_receipt.write_text(json.dumps(bulk))
    roster = json.loads(roster_receipt.read_text())
    roster["completed_at"] = DAY + "T15:12:00+08:00"
    roster_receipt.write_text(json.dumps(roster))


def test_1730_allows_original_one_pass_path_but_does_not_skip_actual_validation(sources, tmp_path, monkeypatch):
    _same_day_cache_clocks(sources)
    calls = fake_provider_stages(sources, monkeypatch)
    answer = cycle.run(DAY, sources[2], wheel(sources), tmp_path / "new_cycle",
                       _now=datetime.fromisoformat(DAY + "T17:30:00+08:00"))
    assert answer["status"] == "completed_bulk_close_cycle_research_only"
    assert calls == ["sdk", "bulk", "roster"] and answer["ordinary_symbols"] == 3


def test_explicit_both_caches_before_update_still_strictly_import_without_sdk(sources, tmp_path, monkeypatch):
    _same_day_cache_clocks(sources)
    calls = fake_provider_stages(sources, monkeypatch)
    answer = cycle.run(DAY, sources[2], wheel(sources), tmp_path / "cached_cycle", bulk_probe=sources[0], roster=sources[1],
                       _now=datetime.fromisoformat(DAY + "T16:00:00+08:00"))
    assert answer["status"] == "completed_bulk_close_cycle_research_only"
    assert calls == [] and answer["provider_contacted"] is False and answer["ordinary_symbols"] == 3


def test_existing_output_no_retry_precedes_provider_wait_guard(sources, tmp_path):
    out = tmp_path / "existing_cycle"
    out.mkdir()
    receipt = out / "receipt.json"
    receipt.write_text('{"prior_failure":"preserved"}')
    before = receipt.read_bytes()
    answer = cycle.run("2026-10-15", sources[2], wheel(sources), out,
                       _now=datetime.fromisoformat("2026-10-15T16:00:00+08:00"))
    assert answer["status"] == "blocked_existing_cycle_no_retry" and receipt.read_bytes() == before
    assert not cycle.DATA_ROOT.exists()


def test_main_provider_wait_is_a_successful_nonstarting_status(sources, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cycle, "run", lambda *args, **kwargs: {"status": "waiting_for_provider_daily_update", "provider_contacted": False})
    code = cycle.main(["--day", "2026-10-15", "--calendar", str(sources[2]), "--sdk-wheel", str(wheel(sources)), "--out", str(tmp_path / "none")])
    assert code == 0 and json.loads(capsys.readouterr().out)["provider_contacted"] is False


def test_nontrading_day_is_blocked_before_any_output_or_provider_operation(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(cycle.daily, "select_sdk", lambda *a: pytest.fail("Invalid date cannot load SDK"))
    out = tmp_path / "weekend"
    result = cycle.run("2026-10-10", sources[2], wheel(sources), out, _now=NOW)
    assert result["status"] == "blocked_invalid_closed_session" and not out.exists()


def test_new_cycle_calls_sdk_once_and_bulk_roster_import_in_order(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch)
    result = invoke(sources, tmp_path / "cycle")
    assert result["status"] == "completed_bulk_close_cycle_research_only"
    assert calls == ["sdk", "bulk", "roster"]
    assert result["bulk_collector_invocations_this_run"] == result["roster_probe_invocations_this_run"] == 1
    assert result["ordinary_symbols"] == 3 and result["nullable_suspension_rows"] == 1
    assert result["retries"] == 0 and result["execution_admitted"] is False
    assert not (cycle.DATA_ROOT / "research/bulk_close_cycles/.active_cycle.lock").exists()
    assert Path(result["full_close_package"]).joinpath("raw_daily.csv").is_file()


@pytest.mark.parametrize("cached", ["both", "bulk", "roster"])
def test_only_missing_stage_is_called_and_successful_cache_is_not_rewritten(sources, tmp_path, monkeypatch, cached):
    calls = fake_provider_stages(sources, monkeypatch)
    before = {path: path.read_bytes() for folder in sources[:2] for path in folder.rglob("*") if path.is_file()}
    kwargs = {}
    if cached in {"both", "bulk"}:
        kwargs["bulk_probe"] = sources[0]
    if cached in {"both", "roster"}:
        kwargs["roster"] = sources[1]
    result = invoke(sources, tmp_path / "cycle", **kwargs)
    assert result["status"] == "completed_bulk_close_cycle_research_only"
    assert calls == {"both": [], "bulk": ["sdk", "roster"], "roster": ["sdk", "bulk"]}[cached]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_failed_bulk_stops_before_roster_and_second_run_is_not_a_retry(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch, failure=True)
    out = tmp_path / "cycle"
    first = invoke(sources, out)
    assert first["status"] == "failed_stage_no_retry" and calls == ["sdk", "bulk"]
    before = (out / "receipt.json").read_bytes()
    second = invoke(sources, out)
    assert second["status"] == "blocked_existing_cycle_no_retry" and calls == ["sdk", "bulk"]
    assert (out / "receipt.json").read_bytes() == before


@pytest.mark.parametrize("corrupt", ["hash", "wrongday", "failed"])
def test_invalid_explicit_cache_never_triggers_reacquisition(sources, tmp_path, monkeypatch, corrupt):
    calls = fake_provider_stages(sources, monkeypatch)
    path = sources[0] / "receipt.json"
    receipt = json.loads(path.read_text())
    if corrupt == "hash":
        receipt["entries"][0]["sha256"] = "bad"
    elif corrupt == "wrongday":
        receipt["days"] = ["2026-10-14"]
    else:
        receipt["status"] = "failed_probe"
    path.write_text(json.dumps(receipt))
    result = invoke(sources, tmp_path / "cycle", bulk_probe=sources[0])
    assert result["status"] == "failed_stage_no_retry" and calls == []
    assert result["stage"] == "cache_validation" and result["provider_contacted"] is False


def old_job(status):
    root = cycle.DATA_ROOT / "research/full_close"
    source = root / "old"
    source.mkdir(parents=True)
    (source / "receipt.json").write_text(json.dumps({"schema": cycle.FULL_CLOSE_SCHEMA, "status": status,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
        "stockdb_modified": False, "ledger_mutated": False}))
    (root / "active_job.json").write_text(json.dumps({"schema": "full_close_active_job_locator_v1",
        "folder": str(source), "collector_pid": 123, "collector_process_started_at": "2026-10-15T00:00:00+00:00",
        "execution_admitted": False}))


@pytest.mark.parametrize("process", ["verified_alive", "identity_unverified"])
def test_verified_or_unknown_existing_collector_blocks_new_cycle(sources, tmp_path, monkeypatch, process):
    calls = fake_provider_stages(sources, monkeypatch)
    old_job("running")
    out = tmp_path / "cycle"
    result = invoke(sources, out, inspect_process=lambda *a: {"status": process})
    assert result["status"] == "blocked_existing_collector" and calls == [] and not out.exists()


def test_terminal_source_is_not_a_live_collector_even_if_pid_remains(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch)
    old_job("complete_full_dated_close_inventory_not_admitted")
    result = invoke(sources, tmp_path / "cycle", bulk_probe=sources[0], roster=sources[1],
        inspect_process=lambda *a: pytest.fail("Terminal receipt must not be displayed as active PID"))
    assert result["status"] == "completed_bulk_close_cycle_research_only" and calls == []


def test_terminal_word_without_authoritative_source_contract_blocks_new_cycle(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch)
    old_job("complete_full_dated_close_inventory_not_admitted")
    path = cycle.DATA_ROOT / "research/full_close/old/receipt.json"
    valid = json.loads(path.read_text())
    # Neither an unrelated terminal receipt nor an unknown research boundary
    # may bypass the existing collector guard merely by naming a final state.
    for index, change in enumerate(({"schema": "unrelated_status_receipt"}, {"ledger_mutated": None})):
        path.write_text(json.dumps(valid | change))
        out = tmp_path / f"blocked_{index}"
        result = invoke(sources, out, inspect_process=lambda *a: pytest.fail("Invalid source identity is already unknown"))
        assert result["status"] == "blocked_existing_collector"
        assert result["collector"]["status"] == "identity_unverified"
        assert calls == [] and not out.exists()


def test_existing_cycle_claim_blocks_duplicate_invocation_and_is_preserved(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch)
    folder = cycle.DATA_ROOT / "research/bulk_close_cycles"
    folder.mkdir(parents=True)
    lock = folder / ".active_cycle.lock"
    lock.write_text(json.dumps({"pid": 123, "process_started_at": "2026-10-15T00:00:00+00:00"}))
    before = lock.read_bytes()
    out = tmp_path / "cycle"
    result = invoke(sources, out, inspect_process=lambda *a: {"status": "not_running"})
    assert result["status"] == "blocked_existing_cycle_claim_no_retry" and calls == []
    assert not out.exists() and lock.read_bytes() == before


def test_status_only_reads_terminal_receipt_without_provider_or_mutation(sources, tmp_path, monkeypatch):
    calls = fake_provider_stages(sources, monkeypatch)
    out = tmp_path / "cycle"
    invoke(sources, out, bulk_probe=sources[0], roster=sources[1])
    before = {path: path.read_bytes() for path in out.rglob("*") if path.is_file()}
    result = cycle.status(out, inspect_process=lambda *a: pytest.fail("Terminal status does not inspect live process"))
    assert result["status_only"] is True and result["provider_contacted_this_call"] is False
    assert calls == [] and all(path.read_bytes() == content for path, content in before.items())
