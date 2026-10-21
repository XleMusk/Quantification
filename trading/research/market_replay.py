"""Replay existing rules on pinned stock snapshots, with explicit research scope."""
import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..feed import HistoricalFeed
from ..market_snapshot import FIXED_SCOPE, INGESTION_SCOPE
from ..market_dataset import normalize_symbol
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from .experiment import load_replay_checkpoint, replay, write_json
from .bulk_feed import BULK_SCOPE, BulkResearchFeed

RULE_SIGNALS = ("risk_adjusted_momentum", "momentum", "low_vol_momentum", "liquid_momentum",
                "equal_weight", "donchian_trend")
DEFAULT_RULE_SIGNALS = ("risk_adjusted_momentum", "momentum", "equal_weight")


def classify_replay_error(error):
    """Turn a strict replay stop into a machine-readable research blocker."""
    message = str(error)
    action = re.search(
        r"Unresolved held corporate action:\s*(?P<symbol>[A-Za-z0-9.]+)\s+"
        r"(?P<decision_date>\d{4}-\d{2}-\d{2});\s*missing=(?P<missing>[^;]+)",
        message)
    if action:
        return {
            "kind": "held_corporate_action",
            "code": "missing_corporate_action_terms",
            "message": message,
            "symbol": action.group("symbol"),
            "decision_date": action.group("decision_date"),
            "missing_fields": [field.strip() for field in action.group("missing").split(",")],
            "requires_source_evidence": True,
            "resume_policy": "requires_reviewed_source_evidence_and_replay",
        }
    reference = re.search(
        r"Unresolved held/execution price reference:\s*(?P<symbol>[A-Za-z0-9.]+)\s+"
        r"(?P<decision_date>\d{4}-\d{2}-\d{2});\s*(?P<reason>[^;]+)",
        message)
    if reference:
        return {
            "kind": "price_reference",
            "code": "unresolved_price_reference",
            "message": message,
            "symbol": reference.group("symbol"),
            "decision_date": reference.group("decision_date"),
            "reason": reference.group("reason"),
            "requires_source_evidence": True,
            "resume_policy": "requires_reviewed_price_reference_and_replay",
        }
    return {
        "kind": "replay_failure",
        "code": "unclassified_replay_failure",
        "message": message,
        "requires_source_evidence": False,
        "resume_policy": "inspect_failure_before_retry",
    }


def checkpoint_used_symbols(state):
    used = set(state["positions"]) | set(state.get("targets") or {}) | set(state.get("pending_targets") or {})
    used.update(row["symbol"] for name in ("orders", "fills") for row in state[name].values())
    for event in state["events"]:
        if event.get("symbol"):
            used.add(event["symbol"])
        used.update(event.get("targets", {}))
    for name in ("entitlements", "receivables"):
        used.update(key.rsplit(":", 1)[0].removeprefix("rights:") for key, value in state[name].items() if value)
    return used


def validate_resume_inputs(previous, current, last_day, state=None):
    """Continue only when corrections cannot alter the completed account prefix."""
    if (previous["status"] not in {"failed_full_source_diagnostic", "completed_full_source_diagnostic"}
            or previous["purpose"] != current["purpose"]
            or previous["protocol"]["portfolio"] != current["protocol"]["portfolio"]
            or list(previous["protocol"]["signals"]) != list(current["protocol"]["signals"])
            or any(previous["protocol"].get(key) != current["protocol"].get(key)
                   for key in ("donchian_portfolio_override", "donchian_rule",
                               "adjusted_continuity_override"))
            or previous.get("adjusted_continuity_override", False)
            != current.get("adjusted_continuity_override", False)
            or any(previous[key] != current[key] for key in ("scope", "start", "end", "sessions", "symbols", "board_counts"))):
        raise ValueError("Resume requires the same terminal fixed-rule comparison, dates and costs")
    return validate_prefix_inputs(previous["inputs"], current["inputs"], last_day, state)


