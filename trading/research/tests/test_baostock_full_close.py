from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_full_close as source

DAY = "2026-10-14"
NOW = datetime(2026, 10, 15, 8, tzinfo=timezone(timedelta(hours=8)))
SDK = {"sha256": source.WHEEL_SHA256, "wheel": "unused-test-wheel"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "calendar_id": "test", "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    root = tmp_path / "job"
    roster = root / "roster"
    roster.mkdir(parents=True)
    raw = pd.DataFrame([[f"sz.{i:06d}", "0" if i == 26 else "1", "unchanged"] for i in range(1, 28)]
                       + [["sh.000001", "1", "index"]], columns=["code", "tradeStatus", "code_name"])
    raw_path = roster / f"query_all_stock_{DAY}.csv"
    raw.to_csv(raw_path, index=False)
    receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "day": DAY,
        "response_day": DAY, "method": "query_all_stock", "kwargs": {"day": DAY},
        "status": "completed_provider_status_list", "quality_issues": [], "provider_error_code": "0",
        "fields_returned": list(raw.columns), "sdk": SDK, "execution_admitted": False,
        "price_bars_generated": False, "raw_csv": str(raw_path), "raw_sha256": source.daily.digest(raw_path),
        "rows_preserved": len(raw)}
    (roster / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(source.daily.time, "sleep", lambda _: None)
    return calendar, root


def fake_session(calls, *, fail_at=None, starts=None):
    @contextmanager
    def session():
        if starts is not None:
            starts.append(True)
        def query(**arguments):
            calls.append(arguments)
            if len(calls) == fail_at:
                raise TimeoutError("provider failed")
            halted = arguments["code"] == "sz.000026"
            row = [DAY, arguments["code"], "10", "10", "10", "10", "10",
                   "" if halted else "100", "" if halted else "1000", "3", "0" if halted else "1", "" if halted else "0", "0"]
            frame = pd.DataFrame([row], columns=source.daily.FIELDS.split(","))
            return SimpleNamespace(error_code="0", error_msg="success", get_data=lambda: frame)
        yield SimpleNamespace(query_history_k_data_plus=query)
    return session


def test_full_roster_budget_resume_is_one_job_and_includes_nullable_halt(setup):
    calendar, root = setup
    calls, starts = [], []
    session = fake_session(calls, starts=starts)
    _, first = source.collect(DAY, calendar, SDK, root, now=NOW, max_new_queries=21, session_factory=session)
    assert first["ordinary_symbols"] == 27 and first["roster_total_rows"] == 28
    assert first["status"] == "partial_budget_checkpoint_not_full_close"
    assert first["completed_symbols"] == 21 and first["new_price_queries_this_run"] == 21
    assert len(starts) == 1 and len(calls) == 21
    with pytest.raises(ValueError, match="not a complete"):
        source.load_complete(root)
    plan = json.loads((root / "plan.json").read_text())
    assert list(map(lambda batch: len(batch["symbols"]), plan["batches"])) == [20, 7]
    _, done = source.collect(DAY, calendar, SDK, root, now=NOW, max_new_queries=200, session_factory=session)
    assert done["status"] == "complete_full_dated_close_inventory_not_admitted"
    assert done["completed_symbols"] == done["logical_price_queries"] == len(calls) == 27
    assert done["new_price_queries_this_run"] == 6 and len(starts) == 2
    assert done["nullable_suspension_rows"] == 1 and done["execution_bar_rows"] == 26
    assert done["full_dated_roster_inventory_complete"] is True
    assert done["execution_bar_coverage_complete"] is False and done["execution_admitted"] is False
    frame = pd.read_csv(root / "raw_daily.csv", dtype=str, keep_default_na=False)
    assert frame.loc[frame.code.eq("sz.000026"), "volume"].item() == ""
    assert all(call["adjustflag"] == "3" and call["start_date"] == call["end_date"] == DAY for call in calls)
    before = {str(path): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
    loaded, metadata = source.load_complete(root)
    assert len(loaded) == len(metadata["ordinary_roster"]) == 27
    assert metadata["receipt"]["execution_admitted"] is False
    assert before == {str(path): path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}


def test_failed_query_is_terminal_and_never_reacquired(setup):
    calendar, root = setup
    calls = []
    session = fake_session(calls, fail_at=2)
    _, failed = source.collect(DAY, calendar, SDK, root, now=NOW, session_factory=session)
    assert failed["status"] == "failed_stopped_no_retry" and len(calls) == 2
    assert failed["completed_symbols"] == 1 and failed["logical_price_queries"] == 2
    with pytest.raises(ValueError, match="automatic retry"):
        source.collect(DAY, calendar, SDK, root, now=NOW, session_factory=session)
    assert len(calls) == 2


def test_changed_cached_bytes_cannot_resume_network(setup):
    calendar, root = setup
    calls = []
    session = fake_session(calls)
    source.collect(DAY, calendar, SDK, root, now=NOW, max_new_queries=1, session_factory=session)
    (root / "batches/0000/sz.000001.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash changed"):
        source.collect(DAY, calendar, SDK, root, now=NOW, session_factory=session)
    assert len(calls) == 1


def test_intraday_or_old_discovery_cannot_query_and_positive_budget_required(setup):
    calendar, root = setup
    for day, budget in [("2026-10-15", 200), ("2026-10-13", 200), (DAY, 0)]:
        with pytest.raises(ValueError):
            source.collect(day, calendar, SDK, root, now=NOW, max_new_queries=budget,
                           roster_probe=lambda *args, **kwargs: pytest.fail("no provider call"))
    assert source.latest_closed_session(calendar, datetime(2026, 10, 15, 15, 1, tzinfo=source.CN)) == "2026-10-15"


def test_same_day_roster_price_status_contradiction_stops_without_promotion(setup):
    calendar, root = setup
    raw_path = root / "roster" / f"query_all_stock_{DAY}.csv"
    raw = pd.read_csv(raw_path, dtype=str)
    raw.loc[raw.code.eq("sz.000002"), "tradeStatus"] = "0"
    raw.to_csv(raw_path, index=False)
    path = root / "roster/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["raw_sha256"] = source.daily.digest(raw_path)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    calls = []
    _, failed = source.collect(DAY, calendar, SDK, root, now=NOW, session_factory=fake_session(calls))
    assert failed["status"] == "failed_stopped_no_retry" and len(calls) == 20
    assert failed["provider_status_disagreements"] == [{"symbol": "000002.SZSE", "roster_status": "0", "daily_status": "1"}]
    assert failed["full_dated_roster_inventory_complete"] is False
    with pytest.raises(ValueError, match="automatic retry"):
        source.collect(DAY, calendar, SDK, root, now=NOW, session_factory=fake_session(calls))
    assert len(calls) == 20


def test_ordinary_job_renews_at_200_and_budget_resume_preserves_sessions(setup):
    calendar, root = setup
    raw_path = root / "roster" / f"query_all_stock_{DAY}.csv"
    raw = pd.DataFrame([[f"sz.{i:06d}", "0" if i == 26 else "1", "unchanged"] for i in range(1, 206)],
                       columns=["code", "tradeStatus", "code_name"])
    raw.to_csv(raw_path, index=False)
    receipt_path = root / "roster/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(raw_sha256=source.daily.digest(raw_path), rows_preserved=len(raw))
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    calls, starts = [], []
    session = fake_session(calls, starts=starts)
    _, bounded = source.collect(DAY, calendar, SDK, root, now=NOW, max_new_queries=201, session_factory=session)
    assert bounded["completed_symbols"] == 201 and len(starts) == 2
    assert [item["new_price_queries"] for item in json.loads((root/'price_sessions.json').read_text())["sessions"]] == [200, 1]
    _, completed = source.collect(DAY, calendar, SDK, root, now=NOW, max_new_queries=4, session_factory=session)
    assert completed["status"] == "complete_full_dated_close_inventory_not_admitted"
    sessions = json.loads((root/'price_sessions.json').read_text())["sessions"]
    assert [item["new_price_queries"] for item in sessions] == [200, 1, 4]
    assert all(item["status"] == "closed" for item in sessions)
    assert len(calls) == 205 and len(starts) == 3
