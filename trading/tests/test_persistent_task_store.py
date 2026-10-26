from pathlib import Path

import pytest

from trading import settings, store
from trading.config import StrategyConfig


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    root = tmp_path / "trading"
    monkeypatch.setattr(settings, "DATA_ROOT", root)
    monkeypatch.setattr(settings, "DB_PATH", root / "trading.sqlite3")
    monkeypatch.setattr(settings, "DATASET", root / "dataset")
    store.initialize()
    return root


def _config(initial_cash=10000):
    return StrategyConfig(initial_cash=initial_cash, holdings=1, retain_rank=1,
                          gross_limit=.8, single_limit=.8).to_dict()


def test_concurrency_rejection_is_persisted_without_creating_a_fifth_run(isolated_store):
    config = _config()
    for index in range(4):
        store.create(f"paper-{index}", config, "2024-01-02", "2024-01-05", f"f05-{index}")

    with pytest.raises(ValueError, match="At most four"):
        store.create("paper-over-limit", config, "2024-01-02", "2024-01-05", "f05-over-limit")

    assert len(store.runs(active_only=True)) == 4
    receipts = store.rejection_receipts("f05-over-limit")
    assert len(receipts) == 1
    assert receipts[0]["reason_code"] == "CONCURRENCY_LIMIT_REACHED"
    assert receipts[0]["context"] == {"active_count": 4, "limit": 4}
    assert receipts[0]["execution_admitted"] is False


def test_conflicting_idempotency_key_is_persisted_and_original_run_is_unchanged(isolated_store):
    first = store.create("paper", _config(), "2024-01-02", "2024-01-05", "f05-conflict")

    with pytest.raises(ValueError, match="Conflicting run request key"):
        store.create("paper-changed", _config(11000), "2024-01-02", "2024-01-05", "f05-conflict")

    current = store.get(first["id"])
    assert current["status"] == "running" and current["cash"] == 10000
    receipts = store.rejection_receipts("f05-conflict")
    assert receipts[0]["reason_code"] == "REQUEST_KEY_CONFLICT"
    assert receipts[0]["context"]["existing_run_id"] == first["id"]


def test_invalid_run_request_records_reason_before_failing(isolated_store):
    with pytest.raises(ValueError, match="Invalid run mode/name"):
        store.create("", _config(), "2024-01-02", "2024-01-05", "f05-invalid", mode="live")

    receipt = store.rejection_receipts("f05-invalid")[0]
    assert receipt["reason_code"] == "INVALID_RUN_REQUEST"
    assert receipt["reason"] == "Invalid run mode/name"
    assert not store.runs()

