"""Frozen-rank cost replays and synthetic liquidation tails through the sole ledger."""
import argparse
import ast
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import shutil
import time
import uuid

import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, TradingMode
from ..feed import HistoricalFeed
from ..ledger import ACTIVE, Ledger
from ..rules import quote_value
from ..settings import DATA_ROOT, ROOT
from .bulk_feed import BulkResearchFeed
from .capital_allocation_replay import SavedRankFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .market_replay import validate_prefix_inputs


ID = "frozen_stock_ledger_stress_v1"
DISRUPTION_ID = "frozen_stock_ledger_disruptions_v1"
CASES = ("stock_low_vol_half_momentum_control", "stock_equal_three_families", "stock_family_ridge_rank")
TAILS = {"two_limits_exit": 1.0, "two_limits_thin_exit": .01}
LOCKED_SESSIONS = 2
REOPEN_SESSIONS = 3
SUSPENDED_SESSIONS = 2
DISRUPTIONS = ("suspension_two_limits_exit", "quote_outage_reconciled_exit")


def doubled_cost(config):
    return replace(config, commission=config.commission * 2,
                   minimum_commission=config.minimum_commission * 2,
                   sell_tax=config.sell_tax * 2, transfer_fee=config.transfer_fee * 2,
                   slippage_bps=config.slippage_bps * 2).validate()


class LiquidationTailFeed:
    """Artificial sessions after a checked stock-only historical endpoint, not a forecast."""

    def __init__(self, base, state, config, volume_fraction, *, suspended_sessions=0):
        if volume_fraction not in TAILS.values() or state.get("broker_binding"):
            raise ValueError("Only unbound replay tails with predeclared liquidity are supported")
        if type(suspended_sessions) is not int or suspended_sessions not in {0, SUSPENDED_SESSIONS}:
            raise ValueError("Suspension length must match the fixed disruption definition")
        self.config = config
        self.anchor = state["day"]
        self.synthetic_dates = [(date.fromisoformat(self.anchor) + timedelta(days=i)).isoformat()
                                for i in range(1, suspended_sessions + LOCKED_SESSIONS + REOPEN_SESSIONS + 1)]
        # These ISO labels do NOT represent actual exchange sessions or the holiday calendar.
        self.calendar = [day for day in base.calendar if day <= self.anchor] + self.synthetic_dates
        self.instrument_profiles = dict(getattr(base, "instrument_profiles", {}))
        book = Ledger(state, config, instrument_profiles=self.instrument_profiles)
        self.symbols = sorted(symbol for symbol in state["positions"] if book.quantity(symbol))
        self.instruments = {symbol: deepcopy(base.instruments[symbol]) for symbol in self.symbols}
        self.actions = deepcopy(base.actions)
        self.manifest = {**base.manifest, "replay_only": True, "synthetic_tail": True,
                         "new_unobserved_corporate_actions": "excluded_synthetic_assumption_not_known_zero"}
        real_rows = base.rows.get(self.anchor, {})
        self.rows, self.quotes = {}, []
        self.volumes = pd.DataFrame(index=[self.anchor] + self.synthetic_dates, columns=self.symbols, dtype=float)
        for symbol in self.symbols:
            row = real_rows.get(symbol)
            rule = book.rule(symbol, self.anchor)
            if rule.is_fund or rule.price_unit != "cent":
                raise ValueError("This tail is stock-only; unreviewed future ETF profiles are not extended")
            if (not row or row.get("tradestatus") != 1 or row.get("isST") not in {0, 1}
                    or str(row.get("adjustflag", "3")) != "3"
                    or quote_value(row["close"], rule.price_unit) != state["prices"][symbol]):
                raise ValueError(f"Unverified terminal raw stock quote: {symbol}")
            volume = float(base.volumes.at[self.anchor, symbol])
            if not math.isfinite(volume) or volume <= 0:
                raise ValueError(f"Unknown terminal liquidity: {symbol}")
            self.volumes.at[self.anchor, symbol] = volume
            previous = state["prices"][symbol]
            for step, day in enumerate(self.synthetic_dates, start=1):
                suspended = step <= suspended_sessions
                preclose = previous / 100
                lower, _ = book.rule(symbol, day).limits(preclose, day, bool(row["isST"]))
                price = lower if suspended_sessions < step <= suspended_sessions + LOCKED_SESSIONS else previous
                assumed_volume = 0 if suspended else volume * volume_fraction
                synthetic = {**row, "open": price / 100, "high": price / 100, "low": price / 100,
                             "close": price / 100, "preclose": preclose, "volume": assumed_volume,
                             "amount": assumed_volume * price / 100, "tradestatus": 0 if suspended else 1,
                             "adjustflag": "3"}
                self.rows.setdefault(day, {})[symbol] = synthetic
                self.volumes.at[day, symbol] = assumed_volume
                self.quotes.append({"synthetic_label": day, "step": step, "symbol": symbol,
                                    "board": rule.board, "terminal_quantity": book.quantity(symbol),
                                    "volume_fraction": volume_fraction,
                                    "valuation_basis": "last_known_suspended_mark" if suspended else "synthetic_raw_price",
                                    **synthetic})
                previous = price

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != self.config or day not in self.synthetic_dates:
            raise ValueError("Synthetic liquidation targets are replay-only")
        return SignalFrame.from_legacy([], day, "synthetic:predeclared_full_liquidation", context)

    next_day = HistoricalFeed.next_day


