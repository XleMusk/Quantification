"""Freeze a family-pipeline candidate and gate later snapshots before replay."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from .. import settings
from ..config import StrategyConfig
from ..contracts import Signal, SignalFrame, SignalKind, SignalTime, SecurityId, SignalUnavailable, TradingMode, decision_time
from .bulk_feed import BulkResearchFeed
from .bulk_model_inputs import native_binding
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .factor_families import forward_open_labels, read_open_panel
from .factors import catalog
from .family_pipeline import ID as PIPELINE_ID, RAW_COLUMNS, fit_fold
from .family_portfolios import read_native_monthends
from .low_risk import features as low_risk_features
from .monthly_timing import month_ends
from .family_pipeline import SIGNAL
from .prospective_research import validate_extension
from ..report import metrics as portfolio_metrics
from ..rights_issue import waiver_receipt
from ..worker import lock


ID = "mature_month_family_time_split_holdout_v1"
LIMITS = [
    "Registration freezes a viewed candidate; it does not claim a fresh holdout or run a future account",
    "A later source must extend beyond the candidate end and preserve all involved historical inputs",
    "Candidate rights assumptions, source coverage and execution mode remain research-only",
    "A ready source still requires a separately implemented holdout replay; ready is not a return result",
    "No paper/live/broker admission and NO_TRADE",
]
MIN_HOLDOUT_SESSIONS = 60
MIN_HOLDOUT_DECISIONS = 2


def _plain(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError(f"{label} must be a short plain identifier")
    return value


def _candidate(run_id, signal):
    run_id = _plain(run_id, "candidate run")
    root = settings.ROOT / "outputs/family_pipeline" / run_id
    record_path = root / "run.json"
    if not record_path.exists():
        raise FileNotFoundError(f"Candidate run not found: {record_path}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if (record.get("status") != "completed_mature_month_pipeline_ledger"
            or record.get("execution_admitted") is not False
            or record.get("strategy_validated") is not False):
        raise ValueError("A completed unbound family-pipeline candidate is required")
    if record.get("protocol", {}).get("fresh_holdout_claimed") is not False:
        raise ValueError("Candidate must not already claim a fresh holdout")
    result = next((row for row in record.get("results", []) if row.get("signal") == signal), None)
    if result is None or result.get("status") != "completed":
        raise ValueError("Requested candidate signal is not a completed account")
    manifest = record.get("protocol", {}).get("stock_manifest")
    if not manifest or not manifest.get("end") or not manifest.get("replay_only"):
        raise ValueError("Candidate lacks a pinned replay-only stock manifest")
    return record, result, manifest


def _root(name):
    return settings.DATA_ROOT / "research/family_holdout" / _plain(name, "holdout name")


def _report(root, protocol, head):
    out = settings.ROOT / "outputs/family_holdout" / protocol["name"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", {**head, "protocol": str(root / "protocol.json"), "artifacts": str(root)})
    lines = ["# Family Pipeline Time-Split Holdout Gate", "", f"Status: {head['status']}. NO_TRADE.",
             f"Candidate: `{protocol['candidate_run']}:{protocol['candidate_signal']}`",
             f"Candidate end: `{protocol['candidate_end']}`; source cutoff checked: `{head.get('source_cutoff') or 'UNKNOWN'}`.",
             "The candidate is frozen. Any future account uses the same Engine/Ledger and starts from the saved candidate checkpoint.", "",
             "| Field | Value |", "| --- | --- |",
             f"| Candidate net return (viewed diagnostic) | {protocol['candidate_net_return']:.6%} |",
             f"| Candidate MDD (viewed diagnostic) | {protocol['candidate_mdd']:.6%} |",
             f"| Candidate Sharpe (viewed diagnostic) | {protocol['candidate_sharpe']} |",
             f"| Holdout status | {head['status']} |", "",
             "## Limits", "", *["- " + value for value in LIMITS], "",
             f"Artifacts: `{root}`", ""]
    if head.get("holdout_result"):
        result = head["holdout_result"]
        lines[8:8] = [f"| Holdout sessions | {result.get('holdout_sessions', 0)} |"]
        lines += ["", "## Holdout Replay", "",
                  f"New month-end decisions: {result.get('new_decision_count', 0)}; "
                  f"technical validation threshold: {MIN_HOLDOUT_SESSIONS} sessions / {MIN_HOLDOUT_DECISIONS} decisions.",
                  f"Strategy validation flag: `{head.get('strategy_validated', False)}`.",
                  "Returns below are historical replay evidence, not broker fills or live readiness.",
                  json.dumps(result.get("holdout_metrics"), ensure_ascii=False), ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def register(name, candidate_run, *, signal=SIGNAL, current=None):
    current = current or datetime.now().astimezone()
    record, result, manifest = _candidate(candidate_run, signal)
    root = _root(name)
    protocol = {"id": ID, "name": name, "registered_at": current.isoformat(),
                "candidate_run": candidate_run, "candidate_signal": signal,
                "candidate_end": manifest["end"],
                "candidate_manifest": manifest, "candidate_inputs": record["protocol"]["stock_inputs"],
                "candidate_rights_policy": record["protocol"].get("rights_replay_policy"),
                "candidate_net_return": result["net_return"], "candidate_mdd": result["max_drawdown"],
                "candidate_sharpe": result.get("sharpe_zero_rf_252"),
                "holdout_start_exclusive": manifest["end"], "holdout_replay_status": "not_started",
                "execution_admitted": False, "fresh_holdout_claimed": False, "parameters_searched": False,
                "limits": LIMITS}
    if (root / "protocol.json").exists():
        prior = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
        if prior != protocol:
            raise ValueError("Holdout name is already registered with a different candidate or policy")
        return root, json.loads((root / "head.json").read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=False)
    head = {"id": ID, "name": name, "status": "waiting_for_new_complete_snapshot",
            "source_cutoff": None, "holdout_sessions": 0, "execution_admitted": False,
            "fresh_holdout_claimed": False}
    write_json(root / "protocol.json", protocol)
    write_json(root / "head.json", head)
    _report(root, protocol, head)
    return root, head


def check(name, inputs=None, *, current=None):
    root = _root(name)
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    head = json.loads((root / "head.json").read_text(encoding="utf-8"))
    if head.get("status") == "holdout_replay_completed" or head.get("fresh_holdout_claimed") is True:
        raise ValueError("Completed holdout is immutable and cannot be checked again")
    candidate_inputs = protocol["candidate_inputs"]
    supplied = dict(candidate_inputs)
    if inputs is not None:
        supplied = json.loads(Path(inputs).read_text(encoding="utf-8"))
    price_view = Path(supplied["price_view"])
    receipt = json.loads((price_view / "result.json").read_text(encoding="utf-8"))
    if receipt.get("status") != "research_reconstruction_completed":
        raise ValueError("A completed canonical price snapshot is required")
    cutoff = receipt["cutoff"]
    candidate_end = protocol["candidate_end"]
    if cutoff <= candidate_end:
        head.update(status="waiting_for_new_complete_snapshot", source_cutoff=cutoff, holdout_sessions=0)
        head.pop("checked_inputs", None)
        head.pop("holdout_result", None)
        head.pop("strategy_validated", None)
        head.pop("error", None)
        write_json(root / "head.json", head)
        _report(root, protocol, head)
        return root, head
    required = ("price_view", "event_view", "reference_view", "listing_master", "native_view")
    if any(not supplied.get(key) for key in required):
        raise ValueError("Extended holdout check requires price/event/reference/listing inputs")
    feed = BulkResearchFeed(supplied["price_view"], supplied["event_view"], supplied["reference_view"],
                            supplied["listing_master"])
    try:
        validate_extension(protocol["candidate_manifest"], feed.manifest, candidate_end)
        head.update(status="holdout_source_ready", source_cutoff=cutoff, holdout_sessions=0,
                    checked_inputs=supplied)
        head.pop("error", None)
    except Exception as error:
        head.update(status="holdout_source_rejected", source_cutoff=cutoff,
                    error=f"{type(error).__name__}: {error}")
    finally:
        feed.close()
    write_json(root / "head.json", head)
    _report(root, protocol, head)
    return root, head


class _HoldoutPipelineFeed:
    """Dynamic-date score feed for the frozen pipeline's post-candidate prefix."""

    def __init__(self, base, forecasts, config, *, rights_replay_policy=None):
        self.base, self.config = base, config
        if forecasts.index.names != ["date", "symbol"] or forecasts.index.has_duplicates:
            raise ValueError("Holdout forecasts need a distinct date/symbol index")
        if not forecasts.index.is_monotonic_increasing or not np.isfinite(forecasts.expected_return.to_numpy()).all():
            raise ValueError("Holdout forecasts must be ordered and finite")
        if not set(forecasts.index.get_level_values("symbol")) <= set(base.symbols):
            raise ValueError("Holdout forecast symbols are outside the pinned source")
        self.forecasts = {day.strftime("%Y-%m-%d"): batch.droplevel("date").expected_return
                          for day, batch in forecasts.groupby(level="date", sort=True)}
        self.rights_replay_policy = rights_replay_policy

    def __getattr__(self, name):
        return getattr(self.base, name)

    def rights_issue_receipt(self, reference, quantity, context):
        return waiver_receipt(reference, quantity, context, self.rights_replay_policy)

    def signal_frame(self, day, config, context):
        if config != self.config or context.mode != TradingMode.REPLAY:
            raise ValueError("Holdout forecasts are fixed replay-only research")
        values = self.forecasts.get(day)
        if values is None:
            return SignalUnavailable("no_holdout_month_end_forecast")
        stamp = SignalTime(day + "T15:00:00+08:00", decision_time(day),
                           availability_evidence="historical_eod_assumption")
        signals = tuple(Signal(SecurityId(symbol), float(value), SignalKind.EXPECTED_RETURN, stamp,
                               horizon_sessions=20) for symbol, value in values.items())
        return SignalFrame(day, signals, PIPELINE_ID, context)


