"""Exercise the persistent run-store decision boundary in an isolated paper DB.

The receipt proves that rejected requests leave durable, explainable records
while the shared paper run store remains unchanged.  It does not schedule a
worker, contact a broker, or enable live execution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any

from trading import settings, store
from trading.config import StrategyConfig


SCHEMA = "quantification_persistent_task_decision_receipt_v1"


def _config(initial_cash: int = 10_000) -> dict[str, Any]:
    return StrategyConfig(initial_cash=initial_cash, holdings=1, retain_rank=1,
                          gross_limit=.8, single_limit=.8).to_dict()


def run(output: str | Path) -> dict[str, Any]:
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    original = (settings.DATA_ROOT, settings.DB_PATH, settings.DATASET)
    try:
        with tempfile.TemporaryDirectory(prefix="quantification_f05_") as temp:
            root = Path(temp) / "trading"
            settings.DATA_ROOT = root
            settings.DB_PATH = root / "trading.sqlite3"
            settings.DATASET = root / "dataset"
            store.initialize()
            config = _config()
            created = []
            for index in range(4):
                created.append(store.create(
                    f"f05-paper-{index}", config, "2024-01-02", "2024-01-05", f"f05-paper-{index}"
                ))
            failures: list[dict[str, str]] = []
            try:
                store.create("f05-over-limit", config, "2024-01-02", "2024-01-05", "f05-over-limit")
            except ValueError as exc:
                failures.append({"request_key": "f05-over-limit", "error": str(exc)})
            try:
                store.create("f05-changed", _config(11_000), "2024-01-02", "2024-01-05", "f05-paper-0")
            except ValueError as exc:
                failures.append({"request_key": "f05-paper-0", "error": str(exc)})
            try:
                store.create("", config, "2024-01-02", "2024-01-05", "f05-invalid", mode="live")
            except ValueError as exc:
                failures.append({"request_key": "f05-invalid", "error": str(exc)})
            rejections = store.rejection_receipts()
            result = {
                "schema": SCHEMA,
                "status": "VERIFIED_LOCAL" if len(rejections) == 3 and len(created) == 4 else "FAILED",
                "execution_admitted": False,
                "ledger_mutated": False,
                "created_run_count": len(created),
                "active_run_count": len(store.runs(active_only=True)),
                "failure_count": len(failures),
                "rejections": rejections,
                "rejection_codes": sorted(item["reason_code"] for item in rejections),
                "failures": failures,
                "acceptance": {
                    "concurrency_rejection_persisted": any(item["reason_code"] == "CONCURRENCY_LIMIT_REACHED" for item in rejections),
                    "idempotency_conflict_persisted": any(item["reason_code"] == "REQUEST_KEY_CONFLICT" for item in rejections),
                    "invalid_request_persisted": any(item["reason_code"] == "INVALID_RUN_REQUEST" for item in rejections),
                    "no_fifth_run_created": len(store.runs(active_only=True)) == 4,
                },
            }
    finally:
        settings.DATA_ROOT, settings.DB_PATH, settings.DATASET = original
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def render_report(receipt: dict[str, Any]) -> str:
    checks = receipt.get("acceptance", {})
    return "\n".join([
        "# Persistent task decision receipt v1",
        "",
        f"- status: `{receipt.get('status')}`",
        f"- created runs: `{receipt.get('created_run_count')}`; active runs: `{receipt.get('active_run_count')}`",
        f"- rejection codes: `{', '.join(receipt.get('rejection_codes', []))}`",
        f"- concurrency rejection persisted: `{checks.get('concurrency_rejection_persisted')}`",
        f"- idempotency conflict persisted: `{checks.get('idempotency_conflict_persisted')}`",
        f"- invalid request persisted: `{checks.get('invalid_request_persisted')}`",
        f"- no fifth run created: `{checks.get('no_fifth_run_created')}`",
        "",
        "This is an isolated local paper-store exercise. It records rejection reasons and does not schedule a worker or contact a broker/THS.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    receipt = run(args.output)
    if args.report:
        args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.report.resolve().write_text(render_report(receipt), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt["status"] == "VERIFIED_LOCAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())

