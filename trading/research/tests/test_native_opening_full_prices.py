"""New priced FULL target through the existing copied-Ledger opening path."""
import csv
import json
from pathlib import Path

from trading.research import native_saved_opening as native, opening_daily as core
from trading.research.opening_daily_detail import opening_daily_detail
from trading.research.opening_daily_view import RUN_RELATIVE
from trading.research.tests.test_native_saved_opening import fixture_native, A, B
from trading.research.tests.test_native_saved_opening_prices import augment_native_prices


def test_exact_day_full_targets_create_lotted_orders_in_common_pack_without_account_changes(tmp_path, monkeypatch):
    b = augment_native_prices(fixture_native(tmp_path, monkeypatch, decision="FULL"))
    data = tmp_path / "data-root"
    monkeypatch.setattr(core, "DATA_ROOT", data)
    original = {p: p.read_bytes() for p in (b["run"] / "head.json", b["plan"], b["price_folder"] / "raw_daily.csv")}
    native.build_source(b["run"], b["plan"], b["source"], current=b["current"])
    out, result = native.build_opening(b["source"], b["out"], current=b["current"])
    assert result["preview_count"] == 2 and result["candidate_order_count"] == 4
    evidence = json.loads(Path(result["price_evidence"]["path"]).read_text())
    assert evidence["source_day"] == "2026-10-16" and evidence["status"] == "available"
    assert evidence["price_basis"] == "research_raw_close_rmb" and evidence["broker_quote"] is False
    route = {"id": "native", "family": "native_preopen", "source": str(b["source"]),
        "adopt": {"result": str(out / "result.json"), "opening_pack": result["opening_pack"]}}
    core.register(data / RUN_RELATIVE, [route], current=b["current"])
    details = opening_daily_detail(data, "native", now=b["current"])
    for index, case in enumerate(details["cases"]):
        targets = {row["symbol"]: row for row in case["targets"]}
        # Equity 99999.96 * .16 / 20.00 is just below 800 shares: retain
        # the original Engine's 100-share rounding rather than round to 800.
        assert targets[B]["target_qty"] == 700 and targets[B]["close_cents"] == 2000
        assert targets[A]["current_qty"] == 100 * (index + 1) and targets[A]["target_qty"] == 0
        assert [row["side"] for row in case["orders"]] == ["sell", "buy"]
        assert [row["qty"] for row in case["orders"]] == [100 * (index + 1), 700]
        assert all(row["preflight"]["preflight_status"] == "accepted" for row in case["orders"])
        assert case["preflight"]["submitted"] == 2 and case["preflight"]["rejected"] == 0
        assert case["preflight"]["fills_created"] == 0 and case["preflight"]["saved_ledger_mutated"] is False
        assert case["intent"]["weights"] == {B: .16}
    assert original == {p: p.read_bytes() for p in original}