def _ranked_centered(frame):
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if frame.empty:
        return frame
    return frame.rank(pct=True).sub(frame.rank(pct=True).mean())


def _extended_raw(feed, native_view, candidate_raw, candidate_end, cutoff):
    """Keep the candidate raw prefix byte-for-byte and append fixed future month-end ranks."""
    raw = pd.read_parquet(candidate_raw)
    if raw.index.names != list(["date", "symbol"]) or raw.columns.tolist() != list(RAW_COLUMNS):
        raise ValueError("Frozen candidate raw family cache has changed")
    raw = raw.sort_index()
    future_days = [pd.Timestamp(day) for day in month_ends(feed.calendar, cutoff)
                   if pd.Timestamp(day) > raw.index.get_level_values("date").max()]
    if not future_days:
        return raw, []
    values = low_risk_features(feed, cutoff, feature_days=[day.strftime("%Y-%m-%d") for day in future_days])
    native = read_native_monthends(feed, native_view, future_days)
    appended = []
    for day in future_days:
        date_key = day.strftime("%Y-%m-%d")
        frame = pd.DataFrame({
            "risk": -values["volatility"].loc[date_key],
            "momentum": values["momentum"].loc[date_key],
        })
        frame = frame.join(native.xs(day, level="date"), how="inner")
        frame = _ranked_centered(frame[list(RAW_COLUMNS)])
        if not frame.empty:
            frame.index = pd.MultiIndex.from_product([[day], frame.index], names=["date", "symbol"])
            appended.append(frame)
    if not appended:
        raise ValueError("No finite future family feature rows were available")
    result = pd.concat([raw, *appended]).sort_index()
    if result.index.has_duplicates or not np.isfinite(result.to_numpy()).all():
        raise ValueError("Extended family feature cache is not distinct and finite")
    return result, [day.strftime("%Y-%m-%d") for day in future_days]


