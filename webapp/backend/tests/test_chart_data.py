"""Chart API contracts: full saved observations, causal values and raw fills."""

import json

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from trading import settings as trading_settings, store as trading_store
from trading.config import StrategyConfig
from trading.ledger import Ledger, new_state
from webapp.backend.app import chart_data, data, main, settings, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(trading_settings, "DB_PATH", tmp_path / "trading" / "accounts.sqlite3")
    for module in (settings, store, data, main):
        monkeypatch.setattr(module, "STORAGE", tmp_path / "research", raising=False)
    store.initialize()
    trading_store.initialize()


@pytest.fixture
def client():
    with TestClient(main.app) as value:
        yield value


def register(sessions=5):
    dates = pd.bdate_range("2020-01-02", periods=sessions)
    frame = pd.DataFrame({"date": dates, "symbol": "600000.SSE", "open": 10.,
                          "high": 11., "low": 9., "close": 10., "volume": 1000.})
    return data.register(frame, "synthetic chart", "synthetic", "unit test", "synthetic", dates)


def test_full_daily_history_preserves_calendar_and_source(client):
    item = register(1005)
    value = client.get(f"/api/datasets/{item['id']}/chart").json()
    assert len(value["prices"]) == 1005  # No old preview.tail(800) truncation.
    assert len(value["calendar"]) == 1005
    assert value["dataset"]["id"] == item["id"]
    assert value["source_kind"] == "research_snapshot" and value["source_period"] == "D"
    assert value["price_basis"] == "synthetic" and value["realtime"] is False
    assert value["turnover_available"] is False
    assert "turnover" not in value["available_fields"]
    assert "turnover" not in value["prices"][0]
    cutoff = value["calendar"][9]
    clipped = client.get(f"/api/datasets/{item['id']}/chart", params={"as_of": cutoff}).json()
    assert len(clipped["prices"]) == 10 and clipped["calendar"][-1] == cutoff


def test_chart_unknown_symbol_and_invalid_cutoff_are_rejected(client):
    item = register()
    assert client.get("/api/datasets/unknown/chart").status_code == 404
    assert client.get(f"/api/datasets/{item['id']}/chart?symbol=OTHER").status_code == 400
    assert client.get(f"/api/datasets/{item['id']}/chart?as_of=2025-02-30").status_code == 400
    assert client.get(f"/api/datasets/{item['id']}/chart?as_of=20200102").status_code == 400


def test_factor_endpoint_uses_saved_causal_columns_and_cutoff(client):
    item = register()
    config = {"start": item["start"], "end": item["end"], "factors": list(chart_data.FACTOR_IDS)}
    job = store.create_job(item["id"], config, "saved-factor-chart")
    url = f"/api/jobs/{job['id']}/chart-signals"
    assert client.get(url).status_code == 409
    store.progress(job["id"], 100, "fixture complete", status="completed")
    assert client.get(url).status_code == 404
    folder = settings.STORAGE / "results" / job["id"]
    folder.mkdir()
    dates = pd.bdate_range(item["start"], periods=5)
    pd.DataFrame({"date": dates, "symbol": "600000.SSE", "reversal_5": [-.1] * 5,
                  "momentum_20": [.2] * 5, "relative_volume_20": [1.5] * 5,
                  "forward_open_return_5": [999.] * 5,
                  "entry_date": dates + pd.Timedelta(days=1),
                  "exit_date": dates + pd.Timedelta(days=6)}).to_parquet(folder / "factors.parquet")
    value = client.get(url, params={"as_of": str(dates[1].date())}).json()
    assert len(value["values"]) == 2
    assert value["available_at"] == "session_close" and value["dataset_id"] == item["id"]
    assert value["price_basis"] == "synthetic"
    assert set(value["values"][0]) == {"date", "symbol", *chart_data.FACTOR_IDS}
    assert value["values"][0]["momentum_20"] == .2
    assert "forward_open_return_5" not in json.dumps(value)
    assert client.get("/api/jobs/unknown/chart-signals").status_code == 404


@pytest.fixture
def pinned_feed(tmp_path):
    folder = tmp_path / "frozen_feed"
    folder.mkdir()
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    raw = pd.DataFrame({"date": days, "symbol": "600000.SSE", "open": [10., 11., 12.],
                        "high": [11., 12., 13.], "low": [9., 10., 11.], "close": [10., 11., 12.],
                        "preclose": [10., 10., 11.], "volume": [1000., 2000., 3000.],
                        "amount": [10001., 22001., 36001.], "tradestatus": 1, "isST": 0,
                        "adjustflag": "3"})
    raw.to_parquet(folder / "raw.parquet", index=False)
    raw.assign(adjustflag="2").to_parquet(folder / "adjusted.parquet", index=False)
    pd.DataFrame({"date": days}).to_csv(folder / "calendar.csv", index=False)
    pd.DataFrame([{"symbol": "600000.SSE", "board": "main", "ipoDate": "2000-01-01", "outDate": ""}]).to_csv(folder / "instruments.csv", index=False)
    pd.DataFrame(columns=["symbol", "dividOperateDate"]).to_csv(folder / "actions.csv", index=False)
    (folder / "manifest.json").write_text(json.dumps({"status": "ready", "kind": "synthetic", "source": "pinned unit fixture", "start": days[0], "end": days[-1]}))
    return folder, days


