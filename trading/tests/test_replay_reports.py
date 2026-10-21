import json
from hashlib import sha256

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from trading import api, replay_reports, settings, store
from trading.config import StrategyConfig
from trading.report import metrics

IDENTIFIER = "20260930_175747_ec8fe67c"
MODEL_IDENTIFIER = "20260930_202414_030ba010"
FUND_IDENTIFIER = "20261002_185934_bb4c8ec5"


@pytest.fixture
def seat_saved(saved):
    record, old_out, artifacts = saved
    data = settings.DATA_ROOT
    inputs = {key: str(data / "research" / kind / "frozen") for key, kind in (
        ("price_view", "bulk_adjusted_prices"), ("event_view", "stock_action_events"),
        ("reference_view", "stock_action_references"))}
    event_view = data / "research/stock_action_events/frozen"
    event_view.mkdir(parents=True)
    pd.DataFrame({"date": [record["start"], record["end"]]}).to_csv(event_view / "source_input_calendar.csv", index=False)
    manifest = {**inputs, "scope": "full_source_sh_sz_research_only", "replay_only": True,
                "execution_admitted": False, "canonical_codes": 4, "unsupported_instruments": {"689009.SSE": "unsupported"}}
    cases = [{"signal": "TREND", "mode": "TREND", "lag": 0}] + [
        {"signal": f"lag{lag}_{mode}", "mode": mode, "lag": lag}
        for lag in (1, 2) for mode in ("TREND_ANY_LHB", "TREND_H1", "TREND_H4", "TREND_H1_H4")]
    out = settings.ROOT / "outputs/seat_bulk_replay" / IDENTIFIER
    out.mkdir(parents=True, exist_ok=True)
    (old_out / "run.json").unlink()
    new_artifacts = data / "research/seat_bulk_replay" / IDENTIFIER
    new_artifacts.mkdir(parents=True)
    (artifacts / "donchian_trend").rename(new_artifacts / "TREND")
    record.update(status="partial_seat_bulk_diagnostic", research_family=replay_reports.SEAT_KIND,
                  execution_admitted=False, artifacts=str(new_artifacts), stock_manifest=manifest)
    record["results"] = [{**record["results"][0], **cases[0]}] + [
        {**row, "status": "failed_preparation", "completed_sessions": 0, "error": "Unknown turnover"}
        for row in cases[1:]]
    record["protocol"] = {"id": replay_reports.SEAT_KIND, "cases": cases, "stock_inputs": inputs,
        "execution_admitted": False, "evaluation": [record["start"], record["end"]],
        "portfolio": record["protocol"]["portfolio"], "limits": ["Unknown publication timestamp"]}
    (out / "run.json").write_text(json.dumps(record))
    return record, out


def test_seat_reports_share_existing_catalog_and_read_only_ledger(seat_saved, monkeypatch):
    monkeypatch.setattr(store, "get", lambda *args: pytest.fail("No paper or broker account read"))
    entry = replay_reports.catalog()[0]
    assert entry["symbols"] == 3 and len(entry["results"]) == 9
    assert entry["results"][0]["source_signal"] == "TREND"
    value = replay_reports.detail(IDENTIFIER, "trend")
    assert value["metrics"]["net_return"] == pytest.approx(-.02)
    assert value["config"]["signal"] == seat_saved[0]["protocol"]["portfolio"]["signal"]
    assert value["source_limitations"] == ["Unknown publication timestamp"]
    assert not value["strategy_validated"] and not value["live_ready"]
    blocked = replay_reports.detail(IDENTIFIER, "lag1_trend_h1")
    assert blocked["receipt"] is None and blocked["metrics"] is None and blocked["daily"] == []
    assert blocked["error"] == "Unknown turnover"


def test_seat_reports_reject_changed_price_binding_or_missing_planned_case(seat_saved):
    record, out = seat_saved
    record["protocol"]["stock_inputs"]["price_view"] += "_other"
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Seat price/action inputs"):
        replay_reports.detail(IDENTIFIER, "trend")
    record["results"].pop()
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="fixed public-seat"):
        replay_reports.detail(IDENTIFIER, "trend")


