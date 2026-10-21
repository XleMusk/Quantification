from contextlib import contextmanager
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_full_close as full
from trading.research import baostock_full_close_recovery as recovery
from trading.research.tests.test_baostock_full_close import setup, fake_session, DAY, NOW, SDK


def failed_parent(setup):
    calendar, parent = setup
    calls = []
    @contextmanager
    def session():
        with fake_session(calls)() as provider:
            original = provider.query_history_k_data_plus
            def query(**arguments):
                if len(calls) == 2:
                    calls.append(arguments)
                    raise ConnectionResetError(10054, "[WinError 10054] remote reset")
                return original(**arguments)
            yield SimpleNamespace(query_history_k_data_plus=query)
    _, receipt = full.collect(DAY, calendar, SDK, parent, now=NOW, session_factory=session)
    assert receipt["completed_symbols"] == 2 and receipt["logical_price_queries"] == 3
    return calendar, parent


def healthy_source(calls, starts, *, health_ok=True, fail_price=False):
    @contextmanager
    def session():
        starts.append(True)
        with fake_session(calls)() as provider:
            original = provider.query_history_k_data_plus
            def query(**arguments):
                if fail_price:
                    calls.append(arguments)
                    raise ConnectionResetError(10054, "[WinError 10054] reset again")
                return original(**arguments)
            yield SimpleNamespace(query_history_k_data_plus=query,
                query_trade_dates=lambda **kwargs: SimpleNamespace(error_code="0", error_msg="success",
                    get_data=lambda:pd.DataFrame([[DAY, "1" if health_ok else "0"]], columns=["calendar_date", "is_trading_day"])))
    return session


def test_new_generation_preserves_parent_reuses_raw_rotates_and_counts_recovery(setup, tmp_path):
    calendar, parent = failed_parent(setup)
    before = {str(path):full.daily.digest(path) for path in parent.rglob("*") if path.is_file()}
    calls, starts = [], []
    child, receipt = recovery.recover(parent, tmp_path / "v2", SDK, max_new_queries=25, session_limit=5,
        session_factory=healthy_source(calls, starts))
    assert receipt["status"] == "complete_full_dated_close_inventory_not_admitted"
    assert receipt["reused_completed_symbols"] == 2 and receipt["new_generation_price_queries"] == 25
    assert receipt["parent_generation_price_attempts"] == 3 and receipt["health_queries_this_generation"] == 1
    assert receipt["total_price_attempts_for_prior_failed_code"] == 2
    assert len(calls) == 25 and calls[0]["code"] == "sz.000003" and len(starts) == 6
    sessions=json.loads((child / "price_sessions.json").read_text())["sessions"]
    assert [entry["new_price_queries"] for entry in sessions] == [5] * 5
    raw, metadata = full.load_complete(child)
    assert len(raw) == 27 and metadata["plan"]["symbols"] == json.loads((parent / "plan.json").read_text())["symbols"]
    assert before == {str(path):full.daily.digest(path) for path in parent.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="automatic retry"):
        full.collect(DAY, calendar, SDK, parent, now=NOW)
    altered=json.loads((parent / "plan.json").read_text())
    altered["symbols"]=altered["symbols"][:-1]
    (parent / "plan.json").write_text(json.dumps(altered),encoding="utf-8")
    with pytest.raises(ValueError, match="roster|queue|lineage"):
        full.load_complete(child)


def test_health_failure_does_not_attempt_any_price_or_touch_parent(setup, tmp_path):
    _, parent = failed_parent(setup)
    calls, starts = [], []
    child, receipt = recovery.recover(parent, tmp_path / "v2", SDK, max_new_queries=25,
        session_factory=healthy_source(calls, starts, health_ok=False))
    assert receipt["status"] == "failed_source_health_no_retry" and calls == []
    assert len(starts) == 1 and receipt["logical_price_queries"] == 0
    assert json.loads((child / "health_receipt.json").read_text())["status"] == "failed_source_health_no_price_retry"


def test_recovered_code_reset_is_terminal_not_a_new_loop(setup, tmp_path):
    _, parent = failed_parent(setup)
    calls, starts = [], []
    child, receipt = recovery.recover(parent, tmp_path / "v2", SDK, max_new_queries=25,
        session_factory=healthy_source(calls, starts, fail_price=True))
    assert receipt["status"] == "failed_stopped_no_retry" and len(calls) == 1
    assert receipt["total_price_attempts_for_prior_failed_code"] == 2
    with pytest.raises(FileExistsError):
        recovery.recover(parent, child, SDK, max_new_queries=25)
    with pytest.raises(ValueError, match="original"):
        recovery.prepare(child, tmp_path / "v3", SDK)


def test_quality_failure_cannot_be_reclassified_as_transport_recovery(setup, tmp_path):
    _, parent=failed_parent(setup)
    path=parent/'batches/0000/receipt.json'
    saved=json.loads(path.read_text())
    saved['failures'][0]['error']='ValueError: invalid_prices_or_share_activity'
    path.write_text(json.dumps(saved),encoding='utf-8')
    with pytest.raises(ValueError, match='connection-reset'):
        recovery.prepare(parent,tmp_path/'v2',SDK)
    assert not (tmp_path/'v2').exists()
