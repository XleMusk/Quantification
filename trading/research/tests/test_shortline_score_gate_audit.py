import pandas as pd

from trading.research.shortline_score_gate_audit import BLOCKED, READY, audit_scores, run


def _scores(tmp_path, *, marked=False):
    path = tmp_path / "scores.csv"
    pd.DataFrame([
        {"event_id": "e1", "symbol": "000001.SZSE", "date": "2026-01-05", "stage": 1,
         "model_hit5_probability": 0.2, "paper_candidate_only": marked},
        {"event_id": "e2", "symbol": "600001.SSE", "date": "2026-01-05", "stage": 2,
         "model_hit5_probability": 0.1, "paper_candidate_only": False},
    ]).to_csv(path, index=False)
    return path


def test_all_false_scores_are_blocked_without_mutation(tmp_path):
    path = _scores(tmp_path)
    receipt = audit_scores(path, start="2026-01-01", end="2026-01-31")
    assert receipt["status"] == BLOCKED
    assert receipt["paper_candidate_only_true"] == 0
    assert receipt["paper_candidate_only_false"] == 2
    assert "paper_candidate_only.none_true" in receipt["issues"]
    assert receipt["execution_admitted"] is False


def test_explicit_true_candidate_passes_schema_gate(tmp_path):
    receipt = audit_scores(_scores(tmp_path, marked=True), start="2026-01-01", end="2026-01-31")
    assert receipt["status"] == READY
    assert receipt["paper_candidate_only_true"] == 1
    assert receipt["selection_enabled"] is True
    assert receipt["target_generation_allowed"] is False


def test_run_writes_receipt_and_read_only_report(tmp_path):
    output, report = tmp_path / "receipt.json", tmp_path / "report.md"
    receipt = run(_scores(tmp_path), output, report)
    assert receipt["receipt_path"] == str(output.resolve())
    assert output.is_file() and report.is_file()
    assert "paper_candidate_only=true" in report.read_text(encoding="utf-8")
