"""Same-day matched crossing selection component through the sole Engine.

This independent ticket experiment fixes opportunities, budgets and expiry. It
does not reconstruct the old eight account paths or select a trading strategy.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import mean, median

import pandas as pd

from ..contracts import MARKET_TZ, RunContext, TradingMode
from ..engine import advance, choose_targets
from ..ledger import new_state
from ..market_dataset import write_json
from ..report import metrics
from ..rules import stock_board
from .baostock_daily_slice import digest
from .leader_eight_type_historical_native import load_inputs
from .leader_eight_type_panel_replay import _QualifiedFeed, _config
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .leader_known_identity_scope import CASE_IDS, TYPE_ID
from .leader_posthoc_industry import load as load_industry
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .shortline_decision_v2 import BATCH_SCHEMA, fixed_exit_policy, holding_origin
from .shortline_decision_v3 import FiveSessionExpiryPolicy
from .stockdb_action_accounting import load_action_accounting

METHOD_ID = "same_day_board_industry_amount_matched_crossing_selection_component_v1"
REG_SCHEMA = "leader_selection_component_matched_control_registration_v1"
SCHEMA = "leader_selection_component_matched_control_result_v1"
FLAGS = {"candidate_panel_only": True, "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "returns_reportable": False, "pit_verified": False,
    "independent_holdout_claimed": False, "model_refit": False, "provider_contacted": False,
    "original_accounts_modified": False, "original_accounts_replayed": False,
    "same_source_scope": True, "signal_candidate_pool_identical": False}


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _stamp(value):
    # Reuse close_workbench.process_identity's .NET/Python3.10 normalization.
    # Keep the original saved clock; only its comparison view is microseconds.
    text = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)", r"\1", str(value))
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Selection registration clock requires an explicit timezone")
    return stamp


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _verify(reference):
    path = reference.get("file", reference.get("path"))
    if digest(path) != reference["sha256"]:
        raise ValueError("Registered selection-component source changed")
    return _read(path)


def _slot_config():
    # 5 isolated20k wallets total100k; 80% of one wallet equals the original
    # 16% candidate budget. The 4k remainder pays costs and remains cash.
    return replace(_config(20000, 1), single_limit=.8).validate()


def _opportunity_key(period, day, symbols, weights):
    return _hash([period, day, list(symbols), weights])


def load_registration(path):
    reg = _read(path)
    if (reg.get("schema") != REG_SCHEMA or reg.get("method_id") != METHOD_ID
            or _stamp(reg["registered_at"]) > datetime.now(MARKET_TZ)
            or any(reg.get(k) is not v for k, v in FLAGS.items() if k in reg)):
        raise ValueError("An actual pre-run fixed selection-component registration is required")
    protocol = reg["protocol"]
    required = {"parent_budget_cny": 100000, "slots": 5, "slot_wallet_cny": 20000,
        "original_slot_weight": .16, "slot_target_weight": .8, "slot_target_cny": 16000,
        "type_specific_structural_exit": False, "same_source_scope": True,
        "signal_candidate_pool_identical": False, "no_original_accounts_replayed": True,
        "no_new_source": True, "no_tuning": True}
    if any(protocol.get(k) != v for k, v in required.items()):
        raise ValueError("Registered budget, source-scope or fixed component rule changed")
    inventory = _verify(reg["inventory"])
    sources = {row.get("path", row.get("file")): _verify(row) for row in reg["decision_sources"]}
    opportunities, counts = [], Counter()
    seen, actual_references = set(), Counter()
    for opportunity in inventory["opportunities"]:
        symbols = opportunity["ordered_candidate_symbols"]
        rows = [{"symbol": s, "score": len(symbols) - i} for i, s in enumerate(symbols)]
        weights = choose_targets(rows, [], _config(100000, 5))
        if (weights != opportunity["pre_exit_target_weights"] or not 0 < len(symbols) <= 5
                or len(symbols) != len(set(symbols)) or any(w != .16 for w in weights.values())):
            raise ValueError("Original ordered slots and pre-exit choose_targets budgets must remain exact")
        references = opportunity["references"]
        periods = {r["period"] for r in references}
        if len(periods) != 1:
            raise ValueError("A unique opportunity cannot cross source periods")
        period = next(iter(periods))
        key = _opportunity_key(period, opportunity["decision_day"], symbols, weights)
        if key in seen:
            raise ValueError("Identical source-D/candidates/budget must reuse one opportunity result")
        seen.add(key)
        for reference in references:
            if reference["case_id"] not in CASE_IDS:
                raise ValueError("All and only the four original crossing strata are retained")
            saved = [row for row in sources[reference["decisions_file"]]
                if row["day"] == reference["decision_day"] and row["status"] == "FULL_TARGET"]
            if (len(saved) != 1 or reference["decision_day"] != opportunity["decision_day"]
                    or [r["symbol"] for r in saved[0]["cap_candidates"]] != symbols
                    or saved[0]["execution_day"] != opportunity["execution_day"]):
                raise ValueError("Every original FULL_TARGET D and ordered cap_candidates must be preserved")
            counts[(period, reference["case_id"])] += 1
            actual_references[(reference["decisions_file"], reference["decision_day"])] += 1
        opportunities.append({**deepcopy(opportunity), "period": period, "economic_opportunity_key": key})
    # Prove completeness from all source rows, rather than merely validating a
    # supplied subset of previously profitable or eventually executable days.
    original_count = sum(sum(row["status"] == "FULL_TARGET" for row in rows) for rows in sources.values())
    reference_count = sum(len(o["references"]) for o in opportunities)
    actual = {"case_D_references": reference_count,
        "candidate_slot_references": sum(len(o["ordered_candidate_symbols"]) * len(o["references"]) for o in opportunities),
        "unique_opportunities": len(opportunities),
        "unique_ticket_pairs": sum(len(o["ordered_candidate_symbols"]) for o in opportunities),
        "unique_decision_days": len({o["decision_day"] for o in opportunities})}
    expected_references = Counter({(path, row["day"]): 1 for path, rows in sources.items()
        for row in rows if row["status"] == "FULL_TARGET"})
    if original_count != reference_count or actual_references != expected_references or actual != reg["expected_counts"]:
        raise ValueError("The complete original eight-case opportunity inventory or dedup counts changed")
    for period in reg["periods"]:
        for field in ("input", "prior_method_registration", "calendar", "industry_receipt", "action_event", "action_reference"):
            _verify(period[field])
    if {p["inventory_period"] for p in reg["periods"]} != {o["period"] for o in opportunities}:
        raise ValueError("All registered opportunity periods must run, without period selection")
    return reg, opportunities, actual


def match_opportunity(opportunity, panel, rows, scope, qualification, industry):
    """Only D inputs are accepted. No following raw panel or return exists here."""
    day = opportunity["decision_day"]
    item = next(t for t in panel["types"] if t["id"] == TYPE_ID)
    if panel.get("day", day) != day or panel["environment"].get("source_complete") is not True:
        raise ValueError("Matching requires the exact original complete D panel")
    mapping = (industry or {}).get("membership", {})
    if industry is not None and industry["snapshot_date"] > day:
        raise ValueError("A future broad-industry membership cannot enter matching")
    unknown = set(item.get("unresolved_symbols", []))
    matched = {r["symbol"] for r in item["candidates"]}
    negative = set(item.get("known_exclusions", {})) - unknown - matched
    eligibility, candidate_pool = {}, []
    for symbol in sorted(set(scope) & negative):
        raw, feature = rows.get(symbol), panel.get("risk_features", {}).get(symbol, {})
        amount = None if raw is None else raw.get("amount")
        if (raw is None or raw.get("tradestatus") != 1 or raw.get("isST") != 0
                or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0
                or feature.get("date") != day or feature.get("ipo_feature_support") is not True
                or not mapping.get(symbol)):
            continue
        proof = qualification.assess(symbol, day, decision_as_of=day + "T15:05:00+08:00")
        if proof["engine_block_reason"] is not None or proof.get("ipo_window_feature_support") is not True:
            continue
        candidate_pool.append(symbol)
        eligibility[symbol] = proof
    used, tickets = set(), []
    for index, symbol in enumerate(opportunity["ordered_candidate_symbols"]):
        raw, sector = rows.get(symbol), mapping.get(symbol)
        amount = None if raw is None else raw.get("amount")
        reason, control, distance = None, None, None
        if not sector:
            reason = "leader_D_broad_industry_unknown"
        elif not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            reason = "leader_D_amount_unknown"
        else:
            pool = [s for s in candidate_pool if s not in used and s != symbol
                and mapping[s] == sector and stock_board(s) == stock_board(symbol)]
            if not pool:
                reason = "no_same_D_qualified_known_negative_same_board_industry_control"
            else:
                control = min(pool, key=lambda s: (abs(math.log(rows[s]["amount"]) - math.log(amount)), s))
                distance = abs(math.log(rows[control]["amount"]) - math.log(amount))
                used.add(control)
        tickets.append({"slot": index + 1, "leader_symbol": symbol, "control_symbol": control,
            "match_status": "matched" if control else "unmatched", "match_failure": reason,
            "D": day, "E": opportunity["execution_day"], "board": stock_board(symbol), "broad_industry": sector,
            "industry_snapshot": (industry or {}).get("snapshot_date"), "leader_D_amount_cny": amount,
            "control_D_amount_cny": rows[control]["amount"] if control else None, "abs_log_amount_distance": distance,
            "control_IPO_evidence": eligibility.get(control), "future_bar_used_for_matching": False,
            "slot_wallet_cny": 20000, "slot_target_cny": 16000})
    return {**deepcopy(opportunity), "tickets": tickets, "matching_candidate_pool_count": len(candidate_pool),
        "same_source_scope": True, "signal_candidate_pool_identical": False}


def _matching_index(raw, wanted_days, scope):
    """Index only D amount/status, never following prices or labels."""
    frame = raw.loc[raw.date.isin(wanted_days), ["date", "code", "amount", "tradestatus", "isST"]].copy()
    frame["symbol"] = frame.code.str[3:] + frame.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
    frame = frame.loc[frame.symbol.isin(scope)]
    frame["amount"] = pd.to_numeric(frame.amount, errors="coerce")
    result = {day: {} for day in wanted_days}
    for row in frame.to_dict("records"):
        symbol, day = row["symbol"], row["date"]
        if symbol in result[day]:
            raise ValueError("Duplicate exact D matching identity")
        result[day][symbol] = {"amount": float(row["amount"]) if pd.notna(row["amount"]) else None,
            "tradestatus": int(row["tradestatus"]) if row["tradestatus"] in ("0", "1") else None,
            "isST": int(row["isST"]) if row["isST"] in ("0", "1") else None}
    return result


def _run_arm(execution, qualification, symbol, opportunity, context_name):
    """One isolated20k wallet, one16k entry, existing actual-fill expiry only."""
    config = _slot_config()
    day, execution_day = opportunity["decision_day"], opportunity["execution_day"]
    days = [d for d in execution.base.calendar if d >= day]
    if not days or len(days) < 2 or days[1] != execution_day:
        raise ValueError("Exact D and D+1 must be present in the saved source calendar")
    state, daily, blocked = new_state(config.initial_cash), [], None
    context = RunContext(context_name, TradingMode.REPLAY)
    batches = {day: {"schema": BATCH_SCHEMA, "day": day, "status": "complete",
        "source": "research:" + METHOD_ID, "candidates": [{"symbol": symbol, "score": 1.}]}}
    for current in days[1:]:
        batches[current] = {"schema": BATCH_SCHEMA, "day": current, "status": "incomplete",
            "source": "research:" + METHOD_ID, "candidates": []}
    common = execution.decision_feed(batches, state)
    feed = _QualifiedFeed(common, qualification)
    risk = execution.wrap_risk_policy(FiveSessionExpiryPolicy())
    origin, expiry_decision = None, None
    for ordinal, current in enumerate(days):
        required = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        required.update(state.get("pending_targets") or {})
        if current == day:
            required.add(symbol)
        issues, references = execution.raw_issues(current, required), execution.references(current, required)
        ipo = [qualification.assess(s, current, decision_as_of=current + ("T15:05:00+08:00" if current == day else "T09:30:00+08:00"))
            for s in sorted(required)]
        if issues or any(not execution.reference_ok(r) for r in references) or any(p["engine_block_reason"] is not None for p in ipo):
            blocked = {"day": current, "reason": "owned_pending_or_entry_source_unknown", "raw_issues": issues,
                "reference_evidence": references, "IPO_evidence": ipo, "session_not_committed": True}
            break
        trial = deepcopy(state)
        # The common feed owns the currently attempted trial, not a stale copy.
        common.state = trial
        try:
            row = advance(trial, feed, current, config, ordinal, context=context,
                target_policy=fixed_exit_policy(feed, trial, config), replay_risk_exit_policy=risk)
        except (ValueError, KeyError) as error:
            blocked = {"day": current, "reason": "shared_engine_session_blocked", "error": f"{type(error).__name__}: {error}",
                "session_not_committed": True, "trial_committed": False}
            break
        state = trial
        daily.append(row)
        if origin is None:
            origin = holding_origin(state, symbol)
            if origin is not None:
                index = execution.base.calendar.index(origin)
                expiry_decision = (execution.base.calendar[index + 4]
                    if index + 4 < len(execution.base.calendar) else None)
        held = sum(lot["qty"] for lot in state["positions"].get(symbol, []))
        if (current >= execution_day and not held and not state.get("pending_targets")
                and not state.get("pending_risk_exits") and not state.get("receivables")):
            break
    fills = list(state["fills"].values())
    buys, sells = [f for f in fills if f["side"] == "buy"], [f for f in fills if f["side"] == "sell"]
    residual = {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    complete = (bool(buys) and not residual and blocked is None and not state.get("pending_targets")
        and not state.get("pending_risk_exits") and not state.get("receivables"))
    final_day = daily[-1]["date"] if daily else None
    last_sell = max((f["day"] for f in sells), default=None)
    last_holding_observation = last_sell if not residual and last_sell else final_day
    max_held_sessions = (execution.base.calendar.index(last_holding_observation) - execution.base.calendar.index(origin) + 1
        if origin and last_holding_observation else None)
    stock_values = [float(r["equity"]) - float(r["cash"]) - float(r.get("receivables", 0)) for r in daily]
    return {"context": context.to_dict(), "symbol": symbol, "D": day, "E": execution_day,
        "config": config.to_dict(), "status": "complete_flat_with_fills" if complete else "source_blocked" if blocked else "residual_or_pending" if residual else "no_fill",
        "complete_PnL_available": complete, "metrics": metrics(daily, config.initial_cash) if complete else None,
        "net_PnL_cny": (state["cash"] + sum(state.get("receivables", {}).values())) / 100 - config.initial_cash if complete else None,
        "engine_sessions_committed": len(daily), "last_committed_day": final_day, "blocking_evidence": blocked,
        "residual_quantities": residual, "pending_targets": deepcopy(state.get("pending_targets")),
        "pending_risk_exits": deepcopy(state.get("pending_risk_exits")),
        "buy_fill_notional_cny": sum(f["qty"] * f["price"] for f in buys) / 100,
        "sell_fill_notional_cny": sum(f["qty"] * f["price"] for f in sells) / 100,
        "fees_cny": sum(f["fees"] for f in fills) / 100,
        "entry_fill_day": origin, "expiry_decision_day": expiry_decision,
        "last_sell_fill_day": last_sell,
        "observed_sessions_from_first_buy_including_final_session": max_held_sessions,
        "overdue_after_five_holding_sessions": bool(max_held_sessions is not None and max_held_sessions > 6),
        "strict_five_day_realized_return_claimed": False,
        "summed_close_stock_value_cny": sum(stock_values), "max_close_stock_value_cny": max(stock_values, default=None),
        "order_blocked_events": [e for r in daily for e in r.get("events", []) if e["kind"] == "order_blocked"],
        "daily": daily, "account": state, **FLAGS}


def _pair(ticket, leader, control):
    complete = leader["complete_PnL_available"] and control is not None and control["complete_PnL_available"]
    return {**deepcopy(ticket), "leader": leader, "control": control,
        "complete_pair": complete,
        "net_PnL_difference_cny": leader["net_PnL_cny"] - control["net_PnL_cny"] if complete else None,
        "entry_notional_difference_cny": leader["buy_fill_notional_cny"] - control["buy_fill_notional_cny"] if control else None,
        "fee_difference_cny": leader["fees_cny"] - control["fees_cny"] if control else None}


def _aggregate(opportunities, period):
    selected = [o for o in opportunities if o["period"] == period]
    complete = [o for o in selected if o["complete_basket"]]
    pairs = [p for o in selected for p in o["pairs"]]
    valid = [p for p in pairs if p["complete_pair"]]
    differences = [o["net_return_difference"] for o in complete]
    return {"inventory_period": period, "unique_decision_days": len({o["decision_day"] for o in selected}),
        "unique_opportunities": len(selected), "original_case_D_references": sum(len(o["references"]) for o in selected),
        "unique_ticket_pairs": len(pairs), "matched_pairs": sum(p["match_status"] == "matched" for p in pairs),
        "complete_pairs": len(valid), "incomplete_pairs": len(pairs) - len(valid), "complete_baskets": len(complete),
        "incomplete_baskets": len(selected) - len(complete),
        "mean_unique_complete_basket_return_difference": mean(differences) if differences else None,
        "median_unique_complete_basket_return_difference": median(differences) if differences else None,
        "mean_complete_pair_PnL_difference_cny": mean(p["net_PnL_difference_cny"] for p in valid) if valid else None,
        "coverage_conditioning_explicit": True, "independent_samples_claimed": False,
        "paired_dates_are_not_independent_samples": True, "pooled_strategy_equity_computed": False}


def run(registration, out):
    reg, inventory, counts = load_registration(registration)
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use one new selection-component result directory; never reset or silently retry")
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "attempt.json", {"started_at": _now(), "status": "running", "registration": str(Path(registration).resolve()), **FLAGS})
    results, source_records = [], []
    for period in reg["periods"]:
        loaded, manifest = load_inputs(period["input"]["file"])
        provider = load_industry(period["industry_root"], _stamp(reg["registered_at"]).isoformat())
        # Convert the full source to a matching index once. No E row is passed
        # to match_opportunity; only the exact source D dictionary is available.
        opportunities = [o for o in inventory if o["period"] == period["inventory_period"]]
        matching_rows = _matching_index(loaded["raw"], {o["decision_day"] for o in opportunities}, loaded["scope_members"])
        matched = [match_opportunity(o, loaded["panel_by_day"][o["decision_day"]],
            matching_rows[o["decision_day"]], loaded["scope_members"], loaded["qualification"], provider(o["decision_day"]))
            for o in opportunities]
        write_json(out / period["id"] / "matches.json", matched)
        symbols = sorted({p[key] for o in matched for p in o["tickets"] for key in ("leader_symbol", "control_symbol") if p[key]})
        del matching_rows
        context_raw = pd.read_parquet(manifest["context_raw_file"])
        raw = pd.concat([context_raw, loaded["raw"]], ignore_index=True)
        days = sorted(set(context_raw.date) | set(loaded["source_days"]))
        base = ExactSourceRawFeed(raw, days, symbols, context_anchor=loaded["context_anchor"])
        accounting = load_action_accounting(period["action_event_view"], period["action_reference_view"],
            symbols=symbols, raw_days=days, replay_days=loaded["source_days"], price_source="independent_baostock_raw")
        factory = HistoricalExecutionFactory(accounting)
        execution = factory(base, loaded["source_days"])
        source_records.append({"period": period["id"], "input": period["input"],
            "action_event_view": period["action_event_view"], "action_reference_view": period["action_reference_view"],
            "accounting": accounting.provenance, "industry": provider.provenance,
            "loaded_once_per_period": True, "matching_uses_full_registered_scope": True,
            "execution_symbol_subset_after_matching": len(symbols)})
        for opportunity in matched:
            pairs = []
            for ticket in opportunity["tickets"]:
                name = METHOD_ID + ":" + opportunity["opportunity_id"] + ":" + str(ticket["slot"])
                leader = _run_arm(execution, loaded["qualification"], ticket["leader_symbol"], opportunity, name + ":leader")
                control = (_run_arm(execution, loaded["qualification"], ticket["control_symbol"], opportunity, name + ":control")
                    if ticket["control_symbol"] else None)
                pair = _pair(ticket, leader, control)
                pairs.append(pair)
                write_json(out / period["id"] / opportunity["opportunity_id"] / ("slot_" + str(ticket["slot"]) + ".json"), pair)
            complete = bool(pairs) and all(p["complete_pair"] for p in pairs)
            lead_pnl = sum(p["leader"]["net_PnL_cny"] for p in pairs) if complete else None
            ctrl_pnl = sum(p["control"]["net_PnL_cny"] for p in pairs) if complete else None
            record = {**{k: deepcopy(v) for k, v in opportunity.items() if k != "tickets"},
                "pairs": pairs, "complete_basket": complete, "parent_budget_cny_per_side": 100000,
                "unused_slot_cash_cny": (5 - len(pairs)) * 20000,
                "leader_basket_net_return": lead_pnl / 100000 if complete else None,
                "control_basket_net_return": ctrl_pnl / 100000 if complete else None,
                "net_return_difference": (lead_pnl - ctrl_pnl) / 100000 if complete else None,
                "same_D_opportunity_references_are_not_independent": True}
            results.append(record)
            write_json(out / "progress.json", {"at": _now(), "completed_opportunities": len(results), "total": len(inventory),
                "unique_decision_days": counts["unique_decision_days"], "execution_admitted": False})
        del raw, base, execution, loaded, context_raw
    summaries = [_aggregate(results, period["inventory_period"]) for period in reg["periods"]]
    strata = []
    for period in reg["periods"]:
        for case_id in CASE_IDS:
            rows = [o for o in results if o["period"] == period["inventory_period"]
                and any(r["case_id"] == case_id for r in o["references"])]
            valid = [o for o in rows if o["complete_basket"]]
            strata.append({"period": period["id"], "case_id": case_id, "original_case_D_references": len(rows),
                "candidate_slot_references": sum(len(o["pairs"]) for o in rows), "complete_opportunity_references": len(valid),
                "mean_complete_basket_return_difference": mean(o["net_return_difference"] for o in valid) if valid else None,
                "references_reuse_existing_unique_results": True, "independent_samples_claimed": False})
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "completed_at": _now(),
        "registration": {"file": str(Path(registration).resolve()), "sha256": digest(registration)},
        "registered_at": reg["registered_at"], "counts": counts, "periods": summaries, "strata": strata,
        "sources": source_records, "opportunities": results, "numerical_budget": reg["protocol"],
        "primary_unit": "29_unique_decision_dates_with_overlapping_opportunities_not_332_independent_accounts",
        "source_unknowns_triggered_collection": False, **FLAGS}
    write_json(out / "result.json", result)
    write_json(out / "attempt.json", {"status": "completed", "completed_at": result["completed_at"],
        "unique_opportunities": len(results), "source_dates": counts["unique_decision_days"], **FLAGS})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run")
    command.add_argument("--registration", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.registration, args.out)
    print(json.dumps({"schema": result["schema"], "counts": result["counts"], "periods": result["periods"], **FLAGS}, ensure_ascii=False))
    return 0