def liquidation_checkpoint(checkpoint, config):
    clone = deepcopy(checkpoint)
    state = clone["state"]
    if any(order["status"] in ACTIVE for order in state["orders"].values()):
        raise ValueError("Active historical orders require reconciliation before a stress branch")
    book = Ledger(state, config)
    before = book.equity()
    ignored = []
    # Original stock feeds also marked zero-held funds. Do not extend their expired profiles.
    for symbol in list(state.get("price_units", {})):
        if book.quantity(symbol) == 0:
            state["price_units"].pop(symbol)
            state["prices"].pop(symbol, None)
            state["positions"].pop(symbol, None)
            ignored.append(symbol)
    if book.equity() != before:
        raise ValueError("Pruning unheld quote marks changed endpoint equity")
    # Idealized warning at the historical close: even this cannot bypass a limit-down queue.
    state["pending_targets"], state["targets"] = {}, {}
    book.emit(state["day"], "synthetic_stress_branch", source=checkpoint["source"],
              reason="full_exit_decided_at_anchor_close_no_real_order", overwritten_pending_target=True,
              dropped_unheld_quote_marks=ignored)
    return clone


def tail_outcome(feed, checkpoint, config, dates, destination, result):
    initial_book = Ledger(checkpoint["state"], config)
    equity = initial_book.equity() / 100
    invested = sum(initial_book.position_value(symbol) for symbol in feed.symbols) / 100
    quantity = sum(initial_book.quantity(symbol) for symbol in feed.symbols)
    pd.DataFrame(feed.quotes).to_csv(destination / "synthetic_quotes.csv", index=False)
    state = json.loads((destination / "account.json").read_text(encoding="utf-8"))
    book = Ledger(state, config)
    frame = pd.read_csv(destination / "daily.csv", dtype={"date": str}).iloc[len(dates):]
    events = [event for event in state["events"] if event["day"] in feed.synthetic_dates]
    fills = [fill for fill in state["fills"].values() if fill["day"] in feed.synthetic_dates]
    sessions = []
    for step, row in enumerate(frame.to_dict("records"), start=1):
        today = [fill for fill in fills if fill["day"] == row["date"]]
        stale = ast.literal_eval(row["stale_symbols"]) if isinstance(row["stale_symbols"], str) else row["stale_symbols"]
        sessions.append({"step": step, "synthetic_label": row["date"], "equity": row["equity"],
                         "loss_from_anchor": None if stale else 1 - row["equity"] / equity,
                         "stale_symbols": stale,
                         "valuation_basis": "last_known_marks_not_current_equity" if stale else "synthetic_raw_prices",
                         "holdings": row["holdings"], "sold_shares": sum(fill["qty"] for fill in today),
                         "fees": row["fees"], "new_orders": row["new_orders"]})
    known_losses = [row["loss_from_anchor"] for row in sessions if row["loss_from_anchor"] is not None]
    outcome = {"status": result["status"], "synthetic_sessions_completed": len(frame),
               "source_artifacts": checkpoint["source"], "anchor": checkpoint["state"]["day"],
               "anchor_equity": equity, "anchor_stock_value": invested, "anchor_gross": invested / equity,
               "anchor_quantity": quantity,
               "worst_incremental_loss": max([0] + known_losses) if known_losses else None,
               "last_incremental_loss": sessions[-1]["loss_from_anchor"] if sessions else None,
               "stale_valuation_sessions": sum(bool(row["stale_symbols"]) for row in sessions),
               "unobserved_loss_bound": None,
               "remaining_quantity": sum(book.quantity(symbol) for symbol in feed.symbols),
               "remaining_stock_value": sum(book.position_value(symbol) for symbol in feed.symbols) / 100,
               "additional_fees": sum(fill["fees"] for fill in fills) / 100,
               "blocked_reasons": dict(Counter(event["reason"] for event in events if event["kind"] == "order_blocked")),
               "sold_shares": sum(fill["qty"] for fill in fills if fill["side"] == "sell"),
               "buy_fills": sum(fill["side"] == "buy" for fill in fills), "sessions": sessions,
               "artifacts": str(destination), "execution_admitted": False}
    if result["status"] != "completed":
        outcome.update(error=result.get("error"), attempted_session=result.get("attempted_session"))
    write_json(destination / "stress_result.json", outcome)
    return outcome


