from concurrent.futures import ThreadPoolExecutor
import io
import json
import uuid

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
import pytest

from quant_learning_starter.factor_lab import LABEL, synthetic_panel
from webapp.backend.app import data, main, settings, store
from webapp.workers import factor_worker as worker

HEADERS = {"X-Local-Request": "1", "Origin": settings.UI_ORIGIN}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from trading import settings as trading_settings
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(trading_settings, "DB_PATH", tmp_path / "trading/db.sqlite3")
    for module in (settings, store, data, main, worker):
        monkeypatch.setattr(module, "STORAGE", tmp_path, raising=False)
    store.initialize()


@pytest.fixture
def panel():
    return synthetic_panel(stocks=8, sessions=65)


@pytest.fixture
def dataset(panel):
    frame, calendar = panel
    return data.register(frame, "test synthetic", "synthetic", "unit test", "synthetic", calendar)


@pytest.fixture
def client():
    with TestClient(main.app) as value:
        yield value


def submit(client, dataset, key=None, **overrides):
    body = {"dataset_id": dataset["id"], "start": dataset["start"], "end": dataset["end"], "min_pairs": 5, **overrides}
    return client.post("/api/jobs", json=body, headers={**HEADERS, "Idempotency-Key": key or uuid.uuid4().hex})


def test_csv_import_preview_and_source_flags(client, panel):
    frame, _ = panel
    response = client.post("/api/datasets", headers=HEADERS,
                           data={"name": "CSV test", "kind": "synthetic", "source": "test", "adjustment": "synthetic"},
                           files={"file": ("bars.csv", frame[data.COLUMNS].to_csv(index=False).encode(), "text/csv")})
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["quality"]["calendar_kind"] == "observed_union"
    assert item["kind"] == "synthetic"
    preview = client.get(f"/api/datasets/{item['id']}/preview").json()
    assert len(preview["recent"]) == 10
    assert preview["prices"][0]["close"] == pytest.approx(frame.iloc[0].close)


def test_snapshot_retains_source_and_returns_each_symbols_latest_observed_pair(client, dataset, panel):
    response = client.get(f"/api/datasets/{dataset['id']}/snapshot")
    assert response.status_code == 200
    value = response.json()
    assert value["dataset"] == dataset
    assert value["dataset"]["kind"] == "synthetic"
    assert value["price_change_basis"] == "previous_observed_row"
    assert value["realtime"] is False
    assert value["status"] == "available"
    assert [row["symbol"] for row in value["rows"]] == dataset["symbols"]
    for row in value["rows"]:
        source = panel[0].loc[panel[0].symbol.eq(row["symbol"])].sort_values("date")
        assert pd.Timestamp(row["date"]) == source.iloc[-1].date
        assert row["close"] == pytest.approx(source.iloc[-1].close)
        assert pd.Timestamp(row["previous_date"]) == source.iloc[-2].date
        assert row["previous_close"] == pytest.approx(source.iloc[-2].close)
        assert row["change_pct"] == pytest.approx(source.iloc[-1].close / source.iloc[-2].close - 1)
        assert "amount" not in row and "name" not in row


