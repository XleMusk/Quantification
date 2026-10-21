import json

import pandas as pd
import pytest

from trading import replay_reports, settings, store
from trading.config import StrategyConfig
from trading.report import metrics
from trading.research.loss_attribution import fixed_factor_comparison, order_costs
from trading.rules import fees


IDENTIFIER = "20261005_034355_5dc945f9"
SIGNAL = "small_size_common_pool_control"


@pytest.fixture
def saved_factor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path / "project")
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "data")
    out = settings.ROOT / "outputs/stockdb_factor_replay" / IDENTIFIER
    artifacts = settings.DATA_ROOT / "research/stockdb_factor_replay" / IDENTIFIER
    out.mkdir(parents=True)
    inputs = {key: str(settings.DATA_ROOT / "research" / kind / "pinned")
              for key, kind in (("price_view", "bulk_adjusted_prices"), ("event_view", "stock_action_events"),
                                ("reference_view", "stock_action_references"))}
    event_view = settings.DATA_ROOT / "research/stock_action_events/pinned"
    event_view.mkdir(parents=True)
    (event_view / "source_input_calendar.csv").write_text("date\n2023-01-03\n2023-12-29\n2024-01-02\n")
    daily = [{"date": day, "equity": equity, "cash": equity, "fees": 0., "turnover_value": 0.,
              "stale_symbols": []} for day, equity in (("2023-01-03", 140000.), ("2023-12-29", 150000.))]
    config = StrategyConfig(signal="equal_weight", initial_cash=140000).to_dict()
    record = {"status": "completed_factor_ledger_diagnostics", "artifacts": str(artifacts), "sessions": 2,
              "execution_admitted": False, "stock_manifest": {**inputs, "scope": "full_source_sh_sz_research_only",
                  "replay_only": True, "execution_admitted": False, "canonical_codes": 7, "unsupported_instruments": {}},
              "plan": {"cases": [SIGNAL], "start": "2023-01-01", "end": "2023-12-31", "portfolio": config,
                  "stock_inputs": inputs, "cost_profile": "dated_stamp_tax_2023_v1", "execution_admitted": False,
                  "fresh_holdout_claimed": False},
              "results": [{"signal": SIGNAL, "status": "completed", **metrics(daily, 140000)}]}
    folder = artifacts / SIGNAL
    folder.mkdir(parents=True)
    pd.DataFrame(daily).to_csv(folder / "daily.csv", index=False)
    (folder / "replay.json").write_text(json.dumps({"status": "completed", "completed_sessions": 2,
                                                   "last_completed_session": "2023-12-29"}))
    path = out / "run.json"
    path.write_text(json.dumps(record))
    return record, path


def test_fixed_factor_reader_exposes_actual_calendar_config_without_application_accounts(saved_factor, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Research reader must not use application accounts")
    for name in ("get", "create", "runs"):
        monkeypatch.setattr(store, name, forbidden)
    record, _ = saved_factor
    summary = replay_reports.catalog()[0]
    assert (summary["start"], summary["end"]) == ("2023-01-03", "2023-12-29")
    assert summary["protocol"]["decision_cadence"] == "completed_month_end"
    assert summary["strategy_validated"] is summary["live_ready"] is False
    detail = replay_reports.detail(IDENTIFIER, SIGNAL)
    assert detail["config"] == record["plan"]["portfolio"]
    assert detail["metrics"]["net_return"] == pytest.approx(10000 / 140000)


def test_fixed_factor_reader_retains_incomplete_unknown_and_rejects_changed_views(saved_factor):
    record, path = saved_factor
    record["status"] = "running"
    path.write_text(json.dumps(record))
    assert replay_reports.catalog() == []
    record["status"] = "blocked_factor_ledger_diagnostics"
    record["results"][0]["status"] = "failed"
    path.write_text(json.dumps(record))
    assert replay_reports.detail(IDENTIFIER, SIGNAL)["metrics"] is None
    record["plan"]["stock_inputs"]["event_view"] += "changed"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="saved stock manifest"):
        replay_reports.detail(IDENTIFIER, SIGNAL)


def test_order_cost_attribution_uses_existing_dated_fee_rule_and_rejects_stale_tax():
    config = StrategyConfig()
    orders = [{"id": str(index), "symbol": "600000.SSE", "day": day, "side": "sell", "filled": 100,
               "notional": 1000000, "fees": fees(1000000, "sell", config, day=day)}
              for index, day in enumerate(("2023-08-25", "2023-08-28"))]
    costs = order_costs(orders, config)
    assert costs.sell_tax_cny.tolist() == [10., 5.]
    assert costs.fees_cny.tolist() == [15.1, 10.1]
    orders[0]["fees"] = orders[1]["fees"]
    with pytest.raises(ValueError, match="configured fees"):
        order_costs(orders, config)


def test_fixed_factor_comparison_does_not_rank_incomplete_or_different_basis_accounts():
    rows = [{"case_id": IDENTIFIER + ":" + signal, "status": "completed", "fixed_factor_basis": {"event_view": "same"},
             "net_pnl_cny": pnl, "costs": {"fees_cny": cost}, "raw_open_slippage": {"slippage_cny": 100},
             "metrics": {"net_return": pnl / 140000, "max_drawdown": -.1}}
            for signal, pnl, cost in (("value_size_activity_equal_rank_v1", 1000, 200), (SIGNAL, 3000, 100))]
    result = fixed_factor_comparison(rows)
    assert result["net_pnl_difference_cny"] == -2000
    assert result["fixed_fill_addback_difference_cny"] == -1900
    assert result["positive_net_increment"] is result["execution_admitted"] is False
    rows[1]["status"] = "failed"
    assert fixed_factor_comparison(rows)["net_difference"] is None
    rows[1]["status"] = "completed"
    rows[1]["fixed_factor_basis"]["event_view"] = "different"
    result = fixed_factor_comparison(rows)
    assert result["net_difference"] is None and result["basis_differences"] == ["event_view"]
