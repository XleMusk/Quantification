from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_joint_portfolio_paper_package_route_is_registered_once():
    source = (ROOT / "run_committee.py").read_text(encoding="utf-8")

    assert source.count('if argv[:1] == ["joint-portfolio-paper-package"]:') == 1


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
