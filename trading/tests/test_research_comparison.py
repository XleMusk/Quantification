import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from trading import api, research_comparison as comparison, replay_reports, store
from trading.tests.test_replay_reports import IDENTIFIER, saved


def test_real_saved_reader_exposes_marks_only_when_requested(saved):
    ordinary = replay_reports.detail(IDENTIFIER, "donchian_trend")
    assert "daily_marks" not in ordinary
    value = replay_reports.detail(IDENTIFIER, "donchian_trend", include_marks=True)
    assert value["daily_marks"][1]["fees"] == 5.
    assert value["daily_marks"][1]["stale_symbols"] == ["600000.SSE"]
    assert ordinary["daily"] == value["daily"]


def test_actual_stock_record_schema_maps_inputs_without_changing_saved_accounts(saved):
    record, out, _ = saved
    from trading import settings
    dataset = settings.DATA_ROOT / "research/bulk_adjusted_prices/source"
    record.update(research_family="low_risk_stock_selection", scope="full_source_sh_sz_research_only",
                  inputs={"price_view": str(dataset), "canonical_codes": record["symbols"]})
    del record["dataset"]
    record["protocol"].update(id="low_risk_price_volume_ablation_v1", execution_admitted=False,
                              signals=[row["signal"] for row in record["results"]])
    (out / "run.json").write_text(json.dumps(record))
    before = (out / "run.json").read_bytes()
    value, _ = replay_reports._read(IDENTIFIER)
    assert value["dataset"] == str(dataset.resolve())
    assert replay_reports.detail(IDENTIFIER, "donchian_trend")["config"]["signal"] == "risk_adjusted_momentum"
    assert comparison.compare([IDENTIFIER], "2024-01-02", "2024-01-03")["cases"][0]["symbols"] == 875
    assert (out / "run.json").read_bytes() == before


@pytest.mark.parametrize("family, scope", [
    ("economic_value_monthly_fixed_v1", "full_source_sh_sz_matched_valuation_pool_research_only"),
    ("economic_profitability_monthly_fixed_v1", "available_financial_subset_profitability_research_only"),
    ("economic_profitability_same_period_v1", "available_financial_same_period_subset_profitability_research_only"),
    ("economic_profitability_reporting_calendar_v1", "available_financial_same_period_subset_profitability_research_only")])
def test_value_reader_maps_saved_scope_without_editing_or_replaying_accounts(saved, family, scope):
    record, out, _ = saved
    from trading import settings
    dataset = settings.DATA_ROOT / "research/bulk_adjusted_prices/source"
    record["research_family"] = family
    for key in ("dataset", "scope", "purpose"):
        del record[key]
    signals = [row["signal"] for row in record["results"]]
    record["protocol"].update(id=family, signals=signals, execution_admitted=False,
        stock_inputs={"price_view": str(dataset)}, stock_manifest={"price_view": str(dataset),
            "scope": "full_source_sh_sz_research_only", "canonical_codes": 875,
            "unsupported_instruments": {}, "replay_only": True, "execution_admitted": False},
        signal_portfolio_configs={signal: record["protocol"]["portfolio"] for signal in signals})
    (out / "run.json").write_text(json.dumps(record))
    before = (out / "run.json").read_bytes()
    value = comparison.compare([IDENTIFIER], "2024-01-02", "2024-01-03")
    assert value["comparable_cases"] == 1 and value["cases"][0]["dataset"] == str(dataset.resolve())
    assert value["cases"][0]["scope"] == scope
    assert not value["account_created"] and not value["backtest_run"]
    assert (out / "run.json").read_bytes() == before
    record["protocol"]["stock_manifest"]["execution_admitted"] = True
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="matched valuation"):
        replay_reports._read(IDENTIFIER)


