"""One fixed execution-policy diagnostic, reusing completed full-source rankings."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from .. import replay_reports, settings
from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..entry_risk import RAW_FLOOR1_AND_NOTICES
from ..notice_risk import NoticeRiskView
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, write_json
from .stock_defensive import stage_metrics
from .weekly_reversal import NOTICE_VARIANTS, WeeklyFeed, decision_days


class SavedWeeklyFeed(WeeklyFeed):
    """Top retain-rank is sufficient for unchanged top20/rank30 selection."""
    def __init__(self, base, source, source_artifacts, variant, config):
        self.base, self.config, self.variant = base, config, variant
        self.entry_risk_policy = RAW_FLOOR1_AND_NOTICES
        self.risk_notice_view = NoticeRiskView(source["protocol"]["entry_risk_policy"]["notice_view"]["snapshot"])
        if self.risk_notice_view.binding != source["protocol"]["entry_risk_policy"]["notice_view"]:
            raise ValueError("Source risk-notice binding changed")
        self.fractional_allocation = source["protocol"]["fractional_allocation"]
        self.audit = []
        ranks = pd.read_csv(source_artifacts / "source_target_ranks.csv", dtype={"date": str, "symbol": str})
        ranks = ranks.loc[ranks.variant.eq(variant)].copy()
        decisions = pd.read_csv(source_artifacts / (variant + "_decisions.csv"), dtype={"date": str})
        self.planned = {}
        groups = {day: rows.sort_values("rank") for day, rows in ranks.groupby("date")}
        if decisions.date.tolist() != [day for day in base.calendar if source["start"] <= day <= source["end"]]:
            raise ValueError("Source decisions do not cover the complete original calendar")
        if set(groups) - set(decision_days(base.calendar)):
            raise ValueError("Saved ranks contain a non-decision day")
        for row in decisions.to_dict("records"):
            if row["intent"] == "NO_CHANGE":
                self.planned[row["date"]] = (row["decision"], None, None)
                continue
            ranked = groups.get(row["date"], pd.DataFrame(columns=ranks.columns))
            expected = min(config.retain_rank, int(row["candidate_count"]))
            if ranked["rank"].tolist() != list(range(1, expected + 1)) or ranked.symbol.duplicated().any():
                raise ValueError("Source top retain-rank is incomplete or duplicated")
            candidates = ranked.drop(columns=["date", "variant", "rank"]).to_dict("records")
            self.planned[row["date"]] = (row["decision"], int(row["eligible_count"]), candidates)


def run(identifier):
    source, source_artifacts = replay_reports._read(identifier)
    if (source.get("status") != "completed_weekly_reversal_diagnostic"
            or source.get("research_family") != "weekly_reversal"
            or source["protocol"]["signals"] != list(NOTICE_VARIANTS)
            or source["protocol"]["selection_is_point_in_time"] is not False):
        raise ValueError("Require the completed fixed weekly notice pair; no arbitrary source or parameter search")
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    if config.commission <= 0:
        raise ValueError("Commission breakpoint requires a positive fee rate")
    controls = [replay_reports.detail(identifier, signal) for signal in NOTICE_VARIANTS]
    if any(control["config"] != config.to_dict() for control in controls):
        raise ValueError("Both controls must have the unchanged common strategy/cost configuration")
    inputs = source["inputs"]
    base = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/market_replay" / run_id
    artifacts = settings.DATA_ROOT / "research/market_replay" / run_id
    try:
        if any(base.manifest[key] != inputs[key] for key in ("price_view", "event_view", "reference_view", "raw_view", "listing_master")):
            raise ValueError("Cost-aware case requires exactly the control's pinned sources")
        dates = [day for day in base.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["sessions"]:
            raise ValueError("Control and candidate calendars differ")
        variants = [signal + "_cost_band" for signal in NOTICE_VARIANTS]
        out.mkdir(parents=True, exist_ok=False)
        artifacts.mkdir(parents=True, exist_ok=False)
        protocol = {**source["protocol"], "id": "weekly_minimum_commission_deadband_v1", "signals": variants,
            "signal_portfolio_configs": {signal: config.to_dict() for signal in variants},
            "control_run": identifier, "execution_policy": {
                "id": "minimum_commission_internal_reweights_v1",
                "minimum_internal_trade_cny": config.minimum_commission / config.commission,
                "derivation": "Configured minimum commission / proportional commission rate; NOT optimized on returns",
                "exemptions": "New entries, complete/forced exits, and sells when marked single/gross limits are exceeded",
                "limits": "Only internal rebalance friction targeted. Rank substitutions, unaffordable entry lots, alpha decay and risk exposure remain unchanged"},
            "basis": "Already-inspected historical development experiment. Changes actual account path; NOT a zero-cost addback or untouched holdout",
            "source_ranks": str(source_artifacts / "source_target_ranks.csv"),
            "source_rank_basis": "Reused full-source top30, sufficient for unchanged top20/rank30 selection. No stock-budget scan or future action-quality selection",
            "fresh_holdout_claimed": False, "execution_admitted": False}
        record = {"status": "running", "research_family": "cost_aware_execution",
            "purpose": "fixed_internal_rebalance_cost_diagnostic", "dataset": source["dataset"],
            "artifacts": str(artifacts), "scope": source["scope"], "symbols": source["symbols"],
            "start": dates[0], "end": dates[-1], "sessions": len(dates), "inputs": inputs,
            "protocol": protocol, "results": [], "strategy_validated": False, "live_ready": False}
        write_json(out / "protocol.json", protocol)
        write_json(out / "run.json", record)
        try:
            for signal, control in zip(NOTICE_VARIANTS, controls):
                feed = SavedWeeklyFeed(base, source, source_artifacts, signal, config)
                variant = signal + "_cost_band"
                print(f"Replay {variant}: {len(dates)} sessions; fixed band {config.minimum_commission / config.commission:.2f}CNY", flush=True)
                result = baseline_ledger_result(feed, config, dates, artifacts / variant, commission_deadband=True)
                result.update(signal=variant, label=variant, control_case=identifier + ":" + signal,
                              control_metrics=control["metrics"])
                if protocol.get("fractional_allocation"):
                    result.update(fractional_allocation_assumption=protocol["fractional_allocation"],
                                  label=variant + " / fractional shares " + protocol["fractional_allocation"]["rounding"] + " ASSUMPTION")
                if result["status"] == "completed":
                    result["annual_same_account"] = stage_metrics(artifacts / variant, config.initial_cash)
                account = json.loads((artifacts / variant / "account.json").read_text(encoding="utf-8"))
                result["deferred_internal_reweights"] = dict(Counter(event["side"] for event in account["events"]
                    if event["kind"] == "rebalance_deferred"))
                record["results"].append(result)
                pd.DataFrame(feed.audit).to_csv(artifacts / (variant + "_decisions.csv"), index=False)
                write_json(out / "run.json", record)
                print(f"{variant}: {result['status']}; sessions={result['completed_sessions']}; net={result.get('net_return')}", flush=True)
            record["status"] = "completed_cost_aware_diagnostic" if all(r["status"] == "completed" for r in record["results"]) else "failed_cost_aware_diagnostic"
        except Exception as error:
            record.update(status="failed_cost_aware_diagnostic", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            write_json(out / "run.json", record)
        print(str(out), flush=True)
        return out, record
    finally:
        base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Completed weekly control run ID")
    args = parser.parse_args(argv)
    run(args.run)
    return 0