@pytest.mark.parametrize("family", ["economic_value_monthly_fixed_v1", "economic_profitability_reporting_calendar_v1", "economic_qarp_reporting_calendar_v1"])
def test_saved_rank_monthly_cadence_is_not_the_engine_daily_check_interval(family, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    price_view = tmp_path / "research/bulk_adjusted_prices/frozen"
    manifest = {"scope": "full_source_sh_sz_research_only", "replay_only": True,
                "execution_admitted": False, "canonical_codes": 5, "unsupported_instruments": [],
                "price_view": str(price_view)}
    protocol = {"id": family, "signals": ["qarp_only"], "execution_admitted": False,
                "stock_manifest": manifest, "stock_inputs": {"price_view": str(price_view)},
                "signal_portfolio_configs": {"qarp_only": {"rebalance_sessions": 1}}}
    record = {"research_family": family, "protocol": protocol, "symbols": 5,
              "results": [{"signal": "qarp_only"}]}
    result = replay_reports._value_stock_record(record, profitability=family != "economic_value_monthly_fixed_v1")
    assert result["protocol"]["decision_cadence"] == "completed_month_end"
    assert result["protocol"]["signal_portfolio_configs"]["qarp_only"]["rebalance_sessions"] == 1
    assert "decision_cadence" not in record["protocol"]


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", tmp_path / "project")
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "data")
    out = settings.ROOT / "outputs/market_replay" / IDENTIFIER
    artifacts = settings.DATA_ROOT / "research/market_replay" / IDENTIFIER
    out.mkdir(parents=True)
    daily = [{"date": "2024-01-02", "equity": 10000., "cash": 10000., "fees": 0.,
              "turnover_value": 0., "stale_symbols": []},
             {"date": "2024-01-03", "equity": 9800., "cash": 9000., "fees": 5.,
              "turnover_value": 1000., "stale_symbols": ["600000.SSE"]}]
    config = StrategyConfig(initial_cash=10000).to_dict()
    record = {"status": "failed_ingestion_regression", "artifacts": str(artifacts),
              "dataset": "pinned", "purpose": "data_ingestion_and_ledger_regression_only",
              "scope": "partial", "symbols": 875, "start": daily[0]["date"], "end": daily[-1]["date"],
              "sessions": 2, "protocol": {"portfolio": config, "donchian_portfolio_override":
                  {"rebalance_sessions": 1, "retain_rank": 100}},
              "results": [{"signal": "donchian_trend", "status": "completed", **metrics(daily, 10000)},
                          {"signal": "momentum", "status": "failed", "error": "Fractional shares"}]}
    for signal, rows, status in (("donchian_trend", daily, "completed"), ("momentum", daily[:1], "failed")):
        folder = artifacts / signal
        folder.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(folder / "daily.csv", index=False)
        (folder / "replay.json").write_text(json.dumps({"status": status, "completed_sessions": len(rows),
            "last_completed_session": rows[-1]["date"], "attempted_session": daily[-1]["date"]}))
    (out / "run.json").write_text(json.dumps(record))
    return record, out, artifacts


def test_saved_ledger_metrics_and_stale_symbols(saved):
    result = replay_reports.detail(IDENTIFIER, "donchian_trend")
    assert result["metrics"]["net_return"] == pytest.approx(-.02)
    assert result["metrics"]["stale_valuation_days"] == 1
    assert result["config"]["rebalance_sessions"] == 1
    assert result["config"]["retain_rank"] == 100
    assert result["segments"][0]["sessions"] == 2
    assert result["live_ready"] is False


def test_named_strategy_wrapper_displays_actual_execution_config(saved):
    record, out, _ = saved
    record["protocol"]["signal_portfolio_configs"] = {
        "donchian_trend": {**record["protocol"]["portfolio"], "signal": "momentum"}}
    (out / "run.json").write_text(json.dumps(record))
    result = replay_reports.detail(IDENTIFIER, "donchian_trend")
    assert result["config"]["signal"] == "momentum"


