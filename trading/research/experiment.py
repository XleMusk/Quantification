"""Small offline factor search with a held-out evaluation and the existing ledger."""
import argparse
import ast
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import sys
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..engine import advance
from ..entry_risk import entry_risk_contract, validate_entry_risk_binding
from ..feed import HistoricalFeed
from ..market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from ..ledger import new_state
from ..portfolio_allocation import LOT_ALLOCATION, OPEN_LOT_ALLOCATION
from ..report import metrics as portfolio_metrics
from ..rights_issue import rights_replay_contract, validate_rights_binding
from ..settings import DATA_ROOT, ROOT
from ..signals import frame_from_qlib
from ..contracts import RunContext, SignalUnavailable, TradingMode
from ..target_exit_lifecycle import validate_binding as validate_full_exit_binding
from .factors import (LABEL, candidates, catalog, daily_ic, group_diagnostics, ic_summary,
                      native_features, research_panel, segment_mask)
from .bulk_feed import BULK_SCOPE, BulkResearchFeed
from ..event_frontier import summarize_frontiers

DEFAULT_CONFIG = Path(__file__).with_name("pilot.json")


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, (Path, pd.Timestamp)):
        return str(value)
    return value


def write_json(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def fit_model(train_x, train_y, valid_x, valid_y, config, seed):
    import lightgbm as lgb
    medians = train_x.median().fillna(0)
    if not train_x.columns.equals(valid_x.columns):
        raise ValueError("Model feature columns changed")
    def target(series):
        mean = series.groupby(level="date").transform("mean")
        std = series.groupby(level="date").transform("std")
        return ((series - mean) / std.where(std > 0)).fillna(0)
    model = lgb.LGBMRegressor(**config["model"], random_state=seed,
                            feature_fraction_seed=seed, bagging_seed=seed, data_random_seed=seed)
    model.fit(train_x.fillna(medians), target(train_y), eval_set=[(valid_x.fillna(medians), target(valid_y))],
              callbacks=[lgb.early_stopping(config["early_stopping_rounds"], verbose=False)])
    return model, medians


def predict(fitted, values):
    model, medians = fitted
    return pd.Series(model.predict(values[medians.index].fillna(medians)), index=values.index, name="score")


def search(train_x, train_y, valid_x, valid_y, base_names, definitions, config, out):
    """No test data argument, no holdout feedback, no automatic deployment."""
    names = definitions.name.tolist()
    train_ic = daily_ic(train_x[names], train_y, config["min_pairs"])
    valid_ic = daily_ic(valid_x[names], valid_y, config["min_pairs"])
    train_ic.to_csv(out / "candidate_train_daily_ic.csv")
    valid_ic.to_csv(out / "candidate_valid_daily_ic.csv")
    rows = []
    fingerprints = set()
    for entry in definitions.to_dict("records"):
        name = entry["name"]
        tr, va = ic_summary(train_ic[name]), ic_summary(valid_ic[name])
        sign = -1 if (tr["mean_rank_ic"] or 0) < 0 else 1
        coverage = min(train_x[name].notna().mean(), valid_x[name].notna().mean())
        ranked = train_x[name].groupby(level="date").rank(pct=True).mul(sign).round(10)
        signature = sha256(pd.util.hash_pandas_object(ranked, index=False).values.tobytes()).hexdigest()
        reason = "eligible"
        if tr["ic_days"] < config["min_ic_days"] or va["ic_days"] < config["min_ic_days"]:
            reason = "insufficient_nonconstant_ic_days"
        elif coverage < config["min_coverage"]:
            reason = "low_coverage"
        elif sign * (va["mean_rank_ic"] or 0) <= 0:
            reason = "validation_sign_failure"
        elif signature in fingerprints:
            reason = "duplicate_training_rank_vector"
        if reason == "eligible":
            fingerprints.add(signature)
        rows.append({**entry, "direction_from_train": sign, "coverage": coverage,
                     "train_ic": tr["mean_rank_ic"], "valid_ic": va["mean_rank_ic"],
                     "signed_valid_ic": sign * (va["mean_rank_ic"] or 0),
                     "train_ic_days": tr["ic_days"], "valid_ic_days": va["ic_days"], "screen_status": reason})
    trials = pd.DataFrame(rows)
    pool = trials.loc[trials.screen_status.eq("eligible")].sort_values(["signed_valid_ic", "name"], ascending=[False, True]).head(config["selection_pool"])
    seed = config["seeds"][0]
    current_fit = fit_model(train_x[base_names], train_y, valid_x[base_names], valid_y, config, seed)
    current_ic = daily_ic(predict(current_fit, valid_x).to_frame(), valid_y, config["min_pairs"]).score.mean()
    if not np.isfinite(current_ic):
        raise ValueError("Baseline validation predictions have no valid daily IC")
    selected, decisions = [], []
    baseline_ic = float(current_ic)
    for name in pool.name:
        if len(selected) >= config["max_selected"]:
            break
        columns = base_names + selected + [name]
        fitted = fit_model(train_x[columns], train_y, valid_x[columns], valid_y, config, seed)
        candidate_ic = daily_ic(predict(fitted, valid_x).to_frame(), valid_y, config["min_pairs"]).score.mean()
        gain = float(candidate_ic - current_ic)
        # Similarity is descriptive, NOT an automatic deletion rule.
        base_ranks = train_x[base_names].groupby(level="date").rank(pct=True)
        new_rank = train_x[name].groupby(level="date").rank(pct=True)
        similarity = base_ranks.corrwith(new_rank).abs().dropna()
        keep = bool(np.isfinite(gain) and gain >= config["min_validation_gain"])
        decisions.append({"name": name, "previous_validation_ic": float(current_ic),
                          "trial_validation_ic": float(candidate_ic), "incremental_validation_ic": gain,
                          "accepted": keep, "nearest_alpha158": similarity.idxmax() if len(similarity) else None,
                          "pooled_daily_rank_similarity": similarity.max() if len(similarity) else None,
                          "best_iteration": fitted[0].best_iteration_})
        print(f"Candidate {name}: validation delta={gain:+.6f}; accepted={keep}", flush=True)
        if keep:
            selected.append(name)
            current_ic, current_fit = candidate_ic, fitted
    trials["selected"] = trials.name.isin(selected)
    trials.to_csv(out / "all_candidates.csv", index=False)
    pd.DataFrame(decisions).to_csv(out / "selection_trials.csv", index=False)
    selection = {"selected": selected, "selection_seed": seed, "baseline_validation_ic": baseline_ic,
                 "final_validation_ic": float(current_ic), "decision_rule": config["min_validation_gain"],
                 "pool": pool.name.tolist(), "candidates_generated": len(trials),
                 "eligible_candidates": int(trials.screen_status.eq("eligible").sum()),
                 "frozen_at": datetime.now().astimezone().isoformat(), "test_feedback_used": False,
                 "selected_formulas": trials.loc[trials.selected].to_dict("records")}
    write_json(out / "selection_frozen.json", selection)
    return selection


class ScoreFeed:
    """Replace only the score source, retain historical data and execution path."""
    def __init__(self, feed, predictions):
        self.feed = feed
        self.by_day = {}
        for day, rows in predictions.groupby("datetime"):
            self.by_day[str(pd.Timestamp(day).date())] = frame_from_qlib(rows, str(pd.Timestamp(day).date()))

    def __getattr__(self, key):
        return getattr(self.feed, key)

    def scores(self, day, config):
        batch = self.by_day.get(day)
        return batch.ranked_candidates() if batch is not None else SignalUnavailable("model_score_batch_missing")

    def signal_frame(self, day, config, context):
        batch = self.by_day.get(day)
        return batch.with_context(context) if batch is not None else SignalUnavailable("model_score_batch_missing")


def export_scores(score):
    frame = score.rename("score").reset_index().rename(columns={"date": "datetime", "symbol": "instrument"})
    available = pd.to_datetime(frame.datetime).dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15, minutes=5)
    frame["available_at"] = available.dt.tz_convert("UTC").astype(str)
    frame["mode"] = "historical_research_only"
    return frame


