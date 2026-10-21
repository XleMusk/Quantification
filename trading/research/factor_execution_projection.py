"""Project one completed factor replay onto the supported main-board universe.

This is a fixed-score execution projection, not a refit, tuning pass, or fresh
holdout. It reuses the completed replay's monthly scores and sends only the
main-board subset through the same BulkResearchFeed and Engine/Ledger.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import time
import uuid

import numpy as np
import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_replay import CandidateRankFeed
from .family_portfolios import END, START
from .low_risk import portfolio
from .monthly_timing import month_ends


SCOPE = "mainboard_only"
SIGNAL = "stock_factor_candidate_ridge_mainboard_only"
_MAINBOARD = re.compile(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)")
_HELD_ACTION_ERROR = re.compile(
    r"Unresolved held corporate action:\s*(?P<symbol>[A-Za-z0-9.]+)\s+"
    r"(?P<decision_date>\d{4}-\d{2}-\d{2});\s*missing=(?P<missing>[^;]+)"
)


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _resolve_replay(value, score_artifact=None):
    candidate = Path(value)
    run_path = candidate if candidate.name == "run.json" else ROOT / "outputs" / "factor_candidate_replays" / str(value) / "run.json"
    if not run_path.exists():
        raise FileNotFoundError(f"factor candidate replay not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    if record.get("status") != "completed_factor_candidate_replay":
        raise ValueError("A completed factor candidate replay is required")
    artifacts = Path(record["artifacts"])
    scores = Path(score_artifact).resolve() if score_artifact else artifacts / "monthly_scores.parquet"
    if not scores.exists():
        raise FileNotFoundError(f"Fixed monthly score artifact not found: {scores}")
    return run_path.resolve(), record, scores.resolve()


def _read_scores(path):
    scores = pd.read_parquet(path)
    if scores.index.names != ["date", "symbol"] or scores.columns.tolist() != ["score"]:
        raise ValueError("Monthly score artifact must have the fixed date/symbol/score schema")
    values = pd.to_numeric(scores["score"], errors="coerce")
    if not np.isfinite(values).all():
        raise ValueError("Monthly score artifact contains non-finite scores")
    scores = scores.copy()
    scores["score"] = values.astype(float)
    scores.index = pd.MultiIndex.from_arrays(
        [pd.to_datetime(scores.index.get_level_values("date")),
         scores.index.get_level_values("symbol").astype(str)],
        names=["date", "symbol"],
    )
    return scores.sort_index()


def _classify_blocker(error):
    """Turn a terminal replay error into a resumable research work item."""
    message = str(error)
    match = _HELD_ACTION_ERROR.search(message)
    if match:
        missing = [field.strip() for field in match.group("missing").split(",") if field.strip()]
        return {
            "kind": "unresolved_held_corporate_action",
            "symbol": match.group("symbol"),
            "decision_date": match.group("decision_date"),
            "missing_fields": missing,
            "message": message,
            "resume_policy": "resume_after_prior_successful_session_once_source_evidence_is_added",
            "requires_source_evidence": True,
            "execution_admitted": False,
        }
    return {
        "kind": "projection_error",
        "message": message,
        "resume_policy": "inspect_error_before_any_rerun",
        "requires_source_evidence": False,
        "execution_admitted": False,
    }


def _save_mainboard_rankings(scores, base, config, dates, folder):
    folder.mkdir(parents=True, exist_ok=False)
    source_symbols = set(base.symbols)
    rows, counts = [], []
    for day in month_ends(base.calendar, END):
        if not START <= day <= END:
            continue
        timestamp = pd.Timestamp(day)
        try:
            frame = scores.xs(timestamp, level="date").reset_index()
        except KeyError as error:
            raise ValueError(f"Fixed score artifact is missing decision date {day}") from error
        frame = frame.loc[frame.symbol.isin(source_symbols)].copy()
        total = len(frame)
        frame = frame.loc[frame.symbol.map(lambda symbol: bool(_MAINBOARD.fullmatch(symbol)))].copy()
        frame = frame.sort_values(["score", "symbol"], ascending=[False, True], kind="stable")
        if len(frame) < config.retain_rank:
            raise ValueError(f"Only {len(frame)} main-board scores on {day}; need {config.retain_rank}")
        counts.append({"date": day, "source_candidates": total,
                       "mainboard_candidates": len(frame),
                       "excluded_non_mainboard": total - len(frame),
                       "retain_rank": config.retain_rank})
        rows.extend({"date": day, "rank": rank, "symbol": row.symbol, "score": row.score}
                    for rank, row in enumerate(frame.head(config.retain_rank).itertuples(index=False), 1))
    ranking = pd.DataFrame(rows, columns=["date", "rank", "symbol", "score"])
    ranking.to_csv(folder / "rankings.csv", index=False)
    pd.DataFrame(counts).to_csv(folder / "scope_counts.csv", index=False)
    return counts


def _report(out, record):
    result = record.get("result") or {}
    source = record.get("source_result") or {}
    if result.get("status") == "completed":
        performance = (f"net {result['net_return']:.2%}, MDD {abs(result['max_drawdown']):.2%}, "
                       f"Sharpe {result['sharpe_zero_rf_252']:.4f}, fees {result['fees']:.2f} CNY")
    else:
        performance = f"ledger status {result.get('status')}: {result.get('error', 'unknown')}"
    source_performance = (
        f"net {source.get('net_return', float('nan')):.2%}, "
        f"MDD {abs(source.get('max_drawdown', float('nan'))):.2%}, "
        f"Sharpe {source.get('sharpe_zero_rf_252', float('nan')):.4f}"
    )
    counts = pd.DataFrame(record["scope_counts"])
    lines = [
        "# Factor Execution Projection",
        "",
        "Fixed-score main-board projection; NO_TRADE.",
        f"Score label: `{record.get('score_label')}`.",
        f"Source replay: `{record['source_replay']}`.",
        f"Decision period: {START}..{END}; scope: `{SCOPE}`.",
        f"Projected ledger: {performance}.",
        f"Source ledger: {source_performance}.",
        "",
        "## What ran",
        "",
        f"- Reused the completed replay's frozen monthly scores; no refit, tuning, new provider data or new factor membership.",
        f"- Kept the existing 20 holdings / 30 rank buffer / same costs and Engine/Ledger.",
        f"- Rebuilt {len(counts)} monthly rank files from the full score artifact, then filtered to `60xxxx.SSE` and `00xxxx.SZSE`.",
        f"- Median main-board capacity before selection: {int(counts.mainboard_candidates.median())} names; minimum: {int(counts.mainboard_candidates.min())}.",
        "",
        "## Limits",
        "",
        "- This isolates an execution-universe constraint; it does not prove live readiness or improve the underlying score model.",
        "- The source is still research-only, with the same reconstructed-data and unresolved-action limits as the source replay.",
        "- THS physical-client integration remains paused; no account observation, quote read, order, or fill was performed.",
        f"- Artifacts: `{record['artifacts']}`.",
    ]
    blocker = record.get("blocker")
    if blocker:
        lines.extend([
            "",
            "## Structured blocker",
            "",
            f"- Kind: `{blocker['kind']}`.",
            f"- Message: `{blocker['message']}`.",
            f"- Resume policy: `{blocker['resume_policy']}`.",
            f"- Source evidence required: `{str(blocker['requires_source_evidence']).lower()}`.",
        ])
        if blocker.get("symbol"):
            lines.append(f"- Held security: `{blocker['symbol']}` on `{blocker['decision_date']}`.")
            lines.append(f"- Missing fields: `{', '.join(blocker.get('missing_fields', []))}`.")
        lines.append("- This artifact remains research-only and execution-admission remains false.")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_replay, rights_replay_policy=None, merger_replay_policy=None,
        event_view=None, reference_view=None, score_artifact=None, score_label=None):
    source_path, source, score_path = _resolve_replay(source_replay, score_artifact=score_artifact)
    score_name = score_label or source.get("result", {}).get("signal", SIGNAL)
    projection_signal = score_name + "_mainboard_only"
    identifier = _new_id()
    out = ROOT / "outputs" / "factor_execution_projections" / identifier
    artifacts = DATA_ROOT / "research" / "factor_execution_projections" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    source_inputs = dict(source["stock_inputs"])
    if (event_view is None) != (reference_view is None):
        raise ValueError("Event/reference overrides must be supplied together")
    if event_view is not None:
        source_inputs["event_view"] = str(Path(event_view).resolve())
        source_inputs["reference_view"] = str(Path(reference_view).resolve())
    source_portfolio = source["portfolio"]
    config = portfolio(initial_cash=int(source_portfolio["initial_cash"]))
    if config.to_dict() != source_portfolio:
        raise ValueError("Projection must reuse the source portfolio contract exactly")
    record = {
        "id": identifier,
        "status": "running",
        "research_family": "factor_execution_projection",
        "scope": SCOPE,
        "score_label": score_name,
        "signal": projection_signal,
        "source_replay": str(source_path),
        "source_score_artifact": str(score_path),
        "stock_inputs": source_inputs,
        "action_view_override": {
            "event_view": source_inputs["event_view"] if event_view is not None else None,
            "reference_view": source_inputs["reference_view"] if reference_view is not None else None,
        },
        "start": START,
        "end": END,
        "portfolio": config.to_dict(),
        "fresh_holdout_claimed": False,
        "strategy_validated": False,
        "execution_admitted": False,
        "account_backtest": True,
        "rights_replay_policy": rights_replay_policy,
        "merger_replay_policy": merger_replay_policy,
        "artifacts": str(artifacts),
        "result": None,
        "blocker": None,
    }
    write_json(out / "run.json", record)
    stocks = None
    started = time.monotonic()
    try:
        stocks = BulkResearchFeed(source_inputs["price_view"], source_inputs["event_view"],
                                  source_inputs["reference_view"], source_inputs["listing_master"])
        dates = [day for day in stocks.calendar if START <= day <= END]
        scores = _read_scores(score_path)
        counts = _save_mainboard_rankings(scores, stocks, config, dates, artifacts / "predictions")
        ranked = CandidateRankFeed(
            stocks, artifacts / "predictions", projection_signal, config, dates,
            rights_replay_policy=rights_replay_policy, merger_replay_policy=merger_replay_policy,
        )
        result = {"signal": projection_signal,
                  **baseline_ledger_result(ranked, config, dates, artifacts / SIGNAL)}
        record["scope_counts"] = counts
        record["result"] = result
        record["source_result"] = source.get("result") or {}
        if result.get("status") != "completed":
            record["blocker"] = _classify_blocker(result.get("error", "ledger projection failed"))
        record["status"] = "completed_factor_execution_projection" if result["status"] == "completed" else "failed_factor_execution_ledger"
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        record.update(status="failed_factor_execution_projection", error=error_text,
                      blocker=_classify_blocker(error_text))
    finally:
        if stocks is not None:
            stocks.close()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if record.get("blocker"):
            write_json(out / "blocker.json", record["blocker"])
            write_json(artifacts / "blocker.json", record["blocker"])
        write_json(out / "run.json", record)
        if "scope_counts" in record:
            _report(out, record)
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-replay", required=True, help="completed factor candidate replay run.json or run ID")
    parser.add_argument("--rights-replay-policy", choices=["waive"],
                        help="explicit unbound historical assumption for unresolved rights issues")
    parser.add_argument("--merger-replay-policy", choices=["floor", "ceil"],
                        help="explicit integer remainder policy for source-backed successor transfers")
    parser.add_argument("--event-view", type=Path,
                        help="research-only amended action view; must be paired with --reference-view")
    parser.add_argument("--reference-view", type=Path,
                        help="research-only reference view paired with --event-view")
    parser.add_argument("--score-artifact", type=Path,
                        help="optional frozen date/symbol/score parquet from another completed fixed replay")
    parser.add_argument("--score-label",
                        help="label for an overridden score artifact")
    args = parser.parse_args(argv)
    _, record = run(args.source_replay, rights_replay_policy=args.rights_replay_policy,
                    merger_replay_policy=args.merger_replay_policy,
                    event_view=args.event_view, reference_view=args.reference_view,
                    score_artifact=args.score_artifact, score_label=args.score_label)
    return 0 if record["status"] == "completed_factor_execution_projection" else 1


if __name__ == "__main__":
    raise SystemExit(main())