def test_failed_replay_never_gets_full_period_metrics(saved):
    result = replay_reports.detail(IDENTIFIER, "momentum")
    assert result["metrics"] is None and result["segments"] == []
    assert len(result["daily"]) == result["receipt"]["completed_sessions"] == 1
    assert result["error"] == "Fractional shares"
    assert len(replay_reports.catalog()[0]["results"]) == 2


def test_old_reports_compute_missing_risk_metrics_from_marks(saved):
    record, out, _ = saved
    for key in ("sharpe_zero_rf_252", "annualized_volatility", "worst_day"):
        del record["results"][0][key]
    (out / "run.json").write_text(json.dumps(record))
    assert replay_reports.detail(IDENTIFIER, "donchian_trend")["metrics"]["worst_day"] == pytest.approx(-.02)


@pytest.mark.parametrize("field,value", [("sessions", 3), ("net_return", .9)])
def test_incomplete_or_changed_results_are_rejected(saved, field, value):
    record, out, _ = saved
    if field == "sessions":
        record[field] = value
    else:
        record["results"][0][field] = value
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError):
        replay_reports.detail(IDENTIFIER, "donchian_trend")


def test_artifacts_cannot_redirect_to_another_root(saved):
    record, out, _ = saved
    record["artifacts"] = str(out)
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="configured research root"):
        replay_reports.catalog()