def tail_result(base, checkpoint, config, dates, destination, volume_fraction, *, suspended_sessions=0):
    feed = LiquidationTailFeed(base, checkpoint["state"], config, volume_fraction,
                               suspended_sessions=suspended_sessions)
    result = baseline_ledger_result(feed, config, dates + feed.synthetic_dates, destination,
                                   checkpoint=liquidation_checkpoint(checkpoint, config))
    return tail_outcome(feed, checkpoint, config, dates, destination, result)


def checked_reference_equivalence(state, expected, synthetic_dates):
    account = lambda value: {key: item for key, item in value.items() if key not in {"events", "sequence"}}
    if account(state) != account(expected):
        raise ValueError("Restored financial account differs from the completed same-input reference")
    def core_events(value):
        events, diagnostics = [], 0
        if value["sequence"] != len(value["events"]):
            raise ValueError("Reference event sequence/count mismatch")
        for index, original in enumerate(value["events"], start=1):
            if original["sequence"] != index:
                raise ValueError("Reference event sequence is not contiguous")
            event = {key: item for key, item in original.items() if key != "sequence"}
            # Old saved tails predate informational diagnostics/target-source fields.
            # Limit this compatibility to the declared synthetic liquidation sessions.
            if event["day"] in synthetic_dates:
                if event["kind"] == "target_execution_diagnostic" and event.get("schema_version") == 1:
                    diagnostics += 1
                    continue
                if event["kind"] == "targets_generated":
                    if (event["targets"] != {} or event.get("decision_reason") is not None
                            or event.get("signal_source", "synthetic:predeclared_full_liquidation")
                            != "synthetic:predeclared_full_liquidation"):
                        raise ValueError("Synthetic target decision changed")
                    event.pop("signal_source", None)
                    event.pop("decision_reason", None)
            events.append(event)
        return events, diagnostics
    actual_events, actual_diagnostics = core_events(state)
    expected_events, expected_diagnostics = core_events(expected)
    if actual_events != expected_events:
        raise ValueError("Restored execution events differ from the completed same-input reference")
    return {"financial_account_equal": True, "core_events_equal": True,
            "current_informational_tail_events": actual_diagnostics,
            "reference_informational_tail_events": expected_diagnostics,
            "compatibility": "tail_diagnostic_v1_and_fixed_liquidation_source_metadata_only"}


