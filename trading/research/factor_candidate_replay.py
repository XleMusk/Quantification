"""Fit the fixed candidate factor pack and replay it through the shared ledger.

The candidate pack is selected from training diagnostics only.  This module
does one fixed Ridge fit on 2024 month-end rows, scores 2025-2026 month-ends,
and sends the resulting rank buffers through the existing stock Engine/Ledger.
It is a development comparison, not a holdout or execution-admission path.
"""

import argparse
from datetime import datetime
from types import SimpleNamespace
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import dated_eligibility, native_binding
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, write_json
from .factor_families import forward_open_labels, read_open_panel
from .factor_library_extension import read_field, score360
from .factors import candidate_batches, catalog
from .family_portfolios import END, START
from .low_risk import portfolio
from .monthly_timing import month_ends
from ..rights_issue import waiver_receipt
from .successor_transfers import apply_replay_successor_transfers


HORIZON = 20
TRAIN_START = "2024-01-02"
TRAIN_END = "2024-12-31"
MIN_PAIRS = 100
MIN_TRAIN_MONTHS = 8
ALPHA = 1.0
WINDOWS = (5, 10, 20, 30, 60)
SIGNAL = "stock_factor_candidate_ridge"


class CandidateRankFeed(SavedRankFeed):
    """Rank replay with an explicit, unbound historical rights policy only."""

    def __init__(self, *args, rights_replay_policy=None, merger_replay_policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rights_replay_policy = rights_replay_policy
        self.merger_replay_policy = merger_replay_policy

    def validate_session(self, state, day):
        # Apply only source-backed successor mappings before the base feed's
        # ordinary action and delisting checks.  This remains replay-only.
        apply_replay_successor_transfers(
            state, day, self.base.symbols, self.merger_replay_policy)
        return self.base.validate_session(state, day)

    def rights_issue_receipt(self, reference, quantity, context):
        return waiver_receipt(reference, quantity, context, self.rights_replay_policy)


def _resolve_pack(value):
    candidate = Path(value)
    run_path = candidate if candidate.name == "run.json" else ROOT / "outputs/factor_candidate_packs" / str(value) / "run.json"
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate pack not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    if record.get("status") != "completed_research_candidate_pack":
        raise ValueError("Completed research-only factor candidate pack required")
    factor_file = run_path.parent / "candidate_factors.csv"
    if not factor_file.exists() and record.get("data_artifacts"):
        factor_file = Path(record["data_artifacts"]) / "candidate_factors.csv"
    candidates = pd.read_csv(factor_file)
    if len(candidates) != record.get("selected_factors") or candidates.factor.duplicated().any():
        raise ValueError("Candidate pack count or factor identities changed")
    if not candidates.candidate_decision.eq("selected_cluster_representative").all():
        raise ValueError("Candidate pack contains non-selected rows")
    source_path = Path(record["source_run"])
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if (source.get("status") != "completed_research_diagnostics"
            or source.get("definitions_completed") != 718
            or source.get("fresh_holdout_claimed") is not False):
        raise ValueError("Candidate source must remain the completed diagnostic-only 718 run")
    return run_path, record, candidates, source_path, source


def _read_native_monthly(stocks, native_view, dates, columns):
    definitions = catalog()
    native_view, receipt, provider = native_binding(stocks, native_view, definitions)
    columns = list(dict.fromkeys(columns))
    known = set(definitions.name)
    if not set(columns) <= known:
        raise ValueError(f"Native factor columns missing from Alpha158 catalog: {sorted(set(columns) - known)}")
    wanted_dates = pd.DatetimeIndex(pd.to_datetime(dates))
    pieces = []
    for part in receipt["partitions"]:
        path = (native_view / part["file"]).resolve()
        path.relative_to(native_view)
        frame = pd.read_parquet(path, columns=columns)
        if frame.index.names != ["date", "symbol"] or frame.index.has_duplicates:
            raise ValueError("Native feature partition identity changed")
        frame = frame.loc[frame.index.get_level_values("date").isin(wanted_dates)]
        pieces.append(frame)
    result = pd.concat(pieces).sort_index()
    if result.index.has_duplicates or result.columns.tolist() != columns:
        raise ValueError("Native monthly candidate features are not distinct and ordered")
    return result, provider


def _stack_monthly(values):
    result = values.stack(future_stack=True)
    result.index = result.index.set_names(["date", "symbol"])
    return result


def _alpha360_features(candidates, provider, symbols, calendar, dates):
    selected = candidates.loc[candidates.library.eq("Alpha360")].copy()
    if selected.empty:
        return pd.DataFrame(index=pd.MultiIndex.from_arrays([[], []], names=["date", "symbol"]))
    close = read_field(provider, "close", symbols, calendar)
    volume = read_field(provider, "volume", symbols, calendar)
    pieces = {}
    for family, group in selected.groupby("family", sort=True):
        source = read_field(provider, str(family).lower(), symbols, calendar)
        for row in group.itertuples(index=False):
            item = SimpleNamespace(original_name=row.original_name, formula=row.formula)
            score = score360(source, close, volume, item).loc[pd.DatetimeIndex(pd.to_datetime(dates))]
            pieces[row.factor] = _stack_monthly(score)
    return pd.DataFrame(pieces).sort_index()


def _template_features(candidates, native_monthly, provider, symbols, calendar, dates):
    selected = candidates.loc[candidates.library.eq("Templates")]
    if selected.empty:
        return pd.DataFrame(index=pd.MultiIndex.from_arrays([[], []], names=["date", "symbol"]))
    close = read_field(provider, "close", symbols, calendar)
    returns = close.pct_change(fill_method=None)
    # Keep the rolling calculation in a date x symbol matrix; only the small
    # month-end slice enters the candidate feature frame.
    month_index = pd.DatetimeIndex(pd.to_datetime(dates))
    retvol_parts = {
        f"RETVOL{window}": returns.rolling(window, min_periods=window).std(ddof=1).loc[month_index]
        for window in WINDOWS
    }
    base = native_monthly.join(pd.DataFrame({name: _stack_monthly(values)
                                             for name, values in retvol_parts.items()}), how="left")
    wanted = {"Templates/" + name for name in selected.original_name}
    pieces = []
    for values, definitions in candidate_batches(base, WINDOWS):
        names = ["Templates/" + name for name in definitions.name]
        values = values.copy()
        values.columns = names
        keep = [name for name in names if name in wanted]
        if keep:
            pieces.append(values[keep])
    if not pieces:
        raise ValueError("Selected template definitions were not generated")
    return pd.concat(pieces, axis=1).sort_index()


def build_monthly_features(stocks, source, candidates, dates):
    native_names = sorted(set(candidates.loc[candidates.library.eq("Alpha158"), "original_name"])
                          | set(candidates.loc[candidates.library.eq("Templates"), "original_name"])
                          & set(catalog().name))
    template_base = sorted({f"{family}{window}"
                            for family in ("ROC", "VMA", "RSV", "BETA", "CORR")
                            for window in WINDOWS})
    native_names = sorted(set(native_names) | set(template_base))
    native, provider = _read_native_monthly(stocks, source["bulk_inputs"]["native_view"], dates, native_names)
    alpha = native[[name for name in candidates.loc[candidates.library.eq("Alpha158"), "original_name"]
                    if name in native.columns]].copy()
    alpha.columns = ["Alpha158/" + name for name in alpha.columns]
    alpha360 = _alpha360_features(candidates, provider, stocks.symbols, pd.to_datetime(stocks.calendar), dates)
    templates = _template_features(candidates, native[template_base], provider, stocks.symbols,
                                   pd.to_datetime(stocks.calendar), dates)
    frames = [frame for frame in (alpha, alpha360, templates) if not frame.empty]
    if not frames:
        raise ValueError("Candidate pack has no supported factor library")
    result = pd.concat(frames, axis=1).sort_index()
    wanted = candidates.factor.tolist()
    if result.columns.tolist() != sorted(result.columns.tolist()) or set(result.columns) != set(wanted):
        result = result.reindex(columns=wanted)
    if result.columns.duplicated().any():
        raise ValueError("Candidate factor feature columns are duplicated")
    return result, provider


def oriented_rank_features(raw, candidates):
    columns = {}
    for row in candidates.itertuples(index=False):
        values = pd.to_numeric(raw[row.factor], errors="coerce")
        if row.train_direction == "low":
            values = -values
        ranks = values.groupby(level="date", sort=False).rank(pct=True)
        columns[row.factor] = ranks - 0.5
    return pd.DataFrame(columns, index=raw.index)


def fit_training(features, forward, calendar):
    locations = {day: i for i, day in enumerate(calendar)}
    train_days = [day for day in features.index.get_level_values("date").unique()
                  if pd.Timestamp(TRAIN_START) <= day <= pd.Timestamp(TRAIN_END)]
    rows, split = [], []
    for day in sorted(train_days):
        i = locations[day]
        exit_i = i + HORIZON + 1
        symbols = features.loc[day].index
        observed = forward.loc[day].reindex(symbols).to_numpy(dtype=float)
        finite = np.isfinite(observed)
        if exit_i >= len(calendar) or calendar[exit_i] > pd.Timestamp(TRAIN_END):
            split.append({"date": day, "pairs": int(finite.sum()), "used": False, "reason": "label_crosses_train_cutoff"})
            continue
        if int(finite.sum()) < MIN_PAIRS:
            split.append({"date": day, "pairs": int(finite.sum()), "used": False, "reason": "insufficient_pairs"})
            continue
        day_mask = ((features.index.get_level_values("date") == day)
                    & features.index.get_level_values("symbol").isin(symbols[finite]))
        part = features.loc[day_mask].copy()
        target = pd.Series(observed[finite], index=part.index)
        target = target.groupby(level="date").rank(pct=True)
        target = target - target.mean()
        part["target_rank"] = target
        part["sample_weight"] = 1 / len(part)
        rows.append(part)
        split.append({"date": day, "pairs": int(finite.sum()), "used": True, "reason": "matured_label"})
    if len(rows) < MIN_TRAIN_MONTHS:
        raise ValueError(f"Only {len(rows)} matured training month-ends; need {MIN_TRAIN_MONTHS}")
    train = pd.concat(rows).sort_index()
    medians = train.drop(columns=["target_rank", "sample_weight"]).median().fillna(0)
    model = Ridge(alpha=ALPHA, fit_intercept=True, solver="cholesky")
    model.fit(train[medians.index].fillna(medians), train.target_rank,
              sample_weight=train.sample_weight)
    return model, medians, train, pd.DataFrame(split)


def save_rankings(scores, folder, config, dates):
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    for day in dates:
        frame = scores.loc[day].dropna().rename("score").reset_index()
        frame = frame.sort_values(["score", "symbol"], ascending=[False, True], kind="stable")
        if len(frame) < config.retain_rank:
            raise ValueError(f"Only {len(frame)} finite scores on {day}; need {config.retain_rank}")
        rows.extend({"date": day.strftime("%Y-%m-%d"), "rank": i, **row}
                    for i, row in enumerate(frame.head(config.retain_rank).to_dict("records"), 1))
    ranking = pd.DataFrame(rows)
    ranking.to_csv(folder / "rankings.csv", index=False)
    return ranking


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def report(out, record):
    result = record.get("result") or {}
    if result.get("status") == "completed":
        performance = (f"net {result['net_return']:.2%}, MDD {abs(result['max_drawdown']):.2%}, "
                       f"Sharpe {result['sharpe_zero_rf_252']:.4f}, fees {result['fees']:.2f} CNY")
    else:
        performance = f"ledger status {result.get('status')}: {result.get('error', 'unknown')}"
    lines = [
        "# Factor Candidate Ridge Replay", "", "Research development only; NO_TRADE.",
        f"Candidate pack: `{record['candidate_run']}`; {record['candidate_count']} factors.",
        f"Fixed training: {TRAIN_START}..{TRAIN_END}; horizon {HORIZON}; Ridge alpha {ALPHA}; no CV/grid.",
        f"Evaluation ledger: {START}..{END}; {performance}.",
        f"Rights replay policy: `{record.get('rights_replay_policy') or 'none'}`; any waiver is an explicit historical assumption, not account evidence.", "",
        f"Successor transfer replay policy: `{record.get('merger_replay_policy') or 'none'}`; mapping and ratio are source-backed, but integer remainder allocation is an explicit unbound historical assumption.", "",
        "## What this proves", "",
        "The selected 190-factor pack now reaches a real model fit, monthly rank buffer and the existing Engine/Ledger.",
        "The result is a same-system development diagnostic only. It is not an independent holdout, current signal, broker observation or execution admission.",
        "2026 observations remain development evidence because the snapshot cutoff is 2026-09-30 and this run was initiated after inspecting that data.", "",
        "## Limits", "",
        "- Candidate membership was fixed from training diagnostics; validation/test performance did not choose factors.",
        "- The 2024 training year is short and the underlying full-source reconstruction is research-only, not PIT-complete.",
        "- No industry/size neutralization, independent post-cutoff snapshot or black-swan acceptance was claimed.",
        f"- Saved features, rankings, model and ledger artifacts: `{record['artifacts']}`.",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(candidate_run, reuse_feature_run=None, rights_replay_policy=None,
        event_view=None, reference_view=None, merger_replay_policy=None):
    pack_path, pack, candidates, source_path, source = _resolve_pack(candidate_run)
    inputs = dict(source["bulk_inputs"])
    if (event_view is None) != (reference_view is None):
        raise ValueError("Candidate replay event/reference overrides must be supplied together")
    if event_view is not None:
        inputs["event_view"] = str(Path(event_view).resolve())
        inputs["reference_view"] = str(Path(reference_view).resolve())
    identifier = _new_id()
    out = ROOT / "outputs" / "factor_candidate_replays" / identifier
    artifacts = DATA_ROOT / "research" / "factor_candidate_replays" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    config = portfolio()
    record = {
        "id": identifier, "status": "running", "research_family": "factor_candidate_ridge",
        "candidate_run": str(pack_path), "candidate_count": int(len(candidates)),
        "source_run": str(source_path), "stock_inputs": inputs, "start": START, "end": END,
        "training_period": [TRAIN_START, TRAIN_END], "horizon": HORIZON, "alpha": ALPHA,
        "parameters_searched": False, "fresh_holdout_claimed": False,
        "account_backtest": True, "strategy_validated": False, "execution_admitted": False,
        "portfolio": config.to_dict(), "artifacts": str(artifacts), "result": None,
        "reused_feature_run": str(reuse_feature_run) if reuse_feature_run else None,
        "rights_replay_policy": rights_replay_policy,
        "merger_replay_policy": merger_replay_policy,
        "action_view_override": {
            "event_view": inputs["event_view"] if event_view is not None else None,
            "reference_view": inputs["reference_view"] if reference_view is not None else None,
        },
    }
    write_json(out / "run.json", record)
    stocks = None
    started = time.monotonic()
    try:
        stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        calendar = pd.to_datetime(stocks.calendar)
        dates = [day for day in stocks.calendar if START <= day <= END]
        month_dates = [day for day in month_ends(stocks.calendar, END) if day >= TRAIN_START]
        eval_dates = [pd.Timestamp(day) for day in month_dates if START <= day <= END]
        if reuse_feature_run:
            reuse_root = Path(reuse_feature_run)
            if reuse_root.name == "run.json":
                reuse_root = reuse_root.parent
            if not reuse_root.is_absolute():
                reuse_root = DATA_ROOT / "research" / "factor_candidate_replays" / str(reuse_feature_run)
            feature_path = reuse_root / "monthly_candidate_features.parquet"
            if not feature_path.exists():
                raise FileNotFoundError(f"Reusable candidate feature artifact not found: {feature_path}")
            raw = pd.read_parquet(feature_path)
            expected = candidates.factor.tolist()
            if raw.index.names != ["date", "symbol"] or raw.columns.tolist() != expected:
                raise ValueError("Reusable candidate features do not match the fixed candidate pack")
            _, _, provider = native_binding(stocks, inputs["native_view"], catalog())
        else:
            raw, provider = build_monthly_features(stocks, source, candidates, month_dates)
        eligibility = dated_eligibility(stocks, config, dates=month_dates)
        raw = raw.where(eligibility.reindex(raw.index, fill_value=False))
        features = oriented_rank_features(raw, candidates)
        features.to_parquet(artifacts / "monthly_candidate_features.parquet")
        opening = read_open_panel(provider, stocks.symbols, calendar)
        forward = forward_open_labels(opening, HORIZON)
        model, medians, train, split = fit_training(features, forward, calendar)
        train.to_parquet(artifacts / "training_rows.parquet")
        split.to_csv(artifacts / "training_split.csv", index=False)
        write_json(artifacts / "model.json", {
            "alpha": ALPHA, "horizon": HORIZON, "candidate_count": len(candidates),
            "training_months": int(train.index.get_level_values("date").nunique()),
            "training_rows": int(len(train)), "coefficients": dict(zip(candidates.factor, model.coef_)),
            "intercept": float(model.intercept_), "prediction_units": "relative cross-sectional rank score",
            "medians": medians.to_dict(), "execution_admitted": False,
        })
        evaluated = features.loc[features.index.get_level_values("date").isin(eval_dates)].copy()
        matrix = evaluated[medians.index].fillna(medians)
        predicted = pd.Series(model.predict(matrix), index=evaluated.index, name="score")
        predicted.to_frame().to_parquet(artifacts / "monthly_scores.parquet")
        prediction_folder = artifacts / "predictions"
        save_rankings(predicted, prediction_folder, config, eval_dates)
        ranked = CandidateRankFeed(stocks, prediction_folder, SIGNAL, config, dates,
                                   rights_replay_policy=rights_replay_policy,
                                   merger_replay_policy=merger_replay_policy)
        result = {"signal": SIGNAL, **baseline_ledger_result(ranked, config, dates, artifacts / SIGNAL)}
        record["result"] = result
        record["model"] = {"training_months": int(train.index.get_level_values("date").nunique()),
                            "training_rows": int(len(train)), "last_training_date": str(train.index.get_level_values("date").max())}
        record["status"] = "completed_factor_candidate_replay" if result["status"] == "completed" else "failed_factor_candidate_ledger"
    except Exception as error:
        record.update(status="failed_factor_candidate_replay", error=f"{type(error).__name__}: {error}")
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(out / "run.json", record)
        report(out, record)
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-run", required=True, help="factor candidate pack run ID or run.json")
    parser.add_argument("--reuse-feature-run", help="failed prior replay ID containing a compatible monthly feature artifact")
    parser.add_argument("--rights-replay-policy", choices=["waive"],
                        help="explicit unbound historical assumption for unresolved rights issues")
    parser.add_argument("--merger-replay-policy", choices=["floor", "ceil"],
                        help="explicit unbound integer remainder policy for source-backed successor transfers")
    parser.add_argument("--event-view", type=Path,
                        help="research-only amended action view; must be paired with --reference-view")
    parser.add_argument("--reference-view", type=Path,
                        help="research-only reference view paired with --event-view")
    args = parser.parse_args(argv)
    _, record = run(args.candidate_run, reuse_feature_run=args.reuse_feature_run,
                    rights_replay_policy=args.rights_replay_policy,
                    event_view=args.event_view, reference_view=args.reference_view,
                    merger_replay_policy=args.merger_replay_policy)
    return 0 if record["status"] == "completed_factor_candidate_replay" else 1


if __name__ == "__main__":
    raise SystemExit(main())
