from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_workbench_recovery_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    assert source.count('if argv[:1] == ["workbench-recovery-evidence"]:') == 1


def test_workbench_recovery_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "workbench-recovery-evidence", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--job" in result.stdout
    assert "--output" in result.stdout


def test_workbench_recovery_route_writes_paper_only_receipt(tmp_path):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "id": "cli-recovery-fixture-v1",
        "status": "interrupted",
        "error": "worker stopped",
        "recovery_attempt": 0,
    }), encoding="utf-8")
    output = tmp_path / "receipt.json"
    report = tmp_path / "report.md"
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "workbench-recovery-evidence", "--job", str(job),
         "--output", str(output), "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "quantification_workbench_recovery_evidence_v1"
    assert receipt["decision"] == "MANUAL_RESUBMIT_REQUIRED"
    assert receipt["automatic_retry"] is False
    assert receipt["execution_admitted"] is False
    assert "cli-recovery-fixture-v1" in report.read_text(encoding="utf-8")