def outage_result(base, checkpoint, config, dates, destination, reference):
    """Retain the missing-quote failure, then resume from its persisted account prefix."""
    feed = LiquidationTailFeed(base, checkpoint["state"], config, 1.)
    if not feed.symbols:
        raise ValueError("Quote-outage stress requires a held stock")
    day, symbol = feed.synthetic_dates[0], feed.symbols[0]
    complete_dates = dates + feed.synthetic_dates
    missing = deepcopy(feed)
    del missing.rows[day][symbol]
    missing.quotes = [row for row in missing.quotes if not (row["synthetic_label"] == day and row["symbol"] == symbol)]
    missing.volumes.at[day, symbol] = float("nan")
    opening = liquidation_checkpoint(checkpoint, config)
    failed = baseline_ledger_result(missing, config, complete_dates, destination / "interrupted", checkpoint=opening)
    pd.DataFrame(missing.quotes).to_csv(destination / "interrupted/synthetic_quotes.csv", index=False)
    expected_error = f"ValueError: Unpriceable holding: {symbol} {day}; source reconciliation required"
    if (failed["status"] != "failed" or failed.get("error") != expected_error
            or failed["completed_sessions"] != len(dates) or failed["attempted_session"] != day):
        raise ValueError("Missing held quote did not stop at the expected untouched prefix")
    recovered = load_replay_checkpoint(destination / "interrupted", config, complete_dates)
    if recovered["state"] != opening["state"]:
        raise ValueError("Quote outage changed the cash, holdings, orders, fills or cursor")
    expected = load_replay_checkpoint(reference, config, complete_dates)
    result = baseline_ledger_result(feed, config, complete_dates, destination / "recovered", checkpoint=recovered)
    outcome = tail_outcome(feed, checkpoint, config, dates, destination / "recovered", result)
    final = load_replay_checkpoint(destination / "recovered", config, complete_dates)
    equivalence = checked_reference_equivalence(final["state"], expected["state"], feed.synthetic_dates)
    if result["status"] != "completed" or final["daily"] != expected["daily"]:
        raise ValueError("Restored quote replay differs from the completed same-input reference")
    outcome.update(interruption={**failed, "missing_symbol": symbol, "current_equity": None,
                                 "loss_during_outage": None, "account_unchanged": True,
                                 "artifacts": str(destination / "interrupted")},
                   recovery={"new_sessions": result["newly_completed_sessions"],
                             "reused_sessions": result["reused_sessions"],
                             "reference": str(reference), "same_execution_state_and_daily_as_reference": True,
                             "event_equivalence": equivalence,
                             "basis": "synthetic_quote_reconciliation_not_live_source_recovery"})
    write_json(destination / "stress_result.json", outcome)
    return outcome


def frozen_sources(record):
    if (record.get("status") != "completed_family_ridge_diagnostics"
            or record.get("research_family") != "regularized_family_rank"
            or record.get("execution_admitted") is not False
            or record["protocol"].get("fresh_holdout_claimed") is not False):
        raise ValueError("A completed development-only fixed family/Ridge comparison is required")
    original = json.loads(Path(record["protocol"]["source_run"]).read_text(encoding="utf-8"))
    controls = {row["signal"]: row for row in record["controls"]}
    own = {row["signal"]: row for row in record["results"]}
    cases = []
    for signal in CASES:
        if signal in controls:
            row = controls[signal]
            if row.get("comparable_under_current_sources") is not True:
                raise ValueError("Stress control source must already be reconciled")
            account = Path(row["source_artifacts"])
            ranks = Path(original["artifacts"]) / signal
        else:
            row = own[signal]
            account, ranks = Path(record["artifacts"]) / signal, Path(record["artifacts"]) / "predictions"
        if row["status"] != "completed" or row["completed_sessions"] != record["sessions"]:
            raise ValueError("Incomplete baseline cannot seed terminal stress")
        cases.append((signal, row, account, ranks))
    return cases


