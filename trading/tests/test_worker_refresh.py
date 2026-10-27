from datetime import datetime, timedelta, timezone

import pytest

from trading import settings, store
from trading.worker import RefreshTracker


CST = timezone(timedelta(hours=8))


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    root = tmp_path / "trading"
    monkeypatch.setattr(settings, "DATA_ROOT", root)
    monkeypatch.setattr(settings, "DB_PATH", root / "trading.sqlite3")
    monkeypatch.setattr(settings, "DATASET", root / "dataset")
    store.initialize()
    return root


def test_failed_refresh_is_retryable_after_bounded_backoff():
    first = datetime(2026, 10, 27, 16, 15, tzinfo=CST)
    tracker = RefreshTracker()

    assert tracker.should_refresh(first)
    tracker.record_attempt(first)
    tracker.record_failure(first)

    assert not tracker.should_refresh(first + timedelta(seconds=29))
    assert tracker.should_refresh(first + timedelta(seconds=30))


def test_successful_refresh_is_not_repeated_until_the_next_session():
    close = datetime(2026, 10, 27, 16, 15, tzinfo=CST)
    tracker = RefreshTracker()
    tracker.record_attempt(close)
    tracker.record_success(close, input_revision="snapshot-20261027")

    assert tracker.input_revision == "snapshot-20261027"
    assert not tracker.should_refresh(close + timedelta(minutes=10))
    next_session = datetime(2026, 10, 28, 16, 15, tzinfo=CST)
    assert tracker.should_refresh(next_session)


def test_refresh_tracker_survives_worker_restart(isolated_store):
    current = datetime(2026, 10, 27, 16, 15, tzinfo=CST)
    tracker = RefreshTracker()
    tracker.record_attempt(current)
    tracker.record_failure(current)
    store.refresh_state("failed", "provider unavailable", tracker=tracker.snapshot())

    saved = store.refresh_status()
    resumed = RefreshTracker.from_snapshot(saved["tracker"])
    assert resumed.failure_count == 1
    assert resumed.input_revision is None
    assert resumed.next_retry_at == current + timedelta(seconds=30)
    assert not resumed.should_refresh(current + timedelta(seconds=29))
    assert resumed.should_refresh(current + timedelta(seconds=30))
