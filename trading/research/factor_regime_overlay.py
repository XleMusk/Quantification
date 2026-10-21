"""Replay a fixed factor rank account with one predeclared breadth cap.

The overlay is intentionally narrow: it may reduce the gross target weight on
an existing factor target, but it cannot add names, raise single-name weights,
or alter the signal/rank source. This is a diagnostic for broad market stress,
not an optimized regime timer or an execution candidate.
"""

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .. import settings
from ..config import StrategyConfig
from ..contracts import PortfolioIntent
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .factor_candidate_family_replay import CandidateRankFeed


POLICY_ID = "median_breadth_20d_cap40_v1"
LOOKBACK = 20
BREADTH_THRESHOLD = 0.35
RISK_OFF_GROSS = 0.40
NORMAL_GROSS = 0.80

LIMITS = [
    "The breadth rule and thresholds are fixed before this replay; no 2025/2026 return search or tuning was performed",
    "The overlay only scales existing target weights and keeps the same candidate ranks, names, costs and Engine/Ledger",
    "The source snapshot ends 2026-09-30 and this account was inspected development data, not a fresh holdout",
    "Breadth is a source-covered cross-sectional proxy, not a complete market index or causal regime label",
    "PIT-complete inputs, prospective paper observation and broker verification remain absent; NO_TRADE",
]


def _resolve_run(value):
    candidate = Path(value)
    run_path = (candidate if candidate.name == "run.json"
                else settings.ROOT / "outputs" / "factor_candidate_family_replays" / str(value) / "run.json")
    if not run_path.exists():
        raise FileNotFoundError(f"factor-family replay not found: {run_path}")
    record = json.loads(run_path.read_text(encoding="utf-8"))
    if (record.get("status") != "completed_factor_candidate_family_replay"
            or record.get("execution_admitted") is not False
            or record.get("fresh_holdout_claimed") is not False
            or record.get("result", {}).get("status") != "completed"):
        raise ValueError("Completed unbound factor-family replay required")
    required = {"price_view", "event_view", "reference_view", "listing_master"}
    if not required <= set(record.get("inputs", {})):
        raise ValueError("Factor-family replay does not contain the complete pinned input manifest")
    return run_path, record


def breadth_caps(feed, dates, *, lookback=LOOKBACK, threshold=BREADTH_THRESHOLD):
    if type(lookback) is not int or lookback < 2:
        raise ValueError("lookback must be an integer >= 2")
    if not 0 < threshold < 1:
        raise ValueError("breadth threshold must be between zero and one")
    close = feed.closes.replace([np.inf, -np.inf], np.nan)
    rows = []
    for day in dates:
        history = close.loc[:day].tail(lookback + 1)
        if len(history) < lookback + 1:
            breadth = None
            cap = NORMAL_GROSS
            reason = "insufficient_lookback"
        else:
            first, last = history.iloc[0], history.iloc[-1]
            valid = first.gt(0) & last.gt(0) & first.notna() & last.notna()
            returns = last[valid] / first[valid] - 1
            breadth = float(returns.gt(0).mean()) if len(returns) else None
            risk_off = breadth is not None and breadth <= threshold
            cap = RISK_OFF_GROSS if risk_off else NORMAL_GROSS
            reason = "breadth_risk_off" if risk_off else "breadth_normal"
        rows.append({"date": day, "lookback_sessions": len(history) - 1,
                     "positive_20d_breadth": breadth, "gross_cap": cap, "reason": reason})
    return pd.DataFrame(rows)


class BreadthCapPolicy:
    def __init__(self, caps):
        self.caps = {str(row.date): float(row.gross_cap) for row in caps.itertuples(index=False)}

    def __call__(self, intent):
        if not isinstance(intent, PortfolioIntent) or intent.kind.value != "FULL_TARGET":
            raise ValueError("Breadth cap requires a replay full target")
        cap = self.caps.get(intent.execution_not_before, NORMAL_GROSS)
        weights = dict(intent.weights)
        gross = sum(weights.values())
        if gross <= cap + 1e-12:
            return intent
        scale = cap / gross
        return replace(intent,
                       weights=tuple((symbol, weight * scale) for symbol, weight in intent.weights),
                       reason=(intent.reason + ";" if intent.reason else "") + POLICY_ID)