def test_api_is_read_only_and_rejects_missing_or_unsafe_names(saved, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Research reader must not use account storage")
    monkeypatch.setattr(store, "create", forbidden)
    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr(store, "runs", forbidden)
    app = FastAPI()
    app.include_router(api.router)
    record, out, artifacts = saved
    files = [out / "run.json", *(artifacts.rglob("*.*"))]
    before = {path: path.read_bytes() for path in files}
    with TestClient(app) as client:
        assert client.get("/api/trading/research-replays").status_code == 200
        assert client.get(f"/api/trading/research-replays/{IDENTIFIER}/momentum").json()["metrics"] is None
        assert client.get(f"/api/trading/research-replays/{IDENTIFIER}/unlisted").status_code == 404
        assert client.get("/api/trading/research-replays/invalid/momentum").status_code == 404
        assert client.post(f"/api/trading/research-replays/{IDENTIFIER}/momentum").status_code == 405
    assert before == {path: path.read_bytes() for path in files}


@pytest.fixture
def model_saved(saved):
    dataset = settings.DATA_ROOT / "datasets/pinned"
    dataset.mkdir(parents=True)
    calendar = dataset / "calendar.csv"
    calendar.write_text("date\n2026-01-05\n2026-01-06\n")
    out = settings.ROOT / "outputs/factor_mining" / MODEL_IDENTIFIER
    artifacts = settings.DATA_ROOT / "research/factor_mining" / MODEL_IDENTIFIER
    out.mkdir(parents=True)
    config = StrategyConfig(initial_cash=10000).to_dict()
    daily = [{"date": "2026-01-05", "equity": 10000., "cash": 10000., "fees": 0.,
              "turnover_value": 0., "stale_symbols": []},
             {"date": "2026-01-06", "equity": 9800., "cash": 9000., "fees": 5.,
              "turnover_value": 1000., "stale_symbols": []}]
    results = []
    for variant, seed, folder in (("B0", 17, "B0_seed17/replay"), ("B0", 29, "B0_seed29/replay"),
                                  ("B0", 43, "B0_seed43/replay"), ("RULE", None, "rule_replay")):
        failed = seed == 43
        rows = daily[:1] if failed else daily
        path = artifacts / folder
        path.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(path / "daily.csv", index=False)
        (path / "replay.json").write_text(json.dumps({"status": "failed" if failed else "completed",
            "completed_sessions": len(rows), "last_completed_session": rows[-1]["date"],
            "attempted_session": daily[-1]["date"]}))
        results.append({"variant": variant, "seed": seed, "status": "failed" if failed else "completed",
                        **({"error": "Rights unresolved"} if failed else metrics(daily, 10000))})
    record = {"experiment_kind": "alpha158_ingestion_ledger_comparison", "status": "failed_ledger_comparison",
              "data_output": str(artifacts), "data_quality": {"symbols": 1003},
              "dataset_manifest": {"folder": str(dataset), "scope": "partial", "selection_is_point_in_time": False},
              "input_hashes": {"calendar.csv": sha256(calendar.read_bytes()).hexdigest()},
              "config": {"dataset": str(dataset), "segments": {"test": ["2026-01-01", "2026-01-06"]},
                         "seeds": [17, 29, 43]}, "portfolio_config": config, "portfolio_metrics": results}
    (out / "run.json").write_text(json.dumps(record))
    return record, out, artifacts, calendar


def test_model_baseline_uses_real_sessions_and_preserves_failed_seed(model_saved, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Model report must not import or access trading accounts")
    for name in ("get", "create", "runs"):
        monkeypatch.setattr(store, name, forbidden)
    record, out, artifacts, calendar = model_saved
    files = [out / "run.json", calendar, *(artifacts.rglob("*.*"))]
    before = {path: path.read_bytes() for path in files}
    runs = replay_reports.catalog()
    assert runs[0]["research_family"] == "alpha158_lightgbm"
    assert runs[0]["start"] == "2026-01-05" and runs[0]["sessions"] == 2
    assert len(runs[0]["results"]) == 4
    assert runs[0]["strategy_validated"] is runs[0]["live_ready"] is False
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        model = client.get(f"/api/trading/research-replays/{MODEL_IDENTIFIER}/alpha158_seed17").json()
        assert model["metrics"]["net_return"] == pytest.approx(-.02)
        assert model["config"] == record["portfolio_config"]
        failed = client.get(f"/api/trading/research-replays/{MODEL_IDENTIFIER}/alpha158_seed43").json()
        assert failed["metrics"] is None and failed["segments"] == []
        assert failed["error"] == "Rights unresolved" and len(failed["daily"]) == 1
        assert client.post(f"/api/trading/research-replays/{MODEL_IDENTIFIER}/alpha158_seed17").status_code == 405
    assert before == {path: path.read_bytes() for path in files}


def test_model_catalog_preserves_test_ic_and_paired_delta(model_saved):
    record, out, _, _ = model_saved
    record["model_metrics"] = [{
        "variant": "B0", "seed": 17, "split": "test", "features": 158,
        "best_iteration": 8, "ic_days": 2, "mean_rank_ic": 0.041,
        "ic_std": 0.12, "hac_t_lag5": 1.9,
    }]
    record["paired_deltas"] = [{
        "seed": 17, "comparison": "B0_minus_RULE", "comparison_status": "available",
        "delta_net_return": 0.21, "delta_max_drawdown": 0.01,
        "delta_turnover_over_initial": 27.2,
    }]
    (out / "run.json").write_text(json.dumps(record))

    summary = replay_reports.catalog()[0]

    assert summary["model"] == "LightGBM"
    assert summary["model_diagnostics"][0]["mean_rank_ic"] == 0.041
    assert summary["paired_deltas"][0]["delta_net_return"] == 0.21


@pytest.mark.parametrize("change", ["calendar", "missing_seed", "redirect"])
def test_model_inputs_and_complete_planned_case_set_are_checked(model_saved, change):
    record, out, _, calendar = model_saved
    if change == "calendar":
        calendar.write_text("date\n2026-01-05\n2026-01-07\n")
    elif change == "missing_seed":
        record["portfolio_metrics"].pop(0)
    else:
        record["data_output"] = str(out)
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError):
        replay_reports.detail(MODEL_IDENTIFIER, "alpha158_seed17")


@pytest.mark.parametrize("field,value", [("experiment_kind", "bounded_factor_search"), ("status", "running")])
def test_search_and_unfinished_model_runs_are_not_baseline_reports(model_saved, field, value):
    record, out, _, _ = model_saved
    record[field] = value
    (out / "run.json").write_text(json.dumps(record))
    assert all(row["id"] != MODEL_IDENTIFIER for row in replay_reports.catalog())
    with pytest.raises(FileNotFoundError):
        replay_reports.detail(MODEL_IDENTIFIER, "alpha158_seed17")


def test_full_source_model_reader_keeps_failed_cases_and_research_boundary(model_saved):
    record, out, _, calendar = model_saved
    prices = settings.DATA_ROOT / "research/bulk_adjusted_prices/pinned"
    events = settings.DATA_ROOT / "research/stock_action_events/pinned"
    prices.mkdir(parents=True)
    events.mkdir(parents=True)
    bulk_calendar = events / "source_input_calendar.csv"
    bulk_calendar.write_bytes(calendar.read_bytes())
    record.update(bulk_inputs={"price_view": str(prices), "event_view": str(events)},
                  calendar_file=str(bulk_calendar), data_quality={"symbols": 5368})
    record["config"]["dataset"] = str(prices)
    record["dataset_manifest"].update(folder=str(prices), scope="full_source_sh_sz_research_only",
                                      execution_admitted=False)
    (out / "run.json").write_text(json.dumps(record))
    result = replay_reports.detail(MODEL_IDENTIFIER, "alpha158_seed43")
    assert result["metrics"] is None and len(result["daily"]) == 1
    summary = next(row for row in replay_reports.catalog() if row["id"] == MODEL_IDENTIFIER)
    assert summary["symbols"] == 5368 and len(summary["results"]) == 4
    assert summary["live_ready"] is False
    record["dataset_manifest"]["execution_admitted"] = True
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="research boundary"):
        replay_reports.detail(MODEL_IDENTIFIER, "alpha158_seed17")


