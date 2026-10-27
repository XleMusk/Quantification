from contextlib import asynccontextmanager
import io
import json
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
import pandas as pd

from . import data, research_jobs, store
from .release import latest_release
from .settings import MAX_UPLOAD, STORAGE, UI_ORIGIN
from trading import store as trading_store
from trading.api import router as trading_router
from trading.research.workbench_recovery_evidence import build_recovery_evidence


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    trading_store.initialize()
    yield


app = FastAPI(title="Local Quant Research", lifespan=lifespan)
app.include_router(trading_router)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(CORSMiddleware, allow_origins=[UI_ORIGIN], allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-Local-Request", "Idempotency-Key"])


@app.middleware("http")
async def local_write_guard(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > MAX_UPLOAD + 1024 * 1024):
        return JSONResponse({"detail": "请求体过大或长度无效"}, status_code=413)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("x-local-request") != "1" or (origin and origin != UI_ORIGIN):
            return JSONResponse({"detail": "仅允许本地应用提交写入请求"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ValueError)
async def invalid_input(request, error):
    return JSONResponse({"detail": str(error)}, status_code=400)


def require_dataset(identifier):
    try:
        return research_jobs.require_dataset(identifier)
    except research_jobs.DatasetNotFound as error:
        raise HTTPException(404, "数据集不存在") from error


def require_job(identifier):
    try:
        return research_jobs.require_job(identifier)
    except research_jobs.JobNotFound as error:
        raise HTTPException(404, "任务不存在") from error


@app.get("/api/health")
def health():
    return {"status": "ok", "worker": store.worker_status(), "mode": "research_only", "live_trading": False}


@app.get("/api/release")
def release_status():
    """Expose the latest local research-only release without mutating it."""
    return latest_release()


@app.get("/api/datasets")
def list_datasets():
    return store.datasets()


@app.get("/api/datasets/example.csv")
def example():
    from quant_learning_starter.factor_lab import synthetic_panel
    frame, _ = synthetic_panel(stocks=8, sessions=65)
    return Response(frame[data.COLUMNS].to_csv(index=False), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="synthetic_example.csv"'})


@app.post("/api/datasets/sync-cache")
def sync_cache():
    return data.sync_local_cache()


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile = File(...), calendar_file: UploadFile | None = File(None),
                         name: str = Form(..., min_length=1, max_length=80),
                         kind: Literal["historical", "synthetic"] = Form(...),
                         source: str = Form(..., min_length=1, max_length=200),
                         adjustment: Literal["raw", "forward", "backward", "synthetic", "unknown"] = Form(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise ValueError("首版导入仅接受 UTF-8 CSV 文件")
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "CSV 超过 12 MB")
    try:
        frame = pd.read_csv(io.BytesIO(content), dtype={"symbol": str}, encoding="utf-8-sig")
        calendar = None
        if calendar_file:
            calendar_bytes = await calendar_file.read(256001)
            if len(calendar_bytes) > 256000:
                raise ValueError("日历文件超过 256 KB")
            table = pd.read_csv(io.BytesIO(calendar_bytes), encoding="utf-8-sig")
            if list(table.columns) != ["date"]:
                raise ValueError("日历 CSV 仅接受 date 列")
            calendar = table.date
    except (UnicodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as error:
        raise ValueError("CSV 解析失败，请检查编码和列结构") from error
    return data.register(frame, name.strip(), kind, source.strip(), adjustment, calendar)


@app.get("/api/datasets/{identifier}/preview")
def preview(identifier: str, symbol: str | None = None):
    item = require_dataset(identifier)
    symbol = symbol or item["symbols"][0]
    if symbol not in item["symbols"]:
        raise ValueError("证券代码不在此数据集中")
    frame = data.read_bars(identifier)
    frame = frame.loc[frame.symbol.eq(symbol)].sort_values("date")
    return {"dataset": item, "symbol": symbol, "prices": data.records(frame.tail(800)),
            "recent": data.records(frame.tail(10).iloc[::-1])}


class JobInput(BaseModel):
    dataset_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    min_pairs: int = Field(default=5, ge=5, le=100)


@app.get("/api/jobs")
def list_jobs():
    return store.jobs()


@app.post("/api/jobs", status_code=202)
def submit_job(body: JobInput, idempotency_key: str = Header(..., min_length=8, max_length=80)):
    request = research_jobs.SubmitRequest(
        dataset_id=body.dataset_id,
        start=body.start,
        end=body.end,
        min_pairs=body.min_pairs,
    )
    return research_jobs.submit(request, idempotency_key)


@app.get("/api/jobs/{identifier}")
def get_job(identifier: str):
    return require_job(identifier)


@app.get("/api/jobs/{identifier}/lifecycle")
def job_lifecycle(identifier: str):
    """Expose the research workbench delivery stages without mutating a job."""
    item = require_job(identifier)
    result_dir = STORAGE / "results" / identifier
    allowed = ("summary.csv", "daily_ic.csv", "groups.csv", "factors.parquet", "REPORT.md", "result.json")
    available_files = [name for name in allowed if (result_dir / name).is_file()]
    status = item["status"]
    result_ready = status == "completed" and "result.json" in available_files
    if status in ("queued", "running"):
        compute_status = "active"
    elif status == "completed":
        compute_status = "completed"
    else:
        compute_status = status
    recovery_ready = status in ("failed", "interrupted", "cancelled")
    recovery_evidence = build_recovery_evidence(item)
    return {
        "schema": "quantification_workbench_job_lifecycle_v1",
        "job_id": identifier,
        "execution_mode": "research_only",
        "execution_admitted": False,
        "live_trading": False,
        "stages": [
            {"job_id": identifier, "id": "submitted", "status": "completed", "detail": "任务已登记并绑定数据集与参数"},
            {"job_id": identifier, "id": "compute", "status": compute_status, "detail": f"{status} · {item['progress']}%"},
            {"job_id": identifier, "id": "result", "status": "available" if result_ready else "waiting", "detail": "结果 JSON 已保存" if result_ready else "任务完成前不可读取"},
            {"job_id": identifier, "id": "download", "status": "available" if available_files else "waiting", "detail": f"可下载 {len(available_files)} 个文件" if available_files else "暂无结果文件"},
        ],
        "available_files": available_files,
        "recovery": {
            "status": "ready_for_resubmit" if recovery_ready else "not_required",
            "automatic": False,
            "next_action": "确认失败原因后重新提交一个新任务" if recovery_ready else "保持当前任务状态并继续刷新",
        },
        "recovery_evidence": recovery_evidence,
        "release": latest_release(),
        "source": {"dataset_id": item["dataset_id"], "updated_at": item["updated_at"]},
    }


@app.get("/api/jobs/{identifier}/recovery")
def recovery(identifier: str):
    """Return the paper-only recovery decision for one persisted job.

    This is a read-only projection of the stored job state.  It never retries,
    mutates the job, or infers broker/THS order state.
    """
    return build_recovery_evidence(require_job(identifier))


@app.get("/api/jobs/{identifier}/recovery-chain")
def recovery_chain(identifier: str):
    """Expose the linked manual recovery attempts without mutating a job."""
    item = require_job(identifier)
    chain = store.recovery_chain(identifier)
    if not chain:
        raise HTTPException(404, "任务恢复链不存在")
    return {
        "schema": "quantification_workbench_recovery_chain_v1",
        "status": "paper_recovery_chain",
        "job_id": identifier,
        "root_job_id": chain[0]["id"],
        "latest_job_id": chain[-1]["id"],
        "attempt_count": len(chain),
        "recovery_attempts": [int(row.get("recovery_attempt") or 0) for row in chain],
        "jobs": chain,
        "execution_admitted": False,
        "automatic_retry": False,
        "broker_or_ths_interacted": False,
        "external_order_state": "not_applicable_paper_job",
        "read_only": True,
        "source_status": item["status"],
    }


@app.post("/api/jobs/{identifier}/cancel")
def cancel_job(identifier: str):
    require_job(identifier)
    return store.cancel(identifier)


@app.post("/api/jobs/{identifier}/resubmit", status_code=202)
def resubmit_job(identifier: str, idempotency_key: str = Header(..., min_length=8, max_length=80)):
    """Explicitly create an immutable child for a failed paper job.

    Recovery is manual and idempotent: this endpoint never retries a worker or
    touches an external account.  Reusing the same key returns the same child;
    a new key records a new recovery attempt linked to the original job.
    """
    source = require_job(identifier)
    if source["status"] not in {"failed", "interrupted", "cancelled"}:
        raise HTTPException(409, "只有失败、中断或已取消的任务可以重新提交")
    return store.resubmit_job(identifier, idempotency_key)


@app.get("/api/jobs/{identifier}/result")
def result(identifier: str):
    try:
        return research_jobs.read_result(identifier, STORAGE)
    except research_jobs.JobNotFound as error:
        raise HTTPException(404, "任务不存在") from error
    except research_jobs.ResultUnavailable as error:
        raise HTTPException(409, "任务尚未完成，没有可用结果") from error


@app.get("/api/jobs/{identifier}/files/{filename}")
def download(identifier: str, filename: str):
    try:
        path = research_jobs.resolve_result_file(identifier, filename, STORAGE)
    except research_jobs.JobNotFound as error:
        raise HTTPException(404, "任务不存在") from error
    except research_jobs.ResultUnavailable as error:
        raise HTTPException(404, "结果文件不存在") from error
    return FileResponse(path, filename=filename)
