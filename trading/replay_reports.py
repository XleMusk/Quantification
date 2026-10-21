"""Read saved same-ledger research reports without importing trading accounts."""
import ast
import csv
from hashlib import sha256
import json
from pathlib import Path
import re

from . import settings
from .report import metrics

RUN_ID = re.compile(r"\d{8}_\d{6}_[0-9a-f]{8}")
SIGNAL = re.compile(r"[a-z][a-z0-9_]*")
BASELINE_KINDS = {"alpha158_ingestion_ledger_comparison", "alpha158_baseline_comparison"}
FUND_KINDS = {"three_asset_class_own_total_return_10m_v1", "six_equity_etf_own_total_return_10m_v1",
              "three_asset_equal_risk_1y_v1", "three_asset_equal_risk_controls_v1"}
PROFITABILITY_KINDS = {"economic_profitability_monthly_fixed_v1", "economic_profitability_same_period_v1",
                       "economic_profitability_reporting_calendar_v1", "fixed_positive_momentum_exit_v1",
                       "economic_qarp_reporting_calendar_v1"}
STOCKDB_FACTOR_SIGNALS = {"value_size_activity_equal_rank_v1", "small_size_common_pool_control"}
SEAT_KIND = "full_source_public_seat_donchian_v1"
SEAT_FINISHED = {"completed_seat_bulk_diagnostic", "partial_seat_bulk_diagnostic"}
PIPELINE_KIND = "mature_month_family_pipeline_v1"
PIPELINE_SIGNAL = "stock_mature_month_family_return"


def _pipeline_record(record):
    protocol = record["protocol"]
    if (record.get("purpose") != PIPELINE_KIND or protocol.get("id") != PIPELINE_KIND
            or record.get("execution_admitted") is not False or protocol.get("execution_admitted") is not False
            or protocol.get("fresh_holdout_claimed") is not False
            or record.get("status") not in {"completed_mature_month_pipeline_ledger", "failed_mature_month_pipeline_ledger"}
            or [row["signal"] for row in record.get("results", [])] != [PIPELINE_SIGNAL]
            or not RUN_ID.fullmatch(record.get("source_run", ""))):
        raise ValueError("Terminal unadmitted fixed family pipeline ledger required")
    source = json.loads((settings.ROOT / "outputs/market_replay" / record["source_run"] / "run.json").read_text(encoding="utf-8"))
    if (source.get("status") != "completed_return_cost_diagnostics"
            or source["protocol"]["portfolio"] != protocol["portfolio"]
            or any(source["protocol"]["stock_inputs"][key] != protocol["stock_inputs"][key]
                   for key in ("price_view", "native_view", "listing_master"))):
        raise ValueError("Pipeline common pool, prices or original portfolio binding changed")
    models = record["models"]
    dates = [row["date"] for row in models]
    if (not dates or dates != sorted(set(dates)) or any(
            not str(row["last_training_exit"])[:10] <= row["training_cutoff"] < row["date"] for row in models)):
        raise ValueError("Pipeline models require prior mature label endpoints")
    limits = ["Previously viewed development periods; definitions/search NOT independently validated",
              "Prior-matured sign/selection/refit only; provider-label MSE/IC are not net portfolio returns",
              "PIT/industry/source risks and uncertainty calibration remain unresolved; NO_TRADE",
              "Rights subscription handling is an explicit replay-only policy when present; it is not a broker receipt"]
    return {**record, **{key: source[key] for key in ("dataset", "scope", "start", "end", "sessions")},
            "research_family": PIPELINE_KIND,
            "protocol": {**protocol, "signal_portfolio_configs": {PIPELINE_SIGNAL: protocol["portfolio"]},
                         "limitations": limits}}


def _fund_record(record):
    protocol = record["protocol"]
    if (protocol.get("id") not in FUND_KINDS
            or record["status"] not in {"completed_fund_trend_diagnostic", "failed_fund_trend_diagnostic"}):
        raise FileNotFoundError("No finished fixed fund comparison")
    dataset = Path(protocol["dataset"]).resolve()
    dataset.relative_to((settings.DATA_ROOT / "datasets/mixed_fund_diagnostic/snapshots").resolve())
    path = dataset / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (sha256(path.read_bytes()).hexdigest() != protocol["dataset_manifest_sha256"]
            or manifest.get("replay_only") is not True or manifest.get("execution_admitted") is not False
            or not set(protocol["symbols"]) <= set(manifest["instrument_profiles"])):
        raise ValueError("Fund dataset differs from its pinned diagnostic snapshot")
    variants = protocol["variants"]
    planned = ["monthly_fixed_sleeves", "monthly_equal_risk" if protocol["id"] == "three_asset_equal_risk_1y_v1"
               else "own_total_return_10m"]
    if protocol["id"] == "three_asset_equal_risk_controls_v1":
        planned = ["monthly_fixed_sleeves", "monthly_equal_risk", "static_2023_equal_risk", "sovereign_bond_sleeve"]
    if (variants != planned
            or [row["signal"] for row in record["results"]] != variants):
        raise ValueError("Fund comparison is missing or changing a planned ledger")
    labels = {"monthly_fixed_sleeves": "Fixed ETF allocation", "own_total_return_10m": "ETF own-return 10-month trend",
              "monthly_equal_risk": "ETF equal-risk contribution (one-year covariance)",
              "static_2023_equal_risk": "ETF frozen2023 equal-risk weights",
              "sovereign_bond_sleeve": "Sovereign-bond ETF80percent/cash20percent control"}
    return {**record, "dataset": str(dataset), "purpose": protocol["id"], "scope": manifest["scope"],
            "symbols": len(protocol["symbols"]), "start": protocol["start"], "end": protocol["end"],
            "sessions": protocol["sessions"], "research_family": "fund_allocation",
            "protocol": {**protocol, "portfolio": protocol["config"], "decision_cadence": "completed_month_end",
                         "selection_is_point_in_time": False,
                         "signal_portfolio_configs": {signal: protocol["config"] for signal in variants}},
            "results": [{**row, "label": labels[row["signal"]]} for row in record["results"]]}