def run(value):
    run_path, source = _resolve_run(value)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "factor_regime_overlays" / identifier
    artifacts = settings.DATA_ROOT / "research" / "factor_regime_overlays" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    config = StrategyConfig(**source["portfolio"]).validate()
    record = {"id": identifier, "status": "running",
              "research_family": "factor_breadth_regime_overlay",
              "source_run": str(run_path), "signal": source["result"]["signal"],
              "start": source["start"], "end": source["end"], "portfolio": config.to_dict(),
              "policy": {"id": POLICY_ID, "lookback_sessions": LOOKBACK,
                         "positive_breadth_threshold": BREADTH_THRESHOLD,
                         "risk_off_gross_limit": RISK_OFF_GROSS,
                         "normal_gross_limit": NORMAL_GROSS},
              "inputs": source["inputs"], "artifacts": str(artifacts),
              "execution_admitted": False, "fresh_holdout_claimed": False,
              "result": None, "limitations": LIMITS}
    write_json(out / "run.json", record)
    feed = None
    try:
        inputs = source["inputs"]
        feed = BulkResearchFeed(inputs["price_view"], inputs["event_view"],
                                inputs["reference_view"], inputs["listing_master"])
        dates = [day for day in feed.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["result"]["sessions"]:
            raise ValueError("Pinned source session count differs from factor-family replay")
        caps = breadth_caps(feed, dates)
        caps.to_csv(artifacts / "breadth_caps.csv", index=False)
        policy = BreadthCapPolicy(caps)
        prediction_folder = Path(source["artifacts"]) / "predictions"
        ranked = CandidateRankFeed(feed, prediction_folder, source["result"]["signal"], config, dates,
                                   rights_replay_policy=source.get("rights_replay_policy"),
                                   merger_replay_policy=source.get("merger_replay_policy"))
        contract = {"id": POLICY_ID, "config": config.to_dict()}
        result = baseline_ledger_result(ranked, config, dates, artifacts / source["result"]["signal"],
                                        portfolio_policy=policy,
                                        portfolio_policy_contract=contract)
        result.update(control_metrics=source["result"],
                      net_return_difference=(result.get("net_return") - source["result"]["net_return"])
                      if result.get("status") == "completed" else None,
                      max_drawdown_difference=(result.get("max_drawdown") - source["result"]["max_drawdown"])
                      if result.get("status") == "completed" else None,
                      sharpe_difference=(result.get("sharpe_zero_rf_252") - source["result"]["sharpe_zero_rf_252"])
                      if result.get("status") == "completed" else None,
                      risk_off_decision_days=int(caps.reason.eq("breadth_risk_off").sum()),
                      normal_decision_days=int(caps.reason.eq("breadth_normal").sum()))
        record["result"] = result
        record["status"] = "completed_factor_breadth_regime_overlay" if result["status"] == "completed" else "failed_factor_breadth_regime_overlay"
    except Exception as error:
        record.update(status="failed_factor_breadth_regime_overlay", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if feed is not None:
            feed.close()
        write_json(out / "run.json", record)
        result = record.get("result") or {}
        if result.get("status") == "completed":
            lines = ["# Fixed Factor Breadth Regime Overlay", "", "Research development only; `NO_TRADE`.", "",
                     f"Source: `{run_path}`; fixed 20-session breadth cap rule.",
                     f"Overlay result: net {result['net_return']:.2%}, MDD {result['max_drawdown']:.2%}, Sharpe {result['sharpe_zero_rf_252']:.4f}, fees CNY{result['fees']:.2f}.",
                     f"Control result: net {source['result']['net_return']:.2%}, MDD {source['result']['max_drawdown']:.2%}, Sharpe {source['result']['sharpe_zero_rf_252']:.4f}.",
                     f"Risk-off decision days: {result['risk_off_decision_days']}; normal decision days: {result['normal_decision_days']}.",
                     "This is one fixed broad-exposure diagnostic, not a promoted strategy or an independent validation.", "",
                     "## Limits", "", *["- " + item for item in LIMITS], "", f"Artifacts: `{artifacts}`", ""]
        else:
            lines = ["# Fixed Factor Breadth Regime Overlay", "", "Research development only; `NO_TRADE`.", "",
                     f"Replay status: {result.get('status', record.get('status'))}; {result.get('error', record.get('error', 'unknown'))}", "",
                     "## Limits", "", *["- " + item for item in LIMITS], "", f"Artifacts: `{artifacts}`", ""]
        (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed factor-family replay ID or run.json")
    args = parser.parse_args(argv)
    _, record = run(args.run)
    return 0 if record["status"] == "completed_factor_breadth_regime_overlay" else 1


if __name__ == "__main__":
    raise SystemExit(main())
