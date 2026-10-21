from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from . import settings
from .config import StrategyConfig
from .ledger import new_state


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    settings.prepare()
    db = sqlite3.connect(settings.DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
            mode TEXT NOT NULL, config TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
            state TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, error TEXT,
            request_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily(
            run_id TEXT NOT NULL, date TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(run_id,date));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "dataset" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
            db.execute("ALTER TABLE runs ADD COLUMN dataset TEXT NOT NULL DEFAULT ''")


def decode(row, full=False):
    if row is None:
        return None
    value = dict(row)
    value.pop("request_key", None)
    value["config"] = json.loads(value["config"])
    state = json.loads(value.pop("state"))
    value["day"] = state["day"]
    value["cash"] = state["cash"] / 100
    value["order_count"] = len(state["orders"])
    value["fill_count"] = len(state["fills"])
    if full:
        value["state"] = state
    return value


def create(name, config, start, end, request_key, mode="replay", dataset=None):
    config = StrategyConfig(**config).validate().to_dict()
    if mode not in {"replay", "paper"} or not name.strip():
        raise ValueError("Invalid run mode/name")
    signature = json.dumps(config, sort_keys=True)
    dataset = str(dataset or settings.DATASET)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        if prior:
            if (prior["config"], prior["start"], prior["end"], prior["mode"], prior["dataset"]) != (signature, start, end, mode, dataset):
                raise ValueError("Conflicting run request key")
            return decode(prior)
        if db.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0] >= 4:
            raise ValueError("At most four concurrent simulation runs")
        identifier, stamp = uuid.uuid4().hex, now()
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, name[:80], "running", mode,
                   signature, start, end, json.dumps(new_state(config["initial_cash"])), 0, None, request_key, stamp, stamp, dataset))
    return get(identifier)


def get(identifier, full=False):
    with connection() as db:
        return decode(db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone(), full)


def runs(active_only=False):
    with connection() as db:
        query = "SELECT * FROM runs WHERE status='running' ORDER BY created_at" if active_only else "SELECT * FROM runs ORDER BY created_at DESC LIMIT 50"
        return [decode(row) for row in db.execute(query)]


def daily(identifier):
    with connection() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM daily WHERE run_id=? ORDER BY date", (identifier,))]


def control(identifier, action):
    if action not in {"pause", "resume"}:
        raise ValueError("Unknown command")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,state FROM runs WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown run")
        if action == "resume" and json.loads(row["state"]).get("broker_binding"):
            raise PermissionError("Broker-bound execution is not the EOD simulation worker")
        if action == "pause" and row[0] == "running":
            db.execute("UPDATE runs SET status='paused',updated_at=? WHERE id=?", (now(), identifier))
        elif action == "resume" and row[0] == "paused":
            db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=?", (now(), identifier))
    return get(identifier)


def heartbeat():
    with connection() as db:
        db.execute("INSERT INTO meta VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),))


def refresh_state(status, error=None):
    with connection() as db:
        value = json.dumps({"status": status, "error": error, "at": now()})
        db.execute("INSERT INTO meta VALUES('refresh',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))


def health():
    with connection() as db:
        row = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
        refresh = db.execute("SELECT value FROM meta WHERE key='refresh'").fetchone()
    return {"online": bool(row and (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < 20),
            "last_seen": row[0] if row else None, "live_enabled": False,
            "refresh": json.loads(refresh[0]) if refresh else None}