def _baseline_record(identifier, record):
    if (record.get("experiment_kind") not in BASELINE_KINDS
            or record["status"] not in {"completed", "failed_ledger_comparison"}):
        raise FileNotFoundError("No finished baseline comparison")
    dataset = Path(record["config"]["dataset"]).resolve()
    bulk = record.get("bulk_inputs")
    if bulk:
        dataset.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
        if (Path(bulk["price_view"]).resolve() != dataset
                or record["dataset_manifest"]["scope"] != "full_source_sh_sz_research_only"
                or record["dataset_manifest"].get("execution_admitted") is not False):
            raise ValueError("Invalid full-source model research boundary")
    else:
        dataset.relative_to((settings.DATA_ROOT / "datasets").resolve())
    if Path(record["dataset_manifest"]["folder"]).resolve() != dataset:
        raise ValueError("Model dataset does not match its pinned manifest")
    calendar = (Path(bulk["event_view"]).resolve() / "source_input_calendar.csv") if bulk else dataset / "calendar.csv"
    if bulk:
        calendar.relative_to((settings.DATA_ROOT / "research/stock_action_events").resolve())
        if Path(record["calendar_file"]).resolve() != calendar:
            raise ValueError("Full-source model calendar path changed")
    if sha256(calendar.read_bytes()).hexdigest() != record["input_hashes"]["calendar.csv"]:
        raise ValueError("Model calendar differs from its pinned input")
    start, end = record["config"]["segments"]["test"]
    with calendar.open(encoding="utf-8-sig", newline="") as stream:
        dates = [row["date"] for row in csv.DictReader(stream) if start <= row["date"] <= end]
    if not dates or dates != sorted(set(dates)):
        raise ValueError("Invalid baseline evaluation calendar")
    results, folders = [], {}
    for row in record["portfolio_metrics"]:
        if row["variant"] == "B0" and row["seed"] in record["config"]["seeds"]:
            signal = f"alpha158_seed{int(row['seed'])}"
            label = f"Alpha158 + LightGBM (seed {int(row['seed'])})"
            folder = f"B0_seed{int(row['seed'])}/replay"
        elif row["variant"] == "RULE" and row["seed"] is None:
            signal, label, folder = record["portfolio_config"]["signal"], None, "rule_replay"
        else:
            raise ValueError("Unexpected baseline variant/seed")
        if signal in folders:
            raise ValueError("Duplicate baseline ledger identity")
        folders[signal] = folder
        results.append({**row, "signal": signal, "label": label, "status": row.get("status", "completed")})
    expected = {f"alpha158_seed{seed}" for seed in record["config"]["seeds"]} | {record["portfolio_config"]["signal"]}
    if set(folders) != expected:
        raise ValueError("Baseline comparison is missing a planned ledger")
    model_diagnostics = [
        {
            "variant": item.get("variant"),
            "seed": item.get("seed"),
            "split": item.get("split"),
            "features": item.get("features"),
            "best_iteration": item.get("best_iteration"),
            "ic_days": item.get("ic_days"),
            "mean_rank_ic": item.get("mean_rank_ic"),
            "ic_std": item.get("ic_std"),
            "hac_t_lag5": item.get("hac_t_lag5"),
            "research_only": True,
            "execution_admitted": False,
        }
        for item in record.get("model_metrics") or []
        if isinstance(item, dict) and item.get("variant") == "B0"
    ]
    paired_deltas = [
        {
            "seed": item.get("seed"),
            "comparison": item.get("comparison"),
            "comparison_status": item.get("comparison_status"),
            "delta_net_return": item.get("delta_net_return"),
            "delta_max_drawdown": item.get("delta_max_drawdown"),
            "delta_turnover_over_initial": item.get("delta_turnover_over_initial"),
            "research_only": True,
            "execution_admitted": False,
        }
        for item in record.get("paired_deltas") or []
        if isinstance(item, dict)
    ]
    return {"status": record["status"], "dataset": str(dataset), "purpose": record["experiment_kind"],
            "scope": record["dataset_manifest"]["scope"], "symbols": record["data_quality"]["symbols"],
            "start": dates[0], "end": dates[-1], "sessions": len(dates), "artifacts": record["data_output"],
            "protocol": {"portfolio": record["portfolio_config"], "fresh_holdout_claimed": False,
                         "selection_is_point_in_time": record["dataset_manifest"].get("selection_is_point_in_time", False)},
            "results": results, "research_family": "alpha158_lightgbm", "ledger_folders": folders,
            "model": "LightGBM", "features": 158,
            "model_diagnostics": model_diagnostics, "paired_deltas": paired_deltas,
            "research_only": True, "execution_admitted": False}


