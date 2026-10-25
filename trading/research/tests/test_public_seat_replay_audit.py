import json
from pathlib import Path

from trading.research.public_seat_replay_audit import BLOCKED, VERIFIED, audit_run, run


def _fixture(tmp_path: Path, *, bad_gate: bool = False) -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "protocol.json").write_text(json.dumps({
        "execution_admitted": False if not bad_gate else True,
        "live_orders": False,
        "availability": "unknown publication timestamp; lagged assumption",
        "fit": ["2024-01-01", "2024-06-30"],
        "evaluation": ["2024-07-01", "2024-12-31"],
    }), encoding="utf-8")
    (artifacts / "selected_seats.json").write_text("{}", encoding="utf-8")
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({
        "research_family": "full_source_public_seat_donchian_v1",
        "status": "partial_seat_bulk_diagnostic",
        "execution_admitted": False,
        "artifacts": str(artifacts),
        "start": "2024-07-01",
        "end": "2024-12-31",
        "coverage": {"published_at_known": False},
        "results": [
            {"signal": "TREND", "status": "completed", "completed_sessions": 2, "sessions": 2,
             "net_return": 0.1, "max_drawdown": -0.2, "fees": 5.0, "orders": 3, "fills": 3},
            {"signal": "lag1_TREND_H1", "status": "failed", "completed_sessions": 1,
             "last_completed_session": "2024-07-01", "attempted_session": "2024-07-02",
             "error": "ValueError: unresolved reference"},
        ],
    }), encoding="utf-8")
    return run_path


def test_audit_preserves_completed_and_stopped_cases(tmp_path):
    receipt = audit_run(_fixture(tmp_path))
    assert receipt["status"] == VERIFIED
    assert receipt["case_counts"] == {"total": 2, "completed": 1, "failed_or_stopped": 1}
    assert receipt["completed_sessions"] == 2
    assert len(receipt["stop_points"]) == 1
    assert receipt["publication_timing_verified"] is False
    assert receipt["execution_admitted"] is False


def test_audit_blocks_gate_breach(tmp_path):
    receipt = audit_run(_fixture(tmp_path, bad_gate=True))
    assert receipt["status"] == BLOCKED
    assert "protocol.execution_admitted" in receipt["issues"]


def test_run_writes_machine_receipt_and_report(tmp_path):
    run_path = _fixture(tmp_path)
    output = tmp_path / "receipt.json"
    report = tmp_path / "report.md"
    receipt = run(run_path, output, report)
    assert receipt["receipt_path"] == str(output.resolve())
    assert output.is_file() and report.is_file()
    assert "NO_TRADE" in report.read_text(encoding="utf-8")
