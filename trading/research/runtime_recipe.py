"""Capture a bounded local runtime recipe for reproducible research tasks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any


SCHEMA = "quantification_runtime_recipe_v1"


def _git(root: Path, *args: str) -> str | None:
    """Return one local git value, or ``None`` when the root is unavailable."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _git_identity(root: Path) -> dict[str, Any]:
    status = _git(root, "status", "--short")
    branch = _git(root, "branch", "--show-current")
    commit = _git(root, "rev-parse", "HEAD")
    return {
        "branch": branch,
        "commit": commit,
        "worktree_clean": status == "",
        "status_lines": status.splitlines() if status else [],
    }


def _trading_import_path() -> str | None:
    try:
        import trading
    except ImportError:
        return None
    return str(Path(trading.__file__).resolve()) if trading.__file__ else None


def capture(root: str | Path, package_id: str) -> dict[str, Any]:
    """Capture local interpreter, import, environment and git facts.

    This function only reads process and repository metadata. It never writes a
    trading database, contacts an external provider, or changes execution
    permissions.
    """

    workspace = Path(root).resolve()
    imported_path = _trading_import_path()
    git = _git_identity(workspace)
    interpreter = str(Path(sys.executable).resolve())
    ready = bool(imported_path and git["branch"] is not None and git["commit"] is not None)
    return {
        "schema": SCHEMA,
        "status": "VERIFIED_LOCAL" if ready else "BLOCKED_RUNTIME",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "package_id": str(package_id),
        "workspace": str(workspace),
        "interpreter": {
            "path": interpreter,
            "python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "imported_modules": {"trading": imported_path},
        "environment": {
            "QUANT_TRADING_HOME": os.environ.get("QUANT_TRADING_HOME"),
            "QUANT_WEB_STORAGE": os.environ.get("QUANT_WEB_STORAGE"),
        },
        "git": git,
        "execution_admitted": False,
        "external_interaction": False,
        "ledger_mutated": False,
        "failure": None if ready else "Local git root or trading import could not be resolved",
    }


def render_report(receipt: dict[str, Any]) -> str:
    """Render a short operator-facing explanation without secrets."""

    return "\n".join(
        [
            "# Runtime recipe v1",
            "",
            f"- status: `{receipt.get('status')}`",
            f"- package: `{receipt.get('package_id')}`",
            f"- interpreter: `{receipt.get('interpreter', {}).get('path')}`",
            f"- trading import: `{receipt.get('imported_modules', {}).get('trading')}`",
            f"- git: `{receipt.get('git', {}).get('branch')}` at `{receipt.get('git', {}).get('commit')}`",
            f"- worktree clean: `{receipt.get('git', {}).get('worktree_clean')}`",
            f"- execution_admitted: `{receipt.get('execution_admitted')}`",
            "",
            "This is a read-only local runtime binding; it does not create an account, run a worker, or contact a broker/THS channel.",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    receipt = capture(args.root, args.package_id)
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.report:
        args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.report.resolve().write_text(render_report(receipt), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["status"] == "VERIFIED_LOCAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