def _alpha158_turnover_record(record):
    """Normalize the fixed Alpha158 rank-buffer ledger for saved comparisons."""
    if (record.get("purpose") != "alpha158_fixed_rank_buffer_v1"
            or record.get("research_family") != "alpha158_turnover_buffer_ensemble"
            or record.get("status") != "completed"
            or record.get("execution_admitted") is not False
            or not isinstance(record.get("result"), dict)
            or record["result"].get("status") != "completed"):
        raise ValueError("Invalid unadmitted fixed Alpha158 turnover replay")
    inputs = record.get("inputs") or {}
    price_view = Path(inputs["price_view"]).resolve()
    price_view.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    for key, kind in (("event_view", "stock_action_events"),
                      ("reference_view", "stock_action_references")):
        Path(inputs[key]).resolve().relative_to((settings.DATA_ROOT / "research" / kind).resolve())
    Path(inputs["listing_master"]).resolve().relative_to(
        (settings.DATA_ROOT / "research/listing_metadata").resolve())
    Path(inputs["native_view"]).resolve().relative_to(
        (settings.DATA_ROOT / "research/qlib_native_features").resolve())
    artifacts = Path(record["artifacts"]).resolve()
    artifacts.relative_to((settings.DATA_ROOT / "research/market_replay").resolve())
    signal = record.get("signal")
    if not signal or not SIGNAL.fullmatch(signal):
        raise ValueError("Invalid Alpha158 turnover signal")
    if not (artifacts / signal / "replay.json").is_file():
        raise ValueError("Alpha158 turnover ledger folder is missing")
    portfolio = record.get("variant_config")
    if not isinstance(portfolio, dict):
        raise ValueError("Alpha158 turnover portfolio configuration is missing")
    limitations = [
        "Fixed retained-rank buffer replay; no model refit, parameter search or fresh holdout claim",
        "Reuses the saved Alpha158/LightGBM score bundle and source research snapshot",
        "Research-only ledger; execution admission remains disabled",
    ]
    result = {**record["result"], "signal": signal,
              "label": "Alpha158 + LightGBM all-seed rank mean with retained-rank buffer"}
    protocol = {"portfolio": portfolio, "fresh_holdout_claimed": False,
                "selection_is_point_in_time": False, "execution_admitted": False,
                "limitations": limitations, "signal_portfolio_configs": {signal: portfolio}}
    return {**record, "dataset": str(price_view),
            "scope": "full_source_alpha158_lightgbm_research_only",
            "symbols": "fixed Alpha158 source stock pool", "protocol": protocol,
            "ledger_folders": {signal: signal}, "results": [result]}


def _factor_candidate_record(record, *, family_balanced=False):
    """Normalize a fixed candidate replay into the read-only comparison shape."""
    expected_status = "completed_factor_candidate_family_replay" if family_balanced else "completed_factor_candidate_replay"
    expected_family = "factor_candidate_family_balanced_ridge" if family_balanced else "factor_candidate_ridge"
    if (record.get("status") != expected_status
            or record.get("research_family") != expected_family
            or record.get("execution_admitted") is not False
            or record.get("fresh_holdout_claimed") is not False
            or record.get("parameters_searched") is not False
            or not isinstance(record.get("result"), dict)
            or record["result"].get("status") != "completed"):
        raise ValueError("Invalid unadmitted fixed factor candidate replay")
    inputs = record["inputs"] if family_balanced else record["stock_inputs"]
    price_view = Path(inputs["price_view"]).resolve()
    price_view.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    for key, kind in (("event_view", "stock_action_events"), ("reference_view", "stock_action_references")):
        Path(inputs[key]).resolve().relative_to((settings.DATA_ROOT / "research" / kind).resolve())
    Path(inputs["listing_master"]).resolve().relative_to((settings.DATA_ROOT / "research/listing_metadata").resolve())
    Path(inputs["native_view"]).resolve().relative_to((settings.DATA_ROOT / "research/qlib_native_features").resolve())
    signal = record["result"].get("signal")
    if not signal or not SIGNAL.fullmatch(signal):
        raise ValueError("Invalid fixed factor candidate signal")
    artifacts = Path(record["artifacts"]).resolve()
    artifacts.relative_to((settings.DATA_ROOT / "research/factor_candidate_family_replays").resolve()
                          if family_balanced else
                          (settings.DATA_ROOT / "research/factor_candidate_replays").resolve())
    folder = artifacts / signal
    if not (folder / "replay.json").is_file():
        raise ValueError("Factor candidate replay ledger folder is missing")
    result = record["result"]
    protocol = {"portfolio": record["portfolio"], "fresh_holdout_claimed": False,
                "selection_is_point_in_time": False, "execution_admitted": False,
                "limitations": record.get("limitations", [])}
    return {**record, "dataset": str(price_view),
            "purpose": "factor_candidate_family_balanced_ridge_v1" if family_balanced else "factor_candidate_ridge_v1",
            "scope": "full_source_factor_candidate_research_only",
            "symbols": "fixed full-source stock pool",
            "start": record["start"], "end": record["end"], "sessions": result["completed_sessions"],
            "artifacts": str(artifacts), "research_family": expected_family,
            "protocol": {**protocol, "signal_portfolio_configs": {signal: record["portfolio"]}},
            "ledger_folders": {signal: signal},
            "results": [{**result, "signal": signal,
                         "label": "Factor candidate Ridge with family-size scaling" if family_balanced
                         else "Factor candidate Ridge", "status": result["status"]}]}