def test_snapshot_preserves_gaps_and_null_change_for_single_observation(client):
    rows = [
        {"date": "2025-01-06", "symbol": "ONE", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
        {"date": "2025-01-08", "symbol": "ONE", "open": 12, "high": 12, "low": 12, "close": 12, "volume": 200},
        {"date": "2025-01-07", "symbol": "SINGLE", "open": 20, "high": 20, "low": 20, "close": 20, "volume": 300},
    ]
    item = data.register(pd.DataFrame(rows), "gapped", "synthetic", "test", "synthetic",
                         pd.date_range("2025-01-06", "2025-01-08"))
    before = data.read_bars(item["id"])
    value = client.get(f"/api/datasets/{item['id']}/snapshot").json()
    one, single = value["rows"]
    assert one["symbol"] == "ONE" and one["change_pct"] == pytest.approx(.2)
    assert pd.Timestamp(one["previous_date"]) == pd.Timestamp("2025-01-06")
    assert single["symbol"] == "SINGLE"
    assert pd.Timestamp(single["date"]) == pd.Timestamp("2025-01-07")
    assert single["previous_date"] is None and single["previous_close"] is None
    assert single["change_pct"] is None
    pd.testing.assert_frame_equal(before, data.read_bars(item["id"]))


def test_snapshot_empty_storage_and_unknown_dataset_remain_honest(client, dataset, monkeypatch):
    assert client.get("/api/datasets/unknown/snapshot").status_code == 404
    monkeypatch.setattr(data, "read_bars", lambda identifier: pd.DataFrame(columns=data.COLUMNS))
    value = client.get(f"/api/datasets/{dataset['id']}/snapshot").json()
    assert value["rows"] == [] and value["status"] == "empty"
    assert value["realtime"] is False


def test_snapshot_does_not_turn_invalid_prices_into_percentage_changes(client):
    frame = pd.DataFrame([
        {"date": "2025-01-06", "symbol": "BAD", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 100},
        {"date": "2025-01-07", "symbol": "BAD", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 200},
    ])
    item = data.register(frame, "invalid", "synthetic", "test", "synthetic")
    value = client.get(f"/api/datasets/{item['id']}/snapshot").json()
    assert value["dataset"]["quality"]["invalid_rows"] == 1
    assert value["rows"][0]["previous_close"] == 0
    assert value["rows"][0]["change_pct"] is None


def test_end_to_end_real_worker_function(client, dataset):
    response = submit(client, dataset)
    assert response.status_code == 202
    item = store.claim_job()
    assert item["id"] == response.json()["id"]
    assert client.get(f"/api/jobs/{item['id']}/result").status_code == 409
    worker.execute(item)
    finished = store.job(item["id"])
    assert finished["status"] == "completed", finished["error"]
    assert finished["progress"] == 100
    result = client.get(f"/api/jobs/{item['id']}/result").json()
    assert len(result["summary"]) == 3
    assert result["science_evidence"] is False
    assert result["dataset"]["kind"] == "synthetic"
    assert client.get(f"/api/jobs/{item['id']}/files/summary.csv").status_code == 200
    assert client.get(f"/api/jobs/{item['id']}/files/not-a-file").status_code == 404


def test_idempotent_submit_and_conflicting_payload(client, dataset):
    first = submit(client, dataset, "repeat-key").json()
    second = submit(client, dataset, "repeat-key").json()
    assert first["id"] == second["id"]
    assert len(store.jobs()) == 1
    assert submit(client, dataset, "repeat-key", min_pairs=6).status_code == 400


def test_atomic_claim_only_once(dataset):
    created = store.create_job(dataset["id"], {}, "atomic-claim-key")
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda _: store.claim_job(), range(4)))
    assert [item["id"] for item in items if item] == [created["id"]]


def test_api_restart_preserves_jobs(client, dataset):
    item = submit(client, dataset).json()
    store.initialize()
    with TestClient(main.app) as restarted:
        assert restarted.get(f"/api/jobs/{item['id']}").json()["status"] == "queued"


def test_worker_recovery_marks_interrupted_not_completed(client, dataset):
    item = submit(client, dataset).json()
    store.claim_job()
    store.recover_interrupted()
    assert store.job(item["id"])["status"] == "interrupted"
    assert client.get(f"/api/jobs/{item['id']}/result").status_code == 409


def test_cancel_queued_and_running(client, dataset):
    queued = submit(client, dataset).json()
    assert client.post(f"/api/jobs/{queued['id']}/cancel", headers=HEADERS).json()["status"] == "cancelled"
    assert store.claim_job() is None
    running = submit(client, dataset).json()
    item = store.claim_job()
    client.post(f"/api/jobs/{running['id']}/cancel", headers=HEADERS)
    worker.execute(item)
    assert store.job(item["id"])["status"] == "cancelled"