def checked_portfolio_policy_contract(contract, config):
    if (not isinstance(contract, dict) or not isinstance(contract.get("id"), str) or not contract["id"]
            or contract.get("config") != config.to_dict()):
        raise ValueError("Portfolio checkpoint needs an explicit versioned policy and unchanged config")
    return json.loads(json.dumps(contract, allow_nan=False))


def load_replay_checkpoint(folder, config, dates, previous_config=None, *,
                           portfolio_policy_contract=None, target_policy_contract=None,
                           legacy_policy_protocol=None, entry_risk_policy=None,
                           risk_notice_view=None, commission_deadband=False, lot_allocation=None,
                           rights_replay_policy=None, full_exit_lifecycle=None):
    """Load a terminal replay prefix; its consumer must check data compatibility."""
    folder = Path(folder)
    dates = list(dates)
    receipt = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
    if receipt.get("full_exit_lifecycle") != full_exit_lifecycle:
        raise ValueError("Checkpoint full-exit lifecycle changed; fresh replay required")
    rights_contract = rights_replay_contract(rights_replay_policy)
    if receipt.get("rights_replay_policy") != rights_contract:
        raise ValueError("Checkpoint rights policy changed; fresh replay required")
    if receipt.get("lot_allocation") != lot_allocation:
        raise ValueError("Checkpoint close lot allocation changed")
    if type(commission_deadband) is not bool or receipt.get("commission_deadband", False) != commission_deadband:
        raise ValueError("Checkpoint commission deadband changed")
    entry_contract = entry_risk_contract(entry_risk_policy, risk_notice_view)
    if receipt.get("entry_risk_policy") != entry_contract:
        raise ValueError("Checkpoint entry-risk policy changed; fresh replay required")
    if receipt["status"] not in {"completed", "failed", "interrupted"}:
        raise ValueError("Cannot resume a running replay")
    enabled = receipt.get("portfolio_policy_enabled", False)
    target_binding = (checked_portfolio_policy_contract(target_policy_contract, config)
                      if target_policy_contract is not None else None)
    if (receipt.get("config", previous_config) != config.to_dict()
            or receipt.get("target_policy_enabled", False) != (target_binding is not None)
            or receipt.get("target_policy_contract") != target_binding
            or (enabled and target_binding is not None)
            or enabled != (portfolio_policy_contract is not None)):
        raise ValueError("Checkpoint strategy/cost configuration changed or uses an unpinned target policy")
    binding = None
    if portfolio_policy_contract is not None:
        binding = checked_portfolio_policy_contract(portfolio_policy_contract, config)
        saved = receipt.get("portfolio_policy_contract")
        if saved is None:
            if legacy_policy_protocol is None:
                raise ValueError("Legacy portfolio checkpoint requires its original predeclared protocol")
            original = json.loads(Path(legacy_policy_protocol).read_text(encoding="utf-8"))
            if any(original.get(key) != value for key, value in binding.items()):
                raise ValueError("Legacy portfolio protocol does not match the declared continuation policy")
        elif saved != binding:
            raise ValueError("Checkpoint portfolio policy contract changed")
    count = receipt["completed_sessions"]
    if type(count) is not int or not 0 <= count <= len(dates):
        raise ValueError("Invalid checkpoint session count")
    state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    lifecycle = validate_full_exit_binding(state, full_exit_lifecycle,
        RunContext("standalone_replay", TradingMode.REPLAY))
    if lifecycle is not None and state.get("full_exit_lifecycle") != lifecycle:
        raise ValueError("Checkpoint full-exit lifecycle receipt/account mismatch")
    validate_rights_binding(state, rights_replay_policy)
    if state.get("lot_allocation") != lot_allocation:
        raise ValueError("Checkpoint close lot allocation receipt/account mismatch")
    if state.get("commission_deadband", False) != commission_deadband:
        raise ValueError("Checkpoint commission deadband receipt/account mismatch")
    if state.get("entry_risk_policy") != entry_contract:
        raise ValueError("Checkpoint entry-risk receipt/account mismatch")
    if state.get("broker_binding"):
        raise PermissionError("A broker-bound account cannot seed historical replay")
    if count:
        frame = pd.read_csv(folder / "daily.csv", dtype={"date": str})
        frame["stale_symbols"] = frame.stale_symbols.map(ast.literal_eval)
        if not frame.stale_symbols.map(lambda value: isinstance(value, list) and all(isinstance(s, str) for s in value)).all():
            raise ValueError("Invalid checkpoint stale-mark lists")
        daily = frame.to_dict("records")
    else:
        daily = []
    last = dates[count - 1] if count else None
    if (len(daily) != count or [row["date"] for row in daily] != dates[:count]
            or state["day"] != last or receipt["last_completed_session"] != last
            or receipt["account_basis"] != "last_successfully_completed_session"):
        raise ValueError("Checkpoint account/daily/calendar prefix mismatch")
    if count:
        from ..ledger import Ledger
        if round(float(daily[-1]["equity"]) * 100) != Ledger(state, config).equity():
            raise ValueError("Checkpoint last equity does not match the saved ledger")
    checkpoint = {"state": state, "daily": daily, "source": str(folder.resolve()), "config": config.to_dict(),
                  "commission_deadband": commission_deadband, "lot_allocation": lot_allocation,
                  "rights_replay_policy": rights_contract}
    if binding is not None:
        checkpoint["portfolio_policy_contract"] = binding
        if receipt.get("portfolio_policy_contract") is None:
            checkpoint["legacy_policy_protocol"] = str(Path(legacy_policy_protocol).resolve())
    if target_binding is not None:
        checkpoint["target_policy_contract"] = target_binding
    if full_exit_lifecycle is not None:
        checkpoint["full_exit_lifecycle"] = full_exit_lifecycle
    return checkpoint


