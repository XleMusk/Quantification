from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from trading.research import next_close_d0_d1 as contract


DAY = "2026-10-26"


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _source(path: Path, strategy: str, symbol: str) -> Path:
    return _write(path, {
        "strategy_id": strategy,
        "end_date": DAY,
        "execution_admitted": False,
        "strategy_validated": False,
        "selected_events": [{"date": DAY, "symbol": symbol, "score": 1.0}],
        "state": {"day": DAY, "targets": {symbol: 0.2}, "prices": {symbol: 1000}},
    })


def _fake_capture(stockdb_root, output_root, symbols, dates, minute_time="", **kwargs):
    output_root = Path(output_root)
    output_root.mkdir(parents=True)
    daily = output_root / "daily.csv"
    rows = [{"symbol": symbol, "date": dates[0], "code": symbol[:6],
             "open": 1000, "high": 1100, "low": 900, "close": 1050,
             "pre_close": 1000, "volume": 100, "amount": 1000}
            for symbol in symbols]
    pd.DataFrame(rows).to_csv(daily, index=False)
    import hashlib
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    metadata = {
        "schema": "stockdb_research_snapshot_v1", "captured_at": "2026-10-26T15:05:00+08:00",
        "requested_symbols": list(symbols), "requested_dates": list(dates),
        "effective_session_dates": list(dates), "rows": {"requested_daily": len(rows),
        "available_daily": len(rows), "missing_daily": 0, "available_minute": 0},
        "files": {"daily": {"path": "daily.csv", "sha256": digest}},
        "execution_admitted": False, "research_only": True,
    }
    _write(output_root / "snapshot.json", metadata)
    _write(output_root / "capture_receipt.json", {"status": "completed_snapshot_capture",
                                                   "execution_admitted": False})
    return metadata


def _setup(tmp_path: Path):
    fixed = _write(tmp_path / "fixed.json", {
        "strategy_id": "fixed_close", "day": DAY,
        "execution_admitted": False, "strategy_validated": False,
        "targets": {"600000.SSE": 0.2}, "prices": {"600000.SSE": 1000},
        "candidates": [{"symbol": "600000.SSE", "score": 1.0}],
    })
    candidate = _source(tmp_path / "candidate.json", "candidate_close", "000001.SZSE")
    sources = _write(tmp_path / "sources.json", {"candidates": [
        {"candidate_id": "candidate_a", "replay_result": str(candidate), "strategy_id": "candidate_close"}
    ]})
    catalog = _write(tmp_path / "catalog.json", {"entries": [{"id": "candidate_a",
        "research_selection": {"research_followup_allowed": True}}]})
    return fixed, sources, catalog


def test_next_close_contract_captures_same_day_quotes_and_shared_bindings(tmp_path, monkeypatch):
    fixed, sources, catalog = _setup(tmp_path)
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    receipt = contract.run_contract(
        sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
        stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
        execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000.0)
    assert receipt["status"] == contract.STATUS
    assert receipt["execution_admitted"] is False
    assert receipt["cost_benchmark_binding"]["cost_model_id"] == "cn_a_share_standard_ledger_costs_v1"
    assert receipt["cost_benchmark_binding"]["benchmark_binding_id"] == "paper_target_projection_no_market_benchmark_v1"
    target = json.loads((tmp_path / "run" / "inputs" / "candidate_a" / "target_payload.json").read_text())
    quote = json.loads((tmp_path / "run" / "inputs" / "candidate_a" / "quote_payload.json").read_text())
    assert target["decision_day"] == DAY
    assert quote["snapshot_date"] == DAY
    assert quote["prices"]["000001.SZSE"]["observed_date"] == DAY
    assert receipt["time_semantics"]["source_day"] == DAY
    assert receipt["time_semantics"]["decision_cutoff_at"] == "2026-10-26T15:05:00+08:00"
    assert receipt["time_semantics"]["plan_published_at"] is None
    reused = contract.run_contract(
        sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
        stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
        execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000)
    assert reused["reused_existing_run"] is True


def test_next_close_accepts_same_day_prebuilt_target_payload(tmp_path, monkeypatch):
    fixed, _, catalog = _setup(tmp_path)
    target = _write(tmp_path / "target_payload.json", {
        "schema": "stockdb_candidate_shadow_target_v1",
        "strategy_id": "candidate_close",
        "decision_day": DAY,
        "research_only": True,
        "execution_admitted": False,
        "weights": {"000001.SZSE": 0.2},
        "signal_frame": {"signals": [{
            "security": {"code": "000001", "exchange": "SZSE"},
            "diagnostics": {"rank": 1, "score": 1.0},
        }]},
    })
    sources = _write(tmp_path / "direct_sources.json", {"candidates": [{
        "candidate_id": "candidate_a", "target": str(target), "strategy_id": "candidate_close",
    }]})
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    receipt = contract.run_contract(
        sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
        stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
        execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000.0)
    assert receipt["status"] == contract.STATUS
    assert receipt["execution_admitted"] is False
    assert receipt["candidate_source_manifest"]
    assert json.loads((tmp_path / "run" / "inputs" / "candidate_a" / "target_payload.json").read_text())["strategy_id"] == "candidate_close"