def _factor_candidate_cadence_record(record):
    """Normalize a fixed-cadence candidate ledger for common-window comparison."""
    if (record.get("status") != "completed_factor_candidate_cadence_replay"
            or record.get("research_family") != "factor_candidate_fixed_cadence"
            or record.get("execution_admitted") is not False
            or record.get("fresh_holdout_claimed") is not False
            or record.get("parameters_searched") is not False
            or record.get("model_refit") is not False
            or record.get("candidate_membership_changed") is not False
            or not isinstance(record.get("result"), dict)
            or record["result"].get("status") != "completed"):
        raise ValueError("Invalid unadmitted fixed factor-cadence replay")
    inputs = record["inputs"]
    price_view = Path(inputs["price_view"]).resolve()
    price_view.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    for key, kind in (("event_view", "stock_action_events"), ("reference_view", "stock_action_references")):
        Path(inputs[key]).resolve().relative_to((settings.DATA_ROOT / "research" / kind).resolve())
    Path(inputs["listing_master"]).resolve().relative_to((settings.DATA_ROOT / "research/listing_metadata").resolve())
    Path(inputs["native_view"]).resolve().relative_to((settings.DATA_ROOT / "research/qlib_native_features").resolve())
    artifacts = Path(record["artifacts"]).resolve()
    artifacts.relative_to((settings.DATA_ROOT / "research/factor_candidate_cadence_replays").resolve())
    result = record["result"]
    signal = result.get("signal")
    if signal != record.get("signal") or not signal or not SIGNAL.fullmatch(signal):
        raise ValueError("Invalid fixed factor-cadence signal")
    folder = artifacts / signal
    if not (folder / "replay.json").is_file():
        raise ValueError("Factor cadence replay ledger folder is missing")
    protocol = {"portfolio": record["portfolio"], "fresh_holdout_claimed": False,
                "selection_is_point_in_time": False, "execution_admitted": False,
                "limitations": record.get("limitations", [])}
    return {**record, "dataset": str(price_view),
            "purpose": "factor_candidate_fixed_cadence_v1",
            "scope": "full_source_factor_candidate_research_only",
            "symbols": "fixed full-source stock pool",
            "start": record["start"], "end": record["end"],
            "sessions": result["completed_sessions"], "artifacts": str(artifacts),
            "research_family": "factor_candidate_fixed_cadence",
            "protocol": {**protocol, "signal_portfolio_configs": {signal: record["portfolio"]}},
            "ledger_folders": {signal: signal},
            "results": [{**result, "signal": signal,
                         "label": "Factor candidate fixed bimonthly cadence",
                         "status": result["status"]}]}


def _factor_candidate_library_record(record):
    """Normalize the three predeclared leave-one-library ledgers for comparison."""
    if (record.get("status") != "completed_factor_candidate_library_ablation"
            or record.get("research_family") != "factor_candidate_library_leave_out"
            or record.get("execution_admitted") is not False
            or record.get("fresh_holdout_claimed") is not False
            or record.get("parameters_searched") is not False
            or record.get("candidate_membership_changed") is not False
            or not isinstance(record.get("results"), dict)):
        raise ValueError("Invalid unadmitted fixed factor-library leave-out replay")
    libraries = list(record.get("libraries") or [])
    if libraries != ["Alpha158", "Alpha360", "Templates"]:
        raise ValueError("Leave-out replay must contain the three predeclared libraries")
    inputs = record.get("inputs") or {}
    price_view = Path(inputs["price_view"]).resolve()
    price_view.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    for key, kind in (("event_view", "stock_action_events"), ("reference_view", "stock_action_references")):
        Path(inputs[key]).resolve().relative_to((settings.DATA_ROOT / "research" / kind).resolve())
    Path(inputs["listing_master"]).resolve().relative_to((settings.DATA_ROOT / "research/listing_metadata").resolve())
    Path(inputs["native_view"]).resolve().relative_to((settings.DATA_ROOT / "research/qlib_native_features").resolve())
    artifacts = Path(record["artifacts"]).resolve()
    artifacts.relative_to((settings.DATA_ROOT / "research/factor_candidate_library_ablations").resolve())
    results, folders = [], {}
    for library in libraries:
        result = record["results"].get(library)
        if not isinstance(result, dict):
            raise ValueError(f"Missing leave-out result for {library}")
        signal = result.get("signal")
        expected = f"stock_factor_candidate_leave_out_{library.lower()}"
        if signal != expected or not SIGNAL.fullmatch(signal):
            raise ValueError(f"Invalid leave-out signal for {library}")
        folder = artifacts / library.lower() / signal
        if not (folder / "replay.json").is_file():
            raise ValueError(f"Leave-out replay ledger folder is missing for {library}")
        folders[signal] = str(folder.relative_to(artifacts))
        results.append({**result, "signal": signal,
                        "label": f"Factor candidate Ridge excluding {library}",
                        "status": result.get("status")})
    sessions = {row.get("completed_sessions") for row in results if row.get("status") == "completed"}
    if len(sessions) > 1:
        raise ValueError("Leave-out completed session counts differ")
    return {**record, "dataset": str(price_view),
            "purpose": "factor_candidate_library_leave_out_v1",
            "scope": "full_source_factor_candidate_research_only",
            "symbols": "fixed full-source stock pool",
            "sessions": next(iter(sessions)) if sessions else 0,
            "protocol": {"portfolio": record["portfolio"], "fresh_holdout_claimed": False,
                         "selection_is_point_in_time": False, "execution_admitted": False,
                         "limitations": record.get("limitations", []),
                         "signal_portfolio_configs": {row["signal"]: record["portfolio"] for row in results}},
            "ledger_folders": folders, "results": results}


