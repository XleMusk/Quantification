"""Application use cases for the local research-job journey.

HTTP routes translate request/response details; this module owns the business
validation and result lookup used by submit, query, and download. It never
creates a paper account or touches a broker/THS channel.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

from . import store


RESULT_FILES = frozenset({
    "summary.csv",
    "daily_ic.csv",
    "groups.csv",
    "factors.parquet",
    "REPORT.md",
    "result.json",
})


class DatasetNotFound(LookupError):
    """The submitted dataset identity is unknown to the local store."""


class JobNotFound(LookupError):
    """The requested research-job identity is unknown to the local store."""


class ResultUnavailable(LookupError):
    """The job has no readable result or requested file."""


@dataclass(frozen=True)
class SubmitRequest:
    dataset_id: str
    start: str
    end: str
    min_pairs: int = 5


def require_dataset(identifier: str) -> dict:
    item = store.dataset(identifier)
    if item is None:
        raise DatasetNotFound(identifier)
    return item


def require_job(identifier: str) -> dict:
    item = store.job(identifier)
    if item is None:
        raise JobNotFound(identifier)
    return item


def submit(request: SubmitRequest, idempotency_key: str) -> dict:
    """Validate and register one idempotent research request."""

    item = require_dataset(request.dataset_id)
    start, end = pd.Timestamp(request.start), pd.Timestamp(request.end)
    if (pd.isna(start) or pd.isna(end) or start > end
            or start < pd.Timestamp(item["start"])
            or end > pd.Timestamp(item["end"])):
        raise ValueError("研究日期须位于数据范围内，且开始日期不晚于结束日期")
    if item["quality"]["invalid_rows"]:
        raise ValueError("数据质量阻断：请先修复 OHLC / 成交量异常")
    if len(item["symbols"]) < request.min_pairs:
        raise ValueError("证券数量不足，至少需要 min_pairs 个证券")
    config = {
        "start": request.start,
        "end": request.end,
        "min_pairs": request.min_pairs,
        "factors": ["reversal_5", "momentum_20", "relative_volume_20"],
        "holding_sessions": 5,
    }
    return store.create_job(request.dataset_id, config, idempotency_key)


def read_result(identifier: str, storage: str | Path) -> dict:
    item = require_job(identifier)
    if item["status"] != "completed":
        raise ResultUnavailable(identifier)
    path = Path(storage) / "results" / identifier / "result.json"
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_result_file(identifier: str, filename: str, storage: str | Path) -> Path:
    item = require_job(identifier)
    if item["status"] != "completed" or filename not in RESULT_FILES:
        raise ResultUnavailable(filename)
    path = Path(storage) / "results" / identifier / filename
    if not path.is_file():
        raise ResultUnavailable(filename)
    return path