def replay(feed, config, dates, destination, *, target_policy=None, portfolio_policy=None, checkpoint=None,
           portfolio_policy_contract=None, target_policy_contract=None,
           commission_deadband=False, lot_allocation=None, full_exit_lifecycle=None):
    dates = list(dates)
    if lot_allocation not in {None, LOT_ALLOCATION, OPEN_LOT_ALLOCATION}:
        raise ValueError("Unknown close lot allocation")
    if type(commission_deadband) is not bool or commission_deadband and config.commission <= 0:
        raise ValueError("Explicit commission deadband needs a positive commission rate")
    entry_contract = entry_risk_contract(getattr(feed, "entry_risk_policy", None), getattr(feed, "risk_notice_view", None))
    rights_policy = getattr(feed, "rights_replay_policy", None)
    rights_contract = rights_replay_contract(rights_policy)
    binding = None
    target_binding = None
    if target_policy_contract is not None:
        if not callable(target_policy) or portfolio_policy is not None:
            raise ValueError("Target contract requires its reduction callable, not a portfolio allocation")
        target_binding = checked_portfolio_policy_contract(target_policy_contract, config)
    if portfolio_policy_contract is not None:
        if not callable(portfolio_policy) or target_policy is not None:
            raise ValueError("Portfolio contract requires its allocation callable, not a reduction overlay")
        binding = checked_portfolio_policy_contract(portfolio_policy_contract, config)
    if checkpoint is not None:
        if (checkpoint["config"] != config.to_dict()
                or checkpoint.get("target_policy_contract") != target_binding
                or (target_policy is not None and target_binding is None)
                or checkpoint.get("portfolio_policy_contract") != binding
                or checkpoint.get("commission_deadband", False) != commission_deadband
                or checkpoint.get("lot_allocation") != lot_allocation
                or checkpoint.get("rights_replay_policy") != rights_contract
                or checkpoint.get("full_exit_lifecycle") != full_exit_lifecycle
                or (portfolio_policy is not None and binding is None)):
            raise ValueError("Checkpoint configuration/policy mismatch")
        state, daily = deepcopy(checkpoint["state"]), deepcopy(checkpoint["daily"])
        validate_rights_binding(state, rights_policy)
        if state.get("entry_risk_policy") != entry_contract:
            raise ValueError("Checkpoint entry-risk policy changed; fresh replay required")
        validate_entry_risk_binding(state, entry_contract)
        if ([row["date"] for row in daily] != dates[:len(daily)] or len(daily) > len(dates)
                or state["day"] != (dates[len(daily) - 1] if daily else None)
                or state.get("broker_binding")):
            raise ValueError("Checkpoint is not the requested unbound replay prefix")
    else:
        state, daily = new_state(config.initial_cash), []
        if rights_contract is not None:
            state["rights_replay_policy"] = rights_contract
        if lot_allocation:
            state["lot_allocation"] = lot_allocation
        if entry_contract is not None:
            state["entry_risk_policy"] = entry_contract
        # A new cash-only replay has no holdings at registrations before inception.
        # This is an account opening fact, not a completion of missing event terms.
        if dates:
            for reference in getattr(feed, "manifest", {}).get("historical_price_adjustments", []):
                registration = reference.get("registration_date")
                if (reference["kind"] in {"rights_issue", "restructuring_holder_distribution"}
                        and registration and registration < dates[0]):
                    prefix = "rights:" if reference["kind"] == "rights_issue" else ""
                    state["entitlements"][f"{prefix}{reference['symbol']}:{reference['date']}"] = 0
    lifecycle = validate_full_exit_binding(state, full_exit_lifecycle,
        RunContext("standalone_replay", TradingMode.REPLAY))
    if checkpoint is not None and lifecycle is not None and state.get("full_exit_lifecycle") != lifecycle:
        raise ValueError("Checkpoint full-exit lifecycle receipt/account mismatch")
    if lifecycle is not None:
        state["full_exit_lifecycle"] = lifecycle
    reused = len(daily)
    destination.mkdir(parents=True, exist_ok=False)
    frontier_sessions = []
    if checkpoint is not None:
        source_receipt = Path(checkpoint["source"]) / "replay.json"
        if source_receipt.is_file():
            prior_frontier = json.loads(source_receipt.read_text(encoding="utf-8")).get("event_time_frontier", {})
            frontier_sessions.extend(prior_frontier.get("sessions", []))
    record = {"status": "running", "completed_sessions": reused, "attempted_session": None,
              "config": config.to_dict(), "target_policy_enabled": target_policy is not None,
              "portfolio_policy_enabled": portfolio_policy is not None,
              "portfolio_policy_contract": binding,
              "target_policy_contract": target_binding,
              "commission_deadband": commission_deadband,
              "lot_allocation": lot_allocation,
              "execution_policy": "minimum_commission_internal_reweights_v1" if commission_deadband else "exact_target",
              "entry_risk_policy": entry_contract,
              "rights_replay_policy": rights_contract,
              "legacy_policy_protocol": checkpoint.get("legacy_policy_protocol") if checkpoint else None,
              "reused_sessions": reused, "resume_source": checkpoint["source"] if checkpoint is not None else None,
              "opening_basis": "continued_checkpoint" if checkpoint is not None else "fresh_cash_only_no_prior_holdings",
              "replay_start": dates[0] if dates else None,
              "event_time_frontier": summarize_frontiers(frontier_sessions)}
    if full_exit_lifecycle is not None:
        record["full_exit_lifecycle"] = full_exit_lifecycle
    try:
        for count, day in enumerate(dates[reused:], start=reused):
            record["attempted_session"] = day
            trial = deepcopy(state)
            options = {"target_policy": target_policy} if target_policy is not None else {}
            if portfolio_policy is not None:
                options["portfolio_policy"] = portfolio_policy
            if commission_deadband:
                options["commission_deadband"] = True
            if lot_allocation:
                options["lot_allocation"] = lot_allocation
            if full_exit_lifecycle is not None:
                options["full_exit_lifecycle"] = full_exit_lifecycle
            row = advance(trial, feed, day, config, count, **options)
            state = trial
            daily.append(row)
            if row.get("event_time_frontier") is not None:
                frontier_sessions.append(row["event_time_frontier"])
            record["event_time_frontier"] = summarize_frontiers(frontier_sessions)
        record["status"] = "completed"
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if record["status"] == "running":
            record["status"] = "interrupted"
        record.update(completed_sessions=len(daily), last_completed_session=state["day"],
                      newly_completed_sessions=len(daily) - reused,
                      account_basis="last_successfully_completed_session")
        pd.DataFrame([{k: v for k, v in row.items() if k != "events"} for row in daily]).to_csv(destination / "daily.csv", index=False)
        pd.DataFrame(state["orders"].values()).to_csv(destination / "orders.csv", index=False)
        pd.DataFrame(state["fills"].values()).to_csv(destination / "fills.csv", index=False)
        write_json(destination / "account.json", state)
        write_json(destination / "replay.json", record)
    return {**portfolio_metrics(daily, config.initial_cash), "orders": len(state["orders"]), "fills": len(state["fills"]),
            "reused_sessions": reused, "newly_completed_sessions": len(daily) - reused,
            "turnover_over_initial": sum(row["turnover_value"] for row in daily) / config.initial_cash}


