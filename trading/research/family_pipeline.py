"""Mature-label direction/selection/refit pipeline on the saved common pool."""
import argparse
from datetime import datetime
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..config import StrategyConfig
from ..contracts import SignalFrame
from ..expected_returns import EqualSlotReturnPolicy
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import native_binding
from .experiment import baseline_ledger_result, clean, load_replay_checkpoint, write_json
from .factor_families import forward_open_labels, read_open_panel
from .factors import catalog
from .family_portfolios import END, START, bind_stock_inputs, read_native_monthends
from .family_ridge import ALPHA, FAMILIES, HORIZON, MIN_MONTHS, MIN_PAIRS, TRAIN_START
from .market_replay import validate_prefix_inputs
from ..rights_issue import rights_replay_contract, waiver_receipt
from .return_cost_replay import SavedReturnFeed, selection_summary
from .stock_defensive import stage_metrics


ID = "mature_month_family_pipeline_v1"
SIGNAL = "stock_mature_month_family_return"
RAW_COLUMNS = ("risk", "momentum", "RSQR30", "WVMA20")
METHOD = "https://github.com/microsoft/qlib/blob/main/qlib/workflow/task/gen.py"
SELECTION_METHOD = "https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html"


def raw_components(cached, native, old_directions):
    if (cached.index.names != ["date", "symbol"] or cached.index.has_duplicates
            or cached.columns.tolist() != list(FAMILIES)
            or not np.isfinite(cached.to_numpy()).all()):
        raise ValueError("Original finite common-pool monthly family cache required")
    signs = {name: old_directions[name]["sign"] for name in ("RSQR30", "WVMA20")}
    if any(sign not in (-1, 1) for sign in signs.values()) or native.index.has_duplicates:
        raise ValueError("Original reversible direction binding required")
    aligned = native.reindex(cached.index)
    if not np.isfinite(aligned[["RSQR30", "WVMA20"]].to_numpy()).all():
        raise ValueError("Native components must exist for every ORIGINAL common-pool member")
    ranked = aligned[["RSQR30", "WVMA20"]].groupby(level="date").rank(pct=True)
    ranked -= ranked.groupby(level="date").transform("mean")
    # Centered rank(-x) == -centered rank(x), including average-rank ties.
    # Invert the old signed blend rather than learn from its full-year directions.
    result = ranked.copy()
    result["risk"] = cached.risk
    result["momentum"] = 2 * cached.trend - signs["RSQR30"] * ranked.RSQR30
    if (not np.allclose(cached.volume_stability, signs["WVMA20"] * ranked.WVMA20, atol=1e-12, rtol=0)
            or result.momentum.abs().max() > .5 + 1e-12
            or result.groupby(level="date").mean().abs().to_numpy().max() > 1e-12):
        raise ValueError("Cached family geometry cannot be inverted on this common pool")
    return result[list(RAW_COLUMNS)].sort_index()


def monthly_evidence(values, labels):
    rows = []
    for day, batch in values.groupby(level="date", sort=True):
        batch = batch.droplevel("date")
        target = labels.xs(day, level="date").reindex(batch.index)
        known = target.notna()
        if known.sum() < MIN_PAIRS or not np.isfinite(target.loc[known]).all():
            raise ValueError("Insufficient finite mature monthly labels")
        ranks = batch.rank(pct=True)
        for name in batch.columns:
            signal = batch[name]
            ic = signal.loc[known].corr(target.loc[known], method="spearman") if signal.loc[known].nunique() > 1 else np.nan
            # Quintiles belong to the complete feature pool, before endpoint availability.
            low, high = target.loc[ranks[name].le(.2)], target.loc[ranks[name].gt(.8)]
            spread = high.mean() - low.mean() if high.notna().any() and low.notna().any() else np.nan
            rows.append({"date": day, "component": name, "rank_ic": ic, "q5_minus_q1": spread,
                         "known_labels": int(known.sum()), "unknown_labels": int((~known).sum())})
    return pd.DataFrame(rows)


