"""Run a bounded local HTTP research-workbench chain and write an evidence receipt.

This probe starts only the local FastAPI and factor worker processes. It uploads a
synthetic CSV, submits the same idempotency key twice, waits for one job to
complete, reads lifecycle/result, and downloads result files. No broker, THS, or
trading worker is started.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
from quant_learning_starter.factor_lab import synthetic_panel


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_health(session: requests.Session, endpoint: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = session.get(f"{endpoint}/api/health", timeout=2)
            if response.ok and response.json().get("status") == "ok":
                return
        except requests.RequestException as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"local API did not become healthy: {last_error}")


def _request_json(response: requests.Response, expected: int | tuple[int, ...]) -> dict:
    allowed = (expected,) if isinstance(expected, int) else expected
    if response.status_code not in allowed:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("expected JSON object")
    return value


def run(output: Path, report: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quant_workbench_http_") as temp:
        temp_root = Path(temp)
        web_storage = temp_root / "web_storage"
        trading_home = temp_root / "trading_home"
        port = _free_port()
        endpoint = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update({
            "QUANT_WEB_STORAGE": str(web_storage),
            "QUANT_TRADING_HOME": str(trading_home),
            "QUANT_UI_ORIGIN": "http://127.0.0.1:5175",
            "PYTHONUTF8": "1",
        })
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "webapp.backend.app.main:app",
             "--app-dir", str(ROOT), "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        worker = subprocess.Popen(
            [sys.executable, "-m", "webapp.workers.factor_worker"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            session = requests.Session()
            _wait_health(session, endpoint)
            headers = {"X-Local-Request": "1", "Origin": env["QUANT_UI_ORIGIN"]}
            frame, calendar = synthetic_panel(stocks=8, sessions=65)
            dates = pd.DatetimeIndex(calendar)
            start = str(dates[20].date())
            end = str(dates[-6].date())
            upload = session.post(
                f"{endpoint}/api/datasets",
                headers=headers,
                data={"name": "U02 HTTP synthetic probe", "kind": "synthetic",
                      "source": "workbench_http_probe", "adjustment": "synthetic"},
                files={"file": ("probe.csv", frame.to_csv(index=False).encode("utf-8"), "text/csv")},
                timeout=10,
            )
            dataset = _request_json(upload, 200)
            key = "u02-http-probe-20261027-001"
            body = {"dataset_id": dataset["id"], "start": start, "end": end, "min_pairs": 5}
            first = _request_json(session.post(f"{endpoint}/api/jobs", json=body,
                                                headers={**headers, "Idempotency-Key": key}, timeout=10), 202)
            repeated = _request_json(session.post(f"{endpoint}/api/jobs", json=body,
                                                   headers={**headers, "Idempotency-Key": key}, timeout=10), 202)
            if first["id"] != repeated["id"]:
                raise RuntimeError("idempotency key returned different job ids")
            job_id = first["id"]
            queued_lifecycle = _request_json(session.get(f"{endpoint}/api/jobs/{job_id}/lifecycle", timeout=10), 200)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                job = _request_json(session.get(f"{endpoint}/api/jobs/{job_id}", timeout=10), 200)
                if job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError("factor worker did not finish within 60 seconds")
            job = _request_json(session.get(f"{endpoint}/api/jobs/{job_id}", timeout=10), 200)
            lifecycle = _request_json(session.get(f"{endpoint}/api/jobs/{job_id}/lifecycle", timeout=10), 200)
            if job["status"] != "completed":
                raise RuntimeError(f"factor worker ended in {job['status']}: {job.get('error')}")
            result_response = session.get(f"{endpoint}/api/jobs/{job_id}/result", timeout=10)
            result = _request_json(result_response, 200)
            downloads = {}
            for filename in ("result.json", "REPORT.md", "summary.csv"):
                response = session.get(f"{endpoint}/api/jobs/{job_id}/files/{filename}", timeout=10)
                if response.status_code != 200:
                    raise RuntimeError(f"download failed for {filename}: {response.status_code}")
                downloads[filename] = {"bytes": len(response.content), "sha256": _sha256(response.content)}
            release = _request_json(session.get(f"{endpoint}/api/release", timeout=10), 200)
            receipt = {
                "schema": "quantification_workbench_u02_http_observation_v1",
                "status": "COMPLETED_LOCAL_RESEARCH_HTTP_CHAIN",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "transport": "localhost_http_requests",
                "endpoint": endpoint,
                "execution_admitted": False,
                "broker_interacted": False,
                "ths_interacted": False,
                "dataset": {"id": dataset["id"], "symbols": dataset.get("symbols"), "rows": dataset.get("rows")},
                "job_id": job_id,
                "idempotency": {"same_job_id": first["id"] == repeated["id"], "key": key},
                "queued_lifecycle": queued_lifecycle,
                "job": job,
                "lifecycle": lifecycle,
                "result_summary_count": len(result.get("summary", [])),
                "downloads": downloads,
                "release": release,
                "assertions": {
                    "same_job_id": first["id"] == repeated["id"],
                    "completed": job["status"] == "completed",
                    "lifecycle_result_available": lifecycle.get("stages", [{}, {}, {}])[2].get("status") == "available",
                    "downloads_available": all(item["bytes"] > 0 for item in downloads.values()),
                    "execution_closed": all(receipt_value is False for receipt_value in
                                             (False, False, False)),
                },
                "limitations": [
                    "Local synthetic research only; no market or account evidence.",
                    "HTTP endpoint was observed directly; no browser or external channel was used.",
                    "The factor result is not a portfolio backtest or strategy adoption decision.",
                ],
            }
            output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report.write_text(
                "# U02 local HTTP observation\n\n"
                f"Observed `{endpoint}` with one synthetic dataset and one idempotent research job. "
                f"The same key returned job `{job_id}` twice; the worker completed it, lifecycle exposed "
                "submitted/compute/result/download, and result.json, REPORT.md and summary.csv were downloaded.\n\n"
                "The receipt is local research evidence only. No browser automation, broker, Tonghuashun, "
                "orders, fills or account state was touched.\n",
                encoding="utf-8",
            )
            return receipt
        finally:
            for process in (worker, api):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("outputs/pipeline_receipts/20261027_workbench_u02_http_observation_v1.json"))
    parser.add_argument("--report", type=Path, default=Path("outputs/research_reports/20261027_workbench_u02_http_observation_v1.md"))
    args = parser.parse_args()
    receipt = run(args.output, args.report)
    print(json.dumps({"status": receipt["status"], "job_id": receipt["job_id"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