def report(out, record):
    lines = ["# Fixed Ledger Stress", "", f"Status: {record['status']}. NO_TRADE.", "",
             "Historical costs: fresh accounts using frozen ranks, source dates and the same Engine/Ledger.",
             "Both commission rate AND minimum fee, sell tax, transfer fee and slippage doubled; dividend reserve unchanged.",
             "No alpha, selector, lot policy or fractional assumption changed. Failures are retained, not zero-filled.", "",
             "Synthetic tails: clones of actual saved endpoint stock ledgers, NOT new market observations.",
             "Full exit is assumed decided at the anchor close. First two bars lock each stock at its OWN board limit;",
             "the next three artificial sessions reopen flat. Liquidity is either100% or1% of the anchor volume;",
             "original participation, T+1, price, quantity, cash and fee rules still apply.",
             "ISO tail labels are placeholders, NOT exchange/holiday calendar dates. Known source actions remain;",
             "unobserved new corporate events are excluded as a synthetic assumption, not asserted absent.",
             "Disruption suite (when enabled): two suspended sessions with stale marks, then two own-board",
             "locked limit-down sessions and three flat reopened sessions. Stale-mark returns remain unknown.",
             "Quote outage removes the first sorted held stock's first synthetic quote: the entire account must stop",
             "without mutation. Restoring that synthetic quote resumes the saved prefix and must exactly match",
             "the existing same-input financial state, core execution events and daily reference; only added",
             "v1 tail diagnostics/fixed-target source metadata may differ. NOT actual source/client recovery.",
             "Terminal snapshots only: NOT all historical paths, global worst cases or a guaranteed drawdown bound.", "",
             "## Historical Cost Replay", "", "|Frozen case|Status/sessions|Net|MDD|Sharpe|Fees|Net difference pp|", "|---|---|---:|---:|---:|---:|---:|"]
    for row in record["cost_results"]:
        if row["status"] == "completed":
            lines.append(f"|{row['signal']}|{row['status']}/{row['completed_sessions']}|{row['net_return']:.2%}|"
                         f"{abs(row['max_drawdown']):.2%}|{row['sharpe_zero_rf_252']:.3f}|{row['fees']:.2f}|{100 * row['net_difference']:.2f}|")
        else:
            lines.append(f"|{row['signal']}|{row['status']}/{row['completed_sessions']}|unknown|unknown|unknown|unknown|unknown|")
            lines.append(f"\nFailure: {row.get('error')}\n")
    lines += ["", "## Synthetic Liquidation", "", "|Frozen case|Tail/status|Anchor gross|Worst observed incremental loss|Sold/initial shares|Remaining prefix value|Blocked reasons|", "|---|---|---:|---:|---:|---:|---|"]
    for row in record["tail_results"]:
        loss = f"{row['worst_incremental_loss']:.2%}" if row["worst_incremental_loss"] is not None else "unknown"
        lines.append(f"|{row['signal']}|{row['scenario']}/{row['status']}|{row['anchor_gross']:.2%}|{loss}|"
                     f"{row['sold_shares']}/{row['anchor_quantity']}|{row['remaining_stock_value']:.2f}|{row['blocked_reasons']}|")
        if row["status"] != "completed":
            lines.append(f"\nFailure after {row['synthetic_sessions_completed']} synthetic sessions: {row.get('error')}\n")
        if row.get("stale_valuation_sessions"):
            lines.append(f"\n{row['signal']}: {row['stale_valuation_sessions']} stale valuation sessions; loss then unknown.\n")
        if row.get("recovery"):
            lines.append(f"\n{row['signal']}: quote outage preserved the account/cursor; restored execution state/daily equals saved reference.\n")
    lines += ["", "Artifacts contain actual alternate accounts/daily/orders/fills and separate synthetic quotes/session receipts.",
              f"Source: {record['source_run']}", f"Artifacts: {record['artifacts']}"]
    if record.get("error"):
        lines += ["", f"Preparation/compatibility failure retained: {record['error']}"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reusable_costs(identifier, source_path, protocol):
    if identifier is None:
        return {}
    path = ROOT / "outputs/ledger_stress" / identifier / "run.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    if (saved.get("status") not in {"completed_ledger_stress_diagnostics", "partial_ledger_stress_diagnostics"}
            or saved.get("source_run") != str(source_path)
            or any(saved["protocol"].get(key) != protocol[key] for key in
                   ("stock_inputs", "cases", "original_config", "cost_config", "fractional_allocation"))):
        raise ValueError("Cost reuse requires the same terminal fixed inputs/configs/cases")
    rows = saved["cost_results"]
    if {row["signal"] for row in rows} != set(CASES) or len(rows) != len(CASES):
        raise ValueError("All predeclared cost records required")
    return {row["signal"]: {**row, "reused_from": str(path)} for row in rows if row["status"] == "completed"}