def _stock_record(record):
    protocol, inputs = record["protocol"], record["inputs"]
    if (protocol.get("id") != "low_risk_price_volume_ablation_v1"
            or inputs.get("canonical_codes") != record["symbols"]
            or record["scope"] != "full_source_sh_sz_research_only"
            or protocol.get("execution_admitted") is not False
            or [row["signal"] for row in record["results"]] != protocol["signals"]):
        raise ValueError("Invalid saved full-source stock comparison")
    dataset = Path(inputs["price_view"]).resolve()
    dataset.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    return {**record, "dataset": str(dataset),
        "protocol": {**protocol, "signal_portfolio_configs": {
            signal: protocol["portfolio"] for signal in protocol["signals"]}}}


def _mixed_stock_record(record):
    protocol = record["protocol"]
    stocks = protocol["stock_inputs"]
    profiles = protocol["fund_profiles"]
    if (stocks.get("scope") != "full_source_sh_sz_research_only" or stocks.get("replay_only") is not True
            or protocol.get("execution_admitted") is not False
            or set(profiles) != {"511010.SSE", "518880.SSE"}
            or [row["signal"] for row in record["results"]] != protocol["variants"]):
        raise ValueError("Invalid saved mixed stock/fund comparison")
    dataset, funds = Path(stocks["price_view"]).resolve(), Path(protocol["fund_dataset"]).resolve()
    dataset.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    funds.relative_to((settings.DATA_ROOT / "datasets").resolve())
    return {**record, "dataset": str(dataset), "fund_dataset": str(funds),
        "purpose": protocol["id"], "scope": "full_source_stocks_plus_two_defensive_funds_research_only",
        "symbols": stocks["canonical_codes"] + len(profiles),
        **{key: protocol[key] for key in ("start", "end", "sessions")},
        "research_family": "stock_defensive_allocation",
        "protocol": {**protocol, "portfolio": protocol["config"], "signal_portfolio_configs": {
            signal: protocol["config"] for signal in protocol["variants"]}},
        "results": [{**row, "label": "Stock40 / bond20 / gold20: " + row["signal"]} for row in record["results"]]}


def _value_stock_record(record, *, profitability=False):
    protocol = record["protocol"]
    manifest, inputs = protocol["stock_manifest"], protocol["stock_inputs"]
    family = record["research_family"] if profitability else "economic_value_monthly_fixed_v1"
    if (protocol.get("id") != family
            or manifest.get("scope") != "full_source_sh_sz_research_only"
            or manifest.get("replay_only") is not True
            or manifest.get("execution_admitted") is not False or protocol.get("execution_admitted") is not False
            or record["symbols"] != manifest["canonical_codes"] - len(manifest["unsupported_instruments"])
            or [row["signal"] for row in record["results"]] != protocol["signals"]
            or set(protocol["signal_portfolio_configs"]) != set(protocol["signals"])):
        raise ValueError("Invalid saved matched valuation stock comparison")
    dataset = Path(inputs["price_view"]).resolve()
    dataset.relative_to((settings.DATA_ROOT / "research/bulk_adjusted_prices").resolve())
    if dataset != Path(manifest["price_view"]).resolve():
        raise ValueError("Valuation price input differs from its saved stock manifest")
    return {**record, "dataset": str(dataset), "purpose": protocol["id"],
            "protocol": {**protocol, "decision_cadence": "completed_month_end"},
            "scope": ("available_financial_same_period_subset_profitability_research_only"
                      if family in {"economic_profitability_same_period_v1", "economic_profitability_reporting_calendar_v1",
                                    "fixed_positive_momentum_exit_v1", "economic_qarp_reporting_calendar_v1"} else
                      "available_financial_subset_profitability_research_only" if profitability
                      else "full_source_sh_sz_matched_valuation_pool_research_only")}