def baseline_ledger_result(feed, config, dates, destination, *, target_policy=None, portfolio_policy=None, checkpoint=None,
                           portfolio_policy_contract=None, target_policy_contract=None,
                           commission_deadband=False, lot_allocation=None, full_exit_lifecycle=None):
    """Keep an unresolved ledger case without abandoning the remaining fixed cases."""
    options = {"target_policy": target_policy} if target_policy is not None else {}
    if lot_allocation:
        options["lot_allocation"] = lot_allocation
    if commission_deadband:
        options["commission_deadband"] = True
    if portfolio_policy is not None:
        options["portfolio_policy"] = portfolio_policy
    if checkpoint is not None:
        options["checkpoint"] = checkpoint
    if portfolio_policy_contract is not None:
        options["portfolio_policy_contract"] = portfolio_policy_contract
    if target_policy_contract is not None:
        options["target_policy_contract"] = target_policy_contract
    if full_exit_lifecycle is not None:
        options["full_exit_lifecycle"] = full_exit_lifecycle
    try:
        return {"status": "completed", "completed_sessions": len(dates),
                **replay(feed, config, dates, destination, **options)}
    except ValueError as error:
        if not (destination / "replay.json").is_file():
            raise
        receipt = json.loads((destination / "replay.json").read_text(encoding="utf-8"))
        if receipt["status"] != "failed" or receipt["error"] != f"{type(error).__name__}: {error}":
            raise
        return {"status": "failed", "error": receipt["error"],
                **{k: receipt[k] for k in ("completed_sessions", "last_completed_session",
                                          "attempted_session", "account_basis")}}


def validate_config(config, feed, baseline_only=False, ingestion_only=False):
    if ingestion_only and not baseline_only:
        raise ValueError("Ingestion-only mode requires baseline-only; factor search is not allowed")
    previous = None
    for name in ("train", "valid", "test"):
        start, end = map(pd.Timestamp, config["segments"][name])
        if start > end or (previous is not None and start <= previous) or end > pd.Timestamp(feed.calendar[-1]):
            raise ValueError("Segments must be chronological, disjoint and within the data snapshot")
        if start <= pd.Timestamp(feed.manifest["selection_date"]):
            raise ValueError("Research must begin after universe selection")
        previous = end
    if baseline_only:
        selection = feed.manifest.get("selection", {})
        if ingestion_only:
            if (feed.manifest["scope"] not in {INGESTION_SCOPE, BULK_SCOPE}
                    or feed.manifest.get("selection_is_point_in_time") is not False
                    or (feed.manifest.get("execution_admitted") is not False if feed.manifest["scope"] == BULK_SCOPE
                        else feed.manifest.get("live_ready") is not False)):
                raise ValueError("Ingestion-only comparison requires a declared non-PIT acquired SH/SZ stock snapshot")
        elif (feed.manifest["scope"] != FIXED_SCOPE or not selection.get("source_files_complete")
                or not selection.get("snapshot_validated") or selection.get("missing") != []
                or selection.get("expected_symbols") != len(feed.symbols)
                or selection.get("complete_symbols") != len(feed.symbols)):
            raise ValueError("Baseline comparison requires the complete fixed pre-period main-board snapshot")
    elif config["candidate_budget"] != 200 or 8 * len(config["windows"]) ** 2 != 200:
        raise ValueError("This first-round protocol is bounded to 200 templates")
    if not 0 < config["max_selected"] <= 10 or not config["max_selected"] <= config["selection_pool"] <= 20:
        raise ValueError("Invalid bounded selection budget")
    if len(config["seeds"]) < 3 or len(set(config["seeds"])) != len(config["seeds"]):
        raise ValueError("Use at least three distinct model seeds")


