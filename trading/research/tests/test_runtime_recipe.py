from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from trading.research.runtime_recipe import capture


ROOT = Path(__file__).resolve().parents[3]


def test_capture_binds_interpreter_import_and_read_only_boundaries(monkeypatch):
    monkeypatch.setenv("QUANT_TRADING_HOME", r"D:\QuantificationData\runtime-test")
    monkeypatch.setenv("QUANT_WEB_STORAGE", r"D:\QuantificationData\web-test")

    receipt = capture(ROOT, "F02-test")

    assert receipt["status"] == "VERIFIED_LOCAL"
    assert Path(receipt["interpreter"]["path"]).exists()
    assert Path(receipt["imported_modules"]["trading"]).name == "__init__.py"
    assert receipt["environment"]["QUANT_TRADING_HOME"].endswith("runtime-test")
    assert receipt["environment"]["QUANT_WEB_STORAGE"].endswith("web-test")
    assert receipt["git"]["commit"]
    assert receipt["execution_admitted"] is False
    assert receipt["external_interaction"] is False
    assert receipt["ledger_mutated"] is False


def test_cli_writes_machine_receipt_and_report(tmp_path):
    output = tmp_path / "runtime.json"
    report = tmp_path / "runtime.md"
    result = subprocess.run(
        [sys.executable, "run_committee.py", "runtime-recipe", "--root", str(ROOT),
         "--package-id", "F02-cli-test", "--output", str(output), "--report", str(report)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "quantification_runtime_recipe_v1"
    assert receipt["package_id"] == "F02-cli-test"
    assert "Runtime recipe" in report.read_text(encoding="utf-8")


def test_capture_marks_non_repository_as_blocked(tmp_path):
    receipt = capture(tmp_path, "F02-missing-root")

    assert receipt["status"] == "BLOCKED_RUNTIME"
    assert receipt["execution_admitted"] is False
    assert receipt["failure"]