def reusable_tail_references(identifier, source_path, protocol):
    path = ROOT / "outputs/ledger_stress" / identifier / "run.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    if (saved.get("status") != "completed_ledger_stress_diagnostics" or saved.get("source_run") != str(source_path)
            or saved["protocol"].get("id") != ID
            or saved["protocol"].get("terminal_volume_fractions") != TAILS
            or any(saved["protocol"].get(key) != protocol[key] for key in
                   ("stock_inputs", "cases", "original_config", "locked_sessions", "reopen_sessions",
                    "liquidation_target", "tail_calendar", "fractional_allocation"))):
        raise ValueError("Disruption recovery needs a completed same-definition original tail reference")
    rows = [row for row in saved["tail_results"] if row["scenario"] == "two_limits_exit"]
    if len(rows) != len(CASES) or {row["signal"] for row in rows} != set(CASES) or any(
            row["status"] != "completed" or row["synthetic_sessions_completed"] != 5 for row in rows):
        raise ValueError("All three complete liquidation references required")
    return {row["signal"]: Path(row["artifacts"]) for row in rows}


def run(source_id, *, cost_run=None, tail_suite="original"):
    if tail_suite not in {"original", "disruptions"} or (tail_suite == "disruptions" and cost_run is None):
        raise ValueError("Disruptions require an explicit existing completed cost/tail run; no repeated cost replay")
    source_path = ROOT / "outputs/market_replay" / source_id / "run.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    cases = frozen_sources(source)
    config = StrategyConfig(**source["protocol"]["portfolio"]).validate()
    if config.rebalance_sessions != 1:
        raise ValueError("Liquidation retry requires the existing daily decision schedule")
    stressed = doubled_cost(config)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = ROOT / "outputs/ledger_stress" / identifier
    artifacts = DATA_ROOT / "research/ledger_stress" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, artifacts / "implementation.py")
    protocol = {"id": DISRUPTION_ID if tail_suite == "disruptions" else ID,
                "tail_suite": tail_suite, "source_run": str(source_path), "cases": list(CASES),
                "stock_inputs": source["protocol"]["stock_inputs"], "original_config": config.to_dict(),
                "cost_config": stressed.to_dict(), "costs_multiplier": 2, "cost_reuse_run": cost_run,
                "locked_sessions": LOCKED_SESSIONS, "reopen_sessions": REOPEN_SESSIONS,
                "terminal_volume_fractions": TAILS, "liquidation_target": "anchor_close_full_exit",
                "tail_calendar": "synthetic_consecutive_labels_not_exchange_sessions",
                "parameters_searched": False, "fractional_allocation": None,
                "fresh_holdout_claimed": False, "execution_admitted": False}
    costs = reusable_costs(cost_run, source_path, protocol)
    references = {}
    if tail_suite == "disruptions":
        if set(costs) != set(CASES):
            raise ValueError("All complete historical cost accounts must be reused for disruptions")
        references = reusable_tail_references(cost_run, source_path, protocol)
        protocol.update(disruption_scenarios=list(DISRUPTIONS), suspended_sessions=SUSPENDED_SESSIONS,
                        outage_selection="first_sorted_held_symbol_first_tail_session",
                        outage_loss="unknown_not_zero", recovery_basis="restored_fixed_synthetic_quotes")
    record = {"id": identifier, "status": "running", "source_run": str(source_path),
              "start": source["start"], "end": source["end"], "sessions": source["sessions"],
              "research_family": "fixed_quantity_ledger_stress", "artifacts": str(artifacts),
              "protocol": protocol, "cost_results": [], "tail_results": [], "execution_admitted": False}
    write_json(out / "protocol.json", protocol)
    write_json(out / "run.json", record)
    base, begin = None, time.monotonic()
    try:
        inputs = protocol["stock_inputs"]
        base = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"], inputs["listing_master"])
        validate_prefix_inputs(source["protocol"]["stock_manifest"], base.manifest, None)
        dates = [day for day in base.calendar if source["start"] <= day <= source["end"]]
        if len(dates) != source["sessions"] or dates[0] != source["start"] or dates[-1] != source["end"]:
            raise ValueError("Exact frozen historical calendar required")
        for signal, original, account, ranks in cases:
            checkpoint = load_replay_checkpoint(account, config, dates)
            scenarios = DISRUPTIONS if tail_suite == "disruptions" else TAILS
            for scenario in scenarios:
                destination = artifacts / signal / scenario
                if scenario == "quote_outage_reconciled_exit":
                    outcome = outage_result(base, checkpoint, config, dates, destination, references[signal])
                else:
                    outcome = tail_result(base, checkpoint, config, dates, destination,
                                          TAILS.get(scenario, 1.),
                                          suspended_sessions=SUSPENDED_SESSIONS if scenario == DISRUPTIONS[0] else 0)
                row = {"signal": signal, "scenario": scenario, **outcome}
                record["tail_results"].append(row)
                write_json(out / "run.json", record)
                loss = f"{row['worst_incremental_loss']:.4%}" if row["worst_incremental_loss"] is not None else "unknown"
                print(f"Tail {signal}/{scenario}: {row['status']}; loss={loss}; "
                      f"remaining={row['remaining_stock_value']:.2f}", flush=True)
            if signal in costs:
                row = costs[signal]
                load_replay_checkpoint(Path(row["artifacts"]), stressed, dates)
                if row.get("baseline") != original or row["completed_sessions"] != len(dates):
                    raise ValueError("Reused cost account no longer matches its complete frozen control")
            else:
                ranked = SavedRankFeed(base, ranks, signal, stressed, dates)
                row = {"signal": signal, "scenario": "costs_and_slippage_x2", "baseline": original,
                       "artifacts": str(artifacts / signal / "costs_x2"),
                       **baseline_ledger_result(ranked, stressed, dates, artifacts / signal / "costs_x2")}
            if row["status"] == "completed":
                row.update(net_difference=row["net_return"] - original["net_return"],
                           fee_difference=row["fees"] - original["fees"],
                           drawdown_difference=abs(row["max_drawdown"]) - abs(original["max_drawdown"]))
            record["cost_results"].append(row)
            write_json(out / "run.json", record)
            print(f"Costs {signal}: {row['status']} {row['completed_sessions']}/{len(dates)}; net={row.get('net_return')}", flush=True)
        record["status"] = ("completed_ledger_stress_diagnostics" if all(row["status"] == "completed" for row in
                            record["cost_results"] + record["tail_results"]) else "partial_ledger_stress_diagnostics")
    except Exception as error:
        record.update(status="failed_ledger_stress_preparation", error=f"{type(error).__name__}: {error}")
    finally:
        if base is not None:
            base.close()
        record["elapsed_seconds"] = round(time.monotonic() - begin, 3)
        write_json(out / "run.json", record)
        report(out, record)
    print(str(out), flush=True)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--cost-run", help="Reuse complete same-source cost accounts after a tail-only repair")
    parser.add_argument("--tail-suite", choices=("original", "disruptions"), default="original",
                        help="Fixed suspension/outage recovery suite requires a completed --cost-run")
    args = parser.parse_args(argv)
    _, record = run(args.source_run, cost_run=args.cost_run, tail_suite=args.tail_suite)
    return 0 if record["status"] == "completed_ledger_stress_diagnostics" else 1


if __name__ == "__main__":
    raise SystemExit(main())