def run(config_path=DEFAULT_CONFIG, out=None, baseline_only=False, dataset=None, ingestion_only=False,
        *, price_view=None, event_view=None, reference_view=None, listing_master=None, native_view=None):
    bulk = price_view is not None
    if bulk:
        if not baseline_only or not ingestion_only or dataset is not None or any(
                value is None for value in (event_view, reference_view, listing_master, native_view)):
            raise ValueError("Full-source baseline needs all pinned bulk/native views and explicit baseline/ingestion mode")
    elif any(value is not None for value in (event_view, reference_view, listing_master, native_view)):
        raise ValueError("Bulk feature/view arguments require --price-view")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if dataset is not None:
        config["dataset"] = str(dataset)
    if bulk:
        config["dataset"] = str(Path(price_view).resolve())
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(out) if out else ROOT / "outputs" / "factor_mining" / run_id
    out.mkdir(parents=True, exist_ok=False)
    data_out = DATA_ROOT / "research" / "factor_mining" / run_id
    data_out.mkdir(parents=True, exist_ok=False)
    record = {"id": run_id, "status": "running", "started_at": datetime.now().astimezone().isoformat(),
              "config": config, "output": str(out), "data_output": str(data_out), "command": sys.argv,
              "python": sys.executable, "live_ready": False, "strategy_validated": False,
              "experiment_kind": ("alpha158_ingestion_ledger_comparison" if ingestion_only else
                                  "alpha158_baseline_comparison" if baseline_only else "bounded_factor_search"),
              "ingestion_only": ingestion_only,
              "candidate_search_performed": not baseline_only, "fresh_holdout_claimed": False,
              "versions": {k: metadata.version(k) for k in ("pyqlib", "lightgbm", "pandas", "numpy", "scipy")},
              "source_files": {str(p): sha256(p.read_bytes()).hexdigest() for p in (
                  Path(__file__), Path(__file__).with_name("factors.py"), Path(config_path),
                  ROOT / "trading/engine.py", ROOT / "trading/ledger.py", ROOT / "trading/signals.py")}}
    def progress(stage):
        record["stage"] = stage
        write_json(out / "run.json", record)
        print(stage, flush=True)
    try:
        progress("validate_existing_snapshot")
        feed = (BulkResearchFeed(price_view, event_view, reference_view, listing_master)
                if bulk else HistoricalFeed(config["dataset"]))
        validate_config(config, feed, baseline_only, ingestion_only)
        if baseline_only:
            config["scope"] = feed.manifest["scope"]
        portfolio = StrategyConfig(**config["portfolio"]).validate()
        record["portfolio_config"] = portfolio.to_dict()
        record["dataset_manifest"] = {**feed.manifest, "folder": str(feed.folder)}
        if bulk:
            record["bulk_inputs"] = {key: str(Path(value).resolve()) for key, value in (
                ("price_view", price_view), ("event_view", event_view), ("reference_view", reference_view),
                ("listing_master", listing_master), ("native_view", native_view))}
            record["calendar_file"] = str(Path(event_view).resolve() / "source_input_calendar.csv")
            record["input_hashes"] = {"calendar.csv": sha256(Path(record["calendar_file"]).read_bytes()).hexdigest()}
        else:
            input_names = ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv")
            input_names += ("manifest.json",) if ingestion_only else ("selection.csv",)
            record["input_hashes"] = {name: sha256((feed.folder / name).read_bytes()).hexdigest() for name in input_names}
        definitions = catalog()
        definitions.to_csv(out / "alpha158_catalog.csv", index=False)
        learning = definitions[definitions.family.isin(["ROC", "MA", "BETA", "RSV", "VMA", "CORR", "STD"])]
        learning.to_csv(out / "interpretable_35.csv", index=False)
        if bulk:
            from .bulk_model_inputs import load_inputs
            progress("reuse_full_native_alpha158_with_dated_pool_and_existing_labels")
            features, labels, quality = load_inputs(feed, portfolio, native_view, definitions, segments=config["segments"])
        else:
            panel, labels, eligible, quality = research_panel(feed, portfolio)
            panel.to_parquet(data_out / "adjusted_research_panel.parquet", index=False)
            progress("compute_native_alpha158_only" if baseline_only else "compute_native_alpha158_and_200_templates")
            features = native_features(panel, feed.calendar, data_out / "qlib_provider", definitions,
                                       [] if baseline_only else config["windows"])
            features = features.loc[eligible.reindex(features.index, fill_value=False)]
        write_json(out / "data_quality.json", quality)
        labels.to_parquet(data_out / "labels.parquet")
        all_x = features if bulk else features[definitions.name]
        if not baseline_only:
            generated, candidate_definitions = candidates(features, config["windows"])
            if len(candidate_definitions) != config["candidate_budget"]:
                raise ValueError("Candidate budget mismatch")
            candidate_definitions.to_csv(out / "candidate_formulas.csv", index=False)
            all_x = pd.concat([all_x, generated], axis=1)
        if not bulk:
            all_x.to_parquet(data_out / "features_and_candidates.parquet")
        label = labels[LABEL].reindex(all_x.index)
        parts, split_rows = {}, []
        for name, (start, end) in config["segments"].items():
            mask = segment_mask(all_x.index, labels, start, end)
            parts[name] = (all_x.loc[mask], label.loc[mask])
            in_date = segment_mask(all_x.index, labels, start, end, require_label=False)
            split_rows.append({"segment": name, "start": start, "end": end, "eligible_signals": int(in_date.sum()),
                               "labeled_rows": int(mask.sum()), "purged_or_missing_labels": int(in_date.sum() - mask.sum()),
                               "dates_with_labels": parts[name][0].index.get_level_values("date").nunique()})
            if parts[name][0].empty:
                raise ValueError(f"Empty research segment: {name}")
        pd.DataFrame(split_rows).to_csv(out / "split_counts.csv", index=False)
        train_x, train_y = parts["train"]
        valid_x, valid_y = parts["valid"]
        if bulk:
            del parts["test"]
        if baseline_only:
            selection = {"selected": [], "selected_formulas": [], "candidates_generated": 0,
                         "selection_method": "not_run_baseline_only", "test_feedback_used": False,
                         "frozen_at": datetime.now().astimezone().isoformat()}
            write_json(out / "selection_frozen.json", selection)
        else:
            progress("select_on_train_and_validation_only")
            selection = search(train_x, train_y, valid_x, valid_y, definitions.name.tolist(), candidate_definitions, config, out)
        record["selection"] = selection
        progress("selection_frozen_evaluate_holdout_without_retuning")
        test_start, test_end = config["segments"]["test"]
        test_mask = segment_mask(all_x.index, labels, test_start, test_end, require_label=False)
        test_x = all_x.loc[test_mask]
        test_y = label.reindex(test_x.index).where(labels.exit_date.reindex(test_x.index).le(pd.Timestamp(test_end)))
        if bulk:
            del all_x, features
        test_dates = [d for d in feed.calendar if test_start <= d <= test_end]
        all_metrics, portfolio_rows, prediction_ics, stability, group_frames = [], [], [], [], []
        record.update(model_metrics=all_metrics, portfolio_metrics=portfolio_rows)
        variants = (("B0", []),) if baseline_only else (("B0", []), ("B1", selection["selected"]))
        for seed in config["seeds"]:
            for variant, added in variants:
                columns = definitions.name.tolist() + added
                fitted = fit_model(train_x[columns], train_y, valid_x[columns], valid_y, config, seed)
                model_out = data_out / f"{variant}_seed{seed}"
                model_out.mkdir()
                fitted[0].booster_.save_model(str(model_out / "model.txt"))
                fitted[1].rename("train_median").to_csv(model_out / "preprocessing.csv")
                for split, x, y in (("valid", valid_x, valid_y), ("test", test_x, test_y)):
                    score = predict(fitted, x)
                    ic = daily_ic(score.to_frame(), y, config["min_pairs"]).score
                    all_metrics.append({"variant": variant, "seed": seed, "split": split,
                                        "features": len(columns), "best_iteration": fitted[0].best_iteration_, **ic_summary(ic)})
                    daily = ic.rename("rank_ic").reset_index().assign(variant=variant, seed=seed, split=split)
                    prediction_ics.append(daily)
                    if split == "test":
                        for quarter, values in ic.groupby(ic.index.to_period("Q")):
                            stability.append({"variant": variant, "seed": seed, "quarter": str(quarter), **ic_summary(values)})
                        groups = group_diagnostics(score, y).assign(variant=variant, seed=seed)
                        group_frames.append(groups)
                        predictions = export_scores(score)
                        predictions.to_parquet(model_out / "historical_scores.parquet", index=False)
                        replay_case = baseline_ledger_result if baseline_only else replay
                        result = replay_case(ScoreFeed(feed, predictions), portfolio, test_dates, model_out / "replay")
                        portfolio_rows.append({"variant": variant, "seed": seed, **result})
                progress(f"evaluated_{variant}_seed{seed}_ledger_{result.get('status', 'completed')}")
        replay_case = baseline_ledger_result if baseline_only else replay
        rule = replay_case(feed, portfolio, test_dates, data_out / "rule_replay")
        portfolio_rows.append({"variant": "RULE", "seed": None, **rule})
        for filename, rows in (("model_metrics.csv", all_metrics), ("portfolio_metrics.csv", portfolio_rows), ("quarterly_ic.csv", stability)):
            pd.DataFrame(rows).to_csv(out / filename, index=False)
        pd.concat(prediction_ics).to_csv(out / "model_daily_ic.csv", index=False)
        pd.concat(group_frames, ignore_index=True).to_csv(out / "model_groups.csv", index=False)
        # Holdout factor diagnostics are deliberately computed after selection.
        diagnostic_names = definitions.name.tolist() + selection["selected"]
        daily_ic(test_x[diagnostic_names], test_y, config["min_pairs"]).to_csv(out / "selected_and_alpha158_test_daily_ic.csv")
        changes = []
        metrics_df, portfolios_df = pd.DataFrame(all_metrics), pd.DataFrame(portfolio_rows)
        for seed in config["seeds"]:
            m = metrics_df.loc[metrics_df.seed.eq(seed) & metrics_df.split.eq("test")].set_index("variant")
            p = portfolios_df.loc[portfolios_df.seed.eq(seed)].set_index("variant")
            if baseline_only:
                available = p.at["B0", "status"] == "completed" and rule["status"] == "completed"
                changes.append({"seed": seed, "comparison": "B0_minus_RULE",
                                "comparison_status": "available" if available else "unavailable_failed_ledger",
                                **{"delta_" + key: p.at["B0", key] - rule[key] if available else None for key in
                                   ("net_return", "max_drawdown", "turnover_over_initial")}})
            else:
                changes.append({"seed": seed, "delta_test_ic": m.at["B1", "mean_rank_ic"] - m.at["B0", "mean_rank_ic"],
                                "delta_net_return": p.at["B1", "net_return"] - p.at["B0", "net_return"],
                                "delta_max_drawdown": p.at["B1", "max_drawdown"] - p.at["B0", "max_drawdown"],
                                "delta_turnover": p.at["B1", "turnover_over_initial"] - p.at["B0", "turnover_over_initial"]})
        pd.DataFrame(changes).to_csv(out / "paired_deltas.csv", index=False)
        failed_ledgers = [r for r in portfolio_rows if r.get("status") == "failed"]
        record.update(status="failed_ledger_comparison" if failed_ledgers else "completed",
                      ledger_failures=failed_ledgers, finished_at=datetime.now().astimezone().isoformat(), data_quality=quality,
                      model_metrics=all_metrics, portfolio_metrics=portfolio_rows, paired_deltas=changes,
                      b2_status="not_run_baseline_only" if baseline_only else "not_run_template_search_chosen_for_first_round")
        progress(record["status"])
        (baseline_report if baseline_only else report)(out, record)
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "run.json", record)
        raise
    finally:
        if bulk and "feed" in locals():
            feed.close()
    return out, record