@pytest.fixture
def fund_saved(saved):
    dataset = settings.DATA_ROOT / "datasets/mixed_fund_diagnostic/snapshots/pinned"
    dataset.mkdir(parents=True)
    manifest = dataset / "manifest.json"
    manifest.write_text(json.dumps({"scope": "mixed_fund_research_only", "replay_only": True,
        "execution_admitted": False, "instrument_profiles": {"510300.SSE": "pinned"}}))
    out = settings.ROOT / "outputs/market_replay" / FUND_IDENTIFIER
    artifacts = settings.DATA_ROOT / "research/fund_trend" / FUND_IDENTIFIER
    out.mkdir(parents=True)
    daily = [{"date": "2025-12-31", "equity": 11000., "cash": 11000., "fees": 0.,
              "turnover_value": 0., "stale_symbols": []},
             {"date": "2026-01-05", "equity": 10890., "cash": 10890., "fees": 0.,
              "turnover_value": 0., "stale_symbols": []}]
    variants = ["monthly_fixed_sleeves", "own_total_return_10m"]
    record = {"status": "completed_fund_trend_diagnostic", "artifacts": str(artifacts),
        "protocol": {"id": "three_asset_class_own_total_return_10m_v1", "dataset": str(dataset),
            "dataset_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
            "symbols": ["510300.SSE"], "variants": variants, "start": daily[0]["date"],
            "end": daily[-1]["date"], "sessions": 2,
            "config": StrategyConfig(signal="equal_weight", initial_cash=10000).to_dict()},
        "results": [{"signal": signal, "status": "completed", **metrics(daily, 10000)}
                    for signal in variants]}
    for signal in variants:
        folder = artifacts / signal
        folder.mkdir(parents=True)
        pd.DataFrame(daily).to_csv(folder / "daily.csv", index=False)
        (folder / "replay.json").write_text(json.dumps({"status": "completed", "completed_sessions": 2,
            "last_completed_session": daily[-1]["date"]}))
    (out / "run.json").write_text(json.dumps(record))
    return record, out, artifacts


