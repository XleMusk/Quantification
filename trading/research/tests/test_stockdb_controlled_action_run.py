import json
from types import SimpleNamespace

import pytest
import pandas as pd

from trading.research import stockdb_controlled_signal_comparison as comparison
from trading.research.tests.test_stockdb_controlled_signal_comparison import inputs


def test_explicit_action_pair_reaches_both_new_shared_engine_accounts(tmp_path, monkeypatch):
    from trading.research import stockdb_action_accounting

    raw, closes, scores, instruments, calendar, config, days, _ = inputs()
    start = calendar.index(days[0])
    preceding = calendar[start - 21:start]
    source = (raw, closes, scores, instruments, calendar, config, days,
              {"raw_history_context": {"preceding_provider_sessions": preceding}})
    actions = [{"symbol": symbol, "dividRegistDate": days[1], "dividOperateDate": days[2],
                "dividPayDate": days[2], "dividStockMarketDate": days[2],
                "dividCashPsBeforeTax": "0.1", "dividStocksPs": "0", "dividReserveToStockPs": "0.2"}
               for symbol in closes.columns]
    calls = []
    overlay = SimpleNamespace(actions=actions, special_references=[],
        provenance={"knowledge_mode": "posthoc_component_diagnostic", "returns_reportable": False},
        checks=[],
        validate=lambda state, day, rows: calls.append(day))
    loaded = []

    def loader(event, reference, **kwargs):
        loaded.append((event, reference, kwargs))
        return overlay

    monkeypatch.setattr(comparison, "_sources", lambda *args, **kwargs: source)
    monkeypatch.setattr(stockdb_action_accounting, "load_action_accounting", loader)
    output = tmp_path / "new_action_accounts"
    result = comparison.run("batch", "calendar", "listing", output, corrected_only=True,
        action_event_view="events", action_reference_view="references", execution_native_view=["native_quotes"])
    assert result["status"] == "completed"
    assert result["schema"] == comparison.NATIVE_ACTION_METHOD
    assert result["reported_action_accounting_attached"] is True
    assert result["returns_reportable"] is False
    assert len(loaded) == 1
    assert loaded[0][2]["raw_days"] == preceding + days
    assert loaded[0][2]["price_source"] == "independent_baostock_raw"
    assert calls == days + days
    for arm in comparison.ARMS[:2]:
        account = json.loads((output / arm / "account.json").read_text(encoding="utf-8"))
        events = [event for event in account["events"] if event["kind"] == "corporate_action"]
        payments = [event for event in account["events"] if event["kind"] == "dividend_paid"]
        assert events and events[0]["entitlement"] > 0
        assert payments and payments[0]["cash"] > 0
    with pytest.raises(ValueError, match="cannot be upgraded"):
        comparison.run("batch", "calendar", "listing", tmp_path / "forbidden_upgrade", corrected_only=True,
            resume_incomplete=output, action_event_view="events", action_reference_view="references",
            execution_native_view=["native_quotes"])
    assert not (tmp_path / "forbidden_upgrade").exists()
    assert len(loaded) == 1


def test_mixed_stockdb_reference_cannot_reach_quote_loading_or_engine(monkeypatch, tmp_path):
    batch = {"status": "completed_research_only_dated_score_batch", "native_run": str(tmp_path / "native.json"),
             **{flag: False for flag in ("model_refit", "future_labels_used_for_score", "execution_admitted", "strategy_validated")}}
    provider = {"snapshot": str(tmp_path / "snapshot"), "snapshot_manifest_sha256": "pinned"}
    records = {str((tmp_path / "batch.json").resolve()): batch,
               str((tmp_path / "native.json").resolve()): {"provider_view": str(tmp_path / "provider")},
               str((tmp_path / "provider/result.json").resolve()): provider,
               str((tmp_path / "snapshot/manifest.json").resolve()):
                   {"status": "complete_inventory_only", "execution_admitted": False}}
    monkeypatch.setattr(comparison, "_json", lambda path: records[str(path)])
    monkeypatch.setattr(comparison, "_hash", lambda path: "pinned")
    monkeypatch.setattr(comparison.pd, "read_parquet", lambda *a, **k: pytest.fail("quotes must not load"))
    with pytest.raises(ValueError, match="mixed price basis"):
        comparison._sources(tmp_path / "batch.json", "calendar", "listing")


def test_unknown_native_status_remains_ineligible_without_becoming_a_false_state():
    raw, closes, scores, instruments, calendar, config, days, _ = inputs()
    raw.loc[raw.date.eq(days[0]) & raw.symbol.eq("600000.SSE"), "tradestatus"] = pd.NA
    feed = comparison.ControlledFeed(raw, closes, scores, instruments, calendar, config, arm="lgbm_day_local")
    assert "600000.SSE" not in {row["symbol"] for row in feed.pool(days[0], config)}
    assert pd.isna(feed.rows.get(days[0])["600000.SSE"]["tradestatus"])
