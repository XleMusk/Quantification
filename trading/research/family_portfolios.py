"""Fixed training-oriented family recipes vs matched controls, through the sole ledger."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import time
import uuid

import numpy as np
import pandas as pd

from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..feed import HistoricalFeed
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import native_binding
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .factors import catalog
from .low_risk import LowRiskFeed, VARIANTS, features as low_risk_features, portfolio, rank as low_risk_rank
from .monthly_timing import month_ends
from .market_replay import validate_prefix_inputs
from .stock_defensive import (DEFENSIVE, MixedResearchFeed, StockDefensiveFeed, mixed_config,
                              stage_metrics)


RECIPES = ("low_vol_control", "low_vol_half_momentum_control", "risk_volume_stability",
           "risk_trend_quality", "equal_three_families")
NATIVE_COLUMNS = ("RSQR30", "WVMA20")
HORIZON = 20
START, END = "2025-01-02", "2026-09-30"


def training_directions(summary):
    training = summary.loc[summary.segment.eq("train") & summary.horizon.eq(HORIZON)]
    directions = {}
    for name in NATIVE_COLUMNS:
        rows = training.loc[training.factor.eq("Alpha158/" + name)]
        if (len(rows) != 1 or rows.iloc[0].ic_days < 80
                or rows.iloc[0].train_direction not in {"high", "low"}
                or not np.isfinite(rows.iloc[0].mean_rank_ic)):
            raise ValueError("Fixed family recipe requires an observed training-only direction")
        row = rows.iloc[0]
        if row.mean_rank_ic == 0 or (row.mean_rank_ic > 0) != (row.train_direction == "high"):
            raise ValueError("Training direction does not match its observed IC")
        directions[name] = {"sign": 1 if row.train_direction == "high" else -1,
                            "direction": row.train_direction, "horizon": HORIZON,
                            "training_ic_days": int(row.ic_days), "training_mean_ic": float(row.mean_rank_ic)}
    return directions


def bind_stock_inputs(inputs, event_view=None, reference_view=None):
    if bool(event_view) != bool(reference_view):
        raise ValueError("Event/reference overrides must be supplied together")
    bound = dict(inputs)
    if event_view:
        bound.update(event_view=str(Path(event_view).resolve()),
                     reference_view=str(Path(reference_view).resolve()))
    return bound


def validate_family_resume(previous, current):
    if (previous.get("status") not in {"failed_family_portfolio_diagnostics", "completed_family_portfolio_diagnostics"}
            or previous.get("research_family") != "fixed_family_incremental"
            or any(previous.get(key) != current.get(key) for key in (
                "purpose", "start", "end", "sessions", "symbols", "scope", "library_run"))):
        raise ValueError("Resume requires the same terminal family experiment and calendar")
    for key in ("id", "recipes", "signals", "signal_portfolio_configs", "training_directions", "library_stock_inputs",
                "fund_dataset", "fund_profiles", "native_columns", "sign_calibration_horizon", "family_weights",
                "trend_within_family", "initial_cash", "portfolio"):
        if previous["protocol"].get(key) != current["protocol"].get(key):
            raise ValueError(f"Family continuation contract changed: {key}")
    if ({row["signal"] for row in previous["results"]} != set(current["protocol"]["signals"])
            or len(previous["results"]) != len(current["protocol"]["signals"])
            or "stock_manifest" not in previous["protocol"]):
        raise ValueError("All terminal case records and their original stock manifest required")


def save_diagnostics(folder, name, rows, checkpoint=None):
    fresh = pd.DataFrame(rows)
    if checkpoint:
        source = Path(checkpoint["source"]) / name
        if source.exists() and source.stat().st_size > 1:
            prior = pd.read_csv(source, dtype={"date": str, "symbol": str})
            prior = prior.loc[prior.date.le(checkpoint["state"]["day"])]
            fresh = pd.concat([prior, fresh], ignore_index=True)
    fresh.to_csv(folder / name, index=False)


def action_needs(run_id):
    """Batch data demand from all fixed historical rank buffers, never filter ranks by action quality."""
    source = ROOT / "outputs/market_replay" / run_id / "run.json"
    record = json.loads(source.read_text(encoding="utf-8"))
    if record.get("research_family") != "fixed_family_incremental" or record["status"] == "running":
        raise ValueError("Terminal fixed-family research source required")
    protocol = record["protocol"]
    inputs = protocol["stock_inputs"]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    destination = DATA_ROOT / "research/family_action_needs" / identifier
    destination.mkdir(parents=True, exist_ok=False)
    stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
    try:
        values = low_risk_features(stocks, record["end"])
        dates = [day for day in month_ends(stocks.calendar, record["end"]) if day >= record["start"]]
        native = read_native_monthends(stocks, inputs["native_view"], dates)
        helper = LowRiskFeed(stocks, values, VARIANTS[1], record["end"], diagnostic_extension=True)
        rows = []
        for day in dates:
            pool = helper.pool(day, portfolio())
            if isinstance(pool, SignalUnavailable):
                continue
            shared = common_pool(pool, native.loc[pd.Timestamp(day)])
            for recipe in RECIPES:
                rows.extend({"date": day, "recipe": recipe, "rank": rank, **row}
                            for rank, row in enumerate(ranked_recipe(shared, recipe, protocol["training_directions"])[:30], 1))
        rankings = pd.DataFrame(rows)
        rankings.to_csv(destination / "fixed_rank_buffers.csv", index=False)
        symbols = set(rankings.symbol)
        actions = pd.read_parquet(Path(inputs["event_view"]) / "canonical_actions.parquet").fillna("")
        pending = actions.loc[actions.symbol.isin(symbols) & actions.dividOperateDate.between(record["start"], record["end"])
                              & actions.dividReserveToStockPs.eq("")].copy()
        pending.to_csv(destination / "pending_reserve_events.csv", index=False)
        demand = {"status": "completed_fixed_family_data_demand", "source_run": str(source),
                  "artifact_folder": str(destination), "stock_inputs": inputs, "training_directions": protocol["training_directions"],
                  "recipes": list(RECIPES), "decision_dates": dates, "rank_buffer": 30,
                  "candidate_symbols": len(symbols), "pending_events": len(pending),
                  "pending_symbols": sorted(set(pending.symbol)), "execution_admitted": False,
                  "basis": "All fixed month-end rank buffers; no event-quality universe filtering or account result",
                  "limitations": ["Data-demand superset includes candidates not actually held at an event",
                                  "Future historical ranks used only for archived data preparation, never prior signals or selection",
                                  "Unknown reserve is not necessarily a zero; source matching can fail"]}
        write_json(destination / "result.json", demand)
        print(json.dumps(demand), flush=True)
        return destination, demand
    finally:
        stocks.close()


def prepare_action_terms(demand_folder, primary_view, archive_roots, max_fresh=64, *, source_run=None):
    from .action_events import amend_secondary
    from .action_terms import collect
    if not 0 <= max_fresh <= 64:
        raise ValueError("At most64 fresh public-source securities per preparation")
    demand_folder = Path(demand_folder)
    demand = json.loads((demand_folder / "result.json").read_text(encoding="utf-8"))
    if demand["status"] != "completed_fixed_family_data_demand" or demand["execution_admitted"]:
        raise ValueError("Terminal unadmitted fixed-rank data demand required")
    inputs = demand["stock_inputs"]
    source_path = ROOT / "outputs/market_replay" / source_run / "run.json" if source_run else Path(demand["source_run"])
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source_run:
        if (source.get("research_family") != "fixed_family_incremental" or source["status"] == "running"
                or source["start"] != START or source["end"] != END
                or source["protocol"]["recipes"] != demand["recipes"]
                or source["protocol"]["training_directions"] != demand["training_directions"]
                or any(source["protocol"]["stock_inputs"][key] != inputs[key]
                       for key in ("price_view", "native_view", "listing_master"))):
            raise ValueError("Updated action demand requires the same terminal fixed strategy/features/prices")
        inputs = source["protocol"]["stock_inputs"]
    primary = json.loads((Path(primary_view) / "result.json").read_text(encoding="utf-8"))
    effective = json.loads((Path(inputs["event_view"]) / "result.json").read_text(encoding="utf-8"))
    if Path(primary["raw_view"]).resolve() != Path(effective["raw_view"]).resolve():
        raise ValueError("Prepared terms must retain the original raw scope")
    pending = pd.read_csv(demand_folder / "pending_reserve_events.csv", dtype=str, keep_default_na=False)
    keys = set(zip(pending.symbol, pending.dividOperateDate))
    current = pd.read_parquet(Path(inputs["event_view"]) / "canonical_actions.parquet").fillna("")
    current = current.loc[current.dividReserveToStockPs.eq("")
                          & pd.Series([(symbol, date) in keys for symbol, date in zip(current.symbol, current.dividOperateDate)],
                                      index=current.index)]
    keys = set(zip(current.symbol, current.dividOperateDate))
    remaining, batches = set(current.symbol), []
    for root in archive_roots:
        archived = json.loads((Path(root) / "result.json").read_text(encoding="utf-8"))
        available = {row["symbol"] for row in archived["records"] if row["status"] == "source_histories_compared"}
        chosen = sorted(remaining & available)
        if chosen:
            batches.append((chosen, root))
            remaining.difference_update(chosen)
    failed_symbols = {match.group(1) for row in source["results"] if row["status"] == "failed"
                      for match in re.finditer(r"([0-9]{6}\.(?:SSE|SZSE))", row.get("error", ""))}
    counts = pd.read_csv(demand_folder / "fixed_rank_buffers.csv", usecols=["symbol"]).symbol.value_counts()
    chosen = sorted(remaining, key=lambda symbol: (symbol not in failed_symbols, -int(counts.get(symbol, 0)), symbol))[:max_fresh]
    if chosen:
        batches.append((chosen, None))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    destination = DATA_ROOT / "research/family_action_preparation" / identifier
    destination.mkdir(parents=True, exist_ok=False)
    event_view, reference_view = inputs["event_view"], inputs["reference_view"]
    result = {"status": "running_fixed_family_action_preparation", "demand_folder": str(demand_folder),
              "original_stock_inputs": inputs, "demand_stock_inputs": demand["stock_inputs"],
              "source_run": str(source_path), "primary_view": str(primary_view), "steps": [],
              "fresh_symbols": chosen, "matched_event_keys": [], "execution_admitted": False,
              "fresh_query_priority": "Actual failed securities, then fixed rank-buffer frequency, then canonical identity",
              "limitations": ["Explicit opt-in matched secondary share terms, NOT issuer/PIT/admission confirmation",
                              "Unmatched or not acquired events remain unknown; ranks/weights/costs never change"]}
    write_json(destination / "result.json", result)
    try:
        for symbols, archived_root in batches:
            folder, terms = collect(primary_view, symbols, reuse_root=archived_root, retain_archived=False)
            matched = sorted({(candidate["symbol"], candidate["date"]) for row in terms["records"]
                              for candidate in row.get("candidates", [])
                              if (candidate["symbol"], candidate["date"]) in keys})
            if matched:
                event_view, reference_view, _, _ = amend_secondary(event_view, reference_view, folder, matched)
                keys.difference_update(matched)
                result["matched_event_keys"].extend(matched)
            result["steps"].append({"source_root": str(folder), "symbols": len(symbols),
                                    "reused_symbols": terms["reused_symbols"], "source_errors": terms["source_errors"],
                                    "new_matches": len(matched)})
            result.update(event_view=str(event_view), reference_view=str(reference_view), pending_events=len(keys))
            write_json(destination / "result.json", result)
        result["status"] = "completed_fixed_family_action_preparation"
    except Exception as error:
        result.update(status="failed_fixed_family_action_preparation", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result.update(event_view=str(event_view), reference_view=str(reference_view), pending_events=len(keys))
        write_json(destination / "result.json", result)
        print(str(destination), flush=True)
    return destination, result


def read_native_monthends(stocks, native_view, dates):
    definitions = catalog()
    native_view, receipt, _ = native_binding(stocks, native_view, definitions)
    calendar = pd.to_datetime(stocks.calendar)
    parts = []
    for item in receipt["partitions"]:
        path = (native_view / item["file"]).resolve()
        path.relative_to(native_view)
        frame = pd.read_parquet(path, columns=list(NATIVE_COLUMNS))
        expected = pd.MultiIndex.from_product([calendar, item["symbols"]], names=["date", "symbol"]).sort_values()
        if not frame.index.equals(expected) or len(frame) != item["rows"]:
            raise ValueError("Native feature partition date/symbol identities changed")
        parts.append(frame.loc[frame.index.get_level_values("date").isin(pd.to_datetime(dates))])
    result = pd.concat(parts).sort_index()
    if result.index.has_duplicates or np.isinf(result.to_numpy()).any():
        raise ValueError("Distinct finite-or-missing native monthly rows required")
    return result


def common_pool(pool, native):
    """Current feature availability is shared by ALL cases, never future labels or action coverage."""
    result = []
    for row in pool:
        if row["symbol"] not in native.index:
            raise ValueError("Native calendar grid omitted a canonical candidate")
        extra = native.loc[row["symbol"], list(NATIVE_COLUMNS)]
        if np.isfinite(extra.to_numpy()).all():
            result.append({**row, **extra.to_dict()})
    return result


def ranked_recipe(pool, recipe, directions):
    if recipe not in RECIPES:
        raise ValueError("Unknown fixed family recipe")
    if recipe in RECIPES[:2]:
        return low_risk_rank(pool, VARIANTS[1] if recipe == RECIPES[0] else VARIANTS[2])
    if not pool:
        return []
    frame = pd.DataFrame(pool).set_index("symbol").sort_index()
    risk = (-frame.volatility).rank(pct=True)
    strength = frame.momentum.rank(pct=True)
    quality = (frame.RSQR30 * directions["RSQR30"]["sign"]).rank(pct=True)
    volume_stability = (frame.WVMA20 * directions["WVMA20"]["sign"]).rank(pct=True)
    trend = (strength + quality) / 2
    if recipe == "risk_volume_stability":
        score = (risk + volume_stability) / 2
    elif recipe == "risk_trend_quality":
        score = (risk + trend) / 2
    else:
        score = (risk + trend + volume_stability) / 3
    frame["score"] = score
    frame["risk_rank"], frame["trend_rank"], frame["volume_stability_rank"] = risk, trend, volume_stability
    return sorted(frame.reset_index().to_dict("records"), key=lambda row: (-row["score"], row["symbol"]))


class FamilyFeed:
    def __init__(self, base, values, native, recipe, directions, end):
        self.base, self.native, self.recipe, self.directions = base, native, recipe, directions
        stocks = getattr(base, "stocks", base)
        self.helper = LowRiskFeed(stocks, values, VARIANTS[1], end, diagnostic_extension=True)
        self.ends = set(month_ends(stocks.calendar, end))
        self.audit, self.ranking_rows = [], []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != portfolio(config.initial_cash):
            raise ValueError("Fixed family signal is research replay-only with its declared stock config")
        if not START <= day <= self.helper.end:
            raise ValueError("Training directions cannot be traded in the training period")
        if day not in self.ends:
            return SignalUnavailable("not_month_end")
        pool = self.helper.pool(day, config)
        if isinstance(pool, SignalUnavailable):
            return pool
        date = pd.Timestamp(day)
        if date not in self.native.index.get_level_values("date"):
            raise ValueError("Missing native month-end batch")
        shared = common_pool(pool, self.native.loc[date])
        candidates = ranked_recipe(shared, self.recipe, self.directions)
        self.audit.append({"date": day, "recipe": self.recipe, "original_pool": len(pool),
                           "common_pool": len(shared), "feature_missing": len(pool) - len(shared)})
        self.ranking_rows.extend({"date": day, "rank": i, **row}
                                 for i, row in enumerate(candidates[:config.retain_rank], start=1))
        return SignalFrame.from_legacy(candidates, day, "research:fixed_family:" + self.recipe, context)


def report(out, record):
    lines = ["# Fixed Family Portfolios: Cost-Inclusive Ledger", "",
             f"{record['start']}..{record['end']}, {record['sessions']} sessions; training direction from2024 only. NO_TRADE.",
             "All cases start with140000CNY cash and no prior positions; same dates, common stock pool, sources and cost settings.",
             "These are new matched post-training accounts, NOT reruns or copied prefixes of the old2024-start baselines.", "",
             "| Allocation | Recipe | Status | Sessions | Net return | MDD | Sharpe | Fees CNY | Stale days |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    if record.get("error"):
        lines.insert(5, "Preparation/continuation error: " + record["error"])
    for case in record["results"]:
        if case["status"] == "completed":
            sharpe = case["sharpe_zero_rf_252"]
            lines.append(f"| {case['allocation']} | {case['recipe']} | completed | {case['completed_sessions']} | "
                         f"{case['net_return']:.2%} | {abs(case['max_drawdown']):.2%} | "
                         f"{sharpe if sharpe is None else round(sharpe, 4)} | {case['fees']:.2f} | {case['stale_valuation_days']} |")
        else:
            lines.append(f"| {case['allocation']} | {case['recipe']} | FAILED | {case['completed_sessions']} | unknown | unknown | unknown | unknown | unknown |")
            lines.append(f"Failure: {case['error']}")
    comparisons = []
    for allocation in ("stock80_cash20", "stock40_bond20_gold20_cash20"):
        cases = {row["recipe"]: row for row in record["results"] if row["allocation"] == allocation}
        for control_name in RECIPES[:2]:
            control = cases.get(control_name)
            for recipe in RECIPES[2:]:
                candidate = cases.get(recipe)
                complete = bool(control and candidate and control["status"] == candidate["status"] == "completed")
                years = []
                if complete:
                    baseline_years = {row["year"]: row for row in control["annual_same_account"]}
                    for row in candidate["annual_same_account"]:
                        baseline = baseline_years.get(row["year"])
                        if baseline and baseline["sessions"] == row["sessions"]:
                            years.append({"year": row["year"], "sessions": row["sessions"],
                                          "net_return_difference": row["net_return"] - baseline["net_return"],
                                          "account_reset": False})
                comparisons.append({"allocation": allocation, "candidate": recipe, "control": control_name,
                                    "complete_matched_accounts": complete,
                                    "net_return_difference": candidate["net_return"] - control["net_return"] if complete else None,
                                    "sharpe_difference": (candidate["sharpe_zero_rf_252"] - control["sharpe_zero_rf_252"]
                                                          if complete and candidate["sharpe_zero_rf_252"] is not None
                                                          and control["sharpe_zero_rf_252"] is not None else None),
                                    "drawdown_magnitude_difference": abs(candidate["max_drawdown"]) - abs(control["max_drawdown"]) if complete else None,
                                    "fee_difference_cny": candidate["fees"] - control["fees"] if complete else None,
                                    "carried_year_return_differences": years,
                                    "independent_validation": False, "execution_admitted": False})
    record["matched_incremental_diagnostics"] = comparisons
    lines += ["", "## Matched Increments vs Both Controls", "",
              "Return difference is percentage points; positive drawdown-magnitude difference means deeper loss.",
              "| Allocation | Candidate | Control | Net difference | MDD magnitude difference | Fee difference CNY |",
              "| --- | --- | --- | ---: | ---: | ---: |"]
    for row in comparisons:
        values = (f"{row['net_return_difference']:.2%} | {row['drawdown_magnitude_difference']:.2%} | {row['fee_difference_cny']:.2f}"
                  if row["complete_matched_accounts"] else "unknown | unknown | unknown")
        lines.append(f"| {row['allocation']} | {row['candidate']} | {row['control']} | {values} |")
    lines += ["", "## Limits", "",
              "- Five recipes fixed before these account runs: two existing controls and three additions; no recipe/weight/window/fee search.",
              "- Recipe selection happens after historical library inspection; this is development diagnostics, NOT an uninspected or preregistered test.",
              "- RSQR30 and WVMA20 direction reads only2024 train/horizon20. Positive12-minus1-month strength is fixed, not sign-fitted.",
              "- Directions originate in the broader718 diagnostic pool, not recalibrated on this stricter252-return common pool; IC portability is unproven.",
              "- Risk=-252-return volatility rank; trend averages12-1 strength and oriented30-session fit-quality ranks; volume-stability=orientedWVMA20 rank.",
              "- Blocks weighted1/2+1/2 or1/3 each; two trend features do not receive two whole-family votes. Missing rows excluded from all recipes equally.",
              "- Same common pool: supported ordinary SH/SZ, active/non-ST,365-day age,20-day median turnover>=20m, complete252-return history and both native features.",
              "- Month-end decisions;20stocks/rank30 retention; original lots/T+1/limits/fees/slippage. No price-floor/notice overlay or commission-deadband added.",
              "- Stock-only targets4% each; mixed2% each plus20% bond/20% gold,20% cash. No independently simulated curve blend.",
              "- Existing non-PIT listing/actions/price limitations remain; unknown actions stop affected accounts, never change source ranks or fake successful partial returns.",
              "- No industry/size neutrality, multiplicity correction, forward/client acceptance or black-swan guarantee. Positive development net increments are not admission.",
              f"Detailed orders, fills, targets, decisions, account states and carried-year results: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(library_id, mixed_source_id, *, event_view=None, reference_view=None, resume_run=None, restart_cases=()):
    library_path = ROOT / "outputs/factor_libraries" / library_id / "run.json"
    library = json.loads(library_path.read_text(encoding="utf-8"))
    if (library.get("status") != "completed_research_diagnostics" or library.get("definitions_completed") != 718
            or library.get("fresh_holdout_claimed") is not False or library["segments"]["train"] != ["2024-01-02", "2024-12-31"]):
        raise ValueError("Completed current718 library with fixed2024 training required")
    mixed_source = json.loads((ROOT / "outputs/market_replay" / mixed_source_id / "run.json").read_text(encoding="utf-8"))
    if (mixed_source.get("status") != "completed_stock_defensive_diagnostic"
            or mixed_source["protocol"]["id"] != "stock_defensive_fixed_40_20_20_v1"):
        raise ValueError("Existing completed fixed defensive fund binding required")
    inputs = bind_stock_inputs(library["bulk_inputs"], event_view, reference_view)
    previous = (json.loads((ROOT / "outputs/market_replay" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    restart_cases = set(restart_cases)
    if restart_cases and (not previous or not restart_cases <= {f"{prefix}_{recipe}" for prefix in ("stock", "mixed") for recipe in RECIPES}):
        raise ValueError("Explicit restart cases require an existing matching experiment")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/market_replay" / identifier
    artifacts = DATA_ROOT / "research/market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {"id": identifier, "status": "running", "research_family": "fixed_family_incremental",
              "purpose": "fixed_family_post_training_cost_inclusive_v1", "start": START, "end": END,
              "library_run": str(library_path), "artifacts": str(artifacts), "results": [],
              "dataset": library["bulk_inputs"]["price_view"], "sessions": 0,
              "scope": "full_source_stocks_plus_two_defensive_funds_research_only", "symbols": 0,
              "protocol": {"portfolio": portfolio().to_dict(), "fresh_holdout_claimed": False,
                           "selection_is_point_in_time": False, "execution_admitted": False},
              "strategy_validated": False, "execution_admitted": False, "resume_run": resume_run,
              "restart_cases": sorted(restart_cases)}
    write_json(out / "run.json", record)
    stocks, started = None, time.monotonic()
    try:
        summary = pd.read_csv(Path(library["artifacts"]) / "decay_summary.csv")
        directions = training_directions(summary)
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        funds = HistoricalFeed(mixed_source["protocol"]["fund_dataset"])
        base = MixedResearchFeed(stocks, funds, END)
        dates = [d for d in base.calendar if START <= d <= END]
        if not dates or dates[0] != START or dates[-1] != END:
            raise ValueError("Exact fixed post-training calendar coverage required")
        record.update(sessions=len(dates), symbols=len(base.symbols), fund_dataset=str(funds.folder))
        configs = {f"{prefix}_{recipe}": (portfolio() if prefix == "stock" else mixed_config()).to_dict()
                   for prefix in ("stock", "mixed") for recipe in RECIPES}
        protocol = {**record["protocol"], "id": record["purpose"], "recipes": list(RECIPES),
                    "signals": list(configs), "signal_portfolio_configs": configs, "training_directions": directions,
                    "stock_inputs": {key: str(value) for key, value in inputs.items()},
                    "library_stock_inputs": library["bulk_inputs"], "stock_manifest": stocks.manifest,
                    "action_source_override": bool(event_view),
                    "fund_dataset": str(funds.folder), "fund_profiles": base.instrument_profiles,
                    "start": START, "end": END, "sessions": len(dates), "initial_cash": 140000,
                    "native_columns": list(NATIVE_COLUMNS), "sign_calibration_horizon": HORIZON,
                    "family_weights": {"risk_volume_stability": [0.5, 0.5], "risk_trend_quality": [0.5, 0.5],
                                       "equal_three_families": [1/3, 1/3, 1/3]},
                    "trend_within_family": {"momentum_12_1": 0.5, "RSQR30": 0.5},
                    "selection_parameters_searched": False, "model_fit": False,
                    "opening_basis": "All new matched post-training research cases start cash-only140000; old account prefixes not imported",
                    "basis": "Current-source already-inspected development diagnostics, not independent/PIT or broker eligibility",
                    "limitations": [*base.manifest["limitations"], "Recipe choices after library inspection; no independent holdout", 
                                    "Directions learned on the broader diagnostic pool, not the stricter252-return common replay pool",
                                    "RSQR is fit quality, not directional momentum; WVMA is weighted-move variability, not direct liquidity/turnover", 
                                    "No industry/size/asset-factor attribution or black-swan execution guarantee"]}
        record["protocol"] = protocol
        if previous:
            validate_family_resume(previous, record)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        values = low_risk_features(stocks, END)
        ends = [day for day in month_ends(base.calendar, END) if day >= START]
        native = read_native_monthends(stocks, inputs["native_view"], ends)
        print(f"Pinned common preparation complete: {len(dates)} sessions, {len(ends)} month-ends, {len(native)} reused native rows", flush=True)
        for prefix in ("stock", "mixed"):
            for recipe in RECIPES:
                signal = f"{prefix}_{recipe}"
                folder = artifacts / signal
                producer = FamilyFeed(base, values, native, recipe, directions, END)
                feed, options = producer, {}
                config = portfolio()
                if prefix == "mixed":
                    config = mixed_config()
                    feed = StockDefensiveFeed(base, values, recipe, config.initial_cash, stock_signal=producer)
                    options = {"portfolio_policy": feed.allocate,
                               "portfolio_policy_contract": {"id": protocol["id"] + ":" + signal,
                                                             "config": config.to_dict(), "training_directions": directions,
                                                             "allocation": "stock40_bond20_gold20_cash20", "recipe": recipe}}
                ignored = []
                if previous and signal not in restart_cases:
                    checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / signal, config, dates,
                                                        portfolio_policy_contract=options.get("portfolio_policy_contract"))
                    ignored = validate_prefix_inputs(previous["protocol"]["stock_manifest"], stocks.manifest,
                                                     checkpoint["state"]["day"], checkpoint["state"])
                    options["checkpoint"] = checkpoint
                    print(f"{signal}: continuing after {len(checkpoint['daily'])} saved sessions", flush=True)
                result = {"signal": signal, "recipe": recipe,
                          "allocation": "stock80_cash20" if prefix == "stock" else "stock40_bond20_gold20_cash20",
                          **baseline_ledger_result(feed, config, dates, folder, **options)}
                if previous and signal not in restart_cases:
                    result.update(resume_source=options["checkpoint"]["source"], uninvolved_source_changes=ignored)
                save_diagnostics(folder, "decisions.csv", producer.audit, options.get("checkpoint"))
                save_diagnostics(folder, "rankings.csv", producer.ranking_rows, options.get("checkpoint"))
                if prefix == "mixed":
                    save_diagnostics(folder, "targets.csv", feed.audit, options.get("checkpoint"))
                if result["completed_sessions"]:
                    result["annual_same_account"] = stage_metrics(folder, config.initial_cash)
                record["results"].append(result)
                write_json(out / "run.json", record)
                print(f"{signal}: {result['status']} {result['completed_sessions']}/{len(dates)}; net={result.get('net_return')}", flush=True)
        record["status"] = ("completed_family_portfolio_diagnostics" if all(r["status"] == "completed" for r in record["results"])
                            else "failed_family_portfolio_diagnostics")
        report(out, record)
        return out, record
    except Exception as error:
        record.update(status="failed_family_portfolio_preparation", error=f"{type(error).__name__}: {error}")
        if record["results"]:
            report(out, record)
        raise
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        print(str(out), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True)
    parser.add_argument("--mixed-source", required=True)
    parser.add_argument("--event-view", help="Explicit existing research action snapshot; requires --reference-view")
    parser.add_argument("--reference-view", help="Aligned research reference snapshot; requires --event-view")
    parser.add_argument("--resume-run", help="Continue a terminal matching experiment; unchanged completed accounts reuse all sessions")
    parser.add_argument("--restart-case", action="append", default=[], help="Explicit full replay of an incompatible saved case; repeat as needed")
    args = parser.parse_args(argv)
    _, record = run(args.library, args.mixed_source, event_view=args.event_view, reference_view=args.reference_view,
                    resume_run=args.resume_run, restart_cases=args.restart_case)
    return 0 if record["status"] == "completed_family_portfolio_diagnostics" else 1


def actions_main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare corporate-action data demand from unchanged fixed family rank buffers")
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    action_needs(args.run)
    return 0


def terms_main(argv=None):
    parser = argparse.ArgumentParser(description="Batch reuse archived terms and acquire bounded public-source family data")
    parser.add_argument("--demand", required=True)
    parser.add_argument("--primary-view", required=True)
    parser.add_argument("--archive-root", action="append", default=[])
    parser.add_argument("--source-run", help="Same fixed terminal experiment with updated action inputs and actual failures")
    parser.add_argument("--max-fresh", type=int, default=64)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("Explicit --execute required for public-source data acquisition")
    prepare_action_terms(args.demand, args.primary_view, args.archive_root, args.max_fresh, source_run=args.source_run)
    return 0
