"""Paper advances use the exact saved feed; charts retain each observed candle."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading import settings, store, worker
from trading.config import StrategyConfig
from trading.feed import HistoricalFeed
from trading import paper_market_versions as versions
from trading.paper_market_versions import capture, chart_observations, load_version
from webapp.backend.app import chart_data


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "trading")
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "trading/db.sqlite3")
    store.initialize()


@pytest.fixture
def market(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    days = pd.bdate_range("2025-01-02", periods=40).strftime("%Y-%m-%d").tolist()
    rows = []
    for symbol in ("600000.SSE", "000001.SZSE"):
        closes = 10 + .03 * np.arange(len(days)) + .02 * np.sin(np.arange(len(days)))
        for i, day in enumerate(days):
            close = closes[i]
            rows.append({"date": day, "symbol": symbol, "open": close, "high": close + .1,
                         "low": close - .1, "close": close, "preclose": closes[max(0, i - 1)],
                         "volume": 1_000_000., "amount": close * 1_000_000,
                         "tradestatus": 1, "isST": 0, "adjustflag": "3"})
    frame = pd.DataFrame(rows)
    frame.to_parquet(folder / "raw.parquet", index=False)
    frame.assign(adjustflag="2").to_parquet(folder / "adjusted.parquet", index=False)
    pd.DataFrame({"date": days}).to_csv(folder / "calendar.csv", index=False)
    pd.DataFrame({"symbol": ["600000.SSE", "000001.SZSE"], "ipoDate": "2000-01-01", "outDate": ""}).to_csv(folder / "instruments.csv", index=False)
    pd.DataFrame(columns=["symbol", "dividOperateDate"]).to_csv(folder / "actions.csv", index=False)
    (folder / "manifest.json").write_text(json.dumps({"status": "ready", "kind": "synthetic",
        "source": "generated acceptance fixture", "version": "source-v1", "start": days[0],
        "end": days[-1], "selection_date": days[0]}))
    return folder, days


def account(market):
    folder, days = market
    config = StrategyConfig(signal="equal_weight", lookback=21, skip=1, volatility_window=10,
                            min_amount=1, min_listing_days=1, holdings=1, retain_rank=2,
                            gross_limit=.8, single_limit=.5)
    return store.create("paper version fixture", config.to_dict(), days[25], days[29],
                        "paper-version-fixture", mode="paper", dataset=folder)


def now(day):
    return datetime.fromisoformat(day + "T18:35:00+08:00")


def test_only_new_daily_partitions_are_added_and_source_files_can_disappear(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    first = store.daily(run["id"])[0]
    first_folder, first_manifest = load_version(run["id"], first["market_version"])
    version = first_manifest["paper_market_version"]
    assert first["market_version_status"] == "captured_before_advance"
    assert max(version["raw_parts"]) == days[25]  # No future OHLC was frozen.
    objects = list((settings.DATA_ROOT / "paper_market_versions" / run["id"] / "objects").glob("*.parquet"))
    assert worker.step(run["id"], source, now(days[26]))
    rows = store.daily(run["id"])
    second = load_version(run["id"], rows[-1]["market_version"])[1]["paper_market_version"]
    assert second["raw_parts"][days[25]] == version["raw_parts"][days[25]]
    assert second["adjusted_parts"][days[25]] == version["adjusted_parts"][days[25]]
    assert len(list((settings.DATA_ROOT / "paper_market_versions" / run["id"] / "objects").glob("*.parquet"))) == len(objects) + 2
    for path in folder.iterdir():
        path.unlink()
    chart = chart_data.run_chart(run["id"], "600000.SSE")
    assert chart["snapshot_binding"] == "saved_paper_per_day"
    assert len(chart["market_versions"]) == 2 and chart["prices"][-1]["date"].startswith(days[26])
    assert chart["snapshot"]["kind"] == "synthetic"
    assert store.get(run["id"], full=True)["state"]["fills"]  # Same Engine/Ledger path.
    clipped = chart_data.run_chart(run["id"], "600000.SSE", days[25])
    assert clipped["prices"][-1]["date"].startswith(days[25])
    assert len(clipped["market_versions"]) == 1 and clipped["fills"] == []


def test_failure_before_commit_retries_the_existing_market_version(market, monkeypatch):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    original = worker.advance
    seen = []

    def fail(state, feed, day, *args, **kwargs):
        seen.append(feed.rows[day]["600000.SSE"]["close"])
        state["cash"] = -1
        raise InterruptedError("after snapshot, before atomic ledger commit")

    monkeypatch.setattr(worker, "advance", fail)
    with pytest.raises(InterruptedError):
        worker.step(run["id"], source, now(days[25]))
    path = settings.DATA_ROOT / "paper_market_versions" / run["id"] / "sessions" / (days[25] + ".json")
    pinned = json.loads(path.read_bytes())
    assert store.get(run["id"])["cursor"] == 0 and store.daily(run["id"]) == []
    source.raw.loc[source.raw.date.eq(days[25]), "close"] = 999.
    monkeypatch.setattr(worker, "advance", original)
    assert worker.step(run["id"], source, now(days[25]))
    stored = store.daily(run["id"])[0]
    assert stored["market_version"] == pinned
    assert chart_data.run_chart(run["id"], "600000.SSE")["prices"][-1]["close"] == pytest.approx(seen[0])


def test_later_provider_revision_does_not_rewrite_an_earlier_execution_candle(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    old = chart_data.run_chart(run["id"], "600000.SSE")["prices"][-1]
    # A corrected source is self-consistent, yet it is a later observation.
    for name in ("raw.parquet", "adjusted.parquet"):
        frame = pd.read_parquet(folder / name)
        for field in ("open", "high", "low", "close", "preclose", "amount"):
            frame[field] *= 1.001
        frame.to_parquet(folder / name, index=False)
    changed = HistoricalFeed(folder)
    assert worker.step(run["id"], changed, now(days[26]))
    chart = chart_data.run_chart(run["id"], "600000.SSE")
    assert chart["prices"][-2] == old
    assert chart["prices"][-1]["close"] == pytest.approx(changed.rows[days[26]]["600000.SSE"]["close"])


def test_old_unbound_records_and_tampered_metadata_remain_unmatched(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    row = store.daily(run["id"])[0]
    ref = row.pop("market_version")
    with store.connection() as db:
        db.execute("UPDATE daily SET payload=? WHERE run_id=?", (json.dumps(row), run["id"]))
    with pytest.raises(ValueError, match="旧日终记录"):
        chart_observations(store.get(run["id"], full=True), store.daily(run["id"]), days[25])
    saved, _ = load_version(run["id"], ref)
    (saved / "calendar.csv").write_text("date\n2099-01-01\n")
    with pytest.raises(ValueError, match="changed"):
        load_version(run["id"], ref)


def test_changed_calendar_cannot_skip_an_account_session(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    source.calendar.remove(days[26])
    before = store.get(run["id"], full=True)
    with pytest.raises(ValueError, match="next session changed"):
        # The prior saved calendar still contains the missing session. A
        # changing provider cannot silently move the account to day27.
        capture(run["id"], source, days[27], store.daily(run["id"])[0]["market_version"])
    assert store.get(run["id"], full=True) == before


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("changed", ["source", "loaded_function"])
def test_execution_upgrade_blocks_recovery_and_continuation_but_not_old_chart(market, monkeypatch, cached, changed):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    first = store.daily(run["id"])[0]["market_version"]
    if cached:
        # An interrupted attempt already captured day26 but never committed it.
        capture(run["id"], source, days[26], first)
    before = store.get(run["id"], full=True)
    before_daily = store.daily(run["id"])
    if changed == "source":
        actual = versions.current_implementation()
        changed_files = {name: dict(value) for name, value in actual.items()}
        changed_files["engine.py"]["source_sha256"] = "a" * 64
        monkeypatch.setattr(versions, "current_implementation", lambda: changed_files)
    else:
        from trading import rules
        # The deployed source file can be unchanged while this process has a
        # different loaded function; that also cannot consume the old receipt.
        monkeypatch.setattr(rules, "fees", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "advance", lambda *args, **kwargs: pytest.fail("No Ledger advance after implementation change"))
    with pytest.raises(ValueError, match="implementation changed"):
        worker.step(run["id"], source, now(days[26]))
    assert store.get(run["id"], full=True) == before
    assert store.daily(run["id"]) == before_daily
    assert load_version(run["id"], first)[1]["paper_market_version"]["day"] == days[25]
    chart = chart_data.run_chart(run["id"], "600000.SSE")
    assert chart["prices"][-1]["date"].startswith(days[25])
    assert chart["market_versions"] == [first]


def test_truncated_provider_calendar_cannot_publish_false_completion(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    source.calendar = source.calendar[:26]
    before = store.get(run["id"], full=True)
    with pytest.raises(ValueError, match="calendar changed|sessions are still missing"):
        worker.step(run["id"], source, now(days[39]))
    assert store.get(run["id"], full=True) == before
    assert len(store.daily(run["id"])) == 1
    assert not (settings.DATA_ROOT / "results" / run["id"]).exists()


def test_unbound_legacy_daily_record_cannot_prove_paper_completion(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    assert worker.step(run["id"], source, now(days[25]))
    row = store.daily(run["id"])[0]
    row.pop("market_version")
    with store.connection() as db:
        db.execute("UPDATE daily SET payload=? WHERE run_id=?", (json.dumps(row), run["id"]))
    source.calendar = source.calendar[:26]
    with pytest.raises(ValueError, match="unmatched account sessions"):
        worker.step(run["id"], source, now(days[39]))
    assert store.get(run["id"])["status"] == "running"


def test_fully_bound_paper_book_can_complete(market):
    folder, days = market
    run = account(market)
    source = HistoricalFeed(folder)
    for day in days[25:30]:
        assert worker.step(run["id"], source, now(day))
    assert worker.step(run["id"], source, now(days[39]))
    assert store.get(run["id"])["status"] == "completed"
    assert len(store.daily(run["id"])) == 5