def validate_prefix_inputs(before, after, last_day, state=None, *, flat_start_first_use=None):
    """Shared data compatibility check, independent of the caller's fixed rules."""
    for key in ("price_view", "raw_view", "listing_master", "source_codes", "canonical_codes", "unsupported_instruments"):
        if before[key] != after[key]:
            raise ValueError(f"Resume input changed: {key}; full replay required")
    if not last_day:
        return
    unaffected = set()
    used = checkpoint_used_symbols(state) if state is not None else None
    if before["event_view"] != after["event_view"]:
        fields = ["symbol", "dividOperateDate", "code", "dividRegistDate", "dividPayDate", "dividStockMarketDate",
                  "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
        def economic_terms(root):
            frame = pd.read_parquet(Path(root) / "canonical_actions.parquet")
            selected = frame[fields].copy()
            # An absent tax basis and a blank one are both unknown, not zero.
            selected["bonus_taxable_cash_ps"] = frame.get("bonus_taxable_cash_ps", "")
            return selected.fillna("").set_index(fields[:2]).sort_index()
        old, new = economic_terms(before["event_view"]), economic_terms(after["event_view"])
        if not old.index.is_unique or not new.index.is_unique or not old.index.equals(new.index):
            raise ValueError("Action identities added/removed; full replay required")
        changed = old.ne(new).any(axis=1)
        for symbol, day in old.index[changed]:
            if day <= last_day:
                if used is None or symbol in used:
                    key = (symbol, day)
                    changed_fields = set(old.columns[old.loc[key].ne(new.loc[key])])
                    # A reviewed blank-to-zero completion predating all involvement cannot
                    # affect an empty-start account. Other callers retain the strict guard.
                    zero_completion = bool(changed_fields) and changed_fields <= {
                        "dividStocksPs", "dividReserveToStockPs"} and all(
                            old.at[key, field] == "" and new.at[key, field] == "0"
                            for field in changed_fields)
                    action_end = max(day, *(old.at[key, field] for field in (
                        "dividRegistDate", "dividPayDate", "dividStockMarketDate")))
                    first_use = (flat_start_first_use or {}).get(symbol)
                    if not zero_completion or not first_use or first_use <= action_end:
                        raise ValueError("Past economic action changed for an involved symbol; full replay required")
                unaffected.add((old.at[(symbol, day), "code"], day))
        registration_changed = old.dividRegistDate.ne(new.dividRegistDate)
        if ((old.dividRegistDate.le(last_day) | new.dividRegistDate.le(last_day)) & registration_changed).any():
            raise ValueError("Past entitlement registration changed; full replay required")
    if before["reference_view"] != after["reference_view"]:
        old_root, new_root = Path(before["reference_view"]), Path(after["reference_view"])
        old_special = json.loads((old_root / "reviewed_special_references.json").read_text(encoding="utf-8"))
        new_special = json.loads((new_root / "reviewed_special_references.json").read_text(encoding="utf-8"))
        if old_special != new_special:
            raise ValueError("Special holder/rights policy changed; full replay required")
        old = pd.read_parquet(old_root / "raw_reference_changes.parquet")
        new = pd.read_parquet(new_root / "raw_reference_changes.parquet")
        def prefix(frame):
            return frame.loc[frame.date.le(last_day)].fillna("").sort_values(["code", "date"]).reset_index(drop=True)
        old, new = prefix(old), prefix(new)
        raw_fields = ["code", "date", "close", "preclose", "tradestatus", "previous_observed_close", "reference_delta",
                      "has_reported_distribution", "has_reviewed_special_reference", "source_year_queried"]
        if not old[raw_fields].equals(new[raw_fields]):
            raise ValueError("Past raw price/reference facts changed; full replay required")
        formula_fields = {"reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"}
        if not old.columns.equals(new.columns):
            raise ValueError("Past price-reference schema changed; full replay required")
        changed = old.ne(new)
        for index in changed.index[changed.any(axis=1)]:
            key = (old.at[index, "code"], old.at[index, "date"])
            if key in unaffected:
                continue
            symbol = normalize_symbol(key[0]).vt_symbol
            if (used is None or symbol in used
                    or not set(changed.columns[changed.loc[index]]) <= formula_fields):
                raise ValueError("Past price-reference diagnostics changed; full replay required")
            unaffected.add(key)
        keep = [key not in unaffected for key in zip(old.code, old.date)]
        if not old.loc[keep].reset_index(drop=True).equals(new.loc[keep].reset_index(drop=True)):
            raise ValueError("Past price-reference diagnostics changed; full replay required")
    return sorted(unaffected)


def resume_case_source(previous, current, signal):
    """Use an earlier terminal prefix only if this case failed before creating any output."""
    seen = set()
    while True:
        validate_resume_inputs(previous, current, None)
        folder = Path(previous["artifacts"]) / signal
        identity = folder.resolve()
        if identity in seen:
            raise ValueError("Cyclic replay continuation history")
        seen.add(identity)
        if folder.exists():
            return previous, folder
        case = next(row for row in previous["results"] if row["signal"] == signal)
        if (case["status"] != "failed" or "completed_sessions" in case or not previous.get("resume_run")):
            raise ValueError("Missing case checkpoint; full replay required")
        previous = json.loads((Path(previous["resume_run"]) / "run.json").read_text(encoding="utf-8"))


def run(dataset=None, start=None, end=None, initial_cash=100000, include_donchian=False,
        *, price_view=None, event_view=None, reference_view=None, listing_master=None, resume_run=None,
        restart_cases=(), only_signals=(), allow_adjusted_continuity=False, research_scope=None):
    bulk = price_view is not None
    if allow_adjusted_continuity and not bulk:
        raise ValueError("Adjusted continuity override is research-only and requires bulk views")
    restart_cases = set(restart_cases)
    if only_signals and (len(set(only_signals)) != len(only_signals) or not set(only_signals) <= set(RULE_SIGNALS)):
        raise ValueError("Selected rules must be distinct existing fixed signals")
    include_donchian = include_donchian or "donchian_trend" in only_signals
    signals = tuple(only_signals) or DEFAULT_RULE_SIGNALS + (("donchian_trend",) if include_donchian else ())
    if restart_cases and (not bulk or resume_run is None
                          or not restart_cases <= set(signals)):
        raise ValueError("Case restart requires a terminal bulk comparison and existing fixed signals")
    if resume_run is not None and not bulk:
        raise ValueError("Checkpoint continuation is only supported for the fixed bulk rules")
    if bulk:
        if dataset is not None or event_view is None or reference_view is None:
            raise ValueError("Bulk replay needs price/event/reference views instead of --dataset")
        feed = BulkResearchFeed(
            price_view, event_view, reference_view, listing_master,
            allow_adjusted_continuity=allow_adjusted_continuity,
            research_scope=research_scope)
    else:
        if dataset is None or event_view is not None or reference_view is not None or listing_master is not None:
            raise ValueError("Provide --dataset or all three bulk views")
        feed = HistoricalFeed(dataset)
    fixed = feed.manifest["scope"] == FIXED_SCOPE
    if not fixed and feed.manifest["scope"] not in {
            "acquired_mainboard_ingestion_regression_not_strategy_validation", INGESTION_SCOPE, BULK_SCOPE}:
        raise ValueError("This command requires a declared acquired SH/SZ stock or fixed pre-period main-board snapshot")
    if fixed:
        selection = feed.manifest.get("selection", {})
        if (not selection.get("source_files_complete") or not selection.get("snapshot_validated")
                or selection.get("missing") != [] or selection.get("expected_symbols") != len(feed.symbols)
                or selection.get("complete_symbols") != len(feed.symbols)):
            raise ValueError("Fixed-universe snapshot must include every expected member and pass ingestion checks")
    start = start or next(day for day in feed.calendar if day >= feed.manifest["start"])
    end = end or feed.manifest["end"]
    if start not in feed.calendar or end not in feed.calendar or start > end or start < feed.manifest["start"] or end > feed.manifest["end"]:
        raise ValueError("Replay dates must be exchange sessions within the snapshot research period")
    config = replace(StrategyConfig(), initial_cash=initial_cash).validate()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs" / "market_replay" / identifier
    artifacts = DATA_ROOT / "research" / "market_replay" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    dates = [day for day in feed.calendar if start <= day <= end]
    record = {"status": "running", "dataset": str(feed.folder), "artifacts": str(artifacts),
              "purpose": ("full_source_rule_ledger_diagnostic" if bulk else
                          "fixed_preperiod_rule_comparison" if fixed else "data_ingestion_and_ledger_regression_only"),
              "strategy_validated": False,
              "live_ready": False, "scope": feed.manifest["scope"], "symbols": len(feed.symbols),
              "adjusted_continuity_override": bool(allow_adjusted_continuity),
              "research_only_override": bool(allow_adjusted_continuity),
              "board_counts": dict(Counter("unsupported" if s in getattr(feed, "unsupported_instruments", {}) else stock_board(s) for s in feed.symbols)),
              "start": dates[0], "end": dates[-1], "sessions": len(dates),
              "protocol": {"signals": signals, "portfolio": config.to_dict(), "tuning": False,
                           "selection_is_point_in_time": fixed,
                           "donchian_added_after_initial_comparison": include_donchian,
                           "donchian_portfolio_override": {"rebalance_sessions": 1, "retain_rank": 100} if include_donchian else None,
                           "donchian_rule": "Existing 20-session prior-high entry, 10-session prior-low exit, ATR20 rank" if include_donchian else None,
                           "adjusted_continuity_override": bool(allow_adjusted_continuity),
                           "research_only_override": bool(allow_adjusted_continuity),
                           "fresh_holdout_claimed": False,
                           "prior_evaluation_disclosure": "2026 returns were previously inspected on other samples; no tuning or new untouched-test claim"},
              "results": []}
    if bulk:
        record["inputs"] = {key: feed.manifest[key] for key in (
            "price_view", "event_view", "reference_view", "raw_view", "listing_master",
            "research_scope", "research_scope_kind", "research_scope_excluded_codes",
            "source_codes", "full_canonical_codes", "canonical_codes",
            "unsupported_instruments", "limitations")}
    previous = None
    if resume_run is not None:
        previous = json.loads((Path(resume_run) / "run.json").read_text(encoding="utf-8"))
        validate_resume_inputs(previous, record, None)
        record["resume_run"] = str(Path(resume_run).resolve())
        record["restart_cases"] = sorted(restart_cases)
    write_json(out / "run.json", record)
    for signal in signals:
        trend_feed = None
        try:
            if signal == "donchian_trend":
                trend_config = replace(config, rebalance_sessions=1, retain_rank=100).validate()
                options = {}
                if previous is not None and signal not in restart_cases:
                    source, case_folder = resume_case_source(previous, record, signal)
                    old_config = {**source["protocol"]["portfolio"], **source["protocol"]["donchian_portfolio_override"]}
                    checkpoint = load_replay_checkpoint(case_folder, trend_config, dates, old_config)
                    ignored = validate_resume_inputs(source, record, checkpoint["state"]["day"], checkpoint["state"])
                    record.setdefault("resume_case_sources", {})[signal] = str(case_folder.resolve())
                    record.setdefault("resume_unaffected_past_actions", {})[signal] = ignored
                    options["checkpoint"] = checkpoint
                    print(f"{signal}: continuing after {len(checkpoint['daily'])} completed sessions", flush=True)
                if bulk:
                    if "checkpoint" not in options or len(options["checkpoint"]["daily"]) < len(dates):
                        from .bulk_trend import BulkTrendFeed
                        trend_feed = BulkTrendFeed(feed)
                        record["protocol"]["donchian_data_checks"] = trend_feed.data_checks
                    result = replay(trend_feed or feed, trend_config, dates, artifacts / signal, **options)
                else:
                    from .factors import research_panel
                    from .seat_trend import TrendFeed, candidates_by_day, donchian
                    panel, _, eligible, checks = research_panel(feed, trend_config)
                    features = donchian(panel, feed.calendar)
                    by_day, audit = candidates_by_day(features, eligible, feed.calendar, {}, "TREND")
                    audit.loc[audit.date.isin(dates)].to_csv(artifacts / "donchian_trend_signal_audit.csv", index=False)
                    record["protocol"]["donchian_data_checks"] = checks
                    result = replay(TrendFeed(feed, by_day), trend_config, dates, artifacts / signal)
            else:
                case_config = replace(config, signal=signal)
                options = {}
                if previous is not None and signal not in restart_cases:
                    source, case_folder = resume_case_source(previous, record, signal)
                    old_config = {**source["protocol"]["portfolio"], "signal": signal}
                    checkpoint = load_replay_checkpoint(case_folder, case_config, dates, old_config)
                    ignored = validate_resume_inputs(source, record, checkpoint["state"]["day"], checkpoint["state"])
                    record.setdefault("resume_case_sources", {})[signal] = str(case_folder.resolve())
                    record.setdefault("resume_unaffected_past_actions", {})[signal] = ignored
                    options["checkpoint"] = checkpoint
                    print(f"{signal}: continuing after {len(checkpoint['daily'])} completed sessions", flush=True)
                elif signal in restart_cases:
                    print(f"{signal}: full recomputation requested; fixed dates/rules/costs unchanged", flush=True)
                result = replay(feed, case_config, dates, artifacts / signal, **options)
            result.update(signal=signal, status="completed")
            print(f"{signal}: {result['net_return']:.4%}; {record['purpose']}", flush=True)
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"
            result = {"signal": signal, "status": "failed", "error": error_text,
                      "blocker": classify_replay_error(error_text)}
            receipt_path = artifacts / signal / "replay.json"
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                result.update({key: receipt[key] for key in ("completed_sessions", "last_completed_session", "attempted_session", "account_basis")})
                result.update({key: receipt[key] for key in ("reused_sessions", "newly_completed_sessions") if key in receipt})
            print(f"{signal}: FAILED {error}", flush=True)
        finally:
            if trend_feed is not None:
                pd.DataFrame(trend_feed.audit.values()).to_csv(artifacts / "donchian_trend_signal_audit.csv", index=False)
        record["results"].append(result)
        write_json(out / "run.json", record)
    suffix = "full_source_diagnostic" if bulk else "fixed_rule_comparison" if fixed else "ingestion_regression"
    record["status"] = ("completed_" if all(r["status"] == "completed" for r in record["results"]) else "failed_") + suffix
    write_json(out / "run.json", record)
    pd.DataFrame(record["results"]).to_csv(out / "metrics.csv", index=False)
    lines = [
             ("# Dated research-scope rule ledger replay" if bulk and feed.research_scope else
              "# Full source-covered rule ledger replay" if bulk else
              "# Fixed pre-period rule comparison" if fixed else
              "# Partial market-data ingestion replay"),
             "", f"Status: {record['status']}",
             f"Snapshot: `{feed.folder}`", f"Accounts, daily marks, orders, fills: `{artifacts}`", "",
             f"{len(feed.symbols)} acquired SH/SZ stock codes ({record['board_counts']}); {dates[0]} to {dates[-1]}; {len(dates)} sessions.",
             "All variants use the existing trading.engine/ledger, identical dates, costs and starting cash.",
             "Selected signals: " + ", ".join(signals) + ". Unselected accounts are not rerun or ranked in this run.",
             "Donchian (when included) uses its existing daily exit cadence; the other rules rebalance every five sessions.",
             "low_vol_momentum is a fixed positive 12-1 momentum rule gated to the lower half of the eligible 60-day volatility pool; no threshold was selected from these returns.",
             "No parameters were selected from these results.", "",
             ("Adjusted-panel continuity override is enabled for research diagnostics only; "
              "reference_mismatch rows continue only when adjusted preclose continuity is verified. "
              "It does not complete missing action terms, change raw execution prices, or admit execution."
              if allow_adjusted_continuity else "Adjusted-panel continuity override is disabled."), "",
             ("Dated research scope constrains the replay membership; the full canonical source remains pinned and excluded rows are not account eligible."
              if bulk and feed.research_scope else
              "Full canonical source-covered SH/SZ universe, no stock budget; not independently complete A/BSE/ETF or PIT coverage."
              if bulk else "Complete provider-reported pre-period main-board subset, not a rolling CSI300 index; membership not independently verified."
              if fixed else "NOT strategy validation: this partial download queue is stratified by retrieval-time listing status."),
             "2026 results have been inspected on other samples. This is not a fresh untouched test or deployment approval.",
             ("Donchian was added after the initial three-rule comparison; its result is exploratory, not a fresh holdout."
              if include_donchian else "Donchian is not part of this run."),
             "The equal_weight variant is a deterministic code-ranked control, NOT a broad-market benchmark.",
             "Do not compare these returns with the earlier ten-stock sample as a signal improvement.", "",
             "| Signal | Status | Net return | Max drawdown | Sharpe (zero RF,252) | Stale mark days |", "|---|---|---:|---:|---:|---:|"]
    for row in record["results"]:
        if row["status"] == "completed":
            sharpe = row.get("sharpe_zero_rf_252")
            sharpe_text = f"{sharpe:.4f}" if sharpe is not None else "unavailable"
            lines.append(f"| {row['signal']} | completed | {row['net_return']:.2%} | {row['max_drawdown']:.2%} | {sharpe_text} | {row['stale_valuation_days']} |")
        else:
            progress = f"; {row['completed_sessions']} sessions through {row['last_completed_session']}" if "completed_sessions" in row else ""
            lines.append(f"| {row['signal']} | failed: {row['error']}{progress} | unavailable | unavailable | unavailable | unavailable |")
    if bulk and include_donchian:
        lines.extend(["", "Donchian reuses the original 20/10/20 rule on every source-covered security, without an outcome-based stock budget.",
                      "Signals use adjusted OHLC; orders and cash/share accounting use unadjusted RMB quotes through the sole Engine/Ledger.",
                      "Missing/suspended/zero-volume bars are masked, never forward-filled; the original TA-Lib interior-NaN ATR behavior is retained.",
                      "Daily signal audit counts report active trends lacking a finite score; source coverage does not imply every stock is eligible."])
    if bulk:
        lines.extend(["", f"Unsupported ledger instruments (prices retained, ordinary-stock rules do not rank/execute them): {feed.unsupported_instruments}",
                      "Unheld unresolved actions do not block unrelated research. Held/registered entitlements and affected execution references stop before session mutation.",
                      "Failed runs retain the last completed account and cannot be ranked as full-period returns."])
    if previous is not None:
        lines.extend(["", f"Continued from `{record['resume_run']}` using saved daily/account prefixes, preserving the original rebalance phase.",
                      "Past action/formula corrections are reused only for symbols absent from all historical targets, orders, fills, positions and positive entitlements; raw price/reference facts stay identical.",
                      f"Reused sessions across cases: {sum(row.get('reused_sessions', 0) for row in record['results'])}; newly completed: {sum(row.get('newly_completed_sessions', 0) for row in record['results'])}."])
        if restart_cases:
            lines.append("Cases explicitly recomputed from the first session: " + ", ".join(sorted(restart_cases)) + ". No completed prefix reused for these cases.")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if bulk:
        feed.close()
    print(out)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--price-view", type=Path, help="Full reconstructed daily price view (diagnostic replay only)")
    parser.add_argument("--event-view", type=Path)
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--listing-master", type=Path, help="Optional newer pinned provider listing master for bulk stocks")
    parser.add_argument("--research-scope", type=Path,
                        help="Optional research_scope_v1 receipt to constrain the bulk replay universe")
    parser.add_argument("--resume-run", type=Path, help="Continue a terminal bulk-rule comparison after account-unaffected action corrections")
    parser.add_argument("--restart-case", action="append", default=[],
                        choices=RULE_SIGNALS,
                        help="With --resume-run, recompute this fixed case while reusing unaffected case prefixes")
    parser.add_argument("--start", help="Inclusive exchange session (YYYY-MM-DD)")
    parser.add_argument("--end", help="Inclusive exchange session (YYYY-MM-DD)")
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--include-donchian", action="store_true", help="Add the existing Donchian trend rule as an exploratory candidate")
    parser.add_argument("--allow-adjusted-continuity", action="store_true",
                        help="Research-only diagnostic: permit verified adjusted-panel continuity for reference mismatches")
    parser.add_argument("--only-signal", action="append", default=[], choices=RULE_SIGNALS,
                        help="Run only this existing fixed rule; may be repeated without changing default comparisons")
    args = parser.parse_args(argv)
    out = run(args.dataset, args.start, args.end, args.initial_cash, args.include_donchian,
              price_view=args.price_view, event_view=args.event_view, reference_view=args.reference_view,
              listing_master=args.listing_master, resume_run=args.resume_run, restart_cases=args.restart_case,
              only_signals=args.only_signal, allow_adjusted_continuity=args.allow_adjusted_continuity,
              research_scope=args.research_scope)
    result = json.loads((out / "run.json").read_text(encoding="utf-8"))
    return 0 if result["status"] in {"completed_ingestion_regression", "completed_fixed_rule_comparison", "completed_full_source_diagnostic"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
