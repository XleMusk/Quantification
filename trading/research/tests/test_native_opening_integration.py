"""New native source -> common opening -> selected original Ledger, synthetic only."""
from pathlib import Path

import pytest

from trading.research import native_saved_opening as native
from trading.research import opening_daily as core
from trading.research.opening_daily_detail import opening_daily_detail
from trading.research.opening_daily_ledger import opening_daily_ledger
from trading.research.opening_daily_view import RUN_RELATIVE
from trading.research.shortline_native_preopen import POLICY_ID, QUALIFIED_POLICY_ID
from trading.research.tests.test_native_saved_opening import fixture_native, A, D, E


@pytest.mark.parametrize("policy", [POLICY_ID, QUALIFIED_POLICY_ID])
def test_native_policy_keeps_next_nc_and_prior_fills_separate_in_common_path(tmp_path, monkeypatch, policy):
    b = fixture_native(tmp_path, monkeypatch, policy=policy)
    data = tmp_path / "data-root"
    monkeypatch.setattr(core, "DATA_ROOT", data)
    original_head = (b["run"] / "head.json").read_bytes()
    original_plan = b["plan"].read_bytes()
    native.build_source(b["run"], b["plan"], b["source"], current=b["current"])
    directory, result = native.build_opening(b["source"], b["out"], current=b["current"])
    route = {"id": "native", "family": "native_preopen", "source": str(b["source"]),
             "adopt": {"result": str(directory / "result.json"), "opening_pack": result["opening_pack"]}}
    coordinator = data / RUN_RELATIVE
    core.register(coordinator, [route], current=b["current"])
    frozen = (coordinator / "head.json").read_bytes()
    for name in ("_factory", "_lock", "run", "extend", "register", "recover"):
        monkeypatch.setattr(core, name, lambda *a, **kw: pytest.fail("GET attempted a writer or producer"))
    detail = opening_daily_detail(data, "native", now=b["current"])
    assert [row["case_id"] for row in detail["cases"]] == ["lr", "lgbm"]
    for index, item in enumerate(detail["cases"]):
        kind, qty = item["case_id"], (index + 1) * 100
        assert item["strategy_id"] == "native_preopen:" + policy + ":" + kind
        assert item["intent"]["kind"] == "NO_CHANGE" and item["intent"]["weights"] is None
        assert item["intent"]["stage_unknown_symbols"] == ["600825.SSE"]
        assert item["targets"][0]["current_qty"] == item["targets"][0]["target_qty"] == qty
        assert item["orders"] == []
        ledger = opening_daily_ledger(data, "native", kind, now=b["current"])
        assert ledger["ledger_day"] == D and ledger["planned_execution_day"] == E
        assert ledger["order_count"] == 1 and ledger["fill_count"] == 2
        assert ledger["positions"][0]["symbol"] == A and ledger["positions"][0]["quantity"] == qty
        assert ledger["positions"][0]["sellable_on_ledger_day"] == 0
        assert ledger["orders"][0]["fees"] == 4 and sum(row["fees"] for row in ledger["fills"]) == 4
        assert ledger["executed_decision"]["source_day"] == "2026-10-15"
        assert ledger["executed_decision"]["execution_day"] == D
    assert (coordinator / "head.json").read_bytes() == frozen
    assert (b["run"] / "head.json").read_bytes() == original_head and b["plan"].read_bytes() == original_plan