def test_write_guard_and_host_guard(client, dataset):
    assert client.post("/api/datasets/sync-cache").status_code == 403
    assert client.post("/api/datasets/sync-cache", headers={**HEADERS, "Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "untrusted.example"}).status_code == 400


def test_upload_size_rejected_early(client):
    assert client.post("/api/datasets", headers={**HEADERS, "Content-Length": "99999999"}).status_code == 413


def test_bad_ohlc_stored_but_calculation_blocked(client, panel):
    frame, calendar = panel
    frame.loc[0, "high"] = 0.01
    item = data.register(frame, "broken", "historical", "test", "raw", calendar)
    assert item["quality"]["invalid_rows"] == 1
    assert submit(client, item).status_code == 400


def test_duplicate_keys_rejected(panel):
    frame, calendar = panel
    with pytest.raises(ValueError, match="重复"):
        data.validate_panel(pd.concat([frame, frame.iloc[:1]]), calendar)


def test_missing_columns_rejected(panel):
    with pytest.raises(ValueError, match="缺少字段"):
        data.validate_panel(panel[0].drop(columns="open"))


def test_missing_symbol_does_not_become_nan_ticker(panel):
    frame, _ = panel
    frame.loc[0, "symbol"] = None
    with pytest.raises(ValueError, match="证券代码不能缺失"):
        data.validate_panel(frame)


def test_outside_calendar_and_intraday_rejected(panel):
    frame, calendar = panel
    with pytest.raises(ValueError, match="之外"):
        data.validate_panel(frame, calendar[1:])
    frame.loc[0, "date"] += pd.Timedelta(hours=3)
    with pytest.raises(ValueError, match="日频"):
        data.validate_panel(frame)


def test_calendar_unsorted_and_duplicate_rejected(panel):
    frame, calendar = panel
    for dates in (calendar[::-1], calendar.append(calendar[:1])):
        with pytest.raises(ValueError, match="有序"):
            data.validate_panel(frame, dates)


def test_content_deduplication(panel):
    frame, calendar = panel
    one = data.register(frame, "demo", "synthetic", "test", "synthetic", calendar)
    two = data.register(frame, "demo", "synthetic", "test", "synthetic", calendar)
    assert one["id"] == two["id"]
    assert len(store.datasets()) == 1


def test_short_period_fails_honestly(client, dataset):
    response = submit(client, dataset, end=dataset["start"])
    assert response.status_code == 202
    item = store.claim_job()
    worker.execute(item)
    assert store.job(item["id"])["status"] == "failed"
    assert "有效 IC" in store.job(item["id"])["error"]


def test_label_end_boundary_is_purged(client, dataset):
    end = "2025-03-03"
    assert submit(client, dataset, end=end).status_code == 202
    item = store.claim_job()
    worker.execute(item)
    frame = pd.read_parquet(settings.STORAGE / "results" / item["id"] / "factors.parquet")
    assert frame.loc[frame.exit_date.gt(pd.Timestamp(end)), LABEL].isna().all()


def test_known_source_and_type_survive(client, dataset):
    item = client.get("/api/datasets").json()[0]
    assert item["source"] == "unit test"
    assert item["kind"] == "synthetic"
    assert item["quality"]["calendar_kind"] == "provided"


def test_sync_cache_accepts_legacy_stocks_but_excludes_funds(tmp_path, monkeypatch, panel):
    monkeypatch.setattr(data, "PROJECT", tmp_path)
    folder = tmp_path / "market_data/a_share/daily"
    folder.mkdir(parents=True)
    frame = panel[0].iloc[:1].rename(columns={"symbol": "vt_symbol"})
    for symbol, security_type, adjustment in [
        ("000001.SZSE", None, "2"), ("600000.SSE", "stock", "2"),
        ("159509.SZSE", None, "2"), ("000333.SZSE", "stock", "3"),
    ]:
        item = frame.assign(vt_symbol=symbol, adjustflag=adjustment)
        if security_type:
            item["security_type"] = security_type
        item.to_parquet(folder / f"{symbol}.parquet", index=False)
    result = data.sync_local_cache()
    assert result["symbols"] == ["000001.SZSE", "600000.SSE"]