def _future_forecasts(feed, native_view, candidate_raw, candidate_forecasts, candidate_end, cutoff):
    raw, appended_days = _extended_raw(feed, native_view, candidate_raw, candidate_end, cutoff)
    if not appended_days:
        return candidate_forecasts, [], raw
    native_view, _, provider = native_binding(feed, native_view, catalog())
    calendar = pd.to_datetime(feed.calendar)
    forward = forward_open_labels(read_open_panel(provider, feed.symbols, calendar), 20)
    predictions, skipped = [], []
    for day in pd.to_datetime(appended_days):
        if day <= pd.Timestamp(candidate_end):
            continue
        current, metadata, _ = fit_fold(raw, forward, calendar, day)
        if current is None:
            skipped.append(metadata)
            continue
        predictions.append(current.drop(columns=["relative_label", "head_selected"], errors="ignore"))
    if not predictions:
        return candidate_forecasts, skipped, raw
    future = pd.concat(predictions, names=["date", "symbol"]).sort_index()
    combined = pd.concat([candidate_forecasts, future]).sort_index()
    if combined.index.has_duplicates:
        raise ValueError("Candidate and holdout forecasts overlap")
    return combined, skipped, raw


def replay(name, inputs=None, *, current=None):
    current = current or datetime.now().astimezone()
    root = _root(name)
    with lock(root / "runner.lock"):
        protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
        head = json.loads((root / "head.json").read_text(encoding="utf-8"))
        if head.get("status") != "holdout_source_ready":
            raise ValueError("Holdout source must pass the immutable prefix check before replay")
        if head.get("holdout_sessions", 0) or head.get("fresh_holdout_claimed"):
            raise ValueError("A completed holdout cannot be replayed again")
        supplied = dict(inputs or head.get("checked_inputs") or {})
        required = ("price_view", "event_view", "reference_view", "listing_master", "native_view")
        if any(not supplied.get(key) for key in required):
            raise ValueError("Holdout replay requires the complete price/event/reference/listing/native bundle")
        price_receipt = json.loads((Path(supplied["price_view"]) / "result.json").read_text(encoding="utf-8"))
        cutoff = price_receipt["cutoff"]
        if cutoff <= protocol["candidate_end"]:
            raise ValueError("Holdout source does not extend beyond the candidate")
        feed = BulkResearchFeed(supplied["price_view"], supplied["event_view"], supplied["reference_view"], supplied["listing_master"])
        try:
            candidate_manifest = protocol["candidate_manifest"]
            validate_extension(candidate_manifest, feed.manifest, protocol["candidate_end"])
            signal = protocol["candidate_signal"]
            record = json.loads((settings.ROOT / "outputs/family_pipeline" / protocol["candidate_run"] / "run.json").read_text(encoding="utf-8"))
            result_record = next(row for row in record["results"] if row["signal"] == signal)
            config = StrategyConfig(**record["protocol"].get("signal_portfolio_configs", {}).get(
                signal, record["protocol"]["portfolio"])).validate()
            candidate_artifacts = Path(result_record["source_artifacts"])
            candidate_forecasts = pd.read_parquet(candidate_artifacts.parent / "monthly_return_forecasts.parquet")
            candidate_daily = pd.read_csv(candidate_artifacts / "daily.csv", dtype={"date": str})
            if candidate_daily.empty:
                raise ValueError("Candidate account has no completed daily prefix")
            account_start = str(candidate_daily.date.iloc[0])
            dates = [day for day in feed.calendar if account_start <= day <= cutoff]
            checkpoint = load_replay_checkpoint(candidate_artifacts, config, dates,
                                                 rights_replay_policy=protocol.get("candidate_rights_policy"))
            validate_extension(candidate_manifest, feed.manifest, protocol["candidate_end"])
            forecasts, skipped, raw = _future_forecasts(feed, supplied["native_view"],
                                                         candidate_artifacts.parent / "monthly_raw_components.parquet",
                                                         candidate_forecasts, protocol["candidate_end"], cutoff)
            batch = root / "holdout" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            batch.mkdir(parents=True, exist_ok=False)
            forecasts.to_parquet(batch / "forecasts.parquet")
            raw.to_parquet(batch / "raw_components.parquet")
            write_json(batch / "preparation.json", {"status": "prepared", "cutoff": cutoff,
                                                      "skipped_model_days": skipped, "execution_admitted": False})
            replay_feed = _HoldoutPipelineFeed(feed, forecasts, config,
                                               rights_replay_policy=protocol.get("candidate_rights_policy"))
            ledger = baseline_ledger_result(replay_feed, config, dates, batch / "account", checkpoint=checkpoint,
                                            rights_replay_policy=protocol.get("candidate_rights_policy"))
            daily = pd.read_csv(batch / "account" / "daily.csv", dtype={"date": str})
            holdout_daily = daily.loc[daily.date > protocol["candidate_end"]]
            decision_dates = set(forecasts.index.get_level_values("date").strftime("%Y-%m-%d"))
            new_decisions = sorted(day for day in decision_dates if day > protocol["candidate_end"])
            if not checkpoint["daily"]:
                raise ValueError("Candidate checkpoint has no opening equity for holdout metrics")
            opening_equity = float(checkpoint["daily"][-1]["equity"])
            holdout_metrics = portfolio_metrics(holdout_daily.to_dict("records"), opening_equity)
            result = {"status": ledger["status"], "batch": str(batch), "holdout_sessions": len(holdout_daily),
                      "new_decision_count": len(new_decisions), "new_decisions": new_decisions,
                      "holdout_metrics": holdout_metrics, "overall_metrics": {k: ledger.get(k) for k in
                          ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees", "turnover_value", "final_equity")},
                      "skipped_model_days": skipped, "execution_admitted": False}
            if ledger["status"] != "completed":
                result["error"] = ledger.get("error")
            strategy_validated = bool(ledger["status"] == "completed"
                                      and len(holdout_daily) >= MIN_HOLDOUT_SESSIONS
                                      and len(new_decisions) >= MIN_HOLDOUT_DECISIONS)
            head.update(status="holdout_replay_completed", source_cutoff=cutoff, holdout_sessions=len(holdout_daily),
                        holdout_result=result, checked_inputs=supplied, fresh_holdout_claimed=True,
                        strategy_validated=strategy_validated, execution_admitted=False)
            head.pop("error", None)
        except Exception as error:
            head.update(status="holdout_replay_failed", error=f"{type(error).__name__}: {error}", execution_admitted=False)
            write_json(root / "head.json", head)
            _report(root, protocol, head)
            raise
        finally:
            feed.close()
        write_json(root / "head.json", head)
        _report(root, protocol, head)
        return root, head


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--name", required=True)
    register_parser.add_argument("--candidate-run", required=True)
    register_parser.add_argument("--signal", default=SIGNAL)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--name", required=True)
    check_parser.add_argument("--inputs", type=Path)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--name", required=True)
    replay_parser.add_argument("--inputs", type=Path)
    args = parser.parse_args(argv)
    if args.command == "register":
        root, head = register(args.name, args.candidate_run, signal=args.signal)
    elif args.command == "check":
        root, head = check(args.name, args.inputs)
    else:
        root, head = replay(args.name, args.inputs)
    print(json.dumps({"root": str(root), **head}, ensure_ascii=False), flush=True)
    return 0 if head["status"] in {"waiting_for_new_complete_snapshot", "holdout_source_ready", "holdout_replay_completed"} else 1
