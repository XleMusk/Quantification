"""StockDB inventory -> local registration -> daily chart/research API flow."""

from hashlib import sha256
import json

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from trading import settings as trading_settings
from webapp.backend.app import chart_data, data, main, settings, stockdb_registry, store


HEADERS = {"X-Local-Request": "1"}
DAYS = ["2026-10-16", "2026-10-19", "2026-10-20"]
SYMBOLS = [f"{600000 + i:06d}.SSE" for i in range(5)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(trading_settings, "DB_PATH", tmp_path / "trading/accounts.sqlite3")
    for module in (settings, store, data, main):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "web", raising=False)

    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError("offline test fixture")

    monkeypatch.setattr(main.socket, "create_connection", unavailable)


@pytest.fixture
def client():
    with TestClient(main.app) as value:
        yield value


@pytest.fixture
def saved_snapshot():
    folder = trading_settings.DATA_ROOT / "research/stockdb_snapshots/chart_flow"
    folder.mkdir(parents=True)
    decoded, daily, availability = [], [], []
    for symbol in SYMBOLS:
        for day in DAYS:
            payload = {"code": symbol[:6], "date": int(day.replace("-", "")),
                       "open": 12., "high": 12.5, "low": 11.5, "close": 12.,
                       "volume": 10000., "amount": 120000., "pre_close": 11.9,
                       "turnover": .16, "pe_ttm": 6.36, "pb": .43,
                       "total_mv": 325731000000., "float_mv": 325731000000.,
                       "is_st": False, "name": "浦发银行"}
            decoded.append({"status": "decoded", "symbol": symbol, "date": day, "payload": payload})
            daily.append({"symbol": symbol, "date": day,
                          **{field: payload[field] for field in [*data.PRICE_FIELDS, "volume", "amount", "pre_close"]}})
            availability.append({"symbol": symbol, "date": day, "status": "available"})
    # A requested but absent symbol remains missing; it is never a zero candle.
    missing = "000001.SZSE"
    for day in DAYS:
        decoded.append({"status": "decoded", "symbol": missing, "date": day, "payload": []})
        availability.append({"symbol": missing, "date": day, "status": "missing"})
    pd.DataFrame(daily).to_csv(folder / "daily.csv", index=False)
    pd.DataFrame(availability).to_csv(folder / "availability.csv", index=False)
    (folder / "decoded_daily_records.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in decoded), encoding="utf-8")
    manifest = {"schema": "stockdb_research_snapshot_v1", "capture_complete": True,
                "execution_admitted": False, "effective_session_dates": DAYS,
                "requested_symbols": [*SYMBOLS, missing], "captured_at": "2026-10-20T18:39:00+08:00",
                "rows": {"requested_daily": 18, "available_daily": 15, "missing_daily": 3},
                "files": {key: {"path": filename, "sha256": sha256((folder / filename).read_bytes()).hexdigest()}
                          for key, filename in [("daily", "daily.csv"), ("availability", "availability.csv"),
                                                ("decoded_daily", "decoded_daily_records.jsonl")]}}
    (folder / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def register_api(client):
    response = client.get("/api/data-sources/stockdb")
    assert response.status_code == 200
    inventory = response.json()["inventory"]
    assert len(inventory) == 1
    registered = client.post("/api/data-sources/stockdb/register", headers=HEADERS,
                             json={"inventory_id": inventory[0]["id"]})
    assert registered.status_code == 200, registered.text
    return registered.json()


def test_offline_service_keeps_completed_inventory_visible_and_registration_local(client, saved_snapshot):
    response = client.get("/api/data-sources/stockdb").json()
    assert response["service"]["status"] == "offline"
    assert response["inventory"][0]["rows"] == 15
    identifier = response["inventory"][0]["id"]
    assert client.post("/api/data-sources/stockdb/register", json={"inventory_id": identifier}).status_code == 403
    assert client.post("/api/data-sources/stockdb/register", headers={**HEADERS, "Origin": "https://foreign.invalid"},
                       json={"inventory_id": identifier}).status_code == 403
    item = register_api(client)
    assert item["rows"] == 15 and len(item["symbols"]) == 5
    assert item["source_missing_daily_rows"] == 3 and item["requested_daily_rows"] == 18
    assert client.get("/api/data-sources/stockdb").json()["inventory"][0]["registered_dataset_id"] == item["id"]
    repeat = client.post("/api/data-sources/stockdb/register", headers=HEADERS, json={"inventory_id": identifier})
    assert repeat.status_code == 200 and repeat.json()["id"] == item["id"]


def test_registered_preview_and_chart_share_canonical_cash_and_rate_fields(client, saved_snapshot):
    item = register_api(client)
    preview = client.get(f"/api/datasets/{item['id']}/preview?symbol=600000.SSE").json()
    row = preview["prices"][0]
    assert row["amount"] == 120000 and row["turnover_rate_pct"] == .16
    assert "turnover" not in row and row["name"] == "浦发银行"
    response = client.get(f"/api/datasets/{item['id']}/chart", params={"symbol": "600000.SSE", "as_of": DAYS[1]})
    assert response.status_code == 200
    chart = response.json()
    assert chart["dataset"]["id"] == preview["dataset"]["id"] == item["id"]
    assert chart["calendar"] == DAYS[:2] and len(chart["prices"]) == 2
    assert all(row["date"].startswith(tuple(DAYS[:2])) for row in chart["prices"])
    assert chart["turnover_available"] is True and chart["price_basis"] == "raw"
    assert chart["turnover_unit"] == "CNY" and chart["volume_unit"] == "share"
    assert chart["prices"][0]["turnover"] == chart["prices"][0]["amount"] == 120000
    assert chart["prices"][0]["turnover"] / chart["prices"][0]["volume"] == 12
    assert chart["prices"][0]["turnover_rate_pct"] == .16
    assert "turnover" in chart["available_fields"] and chart["realtime"] is False


@pytest.mark.parametrize("adjustment,missing_amount", [("forward", False), ("raw", True)])
def test_adjusted_prices_or_missing_cash_amount_cannot_enable_cash_weighted_overlay(client, adjustment, missing_amount):
    frame = pd.DataFrame({"date": pd.to_datetime(DAYS), "symbol": "600000.SSE",
                          "open": 6., "high": 6.5, "low": 5.5, "close": 6., "volume": 10000.,
                          "amount": [120000., None if missing_amount else 120000., 120000.],
                          "turnover_rate_pct": .16})
    item = data.register(frame, "cash basis fixture", "historical", "saved rows", adjustment)
    chart = client.get(f"/api/datasets/{item['id']}/chart").json()
    if adjustment == "forward":
        assert chart["turnover_available"] is False and chart["turnover_unit"] is None
        assert "turnover" not in chart["available_fields"]
        assert all("turnover" not in row for row in chart["prices"])
    assert chart["prices"][0]["amount"] == 120000
    if missing_amount:
        assert chart["prices"][1]["amount"] is None
        assert chart["turnover_available"] is True and chart["turnover_complete"] is False
        assert chart["prices"][0]["turnover"] == 120000
        assert chart["prices"][1]["turnover"] is None
        prefix = client.get(f"/api/datasets/{item['id']}/chart", params={"as_of": DAYS[0]}).json()
        assert prefix["turnover_complete"] is True
        assert prefix["prices"] == chart["prices"][:1]


def test_registered_snapshot_uses_latest_cache_without_loading_full_history(client, saved_snapshot, monkeypatch):
    item = register_api(client)
    monkeypatch.setattr(data, "read_bars", lambda *args, **kwargs: pytest.fail("snapshot must use latest.parquet"))
    response = client.get(f"/api/datasets/{item['id']}/snapshot")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["status"] == "available" and len(snapshot["rows"]) == 5
    assert snapshot["rows"][0]["date"].startswith(DAYS[-1])
    assert snapshot["rows"][0]["amount"] == 120000 and snapshot["rows"][0]["turnover_rate_pct"] == .16
    assert snapshot["price_change_basis"] == "previous_observed_row" and snapshot["realtime"] is False


def test_research_uses_registered_version_and_limits_window_with_warmup(client, saved_snapshot, monkeypatch):
    item = register_api(client)
    body = {"dataset_id": item["id"], "start": DAYS[-1], "end": DAYS[-1], "min_pairs": 5}
    # Five symbols * (two warmup days + the requested day) exceed this small
    # test cap; checking only the requested one-day grid would incorrectly pass.
    monkeypatch.setattr(main, "MAX_RESEARCH_GRID_CELLS", 10)
    rejected = client.post("/api/jobs", headers={**HEADERS, "Idempotency-Key": "stockdb-window-too-big"}, json=body)
    assert rejected.status_code == 400 and "预热" in rejected.json()["detail"]
    assert store.jobs() == []
    monkeypatch.setattr(main, "MAX_RESEARCH_GRID_CELLS", 5_000_000)
    accepted = client.post("/api/jobs", headers={**HEADERS, "Idempotency-Key": "stockdb-same-version-job"}, json=body)
    assert accepted.status_code == 202
    assert accepted.json()["dataset_id"] == item["id"]
    assert accepted.json()["config"]["start"] == DAYS[-1]
