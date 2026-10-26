from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from trading.research.current_window_strategy_comparison import build_receipt


def _csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path, name: str, snapshot: str, *, unsafe: bool = False) -> tuple[Path, Path]:
    root = tmp_path / name
    account = root / "account"
    account.mkdir(parents=True)
    run = root / "run.json"
    protocol = root / "protocol.json"
    run.write_text(json.dumps({
        "schema": "stockdb_rolling_low_volatility_ledger_v1",
        "status": "completed_research_only",
        "artifacts": str(account),
        "replay_start": "2026-08-31",
        "replay_end": "2026-10-26",
        "variants": [name],
        "execution_admitted": unsafe,
        "strategy_validated": False,
        "fresh_holdout_claimed": False,
        "results": [{"status": "completed", "signal": name, "sessions": 3}],
    }), encoding="utf-8")
    protocol.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")
    _csv(account / "daily.csv", ["date", "equity", "turnover_value"], [
        {"date": "2026-10-08", "equity": 100.0, "turnover_value": 10.0},
        {"date": "2026-10-09", "equity": 101.0, "turnover_value": 20.0},
        {"date": "2026-10-12", "equity": 102.0, "turnover_value": 30.0},
    ])
    _csv(account / "orders.csv", ["day", "fees"], [
        {"day": "2026-10-09", "fees": 100},
        {"day": "2026-10-12", "fees": 200},
    ])
    _csv(account / "fills.csv", ["day"], [
        {"day": "2026-10-09"},
        {"day": "2026-10-12"},
    ])
    (account / "account.json").write_text("{}", encoding="utf-8")
    (account / "replay.json").write_text("{}", encoding="utf-8")
    return run, account


def test_builds_explicit_current_window_and_keeps_research_boundary(tmp_path: Path) -> None:
    fixed_run, fixed_account = _fixture(tmp_path, "fixed", "snapshot-v1")
    candidate_run, candidate_account = _fixture(tmp_path, "candidate", "snapshot-v1")
    receipt = build_receipt(
        fixed_run=fixed_run,
        fixed_account_dir=fixed_account,
        candidate_run=candidate_run,
        candidate_account_dir=candidate_account,
    )
    assert receipt["status"] == "COMPLETED_RESEARCH_ONLY_CURRENT_WINDOW_COMPARISON"
    assert receipt["common_window"]["sessions"] == 3
    assert receipt["stockdb_snapshot"].endswith("snapshot-v1")
    assert receipt["execution_admitted"] is False
    assert receipt["promotion_allowed"] is False
    assert receipt["strategies"][0]["metrics"]["fees_rmb"] == pytest.approx(3.0)
    assert receipt["strategies"][0]["research_decision"] == "CONTINUE_RESEARCH_ONLY"


def test_rejects_different_stockdb_snapshot(tmp_path: Path) -> None:
    fixed_run, fixed_account = _fixture(tmp_path, "fixed", "snapshot-v1")
    candidate_run, candidate_account = _fixture(tmp_path, "candidate", "snapshot-v2")
    with pytest.raises(ValueError, match="do not share one StockDB snapshot"):
        build_receipt(
            fixed_run=fixed_run,
            fixed_account_dir=fixed_account,
            candidate_run=candidate_run,
            candidate_account_dir=candidate_account,
        )


def test_rejects_unsafe_run(tmp_path: Path) -> None:
    fixed_run, fixed_account = _fixture(tmp_path, "fixed", "snapshot-v1", unsafe=True)
    candidate_run, candidate_account = _fixture(tmp_path, "candidate", "snapshot-v1")
    with pytest.raises(ValueError, match="Unsafe or overstated run flags"):
        build_receipt(
            fixed_run=fixed_run,
            fixed_account_dir=fixed_account,
            candidate_run=candidate_run,
            candidate_account_dir=candidate_account,
        )