def _stockdb_factor_record(record):
    plan, manifest = record["plan"], record["stock_manifest"]
    cases, inputs = plan["cases"], plan["stock_inputs"]
    if (record["status"] not in {"completed_factor_ledger_diagnostics", "blocked_factor_ledger_diagnostics"}
            or not cases or len(set(cases)) != len(cases) or not set(cases) <= STOCKDB_FACTOR_SIGNALS
            or [row["signal"] for row in record["results"]] != cases
            or record.get("execution_admitted") is not False or plan.get("execution_admitted") is not False
            or manifest.get("scope") != "full_source_sh_sz_research_only"
            or manifest.get("replay_only") is not True or manifest.get("execution_admitted") is not False
            or plan.get("cost_profile") != "dated_stamp_tax_2023_v1"
            or plan.get("fresh_holdout_claimed") is not False):
        raise ValueError("Invalid saved fixed2023 factor ledger diagnostic")
    for key, kind in (("price_view", "bulk_adjusted_prices"), ("event_view", "stock_action_events"),
                      ("reference_view", "stock_action_references")):
        path = Path(inputs[key]).resolve()
        path.relative_to((settings.DATA_ROOT / "research" / kind).resolve())
        if path != Path(manifest[key]).resolve():
            raise ValueError("Fixed factor inputs differ from their saved stock manifest")
    with (Path(inputs["event_view"]) / "source_input_calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        dates = [row["date"] for row in csv.DictReader(stream) if plan["start"] <= row["date"] <= plan["end"]]
    if not dates or dates != sorted(set(dates)) or len(dates) != record["sessions"]:
        raise ValueError("Fixed factor calendar differs from its saved sessions")
    labels = {"value_size_activity_equal_rank_v1": "Value / size / activity equal-rank blend (2023)",
              "small_size_common_pool_control": "Small-size control on the same four-input pool (2023)"}
    return {**record, "dataset": inputs["price_view"], "purpose": "stockdb_fixed_factors_2023_v1",
            "scope": "saved_four_input_common_pool_retrospective_research_only",
            "symbols": manifest["canonical_codes"] - len(manifest["unsupported_instruments"]),
            "start": dates[0], "end": dates[-1], "research_family": "stockdb_fixed_factors_2023_v1",
            "protocol": {**plan, "stock_manifest": manifest, "signals": cases,
                         "decision_cadence": "completed_month_end", "selection_is_point_in_time": False,
                         "signal_portfolio_configs": {signal: plan["portfolio"] for signal in cases}},
            "results": [{**row, "label": labels[row["signal"]]} for row in record["results"]]}


def _seat_stock_record(record):
    protocol, manifest = record["protocol"], record["stock_manifest"]
    modes = ["TREND_ANY_LHB", "TREND_H1", "TREND_H4", "TREND_H1_H4"]
    cases = [{"signal": "TREND", "mode": "TREND", "lag": 0}] + [
        {"signal": f"lag{lag}_{mode}", "mode": mode, "lag": lag}
        for lag in (1, 2) for mode in modes]
    if (record["status"] not in SEAT_FINISHED or protocol.get("id") != SEAT_KIND
            or record.get("execution_admitted") is not False or protocol.get("execution_admitted") is not False
            or manifest.get("scope") != "full_source_sh_sz_research_only"
            or manifest.get("replay_only") is not True or manifest.get("execution_admitted") is not False
            or protocol["cases"] != cases
            or [{key: row[key] for key in ("signal", "mode", "lag")} for row in record["results"]] != cases
            or protocol["evaluation"] != [record["start"], record["end"]]):
        raise ValueError("Invalid saved fixed public-seat comparison")
    inputs = protocol["stock_inputs"]
    for key, kind in (("price_view", "bulk_adjusted_prices"), ("event_view", "stock_action_events"),
                      ("reference_view", "stock_action_references")):
        path = Path(inputs[key]).resolve()
        path.relative_to((settings.DATA_ROOT / "research" / kind).resolve())
        if path != Path(manifest[key]).resolve():
            raise ValueError("Seat price/action inputs differ from their saved stock manifest")
    with (Path(inputs["event_view"]) / "source_input_calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        dates = [row["date"] for row in csv.DictReader(stream) if record["start"] <= row["date"] <= record["end"]]
    if not dates or dates != sorted(set(dates)) or len(dates) != record["sessions"]:
        raise ValueError("Seat evaluation calendar differs from its saved sessions")
    folders = {row["signal"].lower(): row["signal"] for row in cases}
    signals = list(folders)
    return {**record, "dataset": inputs["price_view"], "purpose": SEAT_KIND,
            "scope": "full_source_public_seat_availability_assumptions_research_only",
            "symbols": manifest["canonical_codes"] - len(manifest["unsupported_instruments"]),
            "ledger_folders": folders,
            "protocol": {**protocol, "stock_manifest": manifest, "signals": signals, "decision_cadence": "daily_close",
                         "fresh_holdout_claimed": False, "selection_is_point_in_time": False,
                         "limitations": protocol["limits"],
                         "signal_portfolio_configs": {signal: protocol["portfolio"] for signal in signals}},
            "results": [{**row, "source_signal": row["signal"], "signal": row["signal"].lower(),
                         "label": row["mode"] + f" (publication lag {row['lag']} sessions)"}
                        for row in record["results"]]}


def _read(identifier):
    if not RUN_ID.fullmatch(identifier):
        raise FileNotFoundError("Unknown research replay")
    candidates = [settings.ROOT / "outputs" / kind / identifier
                  for kind in ("market_replay", "factor_mining", "factor_candidate_replays",
                               "factor_candidate_family_replays", "factor_candidate_library_ablations",
                               "factor_candidate_cadence_replays", "stockdb_factor_replay",
                               "seat_bulk_replay", "family_pipeline")]
    folders = [folder for folder in candidates if (folder / "run.json").is_file()]
    if len(folders) != 1:
        raise FileNotFoundError("Unknown or ambiguous research replay")
    folder = folders[0].resolve()
    kind = folders[0].parent.name
    folder.relative_to((settings.ROOT / "outputs" / kind).resolve())
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    artifact_kind = kind
    if kind == "market_replay" and record.get("research_family") == "alpha158_turnover_buffer_ensemble":
        record = _alpha158_turnover_record(record)
    elif kind == "family_pipeline":
        record = _pipeline_record(record)
    elif kind == "factor_candidate_replays":
        record = _factor_candidate_record(record)
    elif kind == "factor_candidate_family_replays":
        record = _factor_candidate_record(record, family_balanced=True)
    elif kind == "factor_candidate_cadence_replays":
        record = _factor_candidate_cadence_record(record)
    elif kind == "factor_candidate_library_ablations":
        record = _factor_candidate_library_record(record)
    elif kind == "seat_bulk_replay":
        record = _seat_stock_record(record)
    elif kind == "stockdb_factor_replay":
        record = _stockdb_factor_record(record)
    elif kind == "factor_mining":
        record = _baseline_record(identifier, record)
    elif record.get("protocol", {}).get("id") in FUND_KINDS:
        record = _fund_record(record)
        artifact_kind = "fund_trend"
    elif record.get("research_family") == "low_risk_stock_selection":
        record = _stock_record(record)
    elif record.get("protocol", {}).get("id") == "stock_defensive_fixed_40_20_20_v1":
        record = _mixed_stock_record(record)
    elif record.get("research_family") == "economic_value_monthly_fixed_v1":
        record = _value_stock_record(record)
    elif record.get("research_family") in PROFITABILITY_KINDS:
        record = _value_stock_record(record, profitability=True)
    elif record.get("research_family") == "return_cost_selection" and (
            "scope" not in record or "symbols" not in record):
        # The first terminal preparation receipt predates these catalog fields.
        # Read its source metadata without rewriting that preserved failure.
        source = json.loads(Path(record["protocol"]["source_run"]).read_text(encoding="utf-8"))
        record = {**record, "scope": record.get("scope", source["scope"]),
                  "symbols": record.get("symbols", source["symbols"])}
    artifacts = (settings.DATA_ROOT / "research" / artifact_kind / identifier).resolve()
    if Path(record["artifacts"]).resolve() != artifacts:
        raise ValueError("Replay artifacts do not match the configured research root")
    return record, artifacts


def _summary(identifier, record):
    summary = {key: record[key] for key in (
        "status", "dataset", "purpose", "scope", "symbols", "start", "end", "sessions", "protocol", "results"
    )} | {"id": identifier, "research_family": record.get("research_family", "rules"),
          "strategy_validated": False, "live_ready": False}
    for key in ("model", "features", "model_diagnostics", "paired_deltas", "research_only", "execution_admitted"):
        if key in record:
            summary[key] = record[key]
    return summary


def catalog():
    root = settings.ROOT / "outputs"
    paths = []
    for path in (root / "market_replay").glob("*/run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("protocol", {}).get("id") in FUND_KINDS and record.get("status") == "running":
            continue
        if record.get("research_family") in PROFITABILITY_KINDS and (
                record["status"] == "running" or len(record.get("results", [])) != len(record["protocol"]["signals"])):
            continue
        if not record.get("results") and (record.get("research_family") == "low_risk_stock_selection"
                or record.get("protocol", {}).get("id") == "stock_defensive_fixed_40_20_20_v1"
                or record.get("research_family") in PROFITABILITY_KINDS):
            # Source-target plans/preparation failures have no ledger to display.
            continue
        paths.append(path)
    for path in (root / "factor_mining").glob("*/run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("experiment_kind") in BASELINE_KINDS
                and record.get("status") in {"completed", "failed_ledger_comparison"}):
            paths.append(path)
    for kind, status in (("factor_candidate_replays", "completed_factor_candidate_replay"),
                         ("factor_candidate_family_replays", "completed_factor_candidate_family_replay"),
                         ("factor_candidate_cadence_replays", "completed_factor_candidate_cadence_replay"),
                         ("factor_candidate_library_ablations", "completed_factor_candidate_library_ablation")):
        for path in (root / kind).glob("*/run.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") == status and record.get("execution_admitted") is False:
                paths.append(path)
    for path in (root / "stockdb_factor_replay").glob("*/run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("status") in {"completed_factor_ledger_diagnostics", "blocked_factor_ledger_diagnostics"}
                and record.get("results")):
            paths.append(path)
    for path in (root / "seat_bulk_replay").glob("*/run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") in SEAT_FINISHED and len(record.get("results", [])) == 9:
            paths.append(path)
    for path in (root / "family_pipeline").glob("*/run.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") in {"completed_mature_month_pipeline_ledger", "failed_mature_month_pipeline_ledger"} and record.get("results"):
            paths.append(path)
    summaries = []
    for path in sorted(paths, key=lambda path: path.parent.name, reverse=True):
        if not RUN_ID.fullmatch(path.parent.name):
            continue
        try:
            summaries.append(_summary(path.parent.name, _read(path.parent.name)[0]))
        except (OSError, KeyError, TypeError):
            # Missing or legacy-shaped receipts may remain on disk without
            # preventing the usable research catalog from loading.  A
            # ValueError is different: it means a pinned receipt, artifact
            # root, planned ledger set, or research boundary was changed and
            # must reach the caller instead of being silently hidden.
            continue
    return summaries


def detail(identifier, signal, *, include_marks=False):
    record, artifacts = _read(identifier)
    if not SIGNAL.fullmatch(signal):
        raise FileNotFoundError("Unknown research signal")
    result = next((row for row in record["results"] if row["signal"] == signal), None)
    if result is None:
        raise FileNotFoundError("No saved result for this signal")
    folder = (artifacts / record.get("ledger_folders", {}).get(signal, signal)).resolve()
    folder.relative_to(artifacts)
    if record.get("research_family") == SEAT_KIND and result["status"] == "failed_preparation":
        return {"id": identifier, "signal": signal, "status": result["status"],
                "config": record["protocol"]["portfolio"], "receipt": None, "metrics": None,
                "source_limitations": record["protocol"]["limitations"], "segments": [], "daily": [],
                "error": result["error"], "strategy_validated": False, "live_ready": False}
    receipt = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
    with (folder / "daily.csv").open(encoding="utf-8-sig", newline="") as stream:
        daily = list(csv.DictReader(stream))
    for row in daily:
        for key in ("equity", "cash", "fees", "turnover_value"):
            row[key] = float(row[key])
        row["stale_symbols"] = ast.literal_eval(row["stale_symbols"])
        if not isinstance(row["stale_symbols"], list):
            raise ValueError("Invalid stale-valuation symbols")
    dates = [row["date"] for row in daily]
    if (dates != sorted(set(dates)) or any(not record["start"] <= day <= record["end"] for day in dates)
            or len(daily) != receipt["completed_sessions"]
            or (dates and dates[-1] != receipt["last_completed_session"])):
        raise ValueError("Daily marks do not reconcile with the replay receipt")
    completed = result["status"] == receipt["status"] == "completed"
    if result["status"] == "completed" and (
            not completed or len(daily) != record["sessions"] or not dates
            or dates[0] != record["start"] or dates[-1] != record["end"]):
        raise ValueError("Completed result has incomplete daily marks")
    config = {**record["protocol"]["portfolio"]}
    override = record["protocol"].get("signal_portfolio_configs", {}).get(signal)
    if override is not None:
        config.update(override)
    elif record.get("research_family") != "alpha158_lightgbm":
        config["signal"] = signal
    if signal == "donchian_trend":
        config.update(record["protocol"].get("donchian_portfolio_override") or {})
    initial = config["initial_cash"]
    full_metrics = metrics(daily, initial) if completed else None
    if full_metrics:
        for key, value in full_metrics.items():
            # Older reports predate these metrics; derive them from saved marks.
            if key not in result and key in {"sharpe_zero_rf_252", "annualized_volatility", "worst_day"}:
                continue
            saved = result.get(key)
            if (saved is None) != (value is None) or (
                    value is not None and abs(saved - value) > 1e-8 * max(1, abs(value))):
                raise ValueError(f"Saved result differs from ledger daily marks: {key}")
    segments = []
    for year in sorted({day[:4] for day in dates}):
        selected = [row for row in daily if row["date"].startswith(year)]
        first = daily.index(selected[0])
        beginning = daily[first - 1]["equity"] if first else initial
        segments.append({"year": year, **metrics(selected, beginning)})
    value = {"id": identifier, "signal": signal, "status": result["status"],
            "config": config, "receipt": receipt, "metrics": full_metrics,
            "source_limitations": record["protocol"].get("limitations", []),
            "segments": segments if completed else [],
            "daily": [{key: row[key] for key in ("date", "equity")} for row in daily],
            "error": result.get("error"), "strategy_validated": False, "live_ready": False}
    if record["protocol"].get("fractional_allocation"):
        value["fractional_allocation_assumption"] = record["protocol"]["fractional_allocation"]
    if record["protocol"].get("entry_risk_policy"):
        if receipt.get("entry_risk_policy") != record["protocol"]["entry_risk_policy"]:
            raise ValueError("Entry-risk policy differs between protocol and replay")
        value["entry_risk_policy"] = record["protocol"]["entry_risk_policy"]
        value["entry_risk_blocks"] = result.get("entry_risk_blocks")
        value["evaluation_basis"] = record["protocol"]["basis"]
    if record["protocol"].get("target_policy_contracts"):
        binding = record["protocol"]["target_policy_contracts"][signal]
        if receipt.get("target_policy_contract") != binding or receipt.get("target_policy_enabled") is not True:
            raise ValueError("Target policy differs between protocol and replay")
        value["target_policy_contract"] = binding
    if record.get("research_family") == "return_cost_selection":
        binding = record["protocol"]["selection_policies"][signal]
        if receipt.get("portfolio_policy_contract") != binding or receipt.get("portfolio_policy_enabled") is not True:
            raise ValueError("Return-selection policy differs between protocol and replay")
        value["selection_policy_contract"] = binding
        value["selection_summary"] = result.get("selection")
        path = folder / "selection_decisions.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            decisions = list(csv.DictReader(stream))
        days = [row["date"] for row in decisions]
        if (days != sorted(set(days)) or any(day not in dates for day in days)
                or len(decisions) != result["selection"]["decision_count"]):
            raise ValueError("Selection decisions differ from the saved successful prefix")
        value["selection_decisions"] = decisions
        value["forecast_model"] = record["model"]
        value["evaluation_basis"] = "saved prefix only; expected benefit/cost are estimates, not fills or full-run performance"
        value["source_limitations"] = ["Seen development data; not independent/PIT or execution-admitted",
                                       "No covariance/industry neutrality or black-swan protection",
                                       "V1 zero-quantity nominal-benefit defect retained; not cost-improvement evidence"]
    if record.get("research_family") == PIPELINE_KIND:
        binding = record["protocol"]["portfolio_policy"]
        if receipt.get("portfolio_policy_contract") != binding or receipt.get("portfolio_policy_enabled") is not True:
            raise ValueError("Pipeline return policy differs between protocol and replay")
        value["selection_policy_contract"] = binding
        value["selection_summary"] = result["selection"]
        value["pipeline_diagnostics"] = record["diagnostics"]
        value["pipeline_models"] = record["models"]
        value["rights_replay_policy"] = record["protocol"].get("rights_replay_policy")
        value["evaluation_basis"] = "Saved chronological refits; partial prefixes have no full-run metrics; NOT fresh independent validation"
    if include_marks:
        value["daily_marks"] = daily
    return value