def evidence_gate(rows, *, learn_direction=False):
    usable = rows.dropna(subset=["rank_ic", "q5_minus_q1"])
    mean_ic = float(usable.rank_ic.mean()) if len(usable) else None
    sign = (-1 if mean_ic is not None and mean_ic < 0 else 1) if learn_direction else 1
    spread = float(sign * usable.q5_minus_q1.mean()) if len(usable) else None
    positive_fraction = float((sign * usable.rank_ic > 0).mean()) if len(usable) else None
    selected = (len(usable) >= MIN_MONTHS and mean_ic is not None and sign * mean_ic > 0
                and spread > 0 and positive_fraction > .5)
    return {"sign": sign, "selected": bool(selected), "observed_months": len(usable),
            "raw_mean_rank_ic": mean_ic, "oriented_mean_q5_minus_q1": spread,
            "oriented_positive_ic_fraction": positive_fraction}


def family_geometry(raw, directions):
    result = pd.DataFrame(index=raw.index)
    result["risk"] = raw.risk
    result["trend"] = ((raw.momentum + directions["RSQR30"]["sign"] * raw.RSQR30) / 2
                       if directions["RSQR30"]["selected"] else raw.momentum)
    result["volume_stability"] = directions["WVMA20"]["sign"] * raw.WVMA20
    return result[list(FAMILIES)]


def fit_fold(raw, forward, calendar, day):
    calendar = pd.DatetimeIndex(calendar)
    day = pd.Timestamp(day)
    if (raw.index.names != ["date", "symbol"] or raw.index.has_duplicates
            or raw.columns.tolist() != list(RAW_COLUMNS) or not np.isfinite(raw.to_numpy()).all()
            or not calendar.is_unique or not calendar.is_monotonic_increasing or day not in calendar
            or calendar.get_loc(day) < 1):
        raise ValueError("Distinct finite raw common-pool features and full pinned calendar required")
    cutoff = calendar[calendar.get_loc(day) - 1]
    anchors = raw.index.get_level_values("date").unique().sort_values()
    if not anchors.isin(calendar).all() or day not in anchors:
        raise ValueError("Decision features must be pinned calendar anchors")
    eligible, split_rows, parts = [], [], []
    for anchor in anchors[(anchors >= pd.Timestamp(TRAIN_START)) & (anchors < day)]:
        location = calendar.get_loc(anchor) + HORIZON + 1
        exit_day = calendar[location] if location < len(calendar) else None
        row = {"model_date": day, "date": anchor, "exit_date": exit_day,
               "labeled_rows": 0, "missing_labels": None, "purged_cross_boundary": 0}
        symbols = raw.xs(anchor, level="date").index
        if exit_day is None or exit_day > cutoff:
            row["purged_cross_boundary"] = len(symbols)
        else:
            observed = forward.loc[anchor].reindex(symbols)
            if np.isinf(observed.to_numpy()).any():
                raise ValueError("Infinite mature label is not a missing endpoint")
            row.update(labeled_rows=int(observed.notna().sum()), missing_labels=int(observed.isna().sum()))
            if row["labeled_rows"] < MIN_PAIRS:
                raise ValueError("Insufficient observed mature labels; do not delete a training month")
            eligible.append(anchor)
            parts.append(pd.Series(observed.to_numpy(), index=pd.MultiIndex.from_product(
                [[anchor], symbols], names=["date", "symbol"]), name="forward_return"))
        split_rows.append(row)
    if len(eligible) < MIN_MONTHS:
        return None, {"date": day.strftime("%Y-%m-%d"), "matured_months": len(eligible),
                      "reason": "original_minimum8_matured_months"}, pd.DataFrame(split_rows)
    labels = pd.concat(parts).sort_index()
    past = raw.loc[raw.index.get_level_values("date").isin(eligible)]
    component_stats = monthly_evidence(past[["RSQR30", "WVMA20"]], labels)
    directions = {name: evidence_gate(component_stats.loc[component_stats.component.eq(name)], learn_direction=True)
                  for name in ("RSQR30", "WVMA20")}
    geometry = family_geometry(past, directions)
    family_stats = monthly_evidence(geometry, labels)
    selection = {name: evidence_gate(family_stats.loc[family_stats.component.eq(name)]) for name in FAMILIES}
    selection["volume_stability"]["selected"] &= directions["WVMA20"]["selected"]
    selected = [name for name in FAMILIES if selection[name]["selected"]]
    observed = labels.notna()
    training = geometry.loc[observed].copy()
    target = labels.loc[observed]
    target = target - target.groupby(level="date").transform("mean")
    weights = 1 / target.groupby(level="date").transform("size")
    current = family_geometry(raw.xs(day, level="date"), directions)
    for name in set(FAMILIES) - set(selected):
        training[name] = 0.
        current[name] = 0.
    model = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky")
    model.fit(training, target, sample_weight=weights)
    current["expected_return"] = model.predict(current[list(FAMILIES)])
    if not selected:
        current["expected_return"] = 0.
    # Same mature labels/weights/alpha; a simpler prediction control, not a tuned strategy.
    risk = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky")
    risk.fit(past.loc[observed, ["risk"]], target, sample_weight=weights)
    current["risk_control_prediction"] = risk.predict(raw.xs(day, level="date")[["risk"]])
    if not np.isfinite(current.to_numpy()).all():
        raise ValueError("Nonfinite historical pipeline prediction")
    metadata = {"date": day.strftime("%Y-%m-%d"), "training_cutoff": cutoff.strftime("%Y-%m-%d"),
                "last_training_exit": max(row["exit_date"] for row in split_rows if row["labeled_rows"] > 0),
                "training_months": len(eligible), "training_rows": len(training), "native_directions": directions,
                "family_selection": selection, "selected_families": selected,
                "coefficients": dict(zip(FAMILIES, model.coef_)), "intercept": model.intercept_,
                "risk_control_coefficient": float(risk.coef_[0]), "risk_control_intercept": float(risk.intercept_),
                "uncertainty_calibrated": False, "decision_labels_used_in_fit": False}
    return current, metadata, pd.DataFrame(split_rows)


