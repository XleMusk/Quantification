from hashlib import sha256
import json
from threading import Lock

import pandas as pd
import pytest

from trading.research.full_market_etf import collect


@pytest.fixture
def collection(tmp_path):
    master = {"candidate_codes": [f"{159000 + index}" for index in range(12)]}
    raw = json.dumps(master).encode("utf8")
    (tmp_path / "master.json").write_bytes(raw)
    (tmp_path / "status.json").write_text(json.dumps({"master_sha256": sha256(raw).hexdigest(),
                                                     "receipts": {}}), encoding="utf8")
    return tmp_path


def test_systemic_transport_failure_leaves_unstarted_codes_pending(collection, monkeypatch):
    calls = []
    monkeypatch.setattr("trading.research.full_market_etf.fetch_one",
                        lambda code: (calls.append(code) or code, None, "ProxyError: unavailable"))
    result = collect(collection, 12, workers=1, request_interval=0, transport_failure_limit=3)
    assert result["status"] == "transport_interrupted_backfill"
    assert len(calls) == 3 and result["failed_symbols"] == 3 and result["pending_symbols"] == 9
    assert result["transport_stops"][0]["reason"] == "consecutive_transport_failures"
    # A later invocation skips existing failure receipts; no automatic retries.
    result = collect(collection, 2, workers=1, request_interval=0, transport_failure_limit=3)
    assert len(calls) == 5 and len(set(calls)) == 5
    assert result["failed_symbols"] == 5 and result["pending_symbols"] == 7
    assert len(result["transport_stops"]) == 1


def test_parallel_transport_stop_bounds_in_flight_work(collection, monkeypatch):
    calls, lock = [], Lock()

    def unavailable(code):
        with lock:
            calls.append(code)
        return code, None, "ReadTimeout: unavailable"

    monkeypatch.setattr("trading.research.full_market_etf.fetch_one", unavailable)
    result = collect(collection, 12, workers=3, request_interval=0, transport_failure_limit=3)
    assert 3 <= len(calls) <= 5
    assert result["failed_symbols"] == len(calls)
    assert result["pending_symbols"] == 12 - len(calls)


def test_symbol_error_does_not_impersonate_transport_outage(collection, monkeypatch):
    monkeypatch.setattr("trading.research.full_market_etf.fetch_one",
                        lambda code: (code, None, "ValueError: no eleven-column history"))
    result = collect(collection, 12, workers=2, request_interval=0, transport_failure_limit=2)
    assert result["status"] == "all_attempted_with_failures"
    assert result["failed_symbols"] == 12 and not result.get("transport_stops")


def test_success_resets_transport_streak_and_preserves_received_history(collection, monkeypatch):
    codes = json.loads((collection / "master.json").read_text())["candidate_codes"]
    received = pd.DataFrame({"date": ["20260924"], "close": [1.0]})

    def fetch(code):
        return (code, received, None) if code == codes[1] else (code, None, "ProxyError: unavailable")

    monkeypatch.setattr("trading.research.full_market_etf.fetch_one", fetch)
    result = collect(collection, 12, workers=1, request_interval=0, transport_failure_limit=2)
    assert result["acquired_symbols"] == 1 and result["failed_symbols"] == 3
    assert result["pending_symbols"] == 8
    receipt = result["receipts"][codes[1]]
    assert sha256((collection / receipt["file"]).read_bytes()).hexdigest() == receipt["sha256"]


def test_request_starts_are_paced(collection, monkeypatch):
    sleeps = []
    monkeypatch.setattr("trading.research.full_market_etf.time.monotonic", lambda: 10.0)
    monkeypatch.setattr("trading.research.full_market_etf.time.sleep", sleeps.append)
    monkeypatch.setattr("trading.research.full_market_etf.fetch_one",
                        lambda code: (code, None, "ValueError: no history"))
    collect(collection, 3, workers=1, request_interval=1.5)
    assert sleeps == [0.0, 1.5, 1.5]