def test_actual_mixed_schema_preserves_joint_scope_profiles_and_config(saved):
    record, out, _ = saved
    from trading import settings
    dataset = settings.DATA_ROOT / "research/bulk_adjusted_prices/source"
    funds = settings.DATA_ROOT / "datasets/fund_diagnostic/snapshots/source"
    config = {**record["protocol"]["portfolio"], "signal": "equal_weight", "holdings": 22, "retain_rank": 32}
    record["protocol"] = {"id": "stock_defensive_fixed_40_20_20_v1", "config": config,
        "variants": [row["signal"] for row in record["results"]], "execution_admitted": False,
        "start": record["start"], "end": record["end"], "sessions": record["sessions"],
        "stock_inputs": {"scope": "full_source_sh_sz_research_only", "replay_only": True,
                         "price_view": str(dataset), "canonical_codes": 875},
        "fund_dataset": str(funds), "fund_profiles": {"511010.SSE": {}, "518880.SSE": {}}}
    for key in ("dataset", "scope", "symbols", "start", "end", "sessions", "purpose"):
        del record[key]
    (out / "run.json").write_text(json.dumps(record))
    value, _ = replay_reports._read(IDENTIFIER)
    assert value["symbols"] == 877 and value["research_family"] == "stock_defensive_allocation"
    assert value["fund_dataset"] == str(funds.resolve())
    actual = replay_reports.detail(IDENTIFIER, "donchian_trend")
    assert actual["config"]["holdings"] == 22 and actual["config"]["retain_rank"] == 32
    assert actual["config"]["signal"] == "equal_weight"
    record["protocol"]["fund_profiles"]["510300.SSE"] = {}
    (out / "run.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="mixed stock/fund"):
        replay_reports._read(IDENTIFIER)


def test_source_target_plan_is_not_mislabeled_as_a_saved_account_in_catalog(saved):
    record, out, _ = saved
    plan = out.parent / "20260930_175748_ec8fe67d"
    plan.mkdir()
    source = {**record, "research_family": "low_risk_stock_selection",
              "status": "prepared_low_risk_source_targets", "results": []}
    (plan / "run.json").write_text(json.dumps(source))
    assert [row["id"] for row in replay_reports.catalog()] == [IDENTIFIER]
    assert (plan / "run.json").exists()


@pytest.mark.parametrize("status, results", [("running", []), ("failed_profitability_source_targets", [])])
def test_reporting_calendar_catalog_excludes_unfinished_or_prepared_accounts(saved, status, results):
    record, out, _ = saved
    plan = out.parent / "20261004_155741_4cd6fe0b"
    plan.mkdir()
    source = {**record, "research_family": "economic_profitability_reporting_calendar_v1",
              "status": status, "results": results}
    source["protocol"] = {**record["protocol"], "signals": [row["signal"] for row in record["results"]]}
    (plan / "run.json").write_text(json.dumps(source))
    assert [row["id"] for row in replay_reports.catalog()] == [IDENTIFIER]


def test_window_uses_preceding_equity_includes_first_return_and_stale_marks(saved):
    value = comparison.compare([IDENTIFIER], "2024-01-03", "2024-01-03")
    completed, failed = value["cases"]
    assert value["sessions"] == 1 and value["comparable_cases"] == 1
    assert completed["opening_equity"] == 10000.
    assert completed["opening_basis"] == "preceding_saved_session_equity"
    assert completed["metrics"]["net_return"] == pytest.approx(-.02)
    assert completed["metrics"]["worst_day"] == pytest.approx(-.02)
    assert completed["metrics"]["fees"] == 5.
    assert completed["metrics"]["stale_valuation_days"] == 1
    assert completed["metrics"]["sharpe_zero_rf_252"] is None
    assert failed["status"] == "failed_source_case" and failed["metrics"] is None
    assert value["descriptive_nondominated_case_ids"] == []
    assert value["account_created"] is value["backtest_run"] is value["execution_admitted"] is False


def test_uncovered_period_never_gets_shortened_or_zero_return_metrics(saved):
    value = comparison.compare([IDENTIFIER], "2024-01-02", "2024-01-04")
    assert value["status"] == "no_common_completed_window"
    assert value["cases"][0]["status"] == "window_not_covered"
    assert value["cases"][0]["available_sessions"] == 2
    assert all(row["metrics"] is None for row in value["cases"])
    assert value["calendar"] == []


def test_calendar_mismatch_cannot_be_intersected_into_comparison(saved, monkeypatch):
    other = "20260930_175748_ec8fe67d"
    original_read, original_detail = replay_reports._read, replay_reports.detail
    def read(identifier):
        return original_read(IDENTIFIER)
    def detail(identifier, signal, **kwargs):
        value = original_detail(IDENTIFIER, signal, **kwargs)
        if identifier == other:
            value["daily_marks"].insert(1, {**value["daily_marks"][0], "date": "2024-01-02T12:00"})
        return value
    monkeypatch.setattr(replay_reports, "_read", read)
    monkeypatch.setattr(replay_reports, "detail", detail)
    value = comparison.compare([IDENTIFIER, other], "2024-01-02", "2024-01-03")
    assert value["comparable_cases"] == 1
    assert value["cases"][2]["status"] == "calendar_mismatch"
    assert value["cases"][2]["metrics"] is None


def test_frontier_requires_complete_finite_nonstale_values_and_all_three_objectives():
    def row(name, ret, sharpe, dd, stale=0):
        return {"case_id": name, "status": "comparable", "metrics": {
            "net_return": ret, "sharpe_zero_rf_252": sharpe, "max_drawdown": dd, "stale_valuation_days": stale}}
    rows = [row("better", .1, 1., -.1), row("dominated", .08, .5, -.2),
            row("low_drawdown", .03, 2., -.02), row("stale", .3, 4., -.01, 1),
            row("cash", 0., None, 0.), row("invalid", .4, float("inf"), -.01)]
    assert comparison.nondominated(rows) == ["better", "low_drawdown"]


def test_fractional_assumption_survives_reader_and_is_not_a_frontier_candidate(saved):
    record, out, _ = saved
    assumption = {"id": "ordinary_fraction_floor_v1", "rounding": "floor"}
    record["protocol"]["fractional_allocation"] = assumption
    record["results"][0]["label"] = "Continuous momentum / fractional shares floor ASSUMPTION"
    (out / "run.json").write_text(json.dumps(record))
    assert replay_reports.detail(IDENTIFIER, "donchian_trend")["fractional_allocation_assumption"] == assumption
    row = comparison.compare([IDENTIFIER], "2024-01-02", "2024-01-03")["cases"][0]
    assert row["metrics"] is not None and row["fractional_allocation_assumption"] == assumption
    assert "ASSUMPTION" in row["label"] and row["descriptive_nondominated"] is False
    optimistic = {**row, "metrics": {"net_return": 1, "max_drawdown": 0,
        "sharpe_zero_rf_252": 5, "stale_valuation_days": 0}}
    assert comparison.nondominated([optimistic]) == []


def test_api_compares_without_account_storage_or_writing_sources(saved, monkeypatch):
    record, out, artifacts = saved
    files = [out / "run.json", *artifacts.rglob("*.*")]
    before = {path: path.read_bytes() for path in files}
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only comparison must not use trading account storage")
    for name in ("create", "get", "runs"):
        monkeypatch.setattr(store, name, forbidden)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        result = client.get("/api/trading/research-comparison", params={
            "runs": IDENTIFIER, "start": "2024-01-02", "end": "2024-01-03"})
        assert result.status_code == 200
        assert result.json()["requested_cases"] == 2
        assert client.post("/api/trading/research-comparison").status_code == 405
        assert client.get("/api/trading/research-comparison", params={
            "runs": IDENTIFIER + "," + IDENTIFIER, "start": "2024-01-02", "end": "2024-01-03"}).status_code == 409
    assert before == {path: path.read_bytes() for path in files}


def test_duplicate_runs_invalid_dates_and_unknown_runs_are_not_ignored(saved):
    with pytest.raises(ValueError, match="distinct"):
        comparison.compare([IDENTIFIER, IDENTIFIER], "2024-01-02", "2024-01-03")
    with pytest.raises(ValueError):
        comparison.compare([IDENTIFIER], "2024-1-02", "2024-01-03")
    with pytest.raises(ValueError, match="ordered"):
        comparison.compare([IDENTIFIER], "2024-01-03", "2024-01-02")
    with pytest.raises(FileNotFoundError):
        comparison.compare(["20260930_175748_ec8fe67d"], "2024-01-02", "2024-01-03")


def test_cli_saves_only_derived_comparison_not_an_account(saved):
    _, out, _ = saved
    assert comparison.main(["--run", IDENTIFIER, "--start", "2024-01-02", "--end", "2024-01-03"]) == 1
    root = out.parent.parent / "research_comparisons"
    path = next(root.glob("*/comparison.json"))
    value = json.loads(path.read_text())
    assert value["backtest_run"] is False and value["unavailable_cases"] == 1
    assert (path.parent / "REPORT.md").is_file()
    assert not (path.parent / "account.json").exists()