def test_fund_reader_uses_single_saved_ledger_and_actual_configuration(fund_saved, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fund reader must not access or create accounts")
    for name in ("get", "create", "runs"):
        monkeypatch.setattr(store, name, forbidden)
    summary = next(row for row in replay_reports.catalog() if row["id"] == FUND_IDENTIFIER)
    assert summary["research_family"] == "fund_allocation" and summary["symbols"] == 1
    assert summary["protocol"]["decision_cadence"] == "completed_month_end"
    result = replay_reports.detail(FUND_IDENTIFIER, "own_total_return_10m")
    assert result["config"]["signal"] == "equal_weight"
    assert result["segments"][1]["net_return"] == pytest.approx(-.01)
    assert result["strategy_validated"] is result["live_ready"] is False


@pytest.mark.parametrize("change", ["redirect", "missing_variant", "admission"])
def test_fund_reader_keeps_pinned_root_planned_cases_and_diagnostic_boundary(fund_saved, change):
    record, out, _ = fund_saved
    if change == "redirect":
        record["artifacts"] = str(out)
    elif change == "missing_variant":
        record["results"].pop()
    else:
        manifest = settings.DATA_ROOT / "datasets/mixed_fund_diagnostic/snapshots/pinned/manifest.json"
        data = json.loads(manifest.read_text())
        data["execution_admitted"] = True
        manifest.write_text(json.dumps(data))
        record["protocol"]["dataset_manifest_sha256"] = sha256(manifest.read_bytes()).hexdigest()
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError):
        replay_reports.detail(FUND_IDENTIFIER, "monthly_fixed_sleeves")


def test_running_fund_comparison_does_not_break_finished_catalog(fund_saved):
    record, out, _ = fund_saved
    record["status"] = "running"
    (out / "run.json").write_text(json.dumps(record))
    assert all(row["id"] != FUND_IDENTIFIER for row in replay_reports.catalog())


def test_equal_risk_comparison_uses_its_explicit_planned_case_set(fund_saved):
    import shutil
    record, out, artifacts = fund_saved
    record["protocol"]["id"] = "three_asset_equal_risk_1y_v1"
    record["protocol"]["variants"] = ["monthly_fixed_sleeves", "monthly_equal_risk"]
    record["results"][1]["signal"] = "monthly_equal_risk"
    shutil.copytree(artifacts / "own_total_return_10m", artifacts / "monthly_equal_risk")
    (out / "run.json").write_text(json.dumps(record))
    summary = next(row for row in replay_reports.catalog() if row["id"] == FUND_IDENTIFIER)
    assert summary["results"][1]["signal"] == "monthly_equal_risk"
    result = replay_reports.detail(FUND_IDENTIFIER, "monthly_equal_risk")
    assert result["config"]["signal"] == "equal_weight"
    assert result["metrics"]["net_return"] == pytest.approx(.089)
    record["protocol"]["variants"][1] = "own_total_return_10m"
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="planned ledger"):
        replay_reports.catalog()


def test_allocation_controls_reader_requires_all_four_planned_ledgers(fund_saved):
    import shutil
    record, out, artifacts = fund_saved
    variants = ["monthly_fixed_sleeves", "monthly_equal_risk", "static_2023_equal_risk", "sovereign_bond_sleeve"]
    template = record["results"][0]
    record["protocol"].update(id="three_asset_equal_risk_controls_v1", variants=variants)
    record["results"] = [{**template, "signal": signal} for signal in variants]
    for signal in variants[1:]:
        shutil.copytree(artifacts / "monthly_fixed_sleeves", artifacts / signal)
    (out / "run.json").write_text(json.dumps(record))
    summary = next(row for row in replay_reports.catalog() if row["id"] == FUND_IDENTIFIER)
    assert [row["signal"] for row in summary["results"]] == variants
    for signal in variants[2:]:
        result = replay_reports.detail(FUND_IDENTIFIER, signal)
        assert result["metrics"]["net_return"] == pytest.approx(.089)
        assert result["live_ready"] is False
    record["results"].pop()
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="planned ledger"):
        replay_reports.catalog()