def save_state(run_id, state):
    with trading_store.connection() as db:
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))


def test_run_chart_uses_pinned_raw_prices_full_ledger_fills_and_actual_amount(client, pinned_feed):
    folder, days = pinned_feed
    config = StrategyConfig(initial_cash=100000)
    run = trading_store.create("unit replay", config.to_dict(), days[0], days[-1], "chart-run-key", dataset=folder)
    book = Ledger(new_state(config.initial_cash), config)
    book.submit("order", days[0], "600000.SSE", "buy", 200, 1000)
    for i in range(121):
        book.fill(f"fill-{i:03}", "order", 1, 1000, days[0], days[1])
    book.state["day"] = days[1]
    book.emit(days[0], "targets_generated", targets={"600000.SSE": .2}, signal_date=days[0],
              execution_not_before=days[1], signal_source="rule:momentum")
    save_state(run["id"], book.state)
    before = trading_store.get(run["id"], full=True)
    value = client.get(f"/api/trading/runs/{run['id']}/chart").json()
    assert len(value["fills"]) == 121  # Original detail endpoint caps at100.
    assert len(value["prices"]) == 2 and value["as_of"] == days[1]
    assert value["prices"][0]["open"] == 10
    assert value["prices"][0]["turnover"] == 10001  # Never close * volume.
    assert value["turnover_available"] is True and value["turnover_unit"] == "CNY"
    assert value["price_basis"] == "raw" and value["source_kind"] == "run_snapshot"
    assert value["snapshot"]["kind"] == "synthetic" and value["live_trading"] is False
    assert value["fills"][0]["price_rmb"] == 10
    assert value["fills"][0]["execution_id"] == "fill-000"
    assert value["signals"][0]["target_weight"] == .2
    assert value["signals"][0]["available_at"] == "session_close"
    assert before == trading_store.get(run["id"], full=True)
    clipped = client.get(f"/api/trading/runs/{run['id']}/chart", params={"as_of": "2025-01-01"}).json()
    assert clipped["prices"] == [] and clipped["fills"] == [] and clipped["signals"] == []


def test_mill_quotes_do_not_lose_precision(client, pinned_feed, monkeypatch):
    folder, days = pinned_feed
    run = trading_store.create("mill fixture", StrategyConfig().to_dict(), days[0], days[-1], "mill-chart-key", dataset=folder)
    state = new_state(100000)
    state["day"] = days[-1]
    # Adapter-format receipt tests unit conversion without authorizing execution.
    state["fills"] = {"mill-fill": {"symbol": "600000.SSE", "day": days[0], "side": "buy", "qty": 100,
                                    "price": 1234, "price_unit": "mill", "order_id": "mill-order", "fees": 500}}
    save_state(run["id"], state)
    value = client.get(f"/api/trading/runs/{run['id']}/chart").json()
    assert value["fills"][0]["price_rmb"] == 1.234
    assert value["fills"][0]["fees_rmb"] == 5


def test_missing_snapshot_and_broker_observations_do_not_claim_matched_chart(client, pinned_feed):
    folder, days = pinned_feed
    assert client.get("/api/trading/runs/unknown/chart").status_code == 404
    run = trading_store.create("missing", StrategyConfig().to_dict(), days[0], days[-1], "missing-chart-key", dataset="/nonexistent/chart")
    assert client.get(f"/api/trading/runs/{run['id']}/chart").status_code == 409
    bound = trading_store.create("observed", StrategyConfig().to_dict(), days[0], days[-1], "bound-chart-key", dataset=folder)
    state = new_state(100000)
    state["broker_binding"] = {"account_alias": "unit"}
    save_state(bound["id"], state)
    response = client.get(f"/api/trading/runs/{bound['id']}/chart")
    assert response.status_code == 409 and "同源" in response.json()["detail"]


def test_paper_account_current_dataset_is_not_relabelled_frozen_history(client, pinned_feed):
    folder, days = pinned_feed
    run = trading_store.create("daily paper", StrategyConfig().to_dict(), days[0], days[-1],
                               "paper-chart-key", mode="paper", dataset=folder)
    before = trading_store.get(run["id"], full=True)
    value = client.get(f"/api/trading/runs/{run['id']}/chart")
    assert value.status_code == 409
    assert "逐日行情版本" in value.json()["detail"]
    assert before == trading_store.get(run["id"], full=True)