def test_next_close_can_resolve_registered_fixed_daily_head(tmp_path, monkeypatch):
    _, sources, catalog = _setup(tmp_path)
    monkeypatch.setattr(contract.stockdb_fixed_daily, "status", lambda _run_dir: {
        "terminal_account": str(tmp_path / "fixed.json"), "signal": "fixed_close",
    })
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    receipt = contract.run_contract(
        sources, tmp_path / "run", fixed_run_dir=tmp_path / "fixed_daily",
        fixed_strategy_id=None, stockdb_root=tmp_path / "stockdb", catalog_path=catalog,
        close_date=DAY, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0)
    assert receipt["status"] == contract.STATUS
    assert receipt["fixed"]["strategy_id"] == "fixed_close"
    assert receipt["execution_admitted"] is False


def test_next_close_can_emit_budgeted_joint_paper_package(tmp_path, monkeypatch):
    fixed, sources, catalog = _setup(tmp_path)
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    receipt = contract.run_contract(
        sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
        stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
        execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000.0,
        joint_capacity_policy="budgeted", joint_max_target_rows=22)
    assert receipt["status"] == contract.STATUS
    assert receipt["joint_paper"]["status"] == "COMPLETED_RESEARCH_ONLY_JOINT_PAPER_PACKAGE"
    assert receipt["joint_paper"]["paper_order_count"] == 2
    assert (tmp_path / "run" / "joint_paper" / "joint_paper_receipt.json").is_file()


def test_next_close_contract_preserves_missing_rows_and_blocks_batch(tmp_path, monkeypatch):
    fixed, sources, catalog = _setup(tmp_path)

    def capture_missing(stockdb_root, output_root, symbols, dates, minute_time="", **kwargs):
        metadata = _fake_capture(stockdb_root, output_root, symbols[:1], dates, minute_time, **kwargs)
        metadata["requested_symbols"] = list(symbols)
        metadata["rows"] = {"requested_daily": len(symbols), "available_daily": 1,
                             "missing_daily": len(symbols) - 1, "available_minute": 0}
        _write(Path(output_root) / "snapshot.json", metadata)
        frame = pd.read_csv(Path(output_root) / "daily.csv")
        frame.to_csv(Path(output_root) / "daily.csv", index=False)
        return metadata

    monkeypatch.setattr(contract, "freeze_snapshot", capture_missing)
    receipt = contract.run_contract(
        sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
        stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
        execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000.0)
    assert receipt["status"] == contract.BLOCKED_STATUS
    assert receipt["execution_admitted"] is False
    assert receipt["missing_symbols"]
    assert not (tmp_path / "run" / "candidate_batch" / "batch_receipt.json").exists()


def test_next_close_contract_rejects_stale_target_before_capture(tmp_path, monkeypatch):
    fixed, sources, catalog = _setup(tmp_path)
    stale = json.loads((tmp_path / "candidate.json").read_text())
    stale["end_date"] = "2026-10-23"
    stale["state"]["day"] = "2026-10-23"
    _write(tmp_path / "candidate.json", stale)
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    try:
        contract.run_contract(
            sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
            stockdb_root=tmp_path / "stockdb", catalog_path=catalog, close_date=DAY,
            execution_day="2026-10-27", settlement_day="2026-10-28", initial_cash=100000.0)
    except ValueError as error:
        assert "decision_day" in str(error)
    else:
        raise AssertionError("stale target must be rejected")
    assert json.loads((tmp_path / "run" / "contract_receipt.json").read_text())["status"] == contract.FAILED_STATUS


def test_next_close_binds_adjacent_dates_to_pinned_calendar(tmp_path, monkeypatch):
    fixed, sources, catalog = _setup(tmp_path)
    calendar = _write(tmp_path / "calendar.json", {
        "schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "fixture_2026",
        "year": 2026, "closed_ranges": [], "open_sessions": [],
    })
    monkeypatch.setattr(contract, "freeze_snapshot", _fake_capture)
    try:
        contract.run_contract(
            sources, tmp_path / "run", fixed_terminal_account=fixed, fixed_strategy_id="fixed_close",
            stockdb_root=tmp_path / "stockdb", catalog_path=catalog, calendar_path=calendar,
            close_date=DAY, execution_day="2026-10-28", settlement_day="2026-10-29", initial_cash=100000.0)
    except ValueError as error:
        assert "adjacent sessions" in str(error)
    else:
        raise AssertionError("non-adjacent D0/D1 dates must be rejected")
    assert json.loads((tmp_path / "run" / "contract_receipt.json").read_text())["status"] == contract.FAILED_STATUS


def test_next_close_decision_bound_filter_keeps_only_continue_candidates(tmp_path):
    items = [
        {"candidate_id": "keep", "decision_id": "keep", "target": "keep.json"},
        {"candidate_id": "stop", "decision_id": "stop", "target": "stop.json"},
    ]
    receipt = _write(tmp_path / "decisions.json", {"strategies": [
        {"id": "keep", "research_decision": "CONTINUE_RESEARCH_ONLY"},
        {"id": "stop", "research_decision": "STOP_RESEARCH_ONLY"},
    ]})
    selected, selection = contract._decision_bound_candidates(items, receipt)
    assert [item["candidate_id"] for item in selected] == ["keep"]
    assert selection["included"] == [{"candidate_id": "keep", "decision_id": "keep", "decision": "CONTINUE_RESEARCH_ONLY"}]
    assert selection["excluded"] == [{"candidate_id": "stop", "decision_id": "stop", "decision": "STOP_RESEARCH_ONLY"}]

