"""Saved account results remain comparable only on the exact retained basis."""
from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading import settings as trading_settings, store as trading_store
from trading.config import StrategyConfig
from trading.ledger import new_state
from webapp.backend.app import research_workspace as workspace


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    project, data = tmp_path / "project", tmp_path / "data"
    project.mkdir(); data.mkdir()
    monkeypatch.setattr(trading_settings, "ROOT", project)
    monkeypatch.setattr(trading_settings, "DATA_ROOT", data)
    monkeypatch.setattr(trading_settings, "DB_PATH", data / "trading.sqlite3")
    trading_store.initialize()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def record():
    cfg = StrategyConfig().to_dict()
    return {"experiment_kind": "alpha158_baseline_comparison", "status": "failed_ledger_comparison",
            "portfolio_config": cfg, "dataset_manifest": {"scope": "same_frozen_eight_stocks"},
            "config": {"segments": {"test": ["2025-01-02", "2025-09-30"]}},
            "input_hashes": {name: sha256(name.encode()).hexdigest() for name in
                             ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv")},
            "portfolio_metrics": [
                {"variant": "B0", "seed": 0, "status": "completed", "completed_sessions": 180,
                 "net_return": .12, "max_drawdown": -.08, "fees": 300, "turnover_over_initial": 2.5},
                {"variant": "RULE", "status": "completed", "completed_sessions": 180,
                 "net_return": .10, "max_drawdown": -.06, "fees": 200, "turnover_over_initial": 2},
                {"variant": "B0", "seed": 1, "status": "failed", "completed_sessions": 44,
                 "net_return": .99, "fees": 100, "error": "held action unavailable"}]}


def test_saved_complete_and_failed_arms_retained_without_promoting_prefix():
    path = save(trading_settings.ROOT / "outputs/factor_mining/run/run.json", record())
    result = workspace.workspace()
    rows = result["experiments"]
    assert len(rows) == 3
    assert rows[0]["complete"] is True and rows[0]["metrics"]["net_return"] == .12
    assert rows[0]["comparison_key"] == rows[1]["comparison_key"] is not None
    assert rows[2]["complete"] is False and rows[2]["metrics"] is None
    assert rows[2]["observed_costs"]["fees"] == 100
    assert rows[2]["observed_costs"]["basis"] == "completed_prefix_only"
    assert rows[0]["source"]["path"] == str(path)
    assert result["training_started"] is False and result["ledger_mutated"] is False


def test_comparison_checks_data_costs_dates_and_source_record():
    payload = record()
    row = workspace._entries(payload, {"path": "run.json", "sha256": "a" * 64})[0]
    for transform in (lambda v: v["portfolio_config"].update(commission=.0005),
                      lambda v: v["input_hashes"].update({"raw.parquet": "b" * 64}),
                      lambda v: v["config"]["segments"].update(test=["2024-01-02", "2024-09-30"])):
        changed = deepcopy(payload); transform(changed)
        candidate = workspace._entries(changed, {"path": "run.json", "sha256": "a" * 64})[0]
        assert candidate["comparison_key"] != row["comparison_key"]
    changed = deepcopy(payload); changed["portfolio_config"].pop("slippage_bps")
    candidate = workspace._entries(changed, {"path": "run.json", "sha256": "a" * 64})[0]
    assert candidate["comparison_key"] is None
    assert "费用与执行参数" in candidate["missing_comparison_fields"]


def family_portfolio_record():
    """The family_portfolios producer records stock and mixed arm configs."""
    stock = {**StrategyConfig().to_dict(), "holdings": 20, "retain_rank": 30, "initial_cash": 140000}
    mixed = {**stock, "holdings": 22, "retain_rank": 32}
    return {
        "purpose": "fixed_family_post_training_cost_inclusive_v1", "status": "completed",
        "start": "2025-01-02", "end": "2025-09-30",
        "scope": "full_source_stocks_plus_two_defensive_funds_research_only",
        "input_hashes": record()["input_hashes"],
        "protocol": {"portfolio": stock, "signal_portfolio_configs": {
            "stock_equal_three_families": stock,
            "mixed_equal_three_families": mixed}},
        "results": [{"signal": signal, "status": "completed", "net_return": .1,
                     "completed_sessions": 180}
                    for signal in ("stock_equal_three_families", "mixed_equal_three_families")],
    }


def test_family_portfolio_configs_preserve_each_arms_capacity_and_comparison_basis():
    payload = family_portfolio_record()
    stock, mixed = workspace._entries(payload, {"path": "run.json", "sha256": "a" * 64})
    assert stock["capacity"]["holdings"] == 20
    assert mixed["capacity"]["holdings"] == 22
    assert stock["basis"]["costs"] == mixed["basis"]["costs"]
    assert stock["comparison_key"] is not None and mixed["comparison_key"] is not None
    assert stock["comparison_key"] != mixed["comparison_key"]
    payload["protocol"]["signal_portfolio_configs"]["mixed_equal_three_families"]["commission"] = .0005
    mixed = workspace._entries(payload, {"path": "run.json", "sha256": "a" * 64})[1]
    assert mixed["basis"]["costs"]["commission"] == .0005


@pytest.mark.parametrize("missing_entry", [False, True])
def test_explicit_signal_configs_do_not_borrow_missing_fields_from_default(missing_entry):
    payload = family_portfolio_record()
    configs = payload["protocol"]["signal_portfolio_configs"]
    if missing_entry:
        del configs["mixed_equal_three_families"]
    else:
        del configs["mixed_equal_three_families"]["slippage_bps"]
    stock, mixed = workspace._entries(payload, {"path": "run.json", "sha256": "a" * 64})
    assert stock["comparison_key"] is not None
    assert mixed["comparison_key"] is None
    assert "费用与执行参数" in mixed["missing_comparison_fields"]
    if missing_entry:
        assert mixed["capacity"]["holdings"] is None


def test_monthly_source_review_checks_registration_and_retains_partial_cases():
    registration = save(trading_settings.DATA_ROOT / "research/monthly_max_vs_sigma_source_review/registration/registration.json", {"inputs": "fixed"})
    payload = {"schema": "same_month_source_review_result", "status": "completed_two_arm_attempt",
               "config": StrategyConfig().to_dict(), "spec": {"start": "2024-01-02", "end": "2026-09-30", "universe": "same_pool"},
               "registration": {"path": str(registration), "sha256": sha256(registration.read_bytes()).hexdigest()},
               "cases": [{"arm": "low_max", "status": "failed", "full_period_metrics_available": False,
                          "net_return": .8, "completed_sessions": 106},
                         {"arm": "low_sigma", "status": "completed", "full_period_metrics_available": False,
                          "net_return": .2, "completed_sessions": 115}]}
    path = save(trading_settings.DATA_ROOT / "research/monthly_max_vs_sigma_source_review/final/result.json", payload)
    rows = workspace._entries(*workspace._read(path))
    assert len(rows) == 2 and all(row["metrics"] is None for row in rows)
    registration.write_text('{"changed":true}')
    rows = workspace._entries(*workspace._read(path))
    assert all(row["comparison_key"] is None for row in rows)
    assert any("版本不一致" in note for note in rows[0]["caveats"])


def account():
    state = new_state(100000)
    state["cash"] = 5000000
    state["day"] = "2025-01-03"
    state["positions"] = {"600000.SSE": [{"qty": 3000, "available": "2025-01-03"}],
                          "000001.SZSE": [{"qty": 1000, "available": "2025-01-06"}]}
    state["prices"] = {"600000.SSE": 1000, "000001.SZSE": 2000}
    return {"id": "book", "name": "independent paper", "mode": "paper", "status": "paused",
            "start": "2025-01-02", "dataset": "", "state": state, "config": StrategyConfig().to_dict()}


def test_portfolio_uses_same_ledger_currency_t1_and_invested_hhi():
    run = account(); original = deepcopy(run)
    result = workspace.portfolio_metrics(run)
    assert result["equity"] == 100000 and result["cash"] == 50000
    assert result["gross_exposure"] == .5 and result["largest_weight"] == .3
    assert result["hhi"] == pytest.approx(.6 ** 2 + .4 ** 2)
    assert result["effective_positions"] == pytest.approx(1 / .52)
    assert next(row for row in result["positions"] if row["symbol"] == "000001.SZSE")["sellable"] == 0
    assert all(row["liquidity_participation"] is None for row in result["positions"])
    assert run == original


def test_missing_mark_never_becomes_zero_exposure():
    run = account(); del run["state"]["prices"]["600000.SSE"]
    result = workspace.portfolio_metrics(run)
    assert result["equity"] is None and result["gross_exposure"] is None
    assert result["largest_weight"] is None and result["hhi"] is None
    assert result["valuation_complete"] is False
    assert any("600000.SSE" in warning for warning in result["warnings"])


def test_liquidity_requires_exact_saved_date_price_and_raw_basis(tmp_path):
    run = account(); run["mode"] = "replay"
    folder = trading_settings.DATA_ROOT / "datasets/fixed"
    save(folder / "manifest.json", {"status": "ready"})
    raw = pd.DataFrame({"date": ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-03"],
                        "symbol": ["600000.SSE"] * 3 + ["000001.SZSE"],
                        "close": [10., 10., 999., 999.], "volume": [100., 100000., 1., 100000.], "adjustflag": "3"})
    raw.to_parquet(folder / "raw.parquet", index=False)
    run["dataset"] = str(folder)
    result = workspace.portfolio_metrics(run)
    selected = next(row for row in result["positions"] if row["symbol"] == "600000.SSE")
    assert selected["liquidity_participation"] == .03
    wrong = next(row for row in result["positions"] if row["symbol"] == "000001.SZSE")
    assert wrong["liquidity_participation"] is None


def test_outside_and_nonfinite_results_do_not_get_interpreted(tmp_path):
    outside = tmp_path / "outside.json"; outside.write_text('{}')
    with pytest.raises(ValueError, match="目录"):
        workspace._read(outside)
    path = trading_settings.ROOT / "outputs/factor_mining/bad/run.json"
    path.parent.mkdir(parents=True); path.write_text('{"status":NaN}')
    result = workspace.workspace()
    assert result["experiments"] == [] and len(result["warnings"]) == 1