def evaluate_fold(current, labels, day, holdings):
    if current.index.has_duplicates or len(current) < holdings:
        raise ValueError("Complete current pool and fixed head size required")
    labels = labels.reindex(current.index)
    known = labels.notna()
    if known.sum() < MIN_PAIRS or not np.isfinite(labels.loc[known]).all():
        raise ValueError("Insufficient evaluation endpoints; outcomes remain unknown")
    target = labels - labels.loc[known].mean()
    head = current.reset_index().sort_values(["expected_return", "symbol"], ascending=[False, True], kind="stable").head(holdings).symbol
    selected = target.reindex(head)
    mse = float(((current.loc[known, "expected_return"] - target.loc[known]) ** 2).mean())
    risk_mse = float(((current.loc[known, "risk_control_prediction"] - target.loc[known]) ** 2).mean())
    zero_mse = float((target.loc[known] ** 2).mean())
    ic = (current.loc[known, "expected_return"].corr(target.loc[known], method="spearman")
          if current.loc[known, "expected_return"].nunique() > 1 else None)
    result = {"date": day.strftime("%Y-%m-%d"), "feature_rows": len(current), "known_labels": int(known.sum()),
              "unknown_labels": int((~known).sum()), "mse": mse, "zero_mse": zero_mse, "risk_control_mse": risk_mse,
              "mse_skill_vs_zero": 1 - mse / zero_mse if zero_mse else None,
              "mse_skill_vs_risk_control": 1 - mse / risk_mse if risk_mse else None,
              "rank_ic": ic, "prediction_std": float(current.expected_return.std(ddof=0)),
              "head_size": holdings, "head_known_labels": int(selected.notna().sum()),
              "complete_head_relative_label": float(selected.mean()) if selected.notna().all() else None,
              "head_is_tradable_pnl": False}
    panel = current.copy()
    panel["relative_label"], panel["head_selected"] = target, current.index.isin(head)
    return result, panel


