from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import time
from threading import Event, Thread
import traceback

from . import settings, store
from .config import StrategyConfig
from .contracts import RunContext, TradingMode
from .engine import advance
from .feed import HistoricalFeed
from .report import export


def market_now():
    return datetime.now(timezone(timedelta(hours=8)))


@dataclass
class RefreshTracker:
    """Track attempts separately from a successful end-of-day refresh."""

    last_attempt_at: datetime | None = None
    last_success_day: str | None = None
    next_retry_at: datetime | None = None
    input_revision: str | None = None
    failure_count: int = 0
    base_backoff_seconds: int = 30
    max_backoff_seconds: int = 300

    @classmethod
    def from_snapshot(cls, snapshot: dict | None):
        if not isinstance(snapshot, dict):
            return cls()
        raw_attempt = snapshot.get("last_attempt_at")
        raw_retry = snapshot.get("next_retry_at")
        return cls(
            last_attempt_at=datetime.fromisoformat(raw_attempt) if raw_attempt else None,
            last_success_day=snapshot.get("last_success_day"),
            next_retry_at=datetime.fromisoformat(raw_retry) if raw_retry else None,
            input_revision=snapshot.get("input_revision"),
            failure_count=int(snapshot.get("failure_count", 0)),
        )

    def snapshot(self) -> dict:
        return {
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_day": self.last_success_day,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "input_revision": self.input_revision,
            "failure_count": self.failure_count,
        }

    def should_refresh(self, current: datetime, *, cutoff=(16, 15)) -> bool:
        if current.weekday() >= 5 or (current.hour, current.minute) < cutoff:
            return False
        today = current.date().isoformat()
        if self.last_success_day == today:
            return False
        return self.next_retry_at is None or current >= self.next_retry_at

    def record_attempt(self, current: datetime) -> None:
        self.last_attempt_at = current

    def record_success(self, current: datetime, *, input_revision: str | None = None) -> None:
        self.last_success_day = current.date().isoformat()
        self.next_retry_at = None
        self.input_revision = input_revision
        self.failure_count = 0

    def record_failure(self, current: datetime) -> None:
        self.failure_count += 1
        delay = min(self.base_backoff_seconds * (2 ** (self.failure_count - 1)),
                    self.max_backoff_seconds)
        self.next_retry_at = current + timedelta(seconds=delay)


@contextmanager
def heartbeat_loop():
    stop = Event()
    def pulse():
        while not stop.wait(5):
            try:
                store.heartbeat()
            except Exception:
                traceback.print_exc()
    store.heartbeat()
    thread = Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=35)


@contextmanager
def lock(path=None):
    settings.prepare()
    with (path or settings.DATA_ROOT / "worker.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def step(identifier, feed, current=None):
    current = current or market_now()
    complete = False
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        raw = db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        if raw is None or raw["status"] != "running":
            return False
        run = store.decode(raw, full=True)
        if run["state"].get("broker_binding"):
            raise PermissionError("Broker-bound accounts are not driven or completed by the simulation worker")
        sessions = [day for day in feed.calendar if run["start"] <= day <= run["end"]]
        if run["mode"] == "paper":
            cutoff = current.date().isoformat()
            sessions = [day for day in sessions if day < cutoff or (day == cutoff and (current.hour, current.minute) >= (16, 15))]
        if run["cursor"] >= len(sessions):
            if run["mode"] == "replay" or current.date().isoformat() > run["end"]:
                complete = True
            else:
                return False
        else:
            day = sessions[run["cursor"]]
            context = RunContext(identifier, {"paper": TradingMode.PAPER, "replay": TradingMode.REPLAY}[run["mode"]])
            daily = advance(run["state"], feed, day, StrategyConfig(**run["config"]), run["cursor"], context=context)
            daily["dataset"] = str(getattr(feed, "folder", run["dataset"]))
            db.execute("INSERT INTO daily VALUES(?,?,?)", (identifier, day, json.dumps(daily, ensure_ascii=False, allow_nan=False)))
            db.execute("UPDATE runs SET state=?,cursor=cursor+1,updated_at=?,error=NULL WHERE id=?",
                       (json.dumps(run["state"], ensure_ascii=False, allow_nan=False), store.now(), identifier))
    if complete:
        # Completion is published only after all artifacts exist. An interruption
        # here leaves the cursor intact and regenerates exports on restart.
        export(identifier, completed=True)
        with store.connection() as db:
            db.execute("UPDATE runs SET status='completed',updated_at=? WHERE id=? AND status='running'", (store.now(), identifier))
    return True


def main(max_cycles=None, sleep_seconds=2):
    store.initialize()
    feed, revision = None, None
    prior_refresh = store.refresh_status() or {}
    refresh_tracker = RefreshTracker.from_snapshot(prior_refresh.get("tracker"))
    historical_feeds = {}
    with lock(), heartbeat_loop():
        # A previous process may have died after a durable daily checkpoint.
        # A fresh database has no lease, so newly-created runs are not falsely
        # marked interrupted when the first worker starts.
        previous_lease = store.worker_lease()
        store.begin_worker_lease()
        try:
            if previous_lease:
                store.recover_interrupted_runs()
            store.refresh_state("idle", tracker=refresh_tracker.snapshot())
            cycles = 0
            while max_cycles is None or cycles < max_cycles:
                active = store.runs(active_only=True)
                current = market_now()
                today = current.date().isoformat()
                if any(run["mode"] == "paper" for run in active) and refresh_tracker.should_refresh(current):
                    # End-of-day paper replay, not an intraday quote/execution service.
                    refresh_tracker.record_attempt(current)
                    store.refresh_state("updating", tracker=refresh_tracker.snapshot())
                    try:
                        from .prepare_data import prepare_data
                        manifest = prepare_data(end=today)
                        refresh_tracker.record_success(
                            current, input_revision=manifest.get("folder") if isinstance(manifest, dict) else None)
                        store.refresh_state("ready", tracker=refresh_tracker.snapshot())
                    except Exception as error:
                        refresh_tracker.record_failure(current)
                        traceback.print_exc()
                        store.refresh_state("failed", str(error), tracker=refresh_tracker.snapshot())
                path = settings.DATASET / "manifest.json"
                if path.exists() and path.stat().st_mtime_ns != revision:
                    try:
                        if json.loads(path.read_text(encoding="utf-8"))["status"] == "ready":
                            feed = HistoricalFeed()
                            revision = path.stat().st_mtime_ns
                    except Exception as error:
                        print(f"Feed rejected: {error}", flush=True)
                        revision = path.stat().st_mtime_ns
                        feed = None
                changed = False
                for run in active:
                    try:
                        source = feed
                        if run["mode"] == "replay":
                            if run["dataset"] not in historical_feeds:
                                historical_feeds[run["dataset"]] = HistoricalFeed(run["dataset"])
                            source = historical_feeds[run["dataset"]]
                        if source:
                            changed |= step(run["id"], source, current)
                    except Exception as error:
                        traceback.print_exc()
                        with store.connection() as db:
                            db.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE id=?", (str(error), store.now(), run["id"]))
                cycles += 1
                time.sleep(.05 if changed else sleep_seconds)
        finally:
            store.end_worker_lease()


if __name__ == "__main__":
    main()
