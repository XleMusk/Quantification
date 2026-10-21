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

from . import data, store
from .settings import MAX_UPLOAD, STORAGE, UI_ORIGIN
from trading import store as trading_store
from trading.api import router as trading_router


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
    item = store.dataset(identifier)
    if item is None:
        raise HTTPException(404, "数据集不存在")
    return item


def require_job(identifier):
    item = store.job(identifier)
    if item is None:
        raise HTTPException(404, "任务不存在")
    return item


@app.get("/api/health")
def health():
    return {"status": "ok", "worker": store.worker_status(), "mode": "research_only", "live_trading": False}


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
    item = require_dataset(body.dataset_id)
    start, end = pd.Timestamp(body.start), pd.Timestamp(body.end)
    if pd.isna(start) or pd.isna(end) or start > end or start < pd.Timestamp(item["start"]) or end > pd.Timestamp(item["end"]):
        raise ValueError("研究日期须位于数据范围内，且开始日期不晚于结束日期")
    if item["quality"]["invalid_rows"]:
        raise ValueError("数据质量阻断：请先修复 OHLC / 成交量异常")
    if len(item["symbols"]) < body.min_pairs:
        raise ValueError("证券数量不足，至少需要 min_pairs 个证券")
    config = {"start": body.start, "end": body.end, "min_pairs": body.min_pairs,
              "factors": ["reversal_5", "momentum_20", "relative_volume_20"], "holding_sessions": 5}
    return store.create_job(body.dataset_id, config, idempotency_key)


@app.get("/api/jobs/{identifier}")
def get_job(identifier: str):
    return require_job(identifier)


@app.post("/api/jobs/{identifier}/cancel")
def cancel_job(identifier: str):
    require_job(identifier)
    return store.cancel(identifier)


@app.get("/api/jobs/{identifier}/result")
def result(identifier: str):
    item = require_job(identifier)
    if item["status"] != "completed":
        raise HTTPException(409, "任务尚未完成，没有可用结果")
    return json.loads((STORAGE / "results" / identifier / "result.json").read_text(encoding="utf-8"))


@app.get("/api/jobs/{identifier}/files/{filename}")
def download(identifier: str, filename: str):
    item = require_job(identifier)
    allowed = {"summary.csv", "daily_ic.csv", "groups.csv", "factors.parquet", "REPORT.md", "result.json"}
    if item["status"] != "completed" or filename not in allowed:
        raise HTTPException(404, "结果文件不存在")
    path = STORAGE / "results" / identifier / filename
    return FileResponse(path, filename=filename)
