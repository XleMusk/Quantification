"""Replay a fixed Alpha158 rank-buffer variant through the sole Engine/Ledger.

This is a research consumer, not a new backtester.  It reuses the saved
all-seed score bundle, source data, dates and costs, changing only the
predeclared retained-rank buffer so turnover can be measured before adding
more features.
"""

import argparse
from datetime import datetime
from pathlib import Path
import uuid

from ..config import StrategyConfig
from .. import replay_reports
from ..settings import DATA_ROOT, ROOT
from .alpha158_linear import ID as ALPHA_ID, saved_score
from .bulk_feed import BulkResearchFeed
from .experiment import ScoreFeed, baseline_ledger_result, export_scores, write_json


SIGNAL = "alpha158_allseed_rank_mean"
PURPOSE = "alpha158_fixed_rank_buffer_v1"
BUFFER_RANK = 12


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _report(out, record):
    result = record.get("result") or {}
    base = record.get("baseline") or {}
    if result.get("status") == "completed":
        performance = (f"net {result['net_return']:.2%}, MDD {abs(result['max_drawdown']):.2%}, "
                       f"Sharpe {result['sharpe_zero_rf_252']:.4f}, fees {result['fees']:.2f} CNY")
    else:
        performance = f"ledger status {result.get('status')}: {result.get('error', 'unknown')}"
    lines = [
        "# Alpha158 Fixed Rank-Buffer Replay",
        "",
        "Research development only; NO_TRADE.",
        f"Source: `{record['source_run']}`; signal `{record['signal']}`.",
        f"Frozen window: `{record['start']}..{record['end']}`; sessions {record['sessions']}.",
        f"Variant: holdings {record['variant_config']['holdings']}, retained rank "
        f"{record['variant_config']['retain_rank']}, rebalance every "
        f"{record['variant_config']['rebalance_sessions']} sessions.",
        "Only the retained-rank buffer changes; model scores, universe, prices, "
        "corporate-action views and cost settings are reused.",
        f"Result: {performance}; turnover {result.get('turnover_value', 0):,.2f} CNY; "
        f"fills {result.get('fills', 0)}.",
        "",
        "| Metric | Baseline all-seed | Rank-buffer variant | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in (("net_return", "Net return"), ("max_drawdown", "Max drawdown"),
                       ("sharpe_zero_rf_252", "Sharpe zero rf"),
                       ("turnover_value", "Turnover CNY"), ("orders", "Orders"),
                       ("fills", "Fills"), ("fees", "Fees CNY")):
        left, right = base.get(key), result.get(key)
        delta = None if left is None or right is None else right - left
        if key in {"net_return", "max_drawdown", "sharpe_zero_rf_252"}:
            format_value = lambda value: "UNKNOWN" if value is None else f"{value:.4%}"
        elif key in {"turnover_value", "fees"}:
            format_value = lambda value: "UNKNOWN" if value is None else f"{value:,.2f}"
        else:
            format_value = lambda value: "UNKNOWN" if value is None else f"{int(value):,}"
        lines.append(f"| {label} | {format_value(left)} | {format_value(right)} | {format_value(delta)} |")
    lines += [
        "",
        "This is a fixed turnover experiment, not a parameter search, fresh holdout, "
        "strategy promotion or live-readiness claim.",
        f"Artifacts: `{record['artifacts']}`",
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(source_id):
    source, source_artifacts = replay_reports._read(source_id)
    if (source.get("purpose") != ALPHA_ID
            or source.get("status") != "completed_alpha158_linear_diagnostics"
            or source.get("execution_admitted") is not False):
        raise ValueError("A completed, research-only Alpha158 replay is required")
    result_row = next((row for row in source.get("results", []) if row.get("signal") == SIGNAL), None)
    if not result_row or result_row.get("status") != "completed":
        raise ValueError(f"Completed signal case not found: {SIGNAL}")

    base_config = StrategyConfig(**source["protocol"]["signal_portfolio_configs"][SIGNAL]).validate()
    variant_values = base_config.to_dict()
    variant_values["retain_rank"] = BUFFER_RANK
    variant_config = StrategyConfig(**variant_values).validate()
    inputs = source["protocol"]["stock_inputs"]
    score_path = source_artifacts / "predictions" / SIGNAL / "historical_scores.parquet"
    if not score_path.exists():
        raise FileNotFoundError(f"Saved Alpha158 scores are missing: {score_path}")

    identifier = _new_id()
    out = ROOT / "outputs" / "market_replay" / identifier
    artifacts = DATA_ROOT / "research" / "market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "purpose": PURPOSE,
        "research_family": "alpha158_turnover_buffer_ensemble",
        "status": "running",
        "source_run": str((ROOT / "outputs/market_replay" / source_id / "run.json").resolve()),
        "source_signal": SIGNAL,
        "signal": SIGNAL,
        "source_artifacts": str(source_artifacts),
        "artifacts": str(artifacts),
        "inputs": inputs,
        "predictions": str(score_path),
        "start": source["start"],
        "end": source["end"],
        "sessions": source["sessions"],
        "base_config": base_config.to_dict(),
        "variant_config": variant_config.to_dict(),
        "models_refit": False,
        "parameters_searched": False,
        "fresh_holdout_claimed": False,
        "execution_admitted": False,
    }
    write_json(out / "run.json", record)
    feed = None
    try:
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"],
                                inputs["reference_view"], inputs["listing_master"])
        dates = [day for day in feed.calendar if record["start"] <= day <= record["end"]]
        if len(dates) != record["sessions"]:
            raise ValueError("Source calendar changed while preparing turnover variant")
        score = saved_score(score_path)
        destination = artifacts / SIGNAL
        result = baseline_ledger_result(
            ScoreFeed(feed, export_scores(score)), variant_config, dates, destination)
        record["result"] = result
        record["baseline"] = {key: result_row.get(key) for key in
                              ("net_return", "max_drawdown", "sharpe_zero_rf_252",
                               "turnover_value", "orders", "fills", "fees")}
        record["status"] = "completed" if result.get("status") == "completed" else "failed"
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        _report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, help="Alpha158 linear replay run ID")
    args = parser.parse_args(argv)
    _, record = run(args.source_run)
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