def summarize_folds(folds):
    mse = float(np.mean([row["mse"] for row in folds]))
    zero = float(np.mean([row["zero_mse"] for row in folds]))
    risk = float(np.mean([row["risk_control_mse"] for row in folds]))
    return {"monthly_units": len(folds), "known_pair_rows": sum(row["known_labels"] for row in folds),
            "unknown_pair_rows": sum(row["unknown_labels"] for row in folds),
            "equal_month_mse_skill_vs_zero": 1 - mse / zero if zero else None,
            "equal_month_mse_skill_vs_risk_control": 1 - mse / risk if risk else None,
            "mean_month_rank_ic": float(np.mean([row["rank_ic"] for row in folds if row["rank_ic"] is not None])),
            "positive_ic_months": sum(row["rank_ic"] is not None and row["rank_ic"] > 0 for row in folds),
            "complete_heads": sum(row["head_known_labels"] == row["head_size"] for row in folds),
            "uncertainty_calibrated": False, "fresh_holdout_claimed": False}


class PipelineReturnFeed(SavedReturnFeed):
    def __init__(self, *args, rights_replay_policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rights_replay_policy = rights_replay_policy

    def rights_issue_receipt(self, reference, quantity, context):
        return waiver_receipt(reference, quantity, context, self.rights_replay_policy)

    def signal_frame(self, day, config, context):
        frame = super().signal_frame(day, config, context)
        return SignalFrame(day, frame.signals, ID, context) if isinstance(frame, SignalFrame) else frame


def report(out, record):
    lines = ["# Mature-Label Family Pipeline", "", record["status"] + "; NO_TRADE.",
             "Each historical fit learns native direction, family inclusion and coefficients using ONLY prior matured labels.",
             "Candidate definitions/pool/policy were human-chosen after viewing these periods: NOT fresh independent validation.", "",
             "|Decision|Mature training months|Selected families|Rank IC|MSE skill vs zero|Head labels known /20|",
             "|---|---:|---|---:|---:|---:|"]
    for model, fold in zip(record.get("models", []), record.get("folds", [])):
        ic = f"{fold['rank_ic']:.4f}" if fold["rank_ic"] is not None else "unknown"
        skill = f"{fold['mse_skill_vs_zero']:.4f}" if fold["mse_skill_vs_zero"] is not None else "unknown"
        lines.append(f"|{model['date']}|{model['training_months']}|{','.join(model['selected_families']) or 'none'}|"
                     f"{ic}|{skill}|{fold['head_known_labels']}/{fold['head_size']}|")
    lines += ["", "Equal-month diagnostic summary:", json.dumps(clean(record.get("diagnostics", {})), ensure_ascii=False),
              "", "## Sole Ledger", "",
              "|Case|Status / sessions|Net|MDD|Sharpe|Fees CNY|", "|---|---|---:|---:|---:|---:|"]
    for row in [*record.get("results", []), *([record["saved_control"]] if record.get("saved_control") else [])]:
        if row["status"] == "completed" and row.get("comparable_under_current_sources", True):
            lines.append(f"|{row['signal']}|completed/{row['completed_sessions']}|{row['net_return']:.4%}|"
                         f"{abs(row['max_drawdown']):.4%}|{row['sharpe_zero_rf_252']:.4f}|{row['fees']:.2f}|")
        else:
            lines.append(f"|{row['signal']}|{row['status']}/{row.get('completed_sessions', 0)}|unknown|unknown|unknown|unknown|")
            lines.append(str(row.get("error") or row.get("comparison_error")))
    lines += ["", "## Boundaries", "",
              "- Fixed minimum8 mature months,100 observed labels/month,20-session next-open endpoints, alpha1 and equal-month loss. No window/alpha/cost/seed search.",
              "- Native signs use equal-month mean Rank IC; inclusion requires positive oriented mean IC, positive quintile spread and strictly more than half positive IC months. Not a significance test or independent inner validation.",
              "- RSQR30 may enter the existing trend family only if it passes; otherwise trend is the original positive momentum hypothesis. Risk is the original low-volatility hypothesis. No label-based reversal of these economic hypotheses.",
              "- Family gates use the same mature training months; all preprocessing/selection precedes evaluation. Qlib rolling/truncation and sklearn selection-bias guidance are principles only, NOT exact reproduction or nested hyperparameter CV.",
              "- Old full2024 directions are used ONLY to algebraically invert saved centered ranks. Raw quality/volume ranks come from the original common pool BEFORE future labels; ties preserve the inverse identity.",
              "- Definitions were selected in prior viewed research. Full718 candidate discovery, historical PIT/source reconciliation, industry/style risks and uncertainty calibration remain unvalidated.",
              "- Monthly label means/rank IC/MSE/head means are provider-adjusted diagnostics, NOT net portfolio returns, market benchmark excess or black-swan protection.",
              "- Missing head outcomes remain unknown, never promote a lower-ranked stock. Securities/months are dependent, not thousands of independent samples.",
              "- Ledger option uses the existing Engine/Ledger, fixed return-only legal-capacity top20 and ORIGINAL140000CNY/costs. Saved static return account is contextual whole-pipeline control, not a cost-only experiment.",
              f"- Rights replay policy: {record.get('protocol', {}).get('rights_replay_policy') or 'none'}; any explicit waiver is a research assumption, not a broker/account receipt.",
              "- No account/default/worker/native THS/live order changes. No native client operation; physical Esc stop remains effective.",
              f"- Public rolling principle: {METHOD}", f"- Selection-bias principle: {SELECTION_METHOD}",
              f"- Artifacts: `{record['artifacts']}`."]
    if record.get("error"):
        lines += ["", record["error"]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare_actions(identifier, primary_view, max_fresh=32):
    """Batch archived/new terms for saved forecast heads and actual held blockers."""
    from .action_events import amend_secondary
    from .action_terms import collect

    record = json.loads((ROOT / "outputs/family_pipeline" / identifier / "run.json").read_text(encoding="utf-8"))
    if record.get("status") != "failed_mature_month_pipeline_ledger" or record.get("purpose") != ID:
        raise ValueError("Saved unresolved fixed pipeline ledger required for action preparation")
    if not 0 <= max_fresh <= 64:
        raise ValueError("At most64 fresh public-source histories per batch")
    inputs = record["protocol"]["stock_inputs"]
    source = json.loads((Path(primary_view) / "result.json").read_text(encoding="utf-8"))
    current = json.loads((Path(inputs["event_view"]) / "result.json").read_text(encoding="utf-8"))
    if Path(source["raw_view"]).resolve() != Path(current["raw_view"]).resolve():
        raise ValueError("Action terms must use the original primary-only raw scope")
    forecasts = pd.read_parquet(Path(record["artifacts"]) / "monthly_return_forecasts.parquet").reset_index()
    heads = forecasts.sort_values(["date", "expected_return", "symbol"], ascending=[True, False, True], kind="stable").groupby("date").head(40)
    members = {day: set(batch.symbol) for day, batch in heads.groupby("date")}
    anchors = sorted(members)
    folder = Path(record["artifacts"]) / SIGNAL
    state = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
    actions = pd.read_parquet(Path(inputs["event_view"]) / "canonical_actions.parquet").fillna("")
    pending = actions.loc[actions.dividOperateDate.between(record["results"][0]["attempted_session"], END)
                          & actions.dividReserveToStockPs.eq("")]
    keys = {(row.symbol, row.dividOperateDate) for row in pending.itertuples()
            if row.symbol in held or any(row.symbol in members[anchor]
                for anchor in [day for day in anchors if day <= pd.Timestamp(row.dividOperateDate)][-1:])}
    archived, failed = {}, set()
    for path in sorted((DATA_ROOT / "research/secondary_action_terms").glob("*/result.json"), reverse=True):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("status") != "completed_secondary_action_terms" or receipt.get("execution_admitted") is not False:
            continue
        for row in receipt["records"]:
            if row["status"] == "source_histories_compared":
                for candidate in row["candidates"]:
                    key = (candidate["symbol"], candidate["date"])
                    if key in keys:
                        archived.setdefault(key, path.parent)
            else:
                failed.add(row["symbol"])
    grouped = {}
    for key, root in archived.items():
        grouped.setdefault(root, set()).add(key)
    remaining = keys - set(archived)
    blocked = {key for key in remaining if key[0] in failed}
    fresh = sorted({symbol for symbol, _ in remaining - blocked}, key=lambda symbol: (symbol not in held, symbol))[:max_fresh]
    if fresh:
        grouped[None] = {key for key in remaining - blocked if key[0] in fresh}
    destination = DATA_ROOT / "research/family_pipeline_actions" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    preparation = {"status": "running", "source_run": identifier, "steps": [], "demand_events": sorted(keys),
                   "fresh_symbols": fresh, "blocked_prior_failed_events": sorted(blocked), "execution_admitted": False,
                   "basis": "Saved head40 in each event month plus actual held positions; data demand only, never eligibility filtering"}
    write_json(destination / "result.json", preparation)
    event_view, reference_view = inputs["event_view"], inputs["reference_view"]
    matched = set()
    for root, wanted in grouped.items():
        batch, receipt = collect(primary_view, sorted({symbol for symbol, _ in wanted}), reuse_root=root, retain_archived=False)
        observed = {(candidate["symbol"], candidate["date"]) for row in receipt["records"]
                    if row["status"] == "source_histories_compared" for candidate in row["candidates"]} & wanted
        if observed:
            event_view, reference_view, _, _ = amend_secondary(event_view, reference_view, batch, sorted(observed))
            matched.update(observed)
        preparation["steps"].append({"archive": str(root) if root else None, "collected": str(batch), "matched_events": sorted(observed)})
        preparation.update(event_view=str(event_view), reference_view=str(reference_view), matched_events=sorted(matched))
        write_json(destination / "result.json", preparation)
    preparation.update(status="completed_fixed_pipeline_action_preparation", unmatched_events=sorted(keys - matched))
    write_json(destination / "result.json", preparation)
    print(json.dumps({"output": str(destination), "matched_events": len(matched), "remaining": len(keys - matched),
                      "event_view": str(event_view), "reference_view": str(reference_view)}, ensure_ascii=False), flush=True)
    return destination, preparation


def run(source_id, *, ledger=False, resume_run=None, event_view=None, reference_view=None,
        rights_replay_policy=None):
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_return_cost_diagnostics" or source.get("execution_admitted") is not False
            or source.get("sessions") != 424 or source["start"] != START or source["end"] != END):
        raise ValueError("Complete original return/cost research source required")
    expanding = json.loads(Path(source["protocol"]["source_run"]).read_text(encoding="utf-8"))
    static = json.loads(Path(expanding["source_run"]).read_text(encoding="utf-8"))
    previous = (json.loads((ROOT / "outputs/family_pipeline" / resume_run / "run.json").read_text(encoding="utf-8"))
                if resume_run else None)
    if previous is not None and (not ledger or previous.get("status") != "failed_mature_month_pipeline_ledger"
                                 or previous.get("source_run") != source_id or previous.get("purpose") != ID):
        raise ValueError("Continue only this fixed pipeline's terminal failed historical ledger")
    if (event_view is None) != (reference_view is None):
        raise ValueError("Event and reference views must be supplied together")
    inputs = bind_stock_inputs(previous["protocol"]["stock_inputs"] if previous else source["protocol"]["stock_inputs"],
                               event_view, reference_view)
    rights_contract = rights_replay_contract(rights_replay_policy)
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    policy = EqualSlotReturnPolicy(False)
    if static["protocol"]["portfolio"] != config.to_dict():
        raise ValueError("Saved common pool must use the original portfolio basis")
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/family_pipeline" / identifier
    artifacts = DATA_ROOT / "research/family_pipeline" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": ID, "stock_inputs": inputs, "portfolio": config.to_dict(), "source_run": str(source_path),
                "cached_features": source["protocol"]["cached_features"], "families": list(FAMILIES),
                "training_start": TRAIN_START, "horizon": HORIZON, "alpha": ALPHA,
                "minimum_mature_months": MIN_MONTHS, "minimum_pairs": MIN_PAIRS, "month_loss_weight": 1,
                "native_direction_selection": "mature_mean_IC_sign; positive_mean_IC_and_Q5_Q1_and_majority_IC",
                "family_selection": "positive_mean_IC_and_Q5_Q1_and_majority_IC_on_mature_months_only",
                "portfolio_policy": policy.contract(config), "ledger_requested": ledger,
                "rights_replay_policy": rights_contract,
                "parameters_searched": False, "fresh_holdout_claimed": False, "execution_admitted": False,
                "candidate_discovery_whole_pipeline_validated": False, "selection_precedes_future_labels": True}
    if previous is not None:
        for key, value in previous["protocol"].items():
            if key not in {"stock_inputs", "stock_manifest"} and protocol.get(key) != value:
                raise ValueError(f"Saved pipeline contract changed: {key}")
        for key in ("price_view", "native_view", "listing_master"):
            if previous["protocol"]["stock_inputs"][key] != inputs[key]:
                raise ValueError(f"Frozen input changed: {key}")
    record = {"id": identifier, "purpose": ID, "status": "running", "artifacts": str(artifacts),
              "source_run": source_id, "protocol": protocol, "models": [], "folds": [], "skipped": [], "results": [],
              "execution_admitted": False, "strategy_validated": False, "sklearn_version": version("scikit-learn")}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    stocks, begin = None, time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, None)
        if previous is None:
            cached = pd.read_parquet(protocol["cached_features"])
            native = read_native_monthends(stocks, inputs["native_view"], cached.index.get_level_values("date").unique())
            raw = raw_components(cached, native, static["protocol"]["training_directions"])
            raw.to_parquet(artifacts / "monthly_raw_components.parquet")
            _, _, provider = native_binding(stocks, inputs["native_view"], catalog())
            calendar = pd.to_datetime(stocks.calendar)
            forward = forward_open_labels(read_open_panel(provider, stocks.symbols, calendar), HORIZON)
            predictions, evaluations, splits = {}, {}, []
            for day in raw.index.get_level_values("date").unique():
                current, model, split = fit_fold(raw, forward, calendar, day)
                if not split.empty:
                    splits.append(split)
                if current is None:
                    record["skipped"].append(model)
                    continue
                fold, panel = evaluate_fold(current, forward.loc[day], day, config.holdings)
                record["models"].append(model)
                record["folds"].append(fold)
                predictions[day], evaluations[day] = current, panel
                print(f"{model['date']}: {model['training_months']} mature months; {','.join(model['selected_families']) or 'none'}; IC={fold['rank_ic']}", flush=True)
            if not predictions:
                raise ValueError("No eligible full-pipeline chronological folds")
            forecasts = pd.concat(predictions, names=["date", "symbol"]).sort_index()
            forecasts = forecasts.loc[forecasts.index.get_level_values("date") >= pd.Timestamp(START)]
            forecasts.to_parquet(artifacts / "monthly_return_forecasts.parquet")
            pd.concat(evaluations, names=["date", "symbol"]).sort_index().to_parquet(artifacts / "evaluation_pairs.parquet")
            pd.concat(splits, ignore_index=True).to_csv(artifacts / "training_splits.csv", index=False)
            write_json(artifacts / "models.json", record["models"])
            record["diagnostics"] = summarize_folds(record["folds"])
            record["annual_diagnostics"] = {str(year): summarize_folds([row for row in record["folds"] if row["date"].startswith(str(year))])
                                           for year in (2024, 2025, 2026)}
        else:
            old = Path(previous["artifacts"])
            old.resolve().relative_to((DATA_ROOT / "research/family_pipeline").resolve())
            for name in ("monthly_raw_components.parquet", "monthly_return_forecasts.parquet", "evaluation_pairs.parquet",
                         "training_splits.csv", "models.json"):
                shutil.copy2(old / name, artifacts / name)
            for key in ("models", "folds", "skipped", "diagnostics", "annual_diagnostics"):
                record[key] = previous[key]
            forecasts = pd.read_parquet(artifacts / "monthly_return_forecasts.parquet")
            record.update(resume_run=resume_run, model_refit=False)
        record.update(prediction_rows=len(forecasts), symbols=len(stocks.symbols) - len(stocks.unsupported_instruments),
                      status="completed_mature_month_pipeline_diagnostics", account_created=False)
        write_json(out / "run.json", record)
        dates = [day for day in stocks.calendar if START <= day <= END]
        feed = PipelineReturnFeed(stocks, forecasts, config, dates,
                                  rights_replay_policy=rights_replay_policy)
        record["saved_forecasts_feed_validated"] = True
        no_edge = [row["date"] for row in record["models"] if row["date"] >= START and not row["selected_families"]]
        if ledger and no_edge:
            record["ledger_not_run_reason"] = "No mature training family edge; zero forecasts must not arbitrarily buy alphabetical stocks"
            record["no_edge_months"] = no_edge
        elif ledger:
            folder = artifacts / SIGNAL
            checkpoint = None
            if previous is not None:
                checkpoint = load_replay_checkpoint(Path(previous["artifacts"]) / SIGNAL, config, dates,
                                                    portfolio_policy_contract=policy.contract(config),
                                                    rights_replay_policy=rights_replay_policy)
                validate_prefix_inputs(source["protocol"]["stock_manifest"] if "stock_manifest" not in previous["protocol"]
                                       else previous["protocol"]["stock_manifest"], stocks.manifest,
                                       checkpoint["state"]["day"], checkpoint["state"])
            protocol["stock_manifest"] = stocks.manifest
            result = {"signal": SIGNAL, "source_artifacts": str(folder),
                      **baseline_ledger_result(feed, config, dates, folder, portfolio_policy=policy,
                                               portfolio_policy_contract=policy.contract(config), checkpoint=checkpoint)}
            record["account_created"] = True
            result["selection"] = selection_summary(folder)
            if result["completed_sessions"]:
                result["annual_same_account"] = stage_metrics(folder, config.initial_cash)
            record["results"].append(result)
            original = next(row for row in source["results"] if row["signal"] == "stock_family_return_top20")
            record["saved_control"] = {**original, "replayed": False, "source_run": str(source_path)}
            state = json.loads((Path(original["source_artifacts"]) / "account.json").read_text(encoding="utf-8"))
            try:
                validate_prefix_inputs(source["protocol"]["stock_manifest"], stocks.manifest, END, state)
                record["saved_control"]["comparable_under_current_sources"] = True
            except ValueError as error:
                record["saved_control"].update(comparable_under_current_sources=False, comparison_error=str(error))
            if result["status"] == "completed":
                record["status"] = "completed_mature_month_pipeline_ledger"
                if record["saved_control"]["comparable_under_current_sources"]:
                    record["comparison"] = {"net_difference_pp": (result["net_return"] - original["net_return"]) * 100,
                                            "mdd_magnitude_difference_pp": (abs(result["max_drawdown"]) - abs(original["max_drawdown"])) * 100}
            else:
                record["status"] = "failed_mature_month_pipeline_ledger"
    except Exception as error:
        record.update(status="failed_mature_month_pipeline_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prior-matured direction, family selection, return refit and saved-future diagnostics")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source-run")
    mode.add_argument("--prepare-actions", metavar="FAILED_RUN")
    parser.add_argument("--primary-view")
    parser.add_argument("--max-fresh", type=int, default=32)
    parser.add_argument("--ledger", action="store_true", help="Also use the original sole-ledger return-only top20, never a live account")
    parser.add_argument("--resume-run")
    parser.add_argument("--event-view")
    parser.add_argument("--reference-view")
    parser.add_argument("--rights-replay-policy", choices=["waive"],
                        help="Explicit unbound historical assumption for unresolved rights issues")
    args = parser.parse_args(argv)
    if args.prepare_actions:
        if args.primary_view is None or args.ledger or args.resume_run or args.event_view or args.reference_view:
            parser.error("Action preparation needs --primary-view and no replay options")
        prepare_actions(args.prepare_actions, args.primary_view, args.max_fresh)
        return 0
    if args.primary_view is not None:
        parser.error("--primary-view is only for action preparation")
    if (args.event_view is None) != (args.reference_view is None):
        parser.error("--event-view and --reference-view must be supplied together")
    _, record = run(args.source_run, ledger=args.ledger, resume_run=args.resume_run,
                    event_view=args.event_view, reference_view=args.reference_view,
                    rights_replay_policy=args.rights_replay_policy)
    return 0 if record["status"].startswith("completed_") else 1