def baseline_report(out, record):
    config = record["config"]
    ingestion = record.get("ingestion_only", False)
    metrics = pd.DataFrame(record["portfolio_metrics"])
    model_metrics = pd.DataFrame(record["model_metrics"]).query("variant == 'B0' and split == 'test'")
    baseline = metrics.loc[metrics.variant.eq("B0")]
    rule = metrics.loc[metrics.variant.eq("RULE")].iloc[0]
    bulk = bool(record.get("bulk_inputs"))
    universe = ("supported full source-covered SH/SZ ordinary stocks; research only" if bulk else
                "acquired SH/SZ stocks; ingestion diagnostics only" if ingestion else "fixed pre-period main-board stocks")
    lines = ["# Alpha158-LightGBM vs rule: ingestion diagnostics" if ingestion else "# Alpha158-LightGBM vs fixed rule", "",
             f"Snapshot: `{config['dataset']}`; {record['data_quality']['symbols']} {universe}.",
             f"Train: {config['segments']['train']}; validation: {config['segments']['valid']}; evaluation: {config['segments']['test']}.",
             "No new factors, B1 search, parameter sweep or automatic promotion. Native Alpha158 only.",
             f"Seeds: {config['seeds']}; model settings and portfolio: see run.json.",
             "The same pinned feed, dates, cash, costs and trading.engine/ledger are used for B0 and RULE.", "",
             "| Variant | Seed | Ledger status | Net return | Max drawdown | Sharpe (zero RF,252) | Turnover / initial cash | Stale mark days |",
             "|---|---:|---|---:|---:|---:|---:|---:|"]
    failures = []
    for row in metrics.to_dict("records"):
        seed = str(int(row["seed"])) if pd.notna(row["seed"]) else "n/a"
        if row.get("status", "completed") == "failed":
            lines.append(f"| {row['variant']} | {seed} | failed after {row['completed_sessions']} sessions | unavailable | unavailable | unavailable | unavailable | unavailable |")
            failures.append(f"- {row['variant']} seed {seed}: {row['error']}; account is the last successfully completed session, not a full-period result.")
        else:
            sharpe = row.get("sharpe_zero_rf_252")
            sharpe_text = f"{sharpe:.3f}" if pd.notna(sharpe) else "unavailable"
            stale = row.get("stale_valuation_days")
            stale_text = str(int(stale)) if pd.notna(stale) else "unavailable"
            lines.append(f"| {row['variant']} | {seed} | completed | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {sharpe_text} | {row['turnover_over_initial']:.2f} | {stale_text} |")
    if failures:
        lines += ["", "## Ledger Failures", "", *failures]
    if record.get("ledger_failures"):
        lines += ["", "Aggregate comparison unavailable: at least one planned ledger failed. No failed case is dropped from the seed average."]
    if all(row.get("status", "completed") == "completed" for row in baseline.to_dict("records")):
        lines += ["", f"B0 mean return: {baseline.net_return.mean():.2%}; seed standard deviation: {baseline.net_return.std():.2%}.",
                  "This is the seed mean of separate model accounts, not a traded ensemble or independent market repetitions."]
        if rule.get("status", "completed") == "completed":
            lines += [f"B0 mean minus RULE return: {(baseline.net_return.mean() - rule.net_return) * 100:+.2f} percentage points."]
    lines += [f"B0 mean daily Rank IC: {model_metrics.mean_rank_ic.mean():.5f} (prediction diagnostic, not ledger performance).", "",
              "## Interpretation limits", "",
              ("- Full acquired SH/SZ ordinary-stock source scope, no stock budget. Not independently complete A/BSE/ETF or PIT coverage. Cached native features reused without recalculation."
               if bulk else "- NOT strategy validation: acquisition-order SH/SZ stock sample, stratified using retrieval-time status; not a PIT or representative full-market universe."
               if ingestion else "- This is a provider-reported fixed main-board subset, not all CSI300 or a rolling index. Membership is not independently reconciled."),
              "- 2026 results were inspected in earlier work on other samples. This is not a fresh untouched external test.",
              ("- Saved models and historical forecasts are reused without refitting; account replay is recomputed only where past corrections require it."
               if record.get("models_refit") is False else
               "- The model is refit on the fixed training/validation segments; old weights or old signals are not used as current signals."),
              "- RULE keeps its existing positive-momentum eligibility filter; the comparison is between complete methods, not an isolated factor effect.",
              "- Labels use adjusted O(T+6)/O(T+1)-1 with boundary purging. Inference eligibility does not depend on future labels.",
              "- Three seeds are model repetitions, not independent market periods. No return guarantee or deployment readiness follows.",
              "- Daily-open fills, prior-volume capacity and a fixed dividend-tax reserve remain approximations.",
              "- Stale mark days retain the ledger's carried-price valuation flags. Those marks are not independently verified contemporaneous prices; completed accounting does not remove this comparison limitation.",
              f"- Source limitations: {record['dataset_manifest'].get('limitations', [])}", "",
              "## Artifacts", "",
              "model_metrics.csv, portfolio_metrics.csv, paired_deltas.csv (B0 minus RULE), quarterly_ic.csv, model_daily_ic.csv, model_groups.csv.",
              f"Models, predictions and ledger receipts (including failures): `{record['data_output']}`.",
              (f"Native feature cache reused in place: `{record['data_quality']['native_view']}`."
               if bulk else f"Native features: `{record['data_output']}`.")]
    if record.get("model_sources"):
        lines += [f"Saved models/preprocessing/predictions reused without refitting: `{record['model_sources']}`.",
                  f"Ledger continuation source: `{record['resume_run']}`; explicit cold account cases: {record['restart_cases']}."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def report(out, record):
    test = pd.DataFrame(record["model_metrics"]).query("split == 'test'")
    table = test.groupby("variant").mean(numeric_only=True)
    portfolios = pd.DataFrame(record["portfolio_metrics"]).groupby("variant").mean(numeric_only=True)
    lines = ["# 首轮量价因子挖掘实测", "", "状态：已完成离线试验；未验证全市场有效性；未连接或修改同花顺。", "",
             f"数据：10 只历史选定主板股票，训练 2024、验证 2025、测试 2026 至 {record['config']['segments']['test'][1]}。",
             "标签：T 日收盘决策，复权开盘价 O(T+6)/O(T+1)-1；跨分段边界标签剔除。",
             "", "## 搜索与选择", "",
             f"Qlib 0.9.7 原生 Alpha158；35 个可解释学习特征；200 个确定性模板候选；最终保留 {len(record['selection']['selected'])} 个。",
             "训练期确定因子方向，验证期筛选；最多10次顺序增量模型试验、最多5个因子，固定最小验证IC增量0.002。",
             "模型随机种子17/29/43；相同训练/验证数据、200轮上限、30轮早停、组合规则和成本。",
             "候选生成是确定性模板枚举，不是强化学习、遗传搜索或论文数字复现；B2未运行。", "",
             "这是保留 Alpha158+LightGBM 方法的重新训练对照，不是复用旧模型权重；旧数据错误与旧信号没有被绕过。",
             "", "## 样本外比较", "", "三个种子均值；收益为现有账本撮合后的区间净收益，不是重叠标签复利。", "",
             "|版本|Rank IC|净收益|最大回撤|成交金额/初始资金|", "|---|---:|---:|---:|---:|"]
    for variant in ("B0", "B1", "RULE"):
        ic = f"{table.at[variant, 'mean_rank_ic']:.5f}" if variant in table.index else "不适用"
        p = portfolios.loc[variant]
        lines.append(f"|{variant}|{ic}|{p.net_return:.2%}|{p.max_drawdown:.2%}|{p.turnover_over_initial:.2f}|")
    lines += ["", "## 具体公式", ""]
    for row in record["selection"]["selected_formulas"]:
        lines.append(f"- `{row['name']}`：`{row['formula']}`；训练期定向符号 {row['direction_from_train']:+d}。")
    if not record["selection"]["selected"]:
        lines.append("没有候选通过预设增量选择标准，B1不强行加入因子。")
    changes = pd.DataFrame(record["paired_deltas"])
    lines += ["", f"B1-B0 三种子平均测试 IC 差：{changes.delta_test_ic.mean():+.5f}；净收益差：{changes.delta_net_return.mean():+.2%}。",
              "不能仅凭单因子IC或一段净收益决定替换运行策略；逐种子、逐季度结果均保留。", "",
              "## 边界", "",
              "- 固定10只股票只适合首轮方法试验，不代表完整历史沪深300或全A；没有主动扩大到现有存疑的大股票池。",
              "- 2026年规则策略汇总曾在此前工作中被看到；本轮候选搜索未使用2026反馈，但不能称它是从未被接触的外部测试集。",
              "- 使用供应商复权/原始价比例并按最早观察归一化，消除单股票全区间统一缩放；不能证明供应商历史修订完全符合PIT。",
              "- VWAP由原始成交额/股数计算并同步调整；成交量按Qlib口径逆向调整，不是换手率。",
              "- 截面排名只在当日有效股票池内；缺失价格不前填，停牌不压缩日历；缺失特征的中位数只在训练期拟合。",
              "- 因子相似性仅作说明，增量筛选看加入模型后的验证IC；200次搜索和重叠标签使未经校正的显著性结论不成立。",
              "- HAC lag5数值为诊断，不是因子挖掘已通过多重检验的证明。三个模型种子也不是三个独立市场样本。",
              "- 组合复用trading.engine/ledger：T+1、整数股、费用、滑点、可卖数量、价格限制、公司行动与前日成交量容量。",
              "- 日开盘撮合仍是代理模型；不知道集合竞价队列及真实冲击。期末持仓按收盘估值，没有假设全数清仓并收取清仓成本。",
              "- B0/B1使用同一交易日范围和仓位规则；RULE保留现有正动量过滤，与模型方法不同，结果不得归因于单一因子变化。",
              "- 预测文件是历史研究文件，available_at是历史决策时点假设，不是当时真实上线记录；没有写入latest或自动发布。", "",
              "## 结果文件", "",
              "- alpha158_catalog.csv / interpretable_35.csv：真实公式与分类。",
              "- all_candidates.csv / selection_trials.csv / selection_frozen.json：全部候选和接受、拒绝依据。",
              "- model_metrics.csv / quarterly_ic.csv / paired_deltas.csv：逐种子与分期增量。",
              "- model_daily_ic.csv / model_groups.csv：逐日IC和五组诊断，分组收益不可直接复利。",
              f"- 模型、数据、历史分数、逐笔委托/成交/账户和逐日净值：`{record['data_output']}`。", "",
              "下一步：先读增量与稳定性结果，再在明确历史构成的更大股票池上复核；不因本轮最高分自动部署。", "",
              "## 来源", "",
              "- [Qlib 0.9.7 Alpha158](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/contrib/data/loader.py)",
              "- [Qlib 数据口径](https://qlib.readthedocs.io/en/stable/component/data.html)"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out")
    parser.add_argument("--baseline-only", action="store_true", help="Fixed-universe Alpha158/LightGBM vs existing rule, no candidate search")
    parser.add_argument("--ingestion-only", action="store_true", help="Explicit baseline-only diagnostic on acquired SH/SZ stocks; not strategy validation")
    parser.add_argument("--dataset", type=Path, help="Explicit pinned snapshot override, recorded in run.json")
    for name in ("price-view", "event-view", "reference-view", "listing-master", "native-view"):
        parser.add_argument("--" + name, type=Path, help="Pinned full-source baseline input; all five required together")
    parser.add_argument("--resume-run", type=Path, help="Continue a finished full-source baseline using saved predictions; no refit or factor calculation")
    parser.add_argument("--restart-case", action="append", default=[], help="With --resume-run, explicitly recompute this account from inception (alpha158_seedN or the rule name)")
    args = parser.parse_args(argv)
    if args.resume_run:
        if (args.config != str(DEFAULT_CONFIG) or any((args.dataset, args.price_view, args.listing_master, args.native_view,
                                                      args.baseline_only, args.ingestion_only))):
            parser.error("Resume fixes the source config/prices/listing/features; only event/reference views may be replaced")
        from .baseline_replay import run_saved
        out, record = run_saved(args.resume_run, event_view=args.event_view, reference_view=args.reference_view,
                                out=args.out, restart_cases=args.restart_case)
    else:
        if args.restart_case:
            parser.error("--restart-case requires --resume-run")
        out, record = run(args.config, args.out, args.baseline_only, args.dataset, args.ingestion_only,
                          price_view=args.price_view, event_view=args.event_view, reference_view=args.reference_view,
                          listing_master=args.listing_master, native_view=args.native_view)
    print(json.dumps({"status": record["status"], "output": str(out), "selected": record["selection"]["selected"]}, ensure_ascii=False))
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
