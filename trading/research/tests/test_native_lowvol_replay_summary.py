"""Tiny saved-file fixtures, not market replays or performance recalculation."""
import csv
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

import run_committee
from trading import research_diagnostics as diagnostics
from trading.research import native_lowvol_replay_summary as m


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def fixture_saved(tmp_path):
    directory = tmp_path / "native_lowvol_replay" / "saved_run"
    directory.mkdir(parents=True)
    days = ["2026-06-23", "2026-06-24", "2026-06-25"]
    registration = {"method_id": m.SOURCE_SCHEMA, "start": days[0], "end": days[-1],
        "config": {"initial_cash": 1000, "holdings": 20}, "research_only": True,
        "execution_admitted": False, "execution_source": {"requested_symbols": ["600000.SSE"]}}
    state = {"day": days[-1], "cash": 97000, "orders": {"o1": {}}, "fills": {"f1": {}}}
    values = {"sessions": 3, "net_return": -.03, "max_drawdown": -.03,
        "fees": 10., "final_equity": 970., "sharpe_zero_rf_252": -1.2, "stale_valuation_days": 0}
    result = {"schema": m.SOURCE_SCHEMA, "status": "completed", "error": None,
        "registration": registration, "completed_sessions": 3, "requested_sessions": 3,
        "reused_sessions": 2, "newly_completed_sessions": 1, "resume_from": "synthetic_prior_2",
        "orders": 1, "fills": 1, "metrics": values, "pending_targets": None,
        "open_positions": {}, "remaining_full_exits": {}, "metric_basis": "native_raw_close_with_reported_action_components_research_diagnostic",
        **m.FLAGS}
    for name, value in (("registration.json", registration), ("account.json", state), ("result.json", result)):
        write(directory / name, value)
    with (directory / "daily.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["date", "cash", "equity"])
        writer.writeheader()
        writer.writerows({"date": day, "cash": cash, "equity": cash}
            for day, cash in zip(days, (990, 980, 970)))
    output = tmp_path / "native_lowvol_replay_summaries" / "saved_run.json"
    return directory, output, result


def test_summary_preserves_negative_saved_metrics_counts_and_four_pins(tmp_path):
    source, output, result = fixture_saved(tmp_path)
    before = {p: p.read_bytes() for p in source.iterdir()}
    saved = m.build_summary(source, output)
    assert saved["metrics"] == result["metrics"] and saved["metrics"]["net_return"] < 0
    assert (saved["reused_sessions"], saved["newly_completed_sessions"]) == (2, 1)
    assert saved["returns_reportable"] is False and saved["shared_engine_ledger"] is True
    assert saved["new_cash_account_created"] is False
    assert set(saved["source_refs"]) == set(m.FILES) and output.stat().st_size < 10000
    assert all(path.read_bytes() == value for path, value in before.items())
    with pytest.raises(FileExistsError):
        m.build_summary(source, output)
    with pytest.raises(ValueError, match="outside"):
        m.build_summary(source, source / "forbidden_summary.json")


def test_board_only_reads_small_summary_and_source_change_bypasses_old_cache(tmp_path, monkeypatch):
    data = tmp_path / "data" / "research"
    source, output, _ = fixture_saved(data)
    m.build_summary(source, output)
    monkeypatch.setattr(diagnostics.settings, "DATA_ROOT", data.parent)
    monkeypatch.setattr(diagnostics.settings, "ROOT", tmp_path / "repository")
    monkeypatch.setattr(diagnostics.replay_reports, "catalog", lambda: [])
    monkeypatch.setattr(diagnostics, "_CANDIDATE_BOARD_CACHE", None)
    original_json, reads = m._json, []

    def small_only(path):
        assert Path(path).resolve() == output.resolve(), "GET must not parse saved large source JSON"
        reads.append(Path(path))
        return original_json(path)

    monkeypatch.setattr(m, "_json", small_only)
    assert not any(row["family"] == m.FAMILY for row in diagnostics.catalog())
    first = diagnostics.candidate_board()
    row = next(row for row in first["rows"] if row["family"] == m.FAMILY)
    assert row["net_return"] == -.03 and row["reused_sessions"] == 2
    assert row["order_count"] == 1 and row["fill_count"] == 1
    assert row["order_count"] == 1 and row["fill_count"] == 1
    assert row["research_comparison_eligible"] is False and row["returns_reportable"] is False
    path = source / "result.json"
    stat = path.stat()
    content = path.read_bytes().replace(b'"status": "completed"', b'"status": "corrupted"')
    assert len(content) == stat.st_size
    path.write_bytes(content)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size and path.stat().st_mtime_ns == stat.st_mtime_ns
    second = diagnostics.candidate_board()
    unavailable = next(row for row in second["rows"] if row["family"] == m.FAMILY)
    assert unavailable["status"] == "unavailable" and unavailable["net_return"] is None
    assert unavailable["order_count"] is None and unavailable["fill_count"] is None
    assert unavailable["fees"] is None and "来源已变化" in unavailable["blockers"][0]
    assert unavailable["order_count"] is None and unavailable["fill_count"] is None
    assert reads and set(reads) == {output}


def test_no_fills_stays_null_and_incomplete_source_never_creates_summary(tmp_path):
    source, output, result = fixture_saved(tmp_path)
    state = m._json(source / "account.json")
    state["fills"] = {}
    write(source / "account.json", state)
    result.update(fills=0, metrics=None)
    write(source / "result.json", result)
    saved = m.build_summary(source, output)
    assert saved["metrics"] is None and saved["final_equity_rmb"] is None
    incomplete = deepcopy(result)
    incomplete.update(status="incomplete", error={"day": "2026-06-25", "message": "synthetic missing source"})
    write(source / "result.json", incomplete)
    target = output.with_name("incomplete.json")
    with pytest.raises(ValueError, match="completed unadmitted"):
        m.build_summary(source, target)
    assert not target.exists()


def test_explicit_cli_dispatch_only_builds_new_summary(tmp_path, capsys):
    source, output, _ = fixture_saved(tmp_path)
    assert run_committee.main(["native-lowvol-replay-summary", "--run-dir", str(source), "--out", str(output)]) == 0
    returned = json.loads(capsys.readouterr().out)
    assert returned["sessions"] == 3 and returned["metrics"]["net_return"] == -.03
    assert output.exists()
