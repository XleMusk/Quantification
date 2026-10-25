from contextlib import contextmanager
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
    feed, revision, last_refresh = None, None, None
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
            store.refresh_state("idle")
            cycles = 0
            while max_cycles is None or cycles < max_cycles:
                active = store.runs(active_only=True)
                current = market_now()
                today = current.date().isoformat()
                if any(run["mode"] == "paper" for run in active) and current.weekday() < 5 and (current.hour, current.minute) >= (16, 15) and last_refresh != today:
                    # End-of-day paper replay, not an intraday quote/execution service.
                    last_refresh = today
                    store.refresh_state("updating")
                    try:
                        from .prepare_data import prepare_data
                        prepare_data(end=today)
                        store.refresh_state("ready")
                    except Exception as error:
                        traceback.print_exc()
                        store.refresh_state("failed", str(error))
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
