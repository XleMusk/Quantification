from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from .settings import STORAGE, prepare_storage


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    db = sqlite3.connect(STORAGE / "research.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    prepare_storage()
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL, logs TEXT NOT NULL DEFAULT '[]', error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0, request_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)


def decode_dataset(row):
    return None if row is None else {"id": row["id"], "name": row["name"],
                                     "created_at": row["created_at"], **json.loads(row["metadata"])}


def datasets():
    with connection() as db:
        return [decode_dataset(row) for row in db.execute("SELECT * FROM datasets ORDER BY created_at DESC")]


def dataset(identifier):
    with connection() as db:
        return decode_dataset(db.execute("SELECT * FROM datasets WHERE id=?", (identifier,)).fetchone())


def decode_job(row):
    if row is None:
        return None
    result = dict(row)
    result["config"] = json.loads(result["config"])
    result["logs"] = json.loads(result["logs"])
    result.pop("request_key", None)
    return result


def jobs():
    with connection() as db:
        return [decode_job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]


def job(identifier):
    with connection() as db:
        return decode_job(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())


def create_job(dataset_id, config, request_key):
    encoded = json.dumps(config, sort_keys=True)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM jobs WHERE request_key=?", (request_key,)).fetchone()
        if previous:
            if previous["config"] != encoded or previous["dataset_id"] != dataset_id:
                raise ValueError("同一个请求编号不能用于不同任务")
            return decode_job(previous)
        count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if count >= 8:
            raise ValueError("待处理任务已达 8 个，请等待或取消任务")
        identifier = uuid.uuid4().hex
        stamp = now()
        db.execute("INSERT INTO jobs(id,dataset_id,status,config,request_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (identifier, dataset_id, "queued", encoded, request_key, stamp, stamp))
    return job(identifier)


def claim_job():
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), row["id"]))
        identifier = row["id"]
    return job(identifier)


def progress(identifier, percent, message, status=None, error=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Unknown job")
        logs = json.loads(row["logs"])
        logs.append({"time": now(), "message": message})
        state = status or row["status"]
        if row["cancel_requested"] and state in ("running", "completed"):
            state = "cancelled"
        db.execute("UPDATE jobs SET status=?,progress=?,logs=?,error=?,updated_at=? WHERE id=?",
                   (state, percent, json.dumps(logs, ensure_ascii=False), error, now(), identifier))


def cancel(identifier):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        if row[0] in ("queued", "running"):
            state = "cancelled" if row[0] == "queued" else "running"
            db.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE id=?", (state, now(), identifier))
    return job(identifier)


def recover_interrupted():
    with connection() as db:
        db.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE status='running'",
                   ("计算进程中断；未标记完成，可重新提交", now()))


def heartbeat(pid):
    value = json.dumps({"pid": pid, "at": now()})
    with connection() as db:
        db.execute("INSERT INTO meta(key,value) VALUES('worker',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))


def worker_status():
    with connection() as db:
        row = db.execute("SELECT value FROM meta WHERE key='worker'").fetchone()
    value = json.loads(row[0]) if row else None
    online = bool(value and (datetime.now(timezone.utc) - datetime.fromisoformat(value["at"])).total_seconds() < 15)
    return {"online": online, "last_seen": value["at"] if value else None}
