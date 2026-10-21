"""Source semantics only; fixtures are not market or strategy evidence."""
import json

import pandas as pd
import pytest

from trading.research import leader_close_preview as preview
from trading.research.baostock_daily_slice import FIELDS
from trading.research import baostock_full_close


def raw_close():
    return pd.DataFrame([
        ["2026-10-14", "sz.000001", "10", "11", "10", "11", "10", "100", "1000", "3", "1", "10", "0"],
        ["2026-10-14", "sz.000002", "10", "10", "9", "9", "10", "100", "1000", "3", "1", "-10", "0"],
        ["2026-10-14", "sh.600001", "10", "10", "10", "10", "10", "200", "2000", "3", "1", "0", "0"],
        ["2026-10-14", "sz.000003", "10", "11", "10", "11", "10", "100", "1000", "3", "1", "10", "1"],
        ["2026-10-14", "sz.000004", "10", "10", "10", "10", "10", "", "", "3", "0", "", "0"],
        ["2026-10-14", "sh.600002", "10", "10", "10", "10", "10", "0", "0", "3", "0", "0", "0"],
    ], columns=FIELDS.split(","))


def test_tape_denominators_keep_halted_and_st_in_inventory_without_inventing_flows():
    raw = raw_close()
    original = raw.copy(deep=True)
    tape = preview.market_tape(raw)
    assert tape["ordinary_inventory_rows"] == 6 and tape["active_non_st_rows"] == 3
    assert tape["suspended_inventory_rows"] == 2 and tape["st_inventory_rows"] == 1
    assert [tape[key] for key in ("advance_count", "decline_count", "flat_count")] == [1, 1, 1]
    assert tape["amount_cny"] == 4000 and tape["amount_weighted_return_pct"] == 0
    assert tape["breadth"] == tape["signed_turnover_association"] == 0
    assert tape["median_close_range_location"] == .5 and tape["zero_range_rows"] == 1
    assert "not net capital inflow" in tape["interpretation"]
    assert tape["intraday_order_flow_available"] is False
    pd.testing.assert_frame_equal(raw, original)
    canonical = preview.canonical(raw)
    assert canonical.loc[canonical.code.eq("000004"), "amount"].item() == ""


def test_tape_refuses_mixed_days_or_duplicate_symbols():
    raw = raw_close()
    with pytest.raises(ValueError, match="unique-symbol"):
        preview.market_tape(pd.concat([raw, raw.iloc[:1]]))
    raw.loc[0, "date"] = "2026-10-13"
    with pytest.raises(ValueError, match="exact dated"):
        preview.market_tape(raw)


def test_preview_retains_old_producer_incomplete_status_and_no_account_actions(tmp_path, monkeypatch):
    raw = raw_close()
    monkeypatch.setattr(baostock_full_close, "load_complete", lambda _: (raw, {"scope": "synthetic"}))
    history = preview.canonical(raw).copy()
    history["date"] = "2026-10-13"
    metadata = {"prefixes": ["0", "3", "6"], "partitions": [], "snapshot": "synthetic"}
    monkeypatch.setattr(preview, "read_physical_snapshot", lambda *args, **kwargs: (history, metadata))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                    "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    result = preview.preview(tmp_path / "package", tmp_path / "history", calendar, tmp_path / "out")
    assert result["source_inventory_complete"] is True
    assert {item["producer_batch_status"] for item in result["families"]} == {"incomplete"}
    assert result["producer_coverage"][0]["source_query_partitions_complete"] is False
    assert all(result[key] is False for key in ("execution_admitted", "signal_admitted", "selection_enabled",
        "orders_generated", "ths_payload_generated", "ledger_mutated", "model_refit"))


def test_no_latest_close_report_when_package_is_incomplete(tmp_path, monkeypatch):
    def missing(_):
        raise ValueError("Partial full-close checkpoint cannot be loaded")
    monkeypatch.setattr(baostock_full_close, "load_complete", missing)
    with pytest.raises(ValueError, match="Partial full-close"):
        preview.preview(tmp_path, tmp_path, tmp_path / "calendar.json", tmp_path / "out")
    assert not (tmp_path / "out").exists()
