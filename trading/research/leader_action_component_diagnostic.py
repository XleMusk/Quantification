"""Replay an explicit accounting schedule against saved raw bars and action evidence.

This is a cash/share component diagnostic, never a strategy return or a
historically published signal.  Every reviewed symbol remains in the result,
including unresolved terms and the original failed session.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, SignalFrame, TradingMode
from ..engine import advance
from ..ledger import new_state
from ..market_dataset import write_json
from .baostock_daily_slice import FIELDS, digest
from .ipo_qualification import load_ipo_qualification
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import _config
from .leader_forward_run import ForwardRawFeed
from .research_corporate_action_view import load_research_corporate_action_view

SCHEMA = "leader_action_component_diagnostic_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "current_signal_admitted": False, "historical_signal_publication_verified": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "strategy_returns_reported": False, "user_account_mutated": False,
    "broker_interacted": False, "provider_contacted": False,
    "ths_payload_generated": False, "model_refit": False, "future_labels_used_for_score": False}


class AccountingScheduleFeed(ForwardRawFeed):
    def signal_frame(self, day, config, context):
        rows = ([{"symbol": self.symbols[0], "score": 1.}]
                if day == self.accounting_entry_decision_day else [])
        return SignalFrame.from_legacy(rows, day,
            "research:explicit_accounting_schedule_not_historical_strategy_signal", context)


def _yuan(cents):
    return float(Decimal(cents) / 100)


def run(review, panel, out):
    out, review, panel = (Path(p).resolve() for p in (out, review, panel))
    if out.exists():
        raise FileExistsError("An action component diagnostic cannot reset or overwrite its research books")
    started = datetime.now(MARKET_TZ)
    record = json.loads(panel.read_text(encoding="utf-8-sig"))
    reviewed = json.loads(review.read_text(encoding="utf-8-sig"))
    original_panel_hash, review_hash = digest(panel), digest(review)
    raw_file = (panel.parent / record["raw_file"]).resolve()
    if (digest(raw_file) != record["raw_sha256"] or record.get("candidate_panel_only") is not True
            or any(record.get(key) is not False for key in
                   ("execution_admitted", "selection_enabled", "strategy_validated", "model_refit", "future_labels_used_for_score"))):
        raise ValueError("The saved unadmitted native raw source changed")
    raw = pd.read_parquet(raw_file)
    days = record["source_days"]
    calendar_file = (panel.parent / record["calendar"]).resolve()
    if digest(calendar_file) != record["calendar_sha256"]:
        raise ValueError("The pinned exchange calendar changed")
    calendar = json.loads(calendar_file.read_text(encoding="utf-8"))
    sessions, _, status = _calendar_view(calendar, f"{calendar['year']}-01-01", f"{calendar['year']}-12-31", [])
    if (status != "pinned_static_exchange_calendar" or len(days) < 3
            or [d for d in sessions if days[0] <= d <= days[-1]] != days
            or sorted(raw.date.unique()) != days or raw.duplicated(["date", "code"]).any()
            or not set(FIELDS.split(",")) <= set(raw)):
        raise ValueError("An exact consecutive actual raw source prefix is required")
    events = reviewed["events"]
    symbols = [event["symbol"] for event in events]
    if len(symbols) != len(set(symbols)) or not symbols:
        raise ValueError("This bounded diagnostic retains one distinct reviewed event per symbol")
    if any(event["dividRegistDate"] != days[1] or event["dividOperateDate"] != days[2]
           or event["dividPayDate"] not in days for event in events):
        raise ValueError("The fixed schedule needs entry before its exact record/ex/pay chain")
    config = _config(100000, 5)
    protocol = (panel.parent / record["protocol_file"]).resolve()
    if digest(protocol) != record["protocol_sha256"] or json.loads(protocol.read_text(encoding="utf-8"))["config"] != config.to_dict():
        raise ValueError("Original shared Engine costs and tax reserve must remain unchanged")
    ipo = record["IPO_qualification"]
    qualification = load_ipo_qualification(ipo["evidence_file"], calendar_file,
        master_run=ipo["research_master"]["run"])
    raw = raw.copy()
    raw["symbol"] = raw.code.str[3:] + raw.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    raw = raw.loc[raw.symbol.isin(symbols)].copy()
    if len(raw) != len(symbols) * len(days):
        raise ValueError("Every reviewed symbol needs its actual raw row in each accounting session")
    for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"):
        raw[field] = pd.to_numeric(raw[field], errors="coerce")
    raw["preclose_for_limits"] = raw.preclose
    raw["is_st"] = raw.isST.eq("1")
    raw["is_st_unknown"] = ~raw.isST.isin(["0", "1"])
    raw["provider_raw_observation"] = True
    if raw.is_st_unknown.any() or raw.tradestatus.ne("1").any() or raw.is_st.any():
        raise ValueError("The accounting schedule requires observed active non-ST raw rows")
    cases = []
    # Each case is isolated; a failed symbol is retained without suppressing others.
    for event in events:
        symbol = event["symbol"]
        context = RunContext("action-component-diagnostic:" + sha256(str(out).encode()).hexdigest()[:16]
                             + ":" + symbol,
                             TradingMode.REPLAY)
        view = load_research_corporate_action_view(review, knowledge_mode="posthoc_component_diagnostic",
            context=context, config=config)
        feed = AccountingScheduleFeed(raw, sessions, [symbol], ipo_qualification=qualification,
                                      corporate_action_view=view)
        feed.accounting_entry_decision_day = days[0]
        state, daily, error = new_state(config.initial_cash), [], None
        for index, day in enumerate(days):
            trial = deepcopy(state)
            try:
                row = advance(trial, feed, day, config, index, context=context)
            except (ValueError, KeyError) as failure:
                error = {"day": day, "error": f"{type(failure).__name__}: {failure}",
                    "trial_committed": False, "failed_trial_orders": len(trial["orders"]),
                    "failed_trial_fills": len(trial["fills"])}
                break
            state = trial
            daily.append(row)
        identifier = symbol + ":" + event["dividOperateDate"]
        quantity = state["entitlements"].get(identifier)
        gross = (Decimal(str(event["dividCashPsBeforeTax"])) * quantity
                 if quantity is not None else None)
        modeled_net = (int((gross * (1 - Decimal(str(config.dividend_tax_reserve))) * 100)
                           .quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if gross is not None else None)
        payments = [item for item in state["events"] if item["kind"] == "dividend_paid" and item.get("symbol") == symbol]
        paid = sum(item["cash"] for item in payments)
        if error is None and (quantity is None or modeled_net != paid):
            raise ValueError("A completed component replay must reconcile its registered quantity and modeled dividend")
        exercised = error is None and quantity is not None and quantity > 0 and paid > 0
        folder = out / symbol
        write_json(folder / "state.json", state)
        write_json(folder / "daily.json", daily)
        cases.append({"symbol": symbol, "context": context.to_dict(),
            "status": ("blocked_unresolved_component_preserved" if error else
                "cash_component_reconciled_research_only" if exercised else "no_positive_entitlement_component_not_exercised"),
            "source_share_terms_basis": event.get("share_terms_basis"),
            "processed_days": [row["date"] for row in daily], "error": error,
            "registered_quantity": quantity, "gross_dividend_rmb": None if gross is None else float(gross),
            "modeled_tax_reserve_rate": config.dividend_tax_reserve,
            "modeled_net_dividend_rmb": None if modeled_net is None else _yuan(modeled_net),
            "cash_paid_rmb": _yuan(paid), "dividend_payment_events": payments,
            "open_receivables": deepcopy(state["receivables"]),
            "remaining_positions": deepcopy(state["positions"]),
            "pending_target_weights": deepcopy(state.get("pending_targets")),
            "orders": len(state["orders"]), "fills": len(state["fills"]),
            "state_file": str(folder / "state.json"), "daily_file": str(folder / "daily.json"),
            "action_binding": deepcopy(state.get("research_corporate_action_binding")),
            "action_evidence_binding": view.binding, "metrics": None, **FLAGS})
    if digest(panel) != original_panel_hash or digest(raw_file) != record["raw_sha256"] or digest(review) != review_hash:
        raise ValueError("Original evidence changed during the component replay")
    result = {"schema": SCHEMA, "started_at": started.isoformat(),
        "completed_at": datetime.now(MARKET_TZ).isoformat(),
        "knowledge_mode": "posthoc_component_diagnostic", "accounting_schedule_is_strategy": False,
        "accounting_schedule": "D0 fixed entry score; later explicit empty full targets; no fitted parameter or published historical signal",
        "source": {"panel": str(panel), "panel_sha256": original_panel_hash,
            "raw_file": str(raw_file), "raw_sha256": record["raw_sha256"],
            "review": str(review), "review_sha256": review_hash},
        "config": config.to_dict(), "source_days": days, "case_count": len(cases), "cases": cases,
        "ordinary_components_reconciled": sum(case["status"] == "cash_component_reconciled_research_only" for case in cases),
        "not_exercised_cases_retained": sum(case["status"] == "no_positive_entitlement_component_not_exercised" for case in cases),
        "unresolved_cases_retained": sum(case["error"] is not None for case in cases),
        "real_new_close_processed": False, "metrics": None, **FLAGS}
    write_json(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("review", "panel", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.review, args.panel, args.out)
    print(json.dumps({"file": str(args.out.resolve() / "result.json"),
        "ordinary_components_reconciled": result["ordinary_components_reconciled"],
        "unresolved_cases_retained": result["unresolved_cases_retained"], **FLAGS}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
