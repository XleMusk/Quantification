from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research import post_close_d0_d1 as orchestrator


CLOSE = "2026-10-26"


def _inputs(tmp_path: Path):
    source = tmp_path / "same_day_sources.json"
    source.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    fixed_dir = tmp_path / "fixed_daily"
    fixed_dir.mkdir()
    package = tmp_path / "complete_close_package"
    package.mkdir()
    (package / "plan.json").write_text(json.dumps({"day": CLOSE}), encoding="utf-8")
    (package / "receipt.json").write_text(json.dumps({"day": CLOSE}), encoding="utf-8")
    stockdb = tmp_path / "stockdb"
    stockdb.mkdir()
    return source, fixed_dir, package, stockdb


def test_post_close_advances_fixed_daily_before_d0_d1(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    calls: list[str] = []

    def fixed_run(*args, **kwargs):
        calls.append("fixed")
        assert args[0] == fixed_dir
        assert args[1] == CLOSE
        assert kwargs["packages"] == [package]
        return {"status": "continued_research_only", "last_completed_day": CLOSE,
                "execution_admitted": False}

    def d0_run(*args, **kwargs):
        calls.append("d0")
        assert kwargs["fixed_run_dir"] == fixed_dir
        return {"status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
                "execution_admitted": False}

    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", fixed_run)
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract", d0_run)
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert calls == ["fixed", "d0"]
    assert receipt["status"] == orchestrator.STATUS
    assert receipt["execution_admitted"] is False
    assert (tmp_path / "run" / "post_close_receipt.json").is_file()


def test_post_close_waits_for_complete_fixed_source_without_d0_projection(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "waiting_for_complete_sources", "last_completed_day": "2026-10-23",
        "missing_days": [CLOSE], "execution_admitted": False,
    })
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract",
                        lambda *args, **kwargs: pytest.fail("D0/D1 must wait for fixed sources"))
    receipt = orchestrator.run_pipeline(
        source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
        close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
        initial_cash=100000.0, stockdb_root=stockdb)
    assert receipt["status"] == orchestrator.WAITING_STATUS
    assert receipt["missing_days"] == [CLOSE]
    assert receipt["execution_admitted"] is False


def test_post_close_preserves_d0_failure_receipt(tmp_path: Path, monkeypatch) -> None:
    source, fixed_dir, package, stockdb = _inputs(tmp_path)
    monkeypatch.setattr(orchestrator.stockdb_fixed_daily, "run", lambda *args, **kwargs: {
        "status": "continued_research_only", "last_completed_day": CLOSE,
        "execution_admitted": False,
    })
    monkeypatch.setattr(orchestrator.next_close_d0_d1, "run_contract",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("synthetic D0 failure")))
    with pytest.raises(ValueError, match="synthetic D0 failure"):
        orchestrator.run_pipeline(
            source, tmp_path / "run", fixed_run_dir=fixed_dir, packages=[package],
            close_date=CLOSE, execution_day="2026-10-27", settlement_day="2026-10-28",
            initial_cash=100000.0, stockdb_root=stockdb)
    failure = json.loads((tmp_path / "run" / "post_close_receipt.json").read_text(encoding="utf-8"))
    assert failure["status"] == orchestrator.FAILED_STATUS
    assert failure["execution_admitted"] is False
