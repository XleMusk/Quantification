from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_joint_portfolio_paper_package_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")

    assert source.count('if argv[:1] == ["joint-portfolio-paper-package"]:') == 1


def test_all_launcher_route_guards_are_unique():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    routes = re.findall(r'argv\[:1\] == \["([^"]+)"\]', source)
    assert routes
    assert len(routes) == len(set(routes))


def test_stockdb_baostock_extension_help_remains_compatible():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "stockdb-baostock-extension", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_joint_portfolio_paper_package_help_remains_compatible():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "joint-portfolio-paper-package", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0
    assert "--execution-day" in result.stdout
    assert "--output-dir" in result.stdout


def test_continuous_paper_acceptance_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")

    assert source.count('if argv[:1] == ["continuous-paper-acceptance"]:') == 1


def test_continuous_paper_acceptance_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "continuous-paper-acceptance", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--output-dir" in result.stdout


def test_f2_source_snapshot_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")

    assert source.count('if argv[:1] == ["f2-source-snapshot"]:') == 1


def test_f2_source_snapshot_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "f2-source-snapshot", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--snapshot" in result.stdout
    assert "--decision-day" in result.stdout
    assert "--output" in result.stdout


def test_f2_fact_bridge_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "f2-fact-bridge", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--sidecar" in result.stdout
    assert "--historical-evidence" in result.stdout


def test_f2_source_snapshot_cli_runs_gate_and_writes_receipt(tmp_path):
    source = tmp_path / "issuer.pdf"
    source.write_bytes(b"synthetic issuer source")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "schema": "f2_source_snapshot_v1",
        "snapshot_id": "cli-fixture-v1",
        "captured_at": "2024-04-30T14:02:00+08:00",
        "rows": [{
            "symbol": "000002.SZSE",
            "period": "2023Q1",
            "publication_date": "2023-04-29",
            "retrieved_at": "2024-04-30T14:00:00+08:00",
            "asof_at": "2024-04-30T14:01:00+08:00",
            "revision_id": "cli-fixture-rev-1",
            "source_file": "issuer.pdf",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")
    output = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "f2-source-snapshot", "--snapshot", str(snapshot),
         "--decision-day", "2024-05-01", "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "HISTORICAL_EVIDENCE_READY"
    assert receipt["status"] == "HISTORICAL_EVIDENCE_READY"
    assert receipt["source_checks"] == {"verified": 1, "failed": 0}
    assert receipt["execution_admitted"] is False


def test_candidate_adoption_decision_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    assert source.count('if argv[:1] == ["candidate-adoption-decision"]:') == 1


def test_candidate_adoption_decision_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "candidate-adoption-decision", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--common-window" in result.stdout
    assert "--cost-pressure" in result.stdout
    assert "--selection" in result.stdout


def test_current_window_strategy_comparison_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    assert source.count('if argv[:1] == ["current-window-strategy-comparison"]:') == 1


def test_current_window_strategy_comparison_help_is_available():
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "current-window-strategy-comparison", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--fixed-run" in result.stdout
    assert "--candidate-run" in result.stdout


def test_stockdb_close_package_help_is_available():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    assert source.count('if argv[:1] == ["stockdb-close-package"]:') == 1
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "stockdb-close-package", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--package" in result.stdout


def test_stockdb_close_package_builder_help_is_available():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")
    assert source.count('if argv[:1] == ["stockdb-close-package-build"]:') == 1
    result = subprocess.run(
        [sys.executable, "-Xutf8", str(ROOT / "run_committee.py"),
         "stockdb-close-package-build", "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--snapshot" in result.stdout
    assert "--output" in result.stdout
