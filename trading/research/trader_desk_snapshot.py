"""Build a trader-facing paper desk snapshot from an opening pack."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path

from ..config import StrategyConfig
from ..rules import cents, fees as ledger_fees, notional_cents
from ..routing import route_order
from ..settings import DATA_ROOT, ROOT
from .exchange_calendar import next_exchange_session
from .strategy_board import build as build_strategy_board


INSTITUTIONAL_SOURCES = {
    "bloomberg_aim": "https://professional.bloomberg.com/products/trading/order-management-system/aim/",
    "bloomberg_ems": "https://professional.bloomberg.com/products/trading/execution-management-system/",
    "bloomberg_automation": "https://professional.bloomberg.com/products/trading/automation/",
    "bloomberg_market_data": "https://www.bloomberg.com/professional/products/market-data/",
    "flextrader": "https://flextrade.com/products/flextrader-execution-management-system/",
    "flextca": "https://flextrade.com/products/flextca/",
}


def _qualified_order_identity(symbol: object) -> str | None:
    """Accept only an already exchange-qualified six-digit order identity."""
    value = str(symbol or "").strip().upper()
    code, separator, market = value.partition(".")
    if separator and len(code) == 6 and code.isdigit() and market in {"SSE", "SZSE", "BSE"}:
        return value
    return None


def money(value: object) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _load_ths_readonly_evidence(path: str | Path | None) -> tuple[dict | None, Path | None]:
    if not path:
        return None, None
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if payload.get("schema") != "ths_readonly_evidence_index_v1":
        raise ValueError("THS read-only evidence index schema mismatch")
    return payload, source


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _order_key(row):
    return (row.get("strategy_id", ""), row.get("symbol", ""), row.get("side", ""))


def _saved_no_trade_holdings(pack, strategies):
    """Accept empty operations only when saved previews prove exact retention."""
    target_path = (pack.get("artifacts") or {}).get("candidate_targets")
    if not target_path:
        raise ValueError("NO_TRADE opening pack is missing its saved target artifact")
    targets = read_csv(Path(target_path))
    holdings = []
    for strategy in strategies:
        identifier = strategy.get("strategy_id")
        zero_fields = ("candidate_order_count", "buys", "sells",
                       "ledger_preflight_accepted", "ledger_preflight_rejected")
        if (not identifier or any(strategy.get(key) != 0 for key in zero_fields)
                or Decimal(str(strategy.get("estimated_notional_rmb"))) != 0
                or strategy.get("ledger_preflight_status") != "completed"):
            raise ValueError("Empty opening pack is not an explicit zero-operation strategy")
        preview = json.loads(Path(strategy["preview"]).read_text(encoding="utf-8-sig"))
        if (preview.get("status") != "paper_preview_only"
                or preview.get("execution_admitted") is not False
                or preview.get("trade_status") != "NO_TRADE"
                or preview.get("strategy_id") != identifier
                or preview.get("source_cutoff") != pack.get("source_cutoff")
                or preview.get("execution_day") != pack.get("execution_day")
                or preview.get("provisional_order_count") != 0):
            raise ValueError("Empty opening pack requires a matching saved NO_TRADE preview")
        preview_artifacts = preview.get("artifacts") or {}
        saved = read_csv(Path(preview_artifacts["target_holdings"]))
        saved_orders = read_csv(Path(preview_artifacts["provisional_order_plan"]))
        if any(int(row.get("qty", "")) != 0 for row in saved_orders):
            raise ValueError("Saved NO_TRADE preview contains an order")
        retained = {}
        for row in saved:
            symbol = row.get("symbol")
            current_qty, target_qty = int(row["current_qty"]), int(row["target_qty"])
            if not symbol or symbol in retained or current_qty != target_qty or current_qty < 0:
                raise ValueError("NO_TRADE preview does not preserve saved holding quantities")
            retained[symbol] = target_qty
        selected_targets = [row for row in targets if row.get("strategy_id") == identifier]
        if (len(selected_targets) != len(retained)
                or {row["symbol"]: int(row["target_qty"]) for row in selected_targets} != retained
                or preview.get("position_count") != sum(qty > 0 for qty in retained.values())):
            raise ValueError("NO_TRADE opening targets differ from saved holdings")
        holdings.extend({"strategy_id": identifier, "symbol": symbol, "quantity": qty,
                         "source_preview": strategy["preview"],
                         "basis": "saved_research_holding_not_broker_observation"}
                        for symbol, qty in retained.items())
    return holdings


def _risk_checks(orders, preflight, current_snapshot, current_quotes, execution_scope,
                 instrument_master=None):
    """Emit operator-facing checks without turning paper inputs into live approval."""
    preflight_by_key = {_order_key(row): row for row in preflight}
    checks = []
    for row in orders:
        symbol, side = row.get("symbol", ""), row.get("side", "")
        strategy_id = row.get("strategy_id", "")
        qty_text = row.get("qty", "")
        try:
            qty = int(qty_text)
        except (TypeError, ValueError):
            qty = 0
        copied = preflight_by_key.get((symbol, side), {})
        if not copied:
            copied = preflight_by_key.get((strategy_id, symbol, side), {})
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "copied_ledger_preflight",
            "status": copied.get("preflight_status", "unknown"),
            "reason": copied.get("preflight_reason", ""),
            "basis": "existing unified ledger preflight",
        })
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "a_share_buy_lot",
            "status": "pass" if side != "buy" or qty > 0 and qty % 100 == 0 else "review",
            "reason": "buy quantity is a 100-share lot" if side != "buy" or qty > 0 and qty % 100 == 0 else "buy quantity is not a positive 100-share lot",
            "basis": "paper rule check; sell odd-lot handling remains account-specific",
        })
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "same_session_account",
            "status": "pass" if current_snapshot is not None else "blocked",
            "reason": "normalized account observation supplied" if current_snapshot is not None else "no same-session account observation",
            "basis": "required before account-sized plan or broker prefill",
        })
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "same_session_raw_rmb_quote",
            "status": "pass" if current_quotes is not None else "blocked",
            "reason": "raw RMB quote observation supplied" if current_quotes is not None else "no same-session raw RMB quote",
            "basis": "historical pack price is never treated as current",
        })
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "execution_scope",
            "status": "review" if execution_scope == "strict" else "scoped",
            "reason": f"paper scope={execution_scope}; live submission is disabled",
            "basis": "THS adapter boundary",
        })
        master_row = instrument_master.get(str(symbol).upper()) if instrument_master is not None else None
        identity_fallback = bool(master_row and master_row.get("identity_basis") == "qualified_order_symbol")
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "instrument_master_identity",
            "status": "pass" if master_row else "blocked",
            "reason": "canonical symbol is present in the cutoff-matched research master"
                      if master_row and not identity_fallback else
                      "exchange-qualified order symbol is syntactically valid; PIT master is still absent"
                      if identity_fallback else "no cutoff-matched research instrument master record",
            "basis": "instrument_master_v1; stable code/exchange identity only"
                      if not identity_fallback else "qualified order symbol fallback; identity only, no PIT status",
        })
        observed_status = (master_row or {}).get("observed_status")
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "instrument_master_observed_status",
            "status": "pass" if observed_status == "active_observed" else "review" if master_row else "blocked",
            "reason": "source snapshot observed the security as active"
                      if observed_status == "active_observed" else
                      "PIT/status evidence is unknown; identity fallback does not establish eligibility"
                      if identity_fallback else
                      "source snapshot observed the security as inactive" if master_row else
                      "instrument master record unavailable",
            "basis": "retrieval-time provider status; not point-in-time eligibility",
        })
        checks.append({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "side": side,
            "qty": qty_text,
            "check": "instrument_master_pit_status",
            "status": "review" if master_row else "blocked",
            "reason": "master has no historical PIT status intervals; research-only review required"
                      if master_row and not identity_fallback else
                      "identity fallback has no PIT status intervals; research-only review required"
                      if identity_fallback else "instrument master record unavailable",
            "basis": "permanent identity artifact does not grant execution admission",
        })
    return checks


def _write_static_risk_summary(out, orders, preflight, config, current_snapshot, current_quotes, execution_scope):
    """Summarize deterministic release checks without inventing account capacity."""
    preflight_by_key = {_order_key(row): row for row in preflight}
    buy_rows = [row for row in orders if row.get("side") == "buy"]
    sell_rows = [row for row in orders if row.get("side") == "sell"]

    def notional(row):
        try:
            return Decimal(str(row.get("price_rmb") or "0")) * int(row.get("qty") or 0)
        except (TypeError, ValueError, InvalidOperation):
            return Decimal("0")

    buy_notional = sum((notional(row) for row in buy_rows), Decimal("0"))
    sell_notional = sum((notional(row) for row in sell_rows), Decimal("0"))
    all_notionals = [notional(row) for row in orders]
    accepted = sum(preflight_by_key.get(_order_key(row), {}).get("preflight_status") == "accepted" for row in orders)
    buy_lot_valid = 0
    for row in buy_rows:
        try:
            qty = int(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        buy_lot_valid += int(qty > 0 and qty % 100 == 0)

    rows = [
        {"metric": "paper_order_count", "value": str(len(orders)), "status": "informational", "basis": "frozen candidate order set", "next_required_evidence": "none"},
        {"metric": "paper_preflight_accepted_count", "value": f"{accepted}/{len(orders)}", "status": "pass" if accepted == len(orders) else "review", "basis": "copied unified ledger preflight", "next_required_evidence": "none"},
        {"metric": "gross_reference_notional_rmb", "value": f"{(buy_notional + sell_notional):.2f}", "status": "informational", "basis": "frozen research reference price; no fill", "next_required_evidence": "same-session quote and account-sized plan"},
        {"metric": "buy_reference_notional_rmb", "value": f"{buy_notional:.2f}", "status": "informational", "basis": "frozen research reference price; no fill", "next_required_evidence": "available cash and account restrictions"},
        {"metric": "sell_reference_notional_rmb", "value": f"{sell_notional:.2f}", "status": "informational", "basis": "frozen research reference price; no fill", "next_required_evidence": "sellable quantity and same-session quote"},
        {"metric": "max_single_order_notional_rmb", "value": f"{max(all_notionals, default=Decimal('0')):.2f}", "status": "informational", "basis": "frozen candidate order set", "next_required_evidence": "account equity for configured concentration limit"},
        {"metric": "buy_lot_validity", "value": f"{buy_lot_valid}/{len(buy_rows)}", "status": "pass" if buy_lot_valid == len(buy_rows) else "review", "basis": "paper A-share 100-share buy-lot rule", "next_required_evidence": "broker instrument and account validation"},
        {"metric": "configured_gross_limit_pct", "value": str(config.get("gross_limit", "")), "status": "not_assessed", "basis": "configured strategy policy; account equity unavailable", "next_required_evidence": "current account equity"},
        {"metric": "configured_single_limit_pct", "value": str(config.get("single_limit", "")), "status": "not_assessed", "basis": "configured strategy policy; account equity unavailable", "next_required_evidence": "current account equity and current positions"},
        {"metric": "same_session_account", "value": "observed" if current_snapshot is not None else "missing", "status": "pass" if current_snapshot is not None else "blocked", "basis": "normalized THS account observation", "next_required_evidence": "current account observation"},
        {"metric": "same_session_raw_rmb_quotes", "value": "observed" if current_quotes is not None else "missing", "status": "pass" if current_quotes is not None else "blocked", "basis": "same-session quote observation", "next_required_evidence": "current raw RMB quotes"},
        {"metric": "execution_scope", "value": execution_scope, "status": "review", "basis": "THS live submission disabled", "next_required_evidence": "manual review; no automatic release"},
    ]
    path = out / "static_risk_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _load_stockdb_forward_compare(path):
    """Load an independent frozen-StockDB forward diagnostic for desk context only."""
    if not path:
        return None, {}
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != "stockdb_forward_compare_v1":
        raise ValueError(f"Unsupported StockDB forward comparison: {source}")
    by_candidate = {
        str(row.get("candidate", "")): row
        for row in payload.get("candidates") or []
        if row.get("candidate")
    }
    return payload, by_candidate


def _load_stockdb_ledger_replay(path):
    """Load the shared-Engine/Ledger replay summary for comparison context only."""
    if not path:
        return None, {}
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != "stockdb_shared_ledger_replay_v1":
        raise ValueError(f"Unsupported StockDB Ledger replay: {source}")
    by_candidate = {
        str(row.get("candidate", "")): row
        for row in payload.get("candidates") or []
        if row.get("candidate")
    }
    return payload, by_candidate


def _stockdb_candidate_for_strategy(strategy_id):
    """Map local strategy ids to the explicit StockDB comparison labels."""
    value = str(strategy_id or "").lower()
    direct_candidates = {
        "momentum_12_1_20",
        "low_volatility_252_20",
        "low_vol_half_momentum_20",
    }
    if value in direct_candidates:
        return value
    if "alpha158" in value:
        return "alpha158"
    if "factor" in value or "leave_out" in value:
        return "factor_leave_out"
    if "three_families" in value or "three-family" in value:
        return "three_families"
    if "momentum" in value:
        return "momentum"
    return ""


def _forward_fields(strategy_id, forward_by_candidate, forward_path, forward_payload=None):
    candidate = _stockdb_candidate_for_strategy(strategy_id)
    matched_candidate = candidate
    row = forward_by_candidate.get(candidate, {})
    if not row and candidate == "momentum":
        for alias in ("momentum_v1", "momentum"):
            if alias in forward_by_candidate:
                matched_candidate = alias
                row = forward_by_candidate[alias]
                break
    comparison = (forward_payload or {}).get("comparison") or {}
    return {
        "stockdb_forward_candidate": candidate,
        "stockdb_forward_source_candidate": matched_candidate if row else "",
        "stockdb_forward_independence_group": row.get("independence_group", ""),
        "stockdb_forward_independent_candidate": row.get("independent_candidate", ""),
        "stockdb_forward_duplicate_of": row.get("duplicate_of", ""),
        "stockdb_forward_start_date": row.get("start_date", ""),
        "stockdb_forward_end_date": row.get("end_date", ""),
        "stockdb_forward_selection_cutoff": row.get("selection_cutoff", "") or comparison.get("selection_cutoff", ""),
        "stockdb_forward_coverage": row.get("coverage", ""),
        "stockdb_forward_equal_weight_return": row.get("equal_weight_forward_return", ""),
        "stockdb_forward_target_qty_weighted_return": row.get("target_qty_value_weighted_forward_return", ""),
        "stockdb_forward_status": row.get("status", "not_matched") if candidate else "not_matched",
        "stockdb_forward_evidence": str(forward_path) if row else "",
        "stockdb_forward_basis": (
            "independent frozen StockDB raw-close diagnostic; research-only; no promotion"
            if row else "no matched independent StockDB forward diagnostic"
        ),
    }


def _write_strategy_comparison(out, pack, stockdb_forward_compare_path=None):
    """Expose paper candidates and an independent StockDB diagnostic side by side."""
    forward_payload, forward_by_candidate = _load_stockdb_forward_compare(
        stockdb_forward_compare_path
    )
    forward_path = Path(stockdb_forward_compare_path).resolve() if stockdb_forward_compare_path else None
    rows = []
    for strategy in pack.get("strategies") or []:
        row = {
            "strategy_id": strategy.get("strategy_id", ""),
            "source_cutoff": strategy.get("source_cutoff", pack.get("source_cutoff", "")),
            "execution_day": strategy.get("execution_day", pack.get("execution_day", "")),
            "rank_day": strategy.get("preview_rank_day", ""),
            "rank_current": strategy.get("rank_current", ""),
            "candidate_order_count": strategy.get("candidate_order_count", ""),
            "buys": strategy.get("buys", ""),
            "sells": strategy.get("sells", ""),
            "estimated_notional_rmb": strategy.get("estimated_notional_rmb", ""),
            "ledger_preflight_accepted": strategy.get("ledger_preflight_accepted", ""),
            "ledger_preflight_rejected": strategy.get("ledger_preflight_rejected", ""),
            "deferred_buys": strategy.get("ledger_preflight_deferred_buys", ""),
            "comparison_return": strategy.get("comparison_return", ""),
            "comparison_max_drawdown": strategy.get("comparison_max_drawdown", ""),
            "comparison_sharpe": strategy.get("comparison_sharpe", ""),
            "stale_days": strategy.get("comparison_stale_days", ""),
            "handoff_status": strategy.get("handoff_status", ""),
            "selection_policy": "manual review only; automatic winner selection disabled",
            "basis": "supplied same-cutoff paper candidates; no live/account evidence",
        }
        row.update(_forward_fields(row["strategy_id"], forward_by_candidate, forward_path, forward_payload))
        rows.append(row)
    if not rows:
        row = {
            "strategy_id": "", "source_cutoff": pack.get("source_cutoff", ""),
            "execution_day": pack.get("execution_day", ""), "rank_day": "",
            "rank_current": "", "candidate_order_count": "", "buys": "", "sells": "",
            "estimated_notional_rmb": "", "ledger_preflight_accepted": "",
            "ledger_preflight_rejected": "", "deferred_buys": "", "comparison_return": "",
            "comparison_max_drawdown": "", "comparison_sharpe": "", "stale_days": "",
            "handoff_status": "no_strategy_rows", "selection_policy": "manual review only; automatic winner selection disabled",
            "basis": "opening pack contained no strategy rows",
        }
        row.update(_forward_fields("", forward_by_candidate, forward_path, forward_payload))
        rows.append(row)
    path = out / "strategy_comparison.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_stockdb_forward_comparison(
    out,
    payload,
    source_path,
    ledger_payload=None,
    ledger_source_path=None,
):
    """Materialize all independent StockDB candidates inside the desk package."""
    if not payload:
        return None
    comparison = payload.get("comparison") or {}
    ledger_by_candidate = {
        str(row.get("candidate", "")): row
        for row in (ledger_payload or {}).get("candidates") or []
        if row.get("candidate")
    }
    rows = []
    for candidate in payload.get("candidates") or []:
        ledger = ledger_by_candidate.get(str(candidate.get("candidate", "")), {})
        rows.append({
            "candidate": candidate.get("candidate", ""),
            "independence_group": candidate.get("independence_group", ""),
            "independent_candidate": candidate.get("independent_candidate", ""),
            "duplicate_of": candidate.get("duplicate_of", ""),
            "target_symbols": candidate.get("target_symbols", ""),
            "observed_symbols": candidate.get("observed_symbols", ""),
            "missing_symbols": candidate.get("missing_symbols", ""),
            "coverage": candidate.get("coverage", ""),
            "start_date": candidate.get("start_date", comparison.get("start_date", "")),
            "end_date": candidate.get("end_date", comparison.get("end_date", "")),
            "selection_cutoff": comparison.get("selection_cutoff", ""),
            "equal_weight_forward_return": candidate.get("equal_weight_forward_return", ""),
            "target_qty_value_weighted_forward_return": candidate.get("target_qty_value_weighted_forward_return", ""),
            "median_symbol_return": candidate.get("median_symbol_return", ""),
            "worst_symbol_return": candidate.get("worst_symbol_return", ""),
            "best_symbol_return": candidate.get("best_symbol_return", ""),
            "ledger_replay_sessions": ledger.get("sessions", ""),
            "ledger_replay_net_return": ledger.get("net_return", ""),
            "ledger_replay_max_drawdown": ledger.get("max_drawdown", ""),
            "ledger_replay_fees": ledger.get("fees", ""),
            "ledger_replay_turnover_value": ledger.get("turnover_value", ""),
            "ledger_replay_order_count": ledger.get("order_count", ""),
            "ledger_replay_fill_count": ledger.get("fill_count", ""),
            "ledger_replay_status": "matched" if ledger else "not_matched",
            "ledger_replay_evidence": str(ledger_source_path) if ledger else "",
            "status": candidate.get("status", ""),
            "basis": "frozen StockDB raw close; independent complete-path diagnostic; research-only",
            "source_result": str(source_path),
            "execution_admitted": "false",
        })
    if not rows:
        return None
    path = out / "stockdb_forward_comparison.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_strategy_promotion_card(out, comparison_path, sizing_path=None):
    """Turn comparable candidate metrics into an explicit manual-review card."""
    source_rows = read_csv(comparison_path)
    sizing_rows = read_csv(sizing_path) if sizing_path and Path(sizing_path).exists() else []
    sizing_by_id = {
        row.get("strategy_id", ""): row
        for row in sizing_rows
        if row.get("strategy_id")
    }
    scored = []
    for row in source_rows:
        try:
            sharpe = float(row.get("comparison_sharpe") or "nan")
        except (TypeError, ValueError):
            sharpe = float("nan")
        scored.append((sharpe, row))
    scored.sort(key=lambda item: item[0] if item[0] == item[0] else float("-inf"), reverse=True)
    rows = []
    for review_rank, (sharpe, row) in enumerate(scored, start=1):
        sizing = sizing_by_id.get(row.get("strategy_id", ""), {})
        rows.append({
            "row_type": "candidate",
            "review_rank": review_rank,
            "strategy_id": row.get("strategy_id", ""),
            "comparison_return": row.get("comparison_return", ""),
            "comparison_max_drawdown": row.get("comparison_max_drawdown", ""),
            "comparison_sharpe": row.get("comparison_sharpe", ""),
            "candidate_order_count": row.get("candidate_order_count", ""),
            "stale_days": row.get("stale_days", ""),
            "paper_risk_status": sizing.get("risk_status") or "unavailable",
            "paper_buy_gross_rmb": sizing.get("accepted_buy_gross_rmb") or "",
            "paper_cash_after_buys_rmb": sizing.get("cash_after_buys_only_rmb") or "",
            "paper_gross_headroom_rmb": sizing.get("gross_headroom_rmb") or "",
            "paper_single_headroom_rmb": sizing.get("single_headroom_rmb") or "",
            "paper_account_observed": sizing.get("account_observed") or "unknown",
            "decision_state": "manual_review_only_no_promotion",
            "reason": "same-cutoff descriptive comparison; paper capacity is informational and no untouched holdout, account observation or live execution evidence exists",
            "execution_admitted": "false",
        })
    rows.append({
        "row_type": "summary",
        "review_rank": "",
        "strategy_id": "",
        "comparison_return": "",
        "comparison_max_drawdown": "",
        "comparison_sharpe": "",
        "candidate_order_count": str(len(source_rows)),
        "stale_days": "",
        "paper_risk_status": "not_applicable",
        "paper_buy_gross_rmb": "",
        "paper_cash_after_buys_rmb": "",
        "paper_gross_headroom_rmb": "",
        "paper_single_headroom_rmb": "",
        "paper_account_observed": "not_applicable",
        "decision_state": "manual_review_only_no_promotion",
        "reason": "ranking is for operator review, not automatic strategy selection",
        "execution_admitted": "false",
    })
    path = out / "strategy_promotion_card.csv"
    fields = ["row_type", "review_rank", "strategy_id", "comparison_return",
              "comparison_max_drawdown", "comparison_sharpe", "candidate_order_count",
              "stale_days", "paper_risk_status", "paper_buy_gross_rmb",
              "paper_cash_after_buys_rmb", "paper_gross_headroom_rmb",
              "paper_single_headroom_rmb", "paper_account_observed",
              "decision_state", "reason", "execution_admitted"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _latest_factor_run(source_cutoff):
    """Find the newest completed factor replay and preserve cutoff provenance.

    The factor pipeline currently closes before some StockDB opening snapshots.
    Returning the newest prior replay keeps the operator card honest about an
    existing unified-ledger result without making it look like a same-cutoff
    comparison.  Callers must use ``_cutoff_match`` before comparing it with
    the opening candidate.
    """
    root = ROOT / "outputs" / "factor_mining"
    candidates = []
    if not root.exists():
        return None, None
    for folder in root.iterdir():
        run_path = folder / "run.json"
        if not run_path.is_file():
            continue
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest = payload.get("dataset_manifest") or {}
        manifest_cutoff = manifest.get("source_cutoff") or manifest.get("end")
        if payload.get("status") != "completed":
            continue
        if not payload.get("portfolio_metrics"):
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload, manifest_cutoff))
    if not candidates:
        return None, None
    exact = [item for item in candidates if item[3] == source_cutoff]
    _, path, payload, manifest_cutoff = max(
        exact or candidates,
        key=lambda item: (item[0], str(item[1])),
    )
    payload = dict(payload)
    payload["_cutoff_match"] = bool(exact)
    payload["_observed_cutoff"] = manifest_cutoff
    payload["_requested_cutoff"] = source_cutoff
    return path, payload


def _matching_instrument_master(source_cutoff):
    """Load the immutable research master observed on the pack cutoff."""
    if not source_cutoff:
        return None, None, None
    root = DATA_ROOT / "research" / "instrument_masters"
    candidates = []
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
            if (payload.get("schema") != "instrument_master_v1"
                    or payload.get("status") != "completed_research_instrument_master"
                    or payload.get("observed_as_of") != source_cutoff
                    or payload.get("execution_admitted") is not False):
                continue
            master_path = run_path.parent / str(payload.get("master_file") or "")
            if (not master_path.is_file()
                    or payload.get("master_sha256") != sha256(master_path.read_bytes()).hexdigest()):
                continue
            from .instrument_master import load_master
            rows = load_master(master_path)
            candidates.append((run_path.parent.name, run_path.resolve(), payload,
                               master_path.resolve(), rows))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
    if not candidates:
        return None, None, None
    _, run_path, payload, master_path, rows = sorted(candidates, key=lambda item: item[0])[-1]
    return run_path, {**payload, "master_path": str(master_path)}, {
        row["canonical_symbol"]: row for row in rows
    }


def _latest_prior_instrument_master(source_cutoff):
    """Load the newest verified research master strictly before the cutoff.

    This is identity metadata only.  Callers must keep the exact-cutoff master
    separate so a stale listing snapshot cannot satisfy release controls.
    """
    if not source_cutoff:
        return None, None, None
    try:
        cutoff = date.fromisoformat(str(source_cutoff))
    except ValueError:
        return None, None, None
    root = DATA_ROOT / "research" / "instrument_masters"
    candidates = []
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8-sig"))
            observed = payload.get("observed_as_of")
            if (payload.get("schema") != "instrument_master_v1"
                    or payload.get("status") != "completed_research_instrument_master"
                    or payload.get("execution_admitted") is not False
                    or not observed or date.fromisoformat(str(observed)) >= cutoff):
                continue
            master_path = run_path.parent / str(payload.get("master_file") or "")
            if (not master_path.is_file()
                    or payload.get("master_sha256") != sha256(master_path.read_bytes()).hexdigest()):
                continue
            from .instrument_master import load_master
            rows = load_master(master_path)
            candidates.append((date.fromisoformat(str(observed)), run_path.parent.name,
                               run_path.resolve(), payload, master_path.resolve(), rows))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
    if not candidates:
        return None, None, None
    _, _, run_path, payload, master_path, rows = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    return run_path, {**payload, "master_path": str(master_path)}, {
        row["canonical_symbol"]: row for row in rows
    }


def _latest_instrument_master_coverage(source_cutoff):
    """Load the latest non-admitting source/master scope diagnostic."""
    root = DATA_ROOT / "research" / "instrument_master_coverage"
    candidates = []
    if not root.exists():
        return None, None
    for coverage_path in root.glob("*/coverage.json"):
        try:
            payload = json.loads(coverage_path.read_text(encoding="utf-8"))
            if (payload.get("schema") != "instrument_master_coverage_v1"
                    or payload.get("execution_admitted") is not False
                    or payload.get("research_only") is not True):
                continue
            master_dir = Path(str(payload.get("master") or ""))
            master_run = master_dir / "run.json"
            if not master_run.is_file():
                continue
            master_payload = json.loads(master_run.read_text(encoding="utf-8"))
            if (master_payload.get("schema") != "instrument_master_v1"
                    or master_payload.get("observed_as_of") != source_cutoff
                    or master_payload.get("execution_admitted") is not False):
                continue
            candidates.append((coverage_path.stat().st_mtime, coverage_path, payload))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path.resolve(), payload


def _latest_factor_current_score(source_cutoff):
    """Load the newest frozen-cutoff factor inference bundle, if present."""
    root = ROOT / "outputs" / "factor_current_scores"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        result = payload.get("result") or {}
        if payload.get("status") != "completed_factor_candidate_current_score":
            continue
        if str(result.get("cutoff") or payload.get("cutoff") or "") != str(source_cutoff or ""):
            continue
        result_ranking_path = (payload.get("result") or {}).get("ranking_path")
        ranking_path = (Path(str(result_ranking_path)).resolve()
                        if result_ranking_path else
                        Path(str(payload.get("source_artifacts") or "")) / "predictions" / "rankings.csv")
        if not ranking_path.is_file():
            continue
        try:
            rankings = read_csv(ranking_path)
        except (OSError, UnicodeError, csv.Error):
            continue
        payload["_ranking_path"] = str(ranking_path)
        payload["_top_rankings"] = rankings[:10]
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_factor_paper_preview(source_cutoff):
    """Load the newest paper target preview sourced from a fixed factor score."""
    root = ROOT / "outputs" / "paper_rebalance"
    candidates = []
    if not root.exists():
        return None, None
    for result_path in root.glob("*/result.json"):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "paper_preview_only":
            continue
        if str(payload.get("source_cutoff") or "") != str(source_cutoff or ""):
            continue
        if payload.get("rank_source") != "current_score_bundle":
            continue
        if not payload.get("artifacts"):
            continue
        candidates.append((result_path.stat().st_mtime, result_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_factor_attribution(source_cutoff):
    """Load the newest saved factor accounting attribution for the pinned cutoff."""
    root = ROOT / "outputs" / "factor_candidate_attributions"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_factor_candidate_attribution":
            continue
        if str(payload.get("account_day") or "") != str(source_cutoff or ""):
            continue
        if not payload.get("source_run") or not payload.get("metrics"):
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_factor_cost_replay(source_cutoff):
    """Load the newest fixed cost-path diagnostic for the pinned cutoff."""
    root = ROOT / "outputs" / "factor_candidate_cost_replays"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_factor_candidate_cost_replay":
            continue
        if str(payload.get("end") or "") != str(source_cutoff or ""):
            continue
        result = payload.get("result") or {}
        if result.get("status") != "completed":
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_factor_cadence_replay(source_cutoff):
    """Load the newest completed fixed-cadence diagnostic for the pinned cutoff."""
    root = ROOT / "outputs" / "factor_candidate_cadence_replays"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_factor_candidate_cadence_replay":
            continue
        if str(payload.get("end") or "") != str(source_cutoff or ""):
            continue
        result = payload.get("result") or {}
        if result.get("status") != "completed" or not payload.get("source_run"):
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_factor_cadence_stability(source_cutoff):
    """Load the rolling diagnostic attached to the pinned cadence replay."""
    root = ROOT / "outputs" / "factor_candidate_cadence_stability"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_saved_ledger_rolling_diagnostic":
            continue
        if str(payload.get("source_cutoff") or "") != str(source_cutoff or ""):
            continue
        if not payload.get("replay_run") or not payload.get("windows"):
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_research_decision_matrix(source_cutoff):
    """Load the consolidated read-only research consumer for this cutoff."""
    root = ROOT / "outputs" / "research_decision_matrices"
    candidates = []
    if not root.exists():
        return None, None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_read_only_research_decision_matrix":
            continue
        comparison = payload.get("comparison") or {}
        if str(comparison.get("end") or "") != str(source_cutoff or ""):
            continue
        finished = payload.get("created_at_utc") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None, None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    return path, payload


def _latest_saved_risk_analytics():
    """Load existing historical risk views without treating them as live risk."""
    specs = (
        ("concentration", ROOT / "outputs" / "risk_concentration",
         "completed_saved_account_concentration"),
        ("stress_exposure", ROOT / "outputs" / "stress_exposure",
         "completed_saved_stress_exposure"),
        ("style_exposure", ROOT / "outputs" / "style_exposure",
         "completed_dated_style_diagnostics"),
    )
    result = {}
    for name, root, expected_status in specs:
        candidates = []
        if root.exists():
            for run_path in root.glob("*/run.json"):
                try:
                    payload = json.loads(run_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if payload.get("status") != expected_status:
                    continue
                finished = payload.get("finished_at") or payload.get("started_at") or ""
                candidates.append((finished, run_path, payload))
        if not candidates:
            continue
        _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
        if name == "concentration":
            summary = {
                "case_id": payload.get("case_id"),
                "source_account": payload.get("source_account"),
                "sessions": payload.get("sessions", 0),
                "symbols": payload.get("symbols", 0),
                "positive_symbol_count": payload.get("positive_symbol_count", 0),
                "negative_symbol_count": payload.get("negative_symbol_count", 0),
                "top_5_loss_share": payload.get("top_5_loss_share"),
                "top_10_loss_share": payload.get("top_10_loss_share"),
                "top_5_gain_share": payload.get("top_5_gain_share"),
                "max_single_symbol_loss_cny": payload.get("max_single_symbol_loss_cny"),
                "max_single_symbol_gain_cny": payload.get("max_single_symbol_gain_cny"),
                "worst_day": payload.get("worst_day"),
                "worst_daily_return": payload.get("worst_daily_return"),
                "stale_valuation_days": payload.get("stale_valuation_days"),
                "blocked_attempts": payload.get("blocked_attempts") or {},
            }
        elif name == "stress_exposure":
            summary = {
                "signal": payload.get("signal"),
                "source_run_type": payload.get("source_run_type"),
                "source_account": payload.get("source_account"),
                "stress_dates": payload.get("stress_dates", 0),
                "exposure_rows": payload.get("exposure_rows", 0),
                "known_rows": payload.get("known_rows", 0),
                "unknown_rows": payload.get("unknown_rows", 0),
                "top_symbol_by_mean_known_weight": payload.get("top_symbol_by_mean_known_weight"),
            }
        else:
            cases = payload.get("cases") or []
            summary = {
                "source_funnel": payload.get("source_funnel"),
                "common_dates": len(payload.get("common_dates") or []),
                "decision_dates": len(payload.get("decision_dates") or []),
                "models_refit": bool(payload.get("models_refit")),
                "accounts_replayed": bool(payload.get("accounts_replayed")),
                "fresh_holdout_claimed": bool(payload.get("fresh_holdout_claimed")),
                "cases": [
                    {
                        "signal": row.get("signal"),
                        "target_rows": row.get("target_rows"),
                        "industry_coverage": row.get("industry_coverage"),
                    }
                    for row in cases
                ],
            }
        result[name] = {
            "status": expected_status,
            "path": str(path),
            "run_id": payload.get("id"),
            "summary": summary,
            "artifacts": payload.get("artifacts"),
            "limitations": payload.get("limitations") or payload.get("limits") or [],
            "strategy_validated": bool(payload.get("strategy_validated")),
            "execution_admitted": False,
        }
    return result


def _latest_factor_regime_overlay(source_cutoff):
    """Load the newest fixed breadth-regime overlay for the pinned cutoff."""
    root = ROOT / "outputs" / "factor_regime_overlays"
    candidates = []
    if not root.exists():
        return None
    for run_path in root.glob("*/run.json"):
        try:
            payload = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if payload.get("status") != "completed_factor_breadth_regime_overlay":
            continue
        if str(payload.get("end") or "") != str(source_cutoff or ""):
            continue
        result = payload.get("result") or {}
        if result.get("status") != "completed":
            continue
        finished = payload.get("finished_at") or payload.get("started_at") or ""
        candidates.append((finished, run_path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda item: (item[0], str(item[1])))
    result = payload.get("result") or {}
    control = result.get("control_metrics") or {}
    return {
        "status": payload.get("status"),
        "path": str(path),
        "run_id": payload.get("id"),
        "signal": payload.get("signal"),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "result": {
            "net_return": result.get("net_return"),
            "max_drawdown": result.get("max_drawdown"),
            "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
            "fees": result.get("fees"),
            "risk_off_decision_days": result.get("risk_off_decision_days"),
            "normal_decision_days": result.get("normal_decision_days"),
            "net_return_difference": result.get("net_return_difference"),
            "max_drawdown_difference": result.get("max_drawdown_difference"),
            "sharpe_difference": result.get("sharpe_difference"),
            "control_net_return": control.get("net_return"),
            "control_max_drawdown": control.get("max_drawdown"),
            "control_sharpe_zero_rf_252": control.get("sharpe_zero_rf_252"),
        },
        "limitations": payload.get("limitations") or [],
        "selection_enabled": False,
        "execution_admitted": False,
        "interpretation": "Fixed breadth-regime overlay diagnostic only; it trades return for drawdown protection in this saved ledger and is not automatic promotion evidence.",
    }


def _saved_ledger_comparison_history(limit=8):
    """Load recent read-only saved-ledger comparisons without treating them as promotion evidence."""
    root = ROOT / "outputs" / "research_comparisons"
    if not root.exists():
        return []
    candidates = []
    for path in root.glob("*/comparison.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        cases = payload.get("cases") or []
        families = {str(row.get("family") or "") for row in cases if isinstance(row, dict)}
        # The desk's primary comparison must contain the fixed rule family and
        # the Alpha158 family in the same saved-ledger/cost basis.  A newer
        # seat diagnostic or other research arm must not silently replace that
        # benchmark merely because its file timestamp is later.
        shared_rule_factor_basis = bool(
            payload.get("status") == "compared_saved_accounts"
            and payload.get("same_cost_settings") is True
            and "low_risk_stock_selection" in families
            and any(family.startswith("alpha158") for family in families)
        )
        if payload.get("status") == "compared_saved_accounts" and cases:
            candidates.append((path, payload, shared_rule_factor_basis))

    candidates.sort(
        key=lambda item: (not item[2], -item[0].stat().st_mtime)
    )
    history = []
    seen = set()
    for path, payload, _ in candidates:
        signature = (
            payload.get("start"), payload.get("end"),
            bool(payload.get("same_cost_settings")),
        )
        if signature in seen:
            continue
        seen.add(signature)
        history.append((path, payload))
        if len(history) >= limit:
            break
    return history


def _latest_saved_ledger_comparison():
    """Load the newest read-only saved-ledger comparison for the evidence card."""
    history = _saved_ledger_comparison_history(limit=1)
    return history[0] if history else (None, None)


def _write_strategy_evidence_card(
    out,
    pack,
    pack_path,
    comparison_path,
    promotion_card_path,
    stockdb_forward_compare_path=None,
    stockdb_ledger_replay_path=None,
):
    """Join opening candidates and the pinned factor replay for offline review."""
    comparison_rows = read_csv(comparison_path)
    factor_path, factor = _latest_factor_run(pack.get("source_cutoff"))
    factor_current_path, factor_current = _latest_factor_current_score(pack.get("source_cutoff"))
    factor_preview_path, factor_preview = _latest_factor_paper_preview(pack.get("source_cutoff"))
    factor_attribution_path, factor_attribution = _latest_factor_attribution(pack.get("source_cutoff"))
    factor_cost_path, factor_cost = _latest_factor_cost_replay(pack.get("source_cutoff"))
    factor_cadence_path, factor_cadence = _latest_factor_cadence_replay(pack.get("source_cutoff"))
    factor_cadence_stability_path, factor_cadence_stability = _latest_factor_cadence_stability(pack.get("source_cutoff"))
    research_matrix_path, research_matrix = _latest_research_decision_matrix(pack.get("source_cutoff"))
    saved_comparison_path, saved_comparison = _latest_saved_ledger_comparison()
    comparison_history = _saved_ledger_comparison_history()
    saved_risk_analytics = _latest_saved_risk_analytics()
    factor_regime_overlay = _latest_factor_regime_overlay(pack.get("source_cutoff"))
    factor = factor or {}
    model_metrics = factor.get("model_metrics") or []
    portfolio_metrics = factor.get("portfolio_metrics") or []

    model_test = [
        {
            "variant": row.get("variant"),
            "seed": row.get("seed"),
            "split": row.get("split"),
            "mean_rank_ic": row.get("mean_rank_ic"),
            "hac_t_lag5": row.get("hac_t_lag5"),
            "ic_days": row.get("ic_days"),
        }
        for row in model_metrics
        if row.get("split") == "test"
    ]
    factor_portfolios = [
        {
            "variant": row.get("variant"),
            "seed": row.get("seed"),
            "status": row.get("status"),
            "net_return": row.get("net_return"),
            "max_drawdown": row.get("max_drawdown"),
            "sharpe_zero_rf_252": row.get("sharpe_zero_rf_252"),
            "fees": row.get("fees"),
            "orders": row.get("orders"),
            "fills": row.get("fills"),
            "turnover_over_initial": row.get("turnover_over_initial"),
        }
        for row in portfolio_metrics
    ]
    model_test_ics = [
        float(row["mean_rank_ic"])
        for row in model_test
        if row.get("mean_rank_ic") is not None
    ]
    seeded_portfolios = [
        row for row in factor_portfolios
        if row.get("seed") is not None and row.get("net_return") is not None
    ]
    seeded_returns = [float(row["net_return"]) for row in seeded_portfolios]
    sorted_seeded_returns = sorted(seeded_returns)
    model_stability = {
        "status": "completed_saved_model_seed_stability" if model_test_ics and seeded_returns else "missing",
        "seed_count": len(seeded_portfolios),
        "test_rank_ic_mean": sum(model_test_ics) / len(model_test_ics) if model_test_ics else None,
        "test_rank_ic_min": min(model_test_ics) if model_test_ics else None,
        "test_rank_ic_max": max(model_test_ics) if model_test_ics else None,
        "test_rank_ic_positive_seed_count": sum(value > 0 for value in model_test_ics),
        "portfolio_net_return_median": (
            sorted_seeded_returns[len(sorted_seeded_returns) // 2]
            if len(sorted_seeded_returns) % 2
            else (sorted_seeded_returns[len(sorted_seeded_returns) // 2 - 1] + sorted_seeded_returns[len(sorted_seeded_returns) // 2]) / 2
        ) if sorted_seeded_returns else None,
        "portfolio_net_return_min": min(seeded_returns) if seeded_returns else None,
        "portfolio_net_return_max": max(seeded_returns) if seeded_returns else None,
        "portfolio_positive_seed_count": sum(value > 0 for value in seeded_returns),
        "limitations": [
            "Seed dispersion is descriptive and does not establish model robustness or future returns.",
            "The saved run is not a fresh untouched holdout and execution admission remains disabled.",
        ],
        "execution_admitted": False,
    }
    test_returns = [
        float(row["net_return"])
        for row in factor_portfolios
        if row.get("seed") is not None and row.get("net_return") is not None
    ]
    factor_status = (
        "factor_replay_found_same_cutoff"
        if factor_path and factor.get("_cutoff_match", True)
        else "factor_replay_found_prior_cutoff"
        if factor_path
        else "factor_replay_missing"
    )
    factor_limits = ((factor.get("dataset_manifest") or {}).get("limitations")
                     or (factor.get("data_quality") or {}).get("limitations") or [])
    selected_signal = str(pack.get("selected_strategy_id") or "")

    stockdb_forward_payload, _ = _load_stockdb_forward_compare(
        stockdb_forward_compare_path
    )
    stockdb_ledger_payload, stockdb_ledger_by_candidate = _load_stockdb_ledger_replay(
        stockdb_ledger_replay_path
    )
    stockdb_comparison = (stockdb_forward_payload or {}).get("comparison") or {}
    stockdb_candidates = []
    for candidate in (stockdb_forward_payload or {}).get("candidates") or []:
        ledger = stockdb_ledger_by_candidate.get(
            str(candidate.get("candidate", "")),
            {},
        )
        stockdb_candidates.append({
            "candidate": candidate.get("candidate", ""),
            "independence_group": candidate.get("independence_group", ""),
            "independent_candidate": candidate.get("independent_candidate", ""),
            "duplicate_of": candidate.get("duplicate_of", ""),
            "coverage": candidate.get("coverage", ""),
            "start_date": candidate.get("start_date", stockdb_comparison.get("start_date", "")),
            "end_date": candidate.get("end_date", stockdb_comparison.get("end_date", "")),
            "path_dates": candidate.get("path_dates") or [],
            "path_session_count": candidate.get("path_session_count", 0),
            "equal_weight_forward_return": candidate.get("equal_weight_forward_return", ""),
            "target_qty_value_weighted_forward_return": candidate.get("target_qty_value_weighted_forward_return", ""),
            "path_equal_weight_return": candidate.get("path_equal_weight_return", ""),
            "path_target_qty_value_weighted_return": candidate.get("path_target_qty_value_weighted_return", ""),
            "path_equal_weight_max_drawdown": candidate.get("path_equal_weight_max_drawdown", ""),
            "path_target_qty_value_weighted_max_drawdown": candidate.get("path_target_qty_value_weighted_max_drawdown", ""),
            "median_symbol_return": candidate.get("median_symbol_return", ""),
            "worst_symbol_return": candidate.get("worst_symbol_return", ""),
            "best_symbol_return": candidate.get("best_symbol_return", ""),
            "status": candidate.get("status", ""),
            "shared_ledger_replay": {
                "status": "matched" if ledger else "not_matched",
                "sessions": ledger.get("sessions", ""),
                "net_return": ledger.get("net_return", ""),
                "max_drawdown": ledger.get("max_drawdown", ""),
                "fees": ledger.get("fees", ""),
                "turnover_value": ledger.get("turnover_value", ""),
                "order_count": ledger.get("order_count", ""),
                "fill_count": ledger.get("fill_count", ""),
            },
        })
    stockdb_forward_summary = {
        "status": "completed_independent_frozen_snapshot_diagnostic" if stockdb_forward_payload else "missing",
        "schema": (stockdb_forward_payload or {}).get("schema") if stockdb_forward_payload else None,
        "path": str(Path(stockdb_forward_compare_path).resolve()) if stockdb_forward_compare_path else None,
        "selection_cutoff": stockdb_comparison.get("selection_cutoff"),
        "start_date": stockdb_comparison.get("start_date"),
        "end_date": stockdb_comparison.get("end_date"),
        "sessions": max(
            (int(candidate.get("path_session_count", 0) or 0)
             for candidate in stockdb_candidates),
            default=0,
        ),
        "price_field": stockdb_comparison.get("price_field"),
        "path_policy": stockdb_comparison.get("path_policy"),
        "coverage_manifest": stockdb_forward_payload.get("coverage_manifest") if stockdb_forward_payload else None,
        "coverage_status": stockdb_comparison.get("coverage_status"),
        "calendar_session_count": stockdb_comparison.get("calendar_session_count"),
        "costs_applied": False,
        "candidates": stockdb_candidates,
        "limitations": [
            "Frozen StockDB raw-close forward diagnostic only; it is independent research evidence, not a live quote or broker observation.",
            "Candidate results remain descriptive and cannot promote a strategy or authorize an order.",
        ] if stockdb_forward_payload else [],
        "execution_admitted": False,
    }
    stockdb_ledger_summary = {
        "status": "completed_shared_engine_ledger_replay" if stockdb_ledger_payload else "missing",
        "schema": (stockdb_ledger_payload or {}).get("schema") if stockdb_ledger_payload else None,
        "path": str(Path(stockdb_ledger_replay_path).resolve()) if stockdb_ledger_replay_path else None,
        "snapshot": (stockdb_ledger_payload or {}).get("snapshot") if stockdb_ledger_payload else None,
        "common_path_dates": (stockdb_ledger_payload or {}).get("common_path_dates") or [],
        "coverage_manifest": (stockdb_ledger_payload or {}).get("coverage_manifest") if stockdb_ledger_payload else None,
        "candidates": [
            {
                "candidate": row.get("candidate"),
                "sessions": row.get("sessions"),
                "net_return": row.get("net_return"),
                "max_drawdown": row.get("max_drawdown"),
                "fees": row.get("fees"),
                "turnover_value": row.get("turnover_value"),
                "order_count": row.get("order_count"),
                "fill_count": row.get("fill_count"),
                "execution_admitted": row.get("execution_admitted", False),
            }
            for row in (stockdb_ledger_payload or {}).get("candidates") or []
        ],
        "limitations": [
            "Shared Engine/Ledger replay uses frozen StockDB inputs and paper fills; it is not broker evidence.",
            "Replay results are comparison evidence only and do not promote or authorize execution.",
        ] if stockdb_ledger_payload else [],
        "execution_admitted": False,
    }

    def _comparison_window_summary(path, payload):
        selected_case = next(
            (row for row in payload.get("cases") or []
             if str(row.get("signal") or "") == selected_signal),
            None,
        )
        selected_metrics = (selected_case or {}).get("metrics") or {}
        return {
            "path": str(path),
            "start": payload.get("start"),
            "end": payload.get("end"),
            "sessions": payload.get("sessions", 0),
            "requested_cases": payload.get("requested_cases", 0),
            "comparable_cases": payload.get("comparable_cases", 0),
            "same_cost_settings": bool(payload.get("same_cost_settings")),
            "descriptive_nondominated_case_ids": payload.get("descriptive_nondominated_case_ids") or [],
            "selected_strategy_id": selected_signal or None,
            "selected_case": {
                "net_return": selected_metrics.get("net_return"),
                "max_drawdown": selected_metrics.get("max_drawdown"),
                "sharpe_zero_rf_252": selected_metrics.get("sharpe_zero_rf_252"),
                "fees": selected_metrics.get("fees"),
                "stale_valuation_days": selected_metrics.get("stale_valuation_days"),
            } if selected_case else None,
            "execution_admitted": False,
            "selection_enabled": False,
        }
    comparison_history_rows = [
        _comparison_window_summary(path, payload)
        for path, payload in comparison_history
    ]
    evaluated_windows = [
        row for row in comparison_history_rows
        if row.get("selected_case")
        and row["selected_case"].get("net_return") is not None
    ]
    positive_windows = sum(
        float(row["selected_case"]["net_return"]) > 0
        for row in evaluated_windows
    )
    if not evaluated_windows:
        stability_status = "no_selected_case_metrics"
    elif len(evaluated_windows) < 2:
        stability_status = "insufficient_windows_for_stability"
    elif positive_windows == len(evaluated_windows):
        stability_status = "positive_across_available_windows"
    elif positive_windows == 0:
        stability_status = "negative_across_available_windows"
    else:
        stability_status = "mixed_window_results"
    window_stability = {
        "status": stability_status,
        "selected_strategy_id": selected_signal or None,
        "evaluated_windows": len(evaluated_windows),
        "positive_windows": positive_windows,
        "non_positive_windows": len(evaluated_windows) - positive_windows,
        "worst_net_return": min(
            (float(row["selected_case"]["net_return"]) for row in evaluated_windows),
            default=None,
        ),
        "worst_max_drawdown": min(
            (float(row["selected_case"]["max_drawdown"])
             for row in evaluated_windows
             if row["selected_case"].get("max_drawdown") is not None),
            default=None,
        ),
        "execution_admitted": False,
        "selection_enabled": False,
    }
    card = {
        "schema": "strategy_evidence_card_v1",
        "status": "descriptive_fixed_cutoff_no_automatic_promotion",
        "generated_at": datetime.now().astimezone().isoformat(),
        "opening_pack": {
            "path": str(pack_path),
            "source_cutoff": pack.get("source_cutoff"),
            "execution_day": pack.get("execution_day"),
            "strategy_count": len(comparison_rows),
            "candidates": comparison_rows,
            "promotion_card": str(promotion_card_path),
        },
        "factor_replay": {
            "status": factor_status,
            "path": str(factor_path) if factor_path else None,
            "run_id": factor.get("id"),
            "experiment_kind": factor.get("experiment_kind"),
            "source_cutoff": ((factor.get("dataset_manifest") or {}).get("source_cutoff")
                              or (factor.get("dataset_manifest") or {}).get("end")),
            "segments": (factor.get("config") or {}).get("segments"),
            "model_test_rank_ic": model_test,
            "portfolio_metrics": factor_portfolios,
            "paired_deltas": factor.get("paired_deltas") or [],
            "candidate_search_performed": bool(factor.get("candidate_search_performed")),
            "fresh_holdout_claimed": bool(factor.get("fresh_holdout_claimed")),
            "execution_admitted": bool(
                factor.get("live_ready")
                or factor.get("execution_admitted")
                or (factor.get("dataset_manifest") or {}).get("execution_admitted")
            ),
            "cutoff_match": bool(factor.get("_cutoff_match", False)) if factor_path else False,
            "requested_cutoff": factor.get("_requested_cutoff") if factor_path else pack.get("source_cutoff"),
            "observed_cutoff": factor.get("_observed_cutoff") if factor_path else None,
            "ledger_failures": factor.get("ledger_failures") or [],
            "data_quality": factor.get("data_quality") or {},
            "limitations": factor_limits,
        },
        "stockdb_forward_comparison": stockdb_forward_summary,
        "stockdb_shared_ledger_replay": stockdb_ledger_summary,
        "factor_current_score": {
            "status": "completed_frozen_cutoff_inference" if factor_current else "missing",
            "path": str(factor_current_path) if factor_current_path else None,
            "run_id": factor_current.get("id") if factor_current else None,
            "signal": factor_current.get("signal") if factor_current else None,
            "cutoff": (factor_current.get("result") or {}).get("cutoff") if factor_current else None,
            "rank_count": (factor_current.get("result") or {}).get("rank_count", 0) if factor_current else 0,
            "finite_score_count": (factor_current.get("result") or {}).get("finite_score_count", 0) if factor_current else 0,
            "rank_is_current_for_execution_day": bool((factor_current.get("result") or {}).get("rank_is_current_for_execution_day")) if factor_current else False,
            "ranking_path": factor_current.get("_ranking_path") if factor_current else None,
            "top_rankings": factor_current.get("_top_rankings") or [] if factor_current else [],
            "model_source_run": factor_current.get("model_source_run") if factor_current else None,
            "new_labels_used": bool((factor_current.get("model") or {}).get("new_labels_used")) if factor_current else False,
            "limitations": [
                "This is inference on the pinned research cutoff, not a live quote or current account observation.",
                "The ranking is not an order target until the shared-ledger, account, quote and execution checks are separately satisfied.",
            ] if factor_current else [],
            "execution_admitted": False,
        },
        "factor_paper_preview": {
            "status": "completed_factor_score_paper_preview" if factor_preview else "missing",
            "path": str(factor_preview_path) if factor_preview_path else None,
            "strategy_id": factor_preview.get("strategy_id") if factor_preview else None,
            "source_run": factor_preview.get("source_run") if factor_preview else None,
            "execution_day": factor_preview.get("execution_day") if factor_preview else None,
            "rank_source": factor_preview.get("rank_source") if factor_preview else None,
            "rank_is_current_for_execution_day": bool(factor_preview.get("rank_is_current_for_execution_day")) if factor_preview else False,
            "execution_scope": factor_preview.get("execution_scope") if factor_preview else None,
            "terminal_equity_rmb": factor_preview.get("terminal_equity_rmb") if factor_preview else None,
            "cash_rmb": factor_preview.get("cash_rmb") if factor_preview else None,
            "position_count": factor_preview.get("position_count", 0) if factor_preview else 0,
            "provisional_rank_count": factor_preview.get("provisional_rank_count", 0) if factor_preview else 0,
            "provisional_target_count": factor_preview.get("provisional_target_count", 0) if factor_preview else 0,
            "provisional_order_count": factor_preview.get("provisional_order_count", 0) if factor_preview else 0,
            "adapter_handoff_order_count": factor_preview.get("adapter_handoff_order_count", 0) if factor_preview else 0,
            "missing_quote_symbols": factor_preview.get("provisional_missing_quote_symbols") or [] if factor_preview else [],
            "release_state": factor_preview.get("release_state") if factor_preview else None,
            "release_blockers": factor_preview.get("release_blockers") or [] if factor_preview else [],
            "artifacts": factor_preview.get("artifacts") or {} if factor_preview else {},
            "limitations": [
                "The target and order rows are a paper preview from the research replay account, not the user broker account.",
                "Research closes are not same-session executable quotes; the adapter handoff remains disabled until fresh inputs and manual review exist.",
            ] if factor_preview else [],
            "execution_admitted": False,
        },
        "model_stability": model_stability,
        "factor_regime_overlay": factor_regime_overlay or {"status": "missing", "execution_admitted": False},
        "factor_attribution": {
            "status": "accounting_decomposition_only" if factor_attribution else "missing",
            "path": str(factor_attribution_path) if factor_attribution_path else None,
            "run_id": factor_attribution.get("id") if factor_attribution else None,
            "source_run": factor_attribution.get("source_run") if factor_attribution else None,
            "start": factor_attribution.get("start") if factor_attribution else None,
            "end": factor_attribution.get("end") if factor_attribution else None,
            "sessions": factor_attribution.get("sessions", 0) if factor_attribution else 0,
            "net_pnl_cny": factor_attribution.get("net_pnl_cny") if factor_attribution else None,
            "metrics": factor_attribution.get("metrics") or {} if factor_attribution else {},
            "costs": factor_attribution.get("costs") or {} if factor_attribution else {},
            "raw_open_slippage": factor_attribution.get("raw_open_slippage") or {} if factor_attribution else {},
            "fixed_fill_fee_addback_cny": factor_attribution.get("fixed_fill_fee_addback_cny") if factor_attribution else None,
            "fixed_fill_fee_and_slip_addback_cny": factor_attribution.get("fixed_fill_fee_and_slip_addback_cny") if factor_attribution else None,
            "median_holdings_exposure": factor_attribution.get("median_holdings_exposure") if factor_attribution else None,
            "limitations": factor_attribution.get("limitations") or [] if factor_attribution else [],
            "strategy_validated": bool(factor_attribution.get("strategy_validated")) if factor_attribution else False,
            "execution_admitted": False,
            "interpretation": "Saved-ledger accounting decomposition only; not causal factor alpha, style exposure, market impact, or execution approval.",
        },
        "factor_cost_replay": {
            "status": "completed_fixed_cost_path" if factor_cost else "missing",
            "path": str(factor_cost_path) if factor_cost_path else None,
            "run_id": factor_cost.get("id") if factor_cost else None,
            "control_signal": factor_cost.get("control_signal") if factor_cost else None,
            "execution_policy": factor_cost.get("execution_policy") or {} if factor_cost else {},
            "result": (factor_cost.get("result") or {}) if factor_cost else {},
            "limitations": factor_cost.get("limitations") or [] if factor_cost else [],
            "execution_admitted": False,
            "interpretation": "Fixed cost-path diagnostic only; it changes the ledger path and does not establish factor alpha or live execution quality.",
        },
        "factor_cadence_replay": {
            "status": "completed_fixed_bimonthly_cadence" if factor_cadence else "missing",
            "path": str(factor_cadence_path) if factor_cadence_path else None,
            "run_id": factor_cadence.get("id") if factor_cadence else None,
            "source_run": factor_cadence.get("source_run") if factor_cadence else None,
            "signal": factor_cadence.get("signal") if factor_cadence else None,
            "execution_policy": factor_cadence.get("execution_policy") or {} if factor_cadence else {},
            "result": (factor_cadence.get("result") or {}) if factor_cadence else {},
            "event_view_override": factor_cadence.get("event_view_override") if factor_cadence else None,
            "reference_view_override": factor_cadence.get("reference_view_override") if factor_cadence else None,
            "limitations": factor_cadence.get("limitations") or [] if factor_cadence else [],
            "execution_admitted": False,
            "interpretation": "Fixed bimonthly signal cadence diagnostic only; rankings and model were reused without refit, and the result is not an automatic strategy promotion.",
        },
        "factor_cadence_stability": {
            "status": "completed_saved_ledger_rolling_diagnostic" if factor_cadence_stability else "missing",
            "path": str(factor_cadence_stability_path) if factor_cadence_stability_path else None,
            "run_id": factor_cadence_stability.get("id") if factor_cadence_stability else None,
            "replay_run": factor_cadence_stability.get("replay_run") if factor_cadence_stability else None,
            "windows": factor_cadence_stability.get("windows") or {} if factor_cadence_stability else {},
            "limitations": factor_cadence_stability.get("limitations") or [] if factor_cadence_stability else [],
            "execution_admitted": False,
            "interpretation": "Rolling saved-ledger stability diagnostic only; it does not establish a fresh holdout, causal alpha, or execution readiness.",
        },
        "research_decision_matrix": {
            "status": research_matrix.get("status") if research_matrix else "missing",
            "path": str(research_matrix_path) if research_matrix_path else None,
            "run_id": research_matrix.get("id") if research_matrix else None,
            "comparison": research_matrix.get("comparison") or {} if research_matrix else {},
            "strategy_rows": research_matrix.get("strategy_rows") or [] if research_matrix else [],
            "diagnostics": research_matrix.get("diagnostics") or [] if research_matrix else [],
            "factor_library": research_matrix.get("factor_library") or {} if research_matrix else {},
            "factor_inventory": research_matrix.get("factor_inventory") or {} if research_matrix else {},
            "factor_library_ablation": research_matrix.get("factor_library_ablation") or {} if research_matrix else {},
            "model_stability": research_matrix.get("model_stability") or {} if research_matrix else {},
            "factor_regime_overlay": research_matrix.get("factor_regime_overlay") or {} if research_matrix else {},
            "research_data_exceptions": research_matrix.get("research_data_exceptions") or {} if research_matrix else {},
            "desk_functions": research_matrix.get("desk_functions") or [] if research_matrix else [],
            "limitations": research_matrix.get("limitations") or [] if research_matrix else [],
            "selection_enabled": False,
            "execution_admitted": False,
            "operator_next_action": research_matrix.get("operator_next_action") if research_matrix else None,
        },
        "saved_risk_analytics": {
            "status": "completed_saved_historical_risk_views" if saved_risk_analytics else "missing",
            "views": saved_risk_analytics,
            "selection_enabled": False,
            "execution_admitted": False,
            "interpretation": "Historical concentration, stress-exposure and style views are source-specific diagnostics; they are not current account risk, causal attribution or promotion evidence.",
        },
        "saved_ledger_comparison": {
            "status": "read_only_context" if saved_comparison else "missing",
            "path": str(saved_comparison_path) if saved_comparison_path else None,
            "start": saved_comparison.get("start") if saved_comparison else None,
            "end": saved_comparison.get("end") if saved_comparison else None,
            "sessions": saved_comparison.get("sessions", 0) if saved_comparison else 0,
            "requested_cases": saved_comparison.get("requested_cases", 0) if saved_comparison else 0,
            "comparable_cases": saved_comparison.get("comparable_cases", 0) if saved_comparison else 0,
            "same_cost_settings": bool(saved_comparison.get("same_cost_settings")) if saved_comparison else False,
            "cases": [
                {
                    "family": row.get("family"),
                    "signal": row.get("signal"),
                    "label": row.get("label"),
                    "status": row.get("status"),
                    "metrics": row.get("metrics") or {},
                    "descriptive_nondominated": bool(row.get("descriptive_nondominated")),
                }
                for row in (saved_comparison.get("cases") or [])
            ] if saved_comparison else [],
            "execution_admitted": False,
            "selection_enabled": False,
            "history": comparison_history_rows,
            "window_stability": window_stability,
        },
        "interpretation": {
            "opening_strategy_state": "manual_review_only_no_promotion",
            "factor_strategy_state": "research_benchmark_only",
            "execution_admitted": False,
            "why": [
                "The opening candidates share one frozen cutoff but are not an untouched holdout.",
                "The factor replay has positive test rank IC in the saved run, but seed portfolio returns disagree.",
                "No current account observation, broker order/fill observation, or live submission evidence is present.",
                "The replay data is research-only and retains PIT, corporate-action, and coverage limitations.",
            ],
            "seed_return_range": {
                "min": min(test_returns) if test_returns else None,
                "max": max(test_returns) if test_returns else None,
                "count": len(test_returns),
            },
        },
        "next_evidence_required": [
            "A pre-registered untouched time split after data/PIT and corporate-action gaps are resolved.",
            "A same-cost, same-ledger comparison that survives paper replay without selecting on the inspected cutoff.",
            "Same-session account, sellable quantity, and raw RMB quote observations before any THS prefill.",
            "Observed paper orders and fills before any live-readiness decision; automatic submission remains disabled.",
        ],
    }
    path = out / "strategy_evidence_card.json"
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_target_order_reconciliation(out, target_path, orders, strategy_id=None):
    """Link target holdings to paper orders without inventing held quantities."""
    target_file = Path(target_path) if target_path else None
    target_rows = read_csv(target_file) if target_file and target_file.exists() else []
    if strategy_id:
        target_rows = [
            row for row in target_rows
            if row.get("strategy_id", "") == strategy_id
        ]
    target_index = {}
    duplicate_keys = set()
    for row in target_rows:
        key = (row.get("strategy_id", ""), row.get("symbol", ""))
        if key in target_index:
            duplicate_keys.add(key)
        target_index[key] = row

    rows = []
    for sequence, order in enumerate(orders, start=1):
        key = (order.get("strategy_id", ""), order.get("symbol", ""))
        target = target_index.get(key)
        try:
            target_qty = int(target.get("target_qty") or 0) if target else 0
        except (TypeError, ValueError):
            target_qty = 0
        active = str(target.get("active_target", "")).lower() == "true" if target else False
        side = order.get("side", "")
        if target is None:
            status, reason = "fail", "order has no matching strategy target row"
        elif side == "buy" and not active:
            status, reason = "fail", "buy order points to an inactive target"
        elif side == "buy" and target_qty <= 0:
            status, reason = "fail", "buy order points to a non-positive target quantity"
        elif side == "sell" and active and target_qty > 0:
            status, reason = "review", "sell may be a reduction; held quantity and target delta require account state"
        else:
            status, reason = "pass", "order is linked to the supplied target contract"
        rows.append({
            "row_type": "order",
            "sequence": sequence,
            "strategy_id": order.get("strategy_id", ""),
            "symbol": order.get("symbol", ""),
            "side": side,
            "order_qty": order.get("qty", ""),
            "target_qty": target_qty if target is not None else "",
            "active_target": str(target.get("active_target", "")) if target else "",
            "status": status,
            "reason": reason,
            "account_holdings_observed": "false",
            "execution_admitted": "false",
        })

    order_keys = {(row.get("strategy_id", ""), row.get("symbol", "")) for row in orders}
    missing_order_targets = [row for row in target_rows
                             if (row.get("strategy_id", ""), row.get("symbol", "")) not in order_keys
                             and str(row.get("active_target", "")).lower() == "true"]
    rows.extend([
        {
            "row_type": "summary",
            "sequence": "",
            "strategy_id": "",
            "symbol": "",
            "side": "",
            "order_qty": "",
            "target_qty": "",
            "active_target": "",
            "status": "pass" if not duplicate_keys else "fail",
            "reason": f"target key duplicates={len(duplicate_keys)}",
            "account_holdings_observed": "false",
            "execution_admitted": "false",
        },
        {
            "row_type": "summary",
            "sequence": "",
            "strategy_id": "",
            "symbol": "",
            "side": "",
            "order_qty": "",
            "target_qty": "",
            "active_target": "",
            "status": "unobserved" if missing_order_targets else "pass",
            "reason": f"active targets without an order={len(missing_order_targets)}; may be held/unchanged, account holdings not supplied",
            "account_holdings_observed": "false",
            "execution_admitted": "false",
        },
    ])
    path = out / "target_order_reconciliation.csv"
    fields = ["row_type", "sequence", "strategy_id", "symbol", "side", "order_qty", "target_qty",
              "active_target", "status", "reason", "account_holdings_observed", "execution_admitted"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_paper_release_decision(out, orders, preflight, target_reconciliation_path,
                                  account_observed, quotes_observed):
    """Collapse paper-only release inputs into an operator-facing decision table."""
    target_rows = read_csv(target_reconciliation_path)
    target_by_sequence = {
        str(row.get("sequence", "")): row
        for row in target_rows
        if row.get("row_type") == "order"
    }
    preflight_by_key = {_order_key(row): row for row in preflight}
    rows = []
    for sequence, order in enumerate(orders, start=1):
        preflight_row = preflight_by_key.get(_order_key(order), {})
        target_row = target_by_sequence.get(str(sequence), {})
        preflight_status = preflight_row.get("preflight_status", "unknown")
        target_status = target_row.get("status", "unobserved")
        if preflight_status != "accepted":
            decision = "rejected_preflight"
            reason = preflight_row.get("preflight_reason", "shared-ledger preflight did not accept order")
        elif target_status == "fail":
            decision = "blocked_target_contract"
            reason = target_row.get("reason", "target/order contract failed")
        elif target_status == "review":
            decision = "manual_review_target_delta"
            reason = target_row.get("reason", "target delta requires observed holdings")
        elif not account_observed or not quotes_observed:
            decision = "blocked_missing_live_inputs"
            missing = []
            if not account_observed:
                missing.append("account_observation")
            if not quotes_observed:
                missing.append("same_session_quotes")
            reason = "missing " + ", ".join(missing)
        else:
            decision = "manual_review_ready_for_prefill"
            reason = "inputs supplied; human review and THS prefill readback still required"
        rows.append({
            "row_type": "order",
            "sequence": sequence,
            "strategy_id": order.get("strategy_id", ""),
            "symbol": order.get("symbol", ""),
            "side": order.get("side", ""),
            "qty": order.get("qty", ""),
            "preflight_status": preflight_status,
            "target_reconciliation_status": target_status,
            "paper_release_decision": decision,
            "reason": reason,
            "account_observed": str(bool(account_observed)).lower(),
            "quotes_observed": str(bool(quotes_observed)).lower(),
            "execution_admitted": "false",
        })
    summary = {}
    for row in rows:
        summary[row["paper_release_decision"]] = summary.get(row["paper_release_decision"], 0) + 1
    rows.append({
        "row_type": "summary",
        "sequence": "",
        "strategy_id": "",
        "symbol": "",
        "side": "",
        "qty": "",
        "preflight_status": "",
        "target_reconciliation_status": "",
        "paper_release_decision": "summary",
        "reason": json.dumps(summary, ensure_ascii=False, sort_keys=True),
        "account_observed": str(bool(account_observed)).lower(),
        "quotes_observed": str(bool(quotes_observed)).lower(),
        "execution_admitted": "false",
    })
    path = out / "paper_release_decision.csv"
    fields = ["row_type", "sequence", "strategy_id", "symbol", "side", "qty",
              "preflight_status", "target_reconciliation_status", "paper_release_decision",
              "reason", "account_observed", "quotes_observed", "execution_admitted"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_paper_audit_event_log(
    out, *, pack_path, pack, strategy_id, orders, preflight,
    current_snapshot, quotes_ready, current_hydration, release_blockers,
    strategy_evidence_card_path, paper_lifecycle_path,
    paper_reconciliation_path, paper_release_decision_path,
    recovery_handoff_index_path,
):
    """Persist an append-friendly paper event chain without implying broker events."""
    generated_at = datetime.now().astimezone().isoformat()
    account_observed = current_snapshot is not None
    source_artifacts = pack.get("artifacts") or {}
    accepted_count = sum(row.get("preflight_status") == "accepted" for row in preflight)
    rejected_count = len(preflight) - accepted_count
    events = [
        {
            "event_type": "score_bundle_loaded",
            "state": "paper_only",
            "source_artifact": str(pack_path),
            "detail": "Frozen candidate pack loaded; score inference is not a live signal.",
        },
        {
            "event_type": "target_order_preview_built",
            "state": "paper_only",
            "source_artifact": str(source_artifacts.get("candidate_orders") or ""),
            "detail": f"{len(orders)} provisional order rows built; broker submission was not attempted.",
        },
        {
            "event_type": "ledger_preflight_completed",
            "state": "paper_only",
            "source_artifact": str(source_artifacts.get("ledger_preflight") or source_artifacts.get("ledger_preflight_by_strategy") or ""),
            "detail": f"Shared-ledger preflight accepted={accepted_count}, rejected={rejected_count}; no ledger mutation.",
        },
        {
            "event_type": "paper_lifecycle_projected",
            "state": "paper_only",
            "source_artifact": str(paper_lifecycle_path),
            "detail": "Order lifecycle is synthetic paper state, not broker acknowledgement or fill evidence.",
        },
        {
            "event_type": "paper_reconciliation_projected",
            "state": "paper_only",
            "source_artifact": str(paper_reconciliation_path),
            "detail": "Internal target/order reconciliation is projected without account, trade or cashflow observation.",
        },
        {
            "event_type": "release_inputs_evaluated",
            "state": "inputs_observed" if account_observed and quotes_ready else "missing_live_inputs",
            "source_artifact": str(strategy_evidence_card_path),
            "detail": (
                f"account_observed={str(account_observed).lower()}, "
                f"same_session_quotes_observed={str(bool(quotes_ready)).lower()}, "
                f"account_sized_preview={str(current_hydration is not None).lower()}."
            ),
        },
        {
            "event_type": "paper_release_blocked",
            "state": "blocked",
            "source_artifact": str(paper_release_decision_path),
            "detail": "; ".join(release_blockers) or "manual review required",
        },
        {
            "event_type": (
                "recovery_handoff_materialized"
                if recovery_handoff_index_path.exists()
                else "recovery_handoff_pending"
            ),
            "state": "paper_only",
            "source_artifact": str(recovery_handoff_index_path) if recovery_handoff_index_path.exists() else "",
            "detail": "Recovery requires manual reconciliation; automatic retry remains disabled.",
        },
    ]
    fields = [
        "event_id", "event_sequence", "event_type", "state", "generated_at",
        "execution_day", "strategy_id", "source_cutoff", "source_artifact",
        "evidence_scope", "account_observed", "quotes_observed", "ledger_mutated",
        "client_interacted", "broker_state_observed", "execution_admitted", "detail",
    ]
    rows = []
    for sequence, event in enumerate(events, start=1):
        rows.append({
            "event_id": f"{pack.get('execution_day') or 'unknown'}-{sequence:03d}-{event['event_type']}",
            "event_sequence": sequence,
            "event_type": event["event_type"],
            "state": event["state"],
            "generated_at": generated_at,
            "execution_day": pack.get("execution_day"),
            "strategy_id": strategy_id or pack.get("selected_strategy_id") or "",
            "source_cutoff": pack.get("source_cutoff"),
            "source_artifact": event["source_artifact"],
            "evidence_scope": "paper_only",
            "account_observed": str(account_observed).lower(),
            "quotes_observed": str(bool(quotes_ready)).lower(),
            "ledger_mutated": "false",
            "client_interacted": "false",
            "broker_state_observed": "false",
            "execution_admitted": "false",
            "detail": event["detail"],
        })
    csv_path = out / "paper_audit_event_log.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    jsonl_path = out / "paper_audit_event_log.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"schema": "paper_audit_event_v1", **row}, ensure_ascii=False) + "\n")
    return csv_path, jsonl_path


def _write_paper_next_session_carry(out, paper_fill_path, paper_position_path,
                                    paper_ledger_path, execution_day):
    """Persist the paper-only state that must be resolved on the next session."""
    fill_rows = read_csv(paper_fill_path)
    position_rows = read_csv(paper_position_path)
    ledger = json.loads(Path(paper_ledger_path).read_text(encoding="utf-8"))
    next_session, calendar_evidence = next_exchange_session(str(execution_day or ""))
    pending_buys = []
    sell_release_rows = []
    pending_orders = []
    for row in fill_rows:
        if row.get("execution_status") != "synthetic_full_fill_reference_price":
            continue
        sequence = str(row.get("sequence", ""))
        side = row.get("side", "")
        pending_orders.append({
            "sequence": sequence,
            "strategy_id": row.get("strategy_id", ""),
            "symbol": row.get("symbol", ""),
            "side": side,
            "qty": int(row.get("qty") or 0),
            "paper_execution_id": f"paper:{execution_day}:{sequence}",
            "settlement_state": "pending_next_session_reconciliation",
            "sellable_state": "next_exchange_session_required" if side == "buy" else "sellable_quantity_unproven",
        })
    for row in position_rows:
        buy_qty = int(row.get("buy_qty") or 0)
        sell_qty = int(row.get("sell_qty") or 0)
        if buy_qty:
            pending_buys.append({
                "symbol": row.get("symbol", ""),
                "buy_qty": buy_qty,
                "eligible_from": next_session or "next_exchange_session_unresolved",
                "state": "paper_t1_pending",
            })
        if sell_qty:
            sell_release_rows.append({
                "symbol": row.get("symbol", ""),
                "sell_qty": sell_qty,
                "state": "sellable_quantity_unproven",
                "required_evidence": "same-session account sellable quantity and broker fill",
            })
    accepted_fills = [
        row for row in fill_rows
        if row.get("execution_status") == "synthetic_full_fill_reference_price"
    ]
    next_session_requirements = [
        "read_same_session_account_cash_positions_and_sellable_quantity",
        "read_broker_orders_and_fills_before_any_new_release",
        "reconcile_paper_carry_to_observed_ledger",
    ]
    if next_session is None:
        next_session_requirements.insert(
            0, "pin_next_exchange_session_from_authoritative_calendar"
        )
    carry = {
        "schema": "paper_next_session_carry_v1",
        "status": "paper_cross_session_state_only",
        "execution_day": execution_day,
        "next_exchange_session": next_session,
        "next_exchange_session_status": calendar_evidence.get(
            "status", "calendar_artifact_unavailable"
        ),
        "calendar_id": calendar_evidence.get("calendar_id"),
        "calendar_source_url": calendar_evidence.get("calendar_source_url"),
        "calendar_source_published": calendar_evidence.get(
            "calendar_source_published"
        ),
        "pending_settlement_order_count": len(accepted_fills),
        "pending_orders": pending_orders,
        "pending_t1_buys": pending_buys,
        "sell_release_review": sell_release_rows,
        "synthetic_cash_delta_rmb": ledger.get("cash_delta_rmb"),
        "synthetic_cash_is_account_balance": False,
        "account_observed": False,
        "broker_observed": False,
        "fills_observed": False,
        "next_session_requirements": next_session_requirements,
        "execution_admitted": False,
        "source_artifacts": {
            "paper_fill_projection": str(paper_fill_path),
            "paper_position_projection": str(paper_position_path),
            "paper_ledger_projection": str(paper_ledger_path),
        },
    }
    path = out / "paper_next_session_carry.json"
    path.write_text(json.dumps(carry, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _function_matrix(current_snapshot, current_quotes, risk_checks_path, schedule_path,
                     strategy_explainability_path=None, exception_alerts_path=None,
                     quote_drift_path=None, scenario_path=None, allocation_path=None,
                     execution_cost_stress_path=None,
                     strategy_evidence_card_path=None,
                     risk_summary_path=None, comparison_path=None, paper_fill_path=None,
                     paper_lifecycle_path=None, paper_reconciliation_path=None,
                     child_projection_path=None, account_sizing_path=None,
                     promotion_card_path=None, promotion_review_path=None,
                     recovery_handoff_path=None, factor_current_path=None,
                     factor_preview_path=None, instrument_master_path=None,
                     instrument_master_coverage_path=None,
                     audit_event_log_path=None):
    """Map institutional trader-desk functions to the local single-ledger path."""
    def source_url(reference):
        if reference.startswith(("Bloomberg AIM", "portfolio decision support", "pre-trade risk control")):
            return INSTITUTIONAL_SOURCES["bloomberg_aim"]
        if reference.startswith(("Bloomberg EMS", "OMS/EMS exception", "institutional order lifecycle controls")):
            return INSTITUTIONAL_SOURCES["bloomberg_ems"]
        if reference.startswith("Bloomberg terminal market data"):
            return INSTITUTIONAL_SOURCES["bloomberg_market_data"]
        if reference.startswith("Bloomberg Rule Builder"):
            return INSTITUTIONAL_SOURCES["bloomberg_automation"]
        if reference.startswith("FlexTCA"):
            return INSTITUTIONAL_SOURCES["flextca"]
        if reference.startswith("FlexTRADER"):
            return INSTITUTIONAL_SOURCES["flextrader"]
        return ""

    research_blocker_open = False
    if exception_alerts_path and exception_alerts_path.is_file():
        try:
            with exception_alerts_path.open(encoding="utf-8", newline="") as handle:
                research_blocker_open = any(
                    row.get("source") == "research_diagnostics" and row.get("state") == "open"
                    for row in csv.DictReader(handle)
                )
        except (OSError, csv.Error):
            research_blocker_open = False

    rows = [
        {"function_id": "portfolio_rebalance", "institutional_reference": "Bloomberg AIM", "institutional_capability": "portfolio decision support, benchmarking and rebalancing", "local_state": "paper_comparison_ready" if comparison_path else "paper_ready", "next_gap": "current account sizing", "artifact": comparison_path.name if comparison_path else "opening_pack.json", "tomorrow_use": "after_account_and_quotes", "priority": "P0", "next_implementation": "hydrate observed_account preview"},
        {"function_id": "strategy_review_and_promotion", "institutional_reference": "Bloomberg AIM / PM workflow", "institutional_capability": "capacity-aware candidate review with controlled strategy promotion", "local_state": "promotion_review_template_ready" if promotion_review_path else "capacity_aware_manual_review_ready" if promotion_card_path else "paper_review_only", "next_gap": "fresh holdout, observed account/quotes and explicit human decision", "artifact": promotion_review_path.name if promotion_review_path else promotion_card_path.name if promotion_card_path else "strategy_comparison.csv", "tomorrow_use": "review_only", "priority": "P0", "next_implementation": "fill reviewer, timestamp and fresh-holdout evidence before any promotion"},
        {"function_id": "selected_plan_unified_view", "institutional_reference": "Bloomberg AIM / EMS", "institutional_capability": "one selected strategy view linking target holdings, orders, THS prefill fields, paper lifecycle, reconciliation and capacity", "local_state": "selected_plan_unified_view_ready" if paper_lifecycle_path and paper_reconciliation_path and paper_fill_path else "paper_plan_partial", "next_gap": "same-session account/quote observation and authorized THS readback", "artifact": "/api/trading/operator-package/selected-plan", "tomorrow_use": "paper_only", "priority": "P0", "next_implementation": "bind observed account, quote and broker receipts before release"},
        {"function_id": "ems_blotter_lifecycle", "institutional_reference": "Bloomberg EMS / FlexTRADER", "institutional_capability": "centralized blotter from order entry through execution", "local_state": "paper_lifecycle_rehearsal_ready" if paper_lifecycle_path else "paper_ready", "next_gap": "broker acknowledgements and fills", "artifact": paper_lifecycle_path.name if paper_lifecycle_path else "blotter.csv", "tomorrow_use": "paper_only", "priority": "P1", "next_implementation": "consume order and trade observations"},
        {"function_id": "paper_fill_rehearsal", "institutional_reference": "paper trading / EMS simulation", "institutional_capability": "replay order intent through synthetic fills, cash and T+1 projections", "local_state": "paper_fill_projection_ready" if paper_fill_path else "paper_ready", "next_gap": "observed broker fills and account opening balance", "artifact": paper_fill_path.name if paper_fill_path else "blotter.csv", "tomorrow_use": "paper_only", "priority": "P1", "next_implementation": "replace synthetic rows with broker observation receipts"},
        {"function_id": "order_lifecycle_recovery", "institutional_reference": "OMS/EMS exception and recovery workflow", "institutional_capability": "exception repair, pause, manual takeover and resume", "local_state": "paper_recovery_evidence_template_ready" if recovery_handoff_path else "paper_recovery_defined", "next_gap": "observed broker state after interruption", "artifact": recovery_handoff_path.name if recovery_handoff_path else "recovery_plan.csv", "tomorrow_use": "manual_recovery", "priority": "P1", "next_implementation": "bind observed broker-state reconciliation receipt"},
        {"function_id": "pre_trade_risk_compliance", "institutional_reference": "Bloomberg AIM / FlexTRADER", "institutional_capability": "pre-trade and post-trade compliance with real-time risk checks", "local_state": "paper_account_sized_risk_ready" if account_sizing_path else "paper_static_risk_ready", "next_gap": "same-session account and quote inputs", "artifact": account_sizing_path.name if account_sizing_path else risk_summary_path.name if risk_summary_path else risk_checks_path.name, "tomorrow_use": "after_account_and_quotes", "priority": "P0", "next_implementation": "replace paper cash with observed account limits" if account_sizing_path else "run account-sized risk checks"},
        {"function_id": "portfolio_risk_exposure", "institutional_reference": "portfolio decision support", "institutional_capability": "historical concentration, stress-exposure and style-exposure diagnostics", "local_state": "paper_historical_risk_ready" if _latest_saved_risk_analytics() else "paper_risk_view_missing", "next_gap": "current account, current quotes and benchmark-linked exposures", "artifact": strategy_evidence_card_path.name if strategy_evidence_card_path else "historical risk diagnostics", "tomorrow_use": "review_only", "priority": "P1", "next_implementation": "refresh from observed holdings and current risk inputs"},
        {"function_id": "basket_execution_schedule", "institutional_reference": "FlexTRADER portfolio scheduler", "institutional_capability": "portfolio basket scheduling with multi-factor execution constraints", "local_state": "paper_manual_sequence", "next_gap": "current liquidity and child-order feedback", "artifact": schedule_path.name, "tomorrow_use": "manual_sequence", "priority": "P1", "next_implementation": "refresh schedule from observed quotes"},
        {"function_id": "order_release_controls", "institutional_reference": "Bloomberg Rule Builder", "institutional_capability": "rules-based order routing and conditional release", "local_state": "paper_controls_defined", "next_gap": "live time/price/market-data trigger", "artifact": "execution_controls.csv", "tomorrow_use": "manual_review", "priority": "P1", "next_implementation": "connect clock and quote observations"},
        {"function_id": "child_order_algorithms", "institutional_reference": "Bloomberg EMS / FlexTRADER", "institutional_capability": "algorithmic child-order scheduling and broker/algo selection", "local_state": "paper_fill_aware_child_projection_ready" if child_projection_path else "paper_twap_schedule_ready", "next_gap": "live market and child-fill feedback", "artifact": child_projection_path.name if child_projection_path else "child_order_schedule.csv", "tomorrow_use": "paper_only", "priority": "P2", "next_implementation": "fill-aware child-order adapter"},
        {"function_id": "portfolio_what_if", "institutional_reference": "Bloomberg AIM scenario analysis", "institutional_capability": "scenario analysis and decision support before release", "local_state": "paper_scenario_ready" if scenario_path else "paper_scenario_only", "next_gap": "same-session account and quote inputs", "artifact": scenario_path.name if scenario_path else "tca_pretrade.csv", "tomorrow_use": "after_account_and_quotes", "priority": "P1", "next_implementation": "scenario deltas on observed account"},
        {"function_id": "broker_routing", "institutional_reference": "Bloomberg EMS / FlexLINK", "institutional_capability": "multi-broker connectivity and consolidated liquidity access", "local_state": "offline_prefill_payload_ready", "next_gap": "authorized THS read-only/prefill observation", "artifact": "trading/ths/adapter.py; ths prefill-payload", "tomorrow_use": "manual_ths_only", "priority": "P0", "next_implementation": "bind the offline payload to a freshly verified THS mapping"},
        {"function_id": "security_master_and_scope", "institutional_reference": "institutional security master / OMS reference data", "institutional_capability": "stable instrument identity, observed status and research-universe scope reconciliation", "local_state": "cutoff_matched_scope_checked" if instrument_master_path and instrument_master_coverage_path else "identity_master_available_scope_unchecked" if instrument_master_path else "security_master_missing", "next_gap": "PIT status history, issuer lineage and independent exchange reconciliation", "artifact": instrument_master_coverage_path.name if instrument_master_coverage_path else instrument_master_path.name if instrument_master_path else "instrument_master_v1", "tomorrow_use": "review_only", "priority": "P1", "next_implementation": "keep source scope separate from execution admission and add reviewed PIT evidence"},
        {"function_id": "broker_statement_ingestion", "institutional_reference": "Bloomberg AIM operations", "institutional_capability": "cash-flow, non-trade movement, restriction-release and share-credit statement ingestion with provenance", "local_state": "paper_import_api_ready", "next_gap": "verified account mapping and a broker-native statement endpoint", "artifact": "trading/api.py#/ths-simulated-ledger/import-movements; trading/ths/observation_preview.py; trading/ths/ledger_import.py", "tomorrow_use": "manual_reconciliation", "priority": "P0", "next_implementation": "consume explicit THS exports through the API or CLI before the next paper plan or broker reconciliation"},
        {"function_id": "real_time_market_data", "institutional_reference": "Bloomberg terminal market data", "institutional_capability": "real-time market-data processing for trading and controls", "local_state": "missing_for_open", "next_gap": "same-session raw RMB quote import", "artifact": "quote_request.csv", "tomorrow_use": "required_input", "priority": "P0", "next_implementation": "load same-session raw RMB quotes"},
        {"function_id": "transaction_cost_analysis", "institutional_reference": "FlexTCA", "institutional_capability": "real-time and historical TCA feeding execution optimization", "local_state": "paper_candidate_stress_ready" if execution_cost_stress_path else "paper_quote_drift_ready" if quote_drift_path else "pretrade_estimate_only", "next_gap": "observed fills and benchmarks", "artifact": execution_cost_stress_path.name if execution_cost_stress_path else quote_drift_path.name if quote_drift_path else "tca_pretrade.csv", "tomorrow_use": "paper_candidate_stress" if execution_cost_stress_path else "paper_only" if quote_drift_path else "pretrade_only", "priority": "P1", "next_implementation": "add observed fill benchmark"},
        {"function_id": "post_trade_reconciliation", "institutional_reference": "Bloomberg AIM operations", "institutional_capability": "post-trade matching, settlement and reconciliation", "local_state": "paper_internal_reconciliation_ready" if paper_reconciliation_path else "code_ready_readonly", "next_gap": "account/order/trade observations", "artifact": paper_reconciliation_path.name if paper_reconciliation_path else "ths journal", "tomorrow_use": "after_manual_trade", "priority": "P0", "next_implementation": "capture account/orders/trades"},
        {"function_id": "allocation_and_settlement", "institutional_reference": "Bloomberg AIM operations", "institutional_capability": "single source of truth for positions, cash and exposures", "local_state": "paper_allocation_projection_ready" if allocation_path else "ledger_ready_paper_only", "next_gap": "broker confirms, settlement and cash reconciliation", "artifact": allocation_path.name if allocation_path else "ledger events", "tomorrow_use": "paper_only", "priority": "P1", "next_implementation": "bind confirmed contract and cashflow"},
        {"function_id": "audit_trail_and_alerts", "institutional_reference": "institutional order lifecycle controls", "institutional_capability": "visualization, alerting, automation and auditable history", "local_state": "paper_event_log_ready" if audit_event_log_path else "paper_alerts_ready" if exception_alerts_path else "ledger_events_only", "next_gap": "live acknowledgement/fill alerts", "artifact": audit_event_log_path.name if audit_event_log_path else exception_alerts_path.name if exception_alerts_path else "ledger events", "tomorrow_use": "paper_only", "priority": "P2", "next_implementation": "consume live acknowledgement/fill alert"},
        {"function_id": "strategy_explainability", "institutional_reference": "portfolio decision support", "institutional_capability": "decision support linking portfolio intent to execution evidence", "local_state": "candidate_evidence_card_ready" if strategy_evidence_card_path else "historical_diagnostic_ready" if strategy_explainability_path else "research_ready", "next_gap": "current-session evidence and model promotion", "artifact": strategy_evidence_card_path.name if strategy_evidence_card_path else strategy_explainability_path.name if strategy_explainability_path else "current score bundle", "tomorrow_use": "review_only", "priority": "P0", "next_implementation": "refresh validated current-eod bundle"},
        {"function_id": "frozen_factor_current_score", "institutional_reference": "portfolio decision support / research model inference", "institutional_capability": "reproducible frozen-cutoff factor inference with ranked research candidates", "local_state": "completed_frozen_cutoff_inference" if factor_current_path else "research_score_missing", "next_gap": "new labels, current data and independent validation before any promotion", "artifact": factor_current_path.name if factor_current_path else "strategy_evidence_card.json", "tomorrow_use": "review_only", "priority": "P1", "next_implementation": "keep the frozen score separate from live quotes and account sizing"},
        {"function_id": "factor_target_order_preview", "institutional_reference": "portfolio decision support / EMS paper workflow", "institutional_capability": "turn a frozen score bundle into provisional target and order rows through the shared paper consumer", "local_state": "paper_target_order_preview_ready" if factor_preview_path else "paper_target_preview_missing", "next_gap": "current account, same-session RMB quotes, sellable quantity/T+1 and manual review", "artifact": factor_preview_path.name if factor_preview_path else "strategy_evidence_card.json", "tomorrow_use": "paper_only", "priority": "P0", "next_implementation": "hydrate observed inputs without mutating the ledger or broker state"},
        {"function_id": "research_data_exception_management", "institutional_reference": "portfolio decision support", "institutional_capability": "research-data provenance, held-action exception triage and resumable evidence workflow", "local_state": "research_blocker_open" if research_blocker_open else "research_monitoring_ready" if exception_alerts_path else "research_diagnostics_only", "next_gap": "issuer/source evidence for the next held action" if research_blocker_open else "new reviewed source evidence only when a later projection blocks", "artifact": exception_alerts_path.name if exception_alerts_path else "research diagnostics API", "tomorrow_use": "review_only", "priority": "P1", "next_implementation": "attach reviewed source evidence and resume after the last successful ledger session" if research_blocker_open else "keep the latest completed projection visible and retain historical failures"},
        {"function_id": "kill_switch", "institutional_reference": "pre-trade risk control", "institutional_capability": "controlled release boundary with the ability to stop automation", "local_state": "submission_disabled", "next_gap": "explicitly authorized broker mode", "artifact": "execution_admitted=false", "tomorrow_use": "enabled", "priority": "P0", "next_implementation": "keep automatic submission disabled"},
    ]
    workstreams = {
        "portfolio_rebalance": "research",
        "strategy_review_and_promotion": "research",
        "selected_plan_unified_view": "execution",
        "ems_blotter_lifecycle": "execution",
        "paper_fill_rehearsal": "execution",
        "order_lifecycle_recovery": "execution",
        "pre_trade_risk_compliance": "risk",
        "portfolio_risk_exposure": "risk",
        "basket_execution_schedule": "execution",
        "order_release_controls": "risk",
        "child_order_algorithms": "execution",
        "portfolio_what_if": "risk",
        "broker_routing": "execution",
        "security_master_and_scope": "research",
        "broker_statement_ingestion": "reconciliation",
        "real_time_market_data": "execution",
        "transaction_cost_analysis": "risk",
        "post_trade_reconciliation": "reconciliation",
        "allocation_and_settlement": "reconciliation",
        "audit_trail_and_alerts": "reconciliation",
        "strategy_explainability": "research",
        "frozen_factor_current_score": "research",
        "factor_target_order_preview": "execution",
        "research_data_exception_management": "research",
        "kill_switch": "risk",
    }
    paper_ready_markers = (
        "paper_", "selected_plan_unified_view_ready", "offline_prefill_payload_ready",
        "manual_export_contract_ready", "paper_import_api_ready", "submission_disabled", "cutoff_matched_scope_checked",
    )
    for row in rows:
        row["source_url"] = source_url(row["institutional_reference"])
        row["workstream"] = workstreams.get(row["function_id"], "platform")
        state = str(row["local_state"])
        if state == "submission_disabled":
            row["delivery_stage"] = "safety_boundary"
        elif state.startswith(paper_ready_markers):
            row["delivery_stage"] = "paper_consumer"
        elif state in {"missing_for_open", "research_score_missing", "security_master_missing"}:
            row["delivery_stage"] = "unfilled_gap"
        elif "ready" in state or "defined" in state or "monitoring" in state:
            row["delivery_stage"] = "paper_or_readonly"
        else:
            row["delivery_stage"] = "diagnostic_only"
    return rows


def _write_exception_alerts(out, blockers, *, account_observed, quotes_observed, preview_hydrated):
    """Persist operator-visible paper exceptions without implying live alerts."""
    rows = []
    for sequence, blocker in enumerate(blockers, start=1):
        rows.append({
            "alert_id": f"paper-release-{sequence}",
            "severity": "P0" if blocker in {"current_user_account_observation_missing", "same_session_raw_rmb_quotes_missing"} else "P1",
            "state": "open",
            "source": "paper_release_control",
            "code": blocker,
            "message": blocker.replace("_", " "),
            "live_alert": "disabled",
            "operator_action": "supply fresh evidence and review manually",
            "artifact": "",
            "execution_admitted": "false",
        })
    # Research exceptions belong in the same operator-facing board as paper
    # release blockers, but remain separate from account/quote readiness.
    try:
        from .. import research_diagnostics
        diagnostic_rows = research_diagnostics.catalog()
    except (ImportError, OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        diagnostic_rows = []
    # Keep historical failed projections in their own artifacts, but do not
    # surface every superseded repair stop as a current operator alert. The
    # newest fixed-score projection is the resumable research state consumed
    # by this desk snapshot.
    current_projection = next(
        (row for row in diagnostic_rows
         if row.get("family") == "factor_execution_projection"),
        None,
    )
    if current_projection is not None:
        for sequence, blocker in enumerate(current_projection.get("blockers") or [], start=1):
            rows.append({
                "alert_id": f"research-blocker-{current_projection.get('id', 'unknown')}-{sequence}",
                "severity": "P1",
                "state": "open",
                "source": "research_diagnostics",
                "code": "factor_execution_projection_blocked",
                "message": str(blocker),
                "live_alert": "disabled",
                "operator_action": "attach reviewed source evidence; resume after the last successful ledger session; do not impute missing terms",
                "artifact": current_projection.get("report_path") or "research diagnostics API",
                "execution_admitted": "false",
            })
    if not rows:
        rows.append({
            "alert_id": "paper-release-clear",
            "severity": "P2",
            "state": "informational",
            "source": "paper_release_control",
            "code": "paper_inputs_present",
            "message": "paper inputs present; live submission remains disabled",
            "live_alert": "disabled",
            "operator_action": "manual review only",
            "artifact": "",
            "execution_admitted": "false",
        })
    path = out / "exception_alerts.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_quote_drift(out, orders, current_quotes):
    """Compare same-session raw-RMB quotes with research reference prices.

    This is a paper quote-drift diagnostic, not a fill or execution-cost claim.
    Positive adverse impact means the observed quote moved against the order.
    """
    prices = (current_quotes or {}).get("prices", {})
    rows = []
    for sequence, order in enumerate(orders, start=1):
        symbol = order.get("symbol", "")
        side = order.get("side", "")
        quote = prices.get(symbol) or {}
        reference = Decimal(str(order.get("price_rmb") or "0"))
        observed = Decimal(str(quote.get("price_rmb") or "0"))
        try:
            qty = int(order.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if reference <= 0 or observed <= 0 or qty <= 0:
            rows.append({
                "sequence": sequence, "symbol": symbol, "side": side, "qty": qty,
                "reference_price_rmb": str(reference), "observed_price_rmb": str(observed),
                "quote_status": "missing_or_invalid", "price_delta_rmb": "",
                "adverse_drift_rmb": "", "adverse_drift_bps": "",
                "basis": "same-session quote vs research reference; no fill observed",
            })
            continue
        delta = observed - reference
        adverse = delta if side == "buy" else -delta if side == "sell" else Decimal("0")
        rows.append({
            "sequence": sequence, "symbol": symbol, "side": side, "qty": qty,
            "reference_price_rmb": f"{reference:.2f}", "observed_price_rmb": f"{observed:.2f}",
            "quote_status": "observed_same_session", "price_delta_rmb": f"{delta:.4f}",
            "adverse_drift_rmb": f"{(adverse * qty):.4f}",
            "adverse_drift_bps": f"{(adverse / reference * Decimal('10000')):.2f}",
            "basis": "same-session quote vs research reference; no fill observed",
        })
    path = out / "tca_quote_drift.csv"
    fields = ["sequence", "symbol", "side", "qty", "reference_price_rmb", "observed_price_rmb",
              "quote_status", "price_delta_rmb", "adverse_drift_rmb", "adverse_drift_bps", "basis"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    valid = [row for row in rows if row["quote_status"] == "observed_same_session"]
    adverse_rmb = sum(Decimal(row["adverse_drift_rmb"]) for row in valid)
    reference_notional = sum(
        Decimal(row["reference_price_rmb"]) * Decimal(row["qty"])
        for row in valid
    )
    return path, {
        "coverage_count": len(valid),
        "requested_count": len(rows),
        "adverse_drift_rmb": money(adverse_rmb),
        "adverse_drift_bps": (
            (adverse_rmb / reference_notional * Decimal("10000")).quantize(Decimal("0.01"))
            if reference_notional else Decimal("0.00")
        ),
    }


def _write_pretrade_scenarios(out, orders, config):
    """Write deterministic paper what-if cases from frozen reference prices.

    These scenarios are decision-support arithmetic only. They do not replace
    same-session quotes, account cash/T+1 checks, or broker fills.
    """
    active = []
    for order in orders:
        try:
            qty = int(order.get("qty") or 0)
            price = Decimal(str(order.get("price_rmb") or "0"))
        except (TypeError, ValueError, InvalidOperation):
            continue
        if qty > 0 and price > 0 and order.get("side") in {"buy", "sell"}:
            active.append((order["side"], qty, price))

    commission_rate = Decimal(str(config.get("commission", "0")))
    minimum_commission = Decimal(str(config.get("minimum_commission", "0")))
    sell_tax_rate = Decimal(str(config.get("sell_tax", "0")))
    transfer_rate = Decimal(str(config.get("transfer_fee", "0")))
    slippage_rate = Decimal(str(config.get("slippage_bps", "0"))) / Decimal("10000")
    scenarios = (
        ("reference", Decimal("0"), Decimal("0")),
        ("adverse_1pct", Decimal("0.01"), Decimal("-0.01")),
        ("adverse_3pct", Decimal("0.03"), Decimal("-0.03")),
    )
    rows = []
    reference_buy = sum(price * qty for side, qty, price in active if side == "buy")
    reference_sell = sum(price * qty for side, qty, price in active if side == "sell")
    reference_total = reference_buy + reference_sell
    for scenario_id, buy_shock, sell_shock in scenarios:
        buy_notional = sum(price * qty * (Decimal("1") + buy_shock) for side, qty, price in active if side == "buy")
        sell_notional = sum(price * qty * (Decimal("1") + sell_shock) for side, qty, price in active if side == "sell")
        commission = max(buy_notional * commission_rate, minimum_commission if buy_notional else Decimal("0"))
        commission += max(sell_notional * commission_rate, minimum_commission if sell_notional else Decimal("0"))
        sell_tax = sell_notional * sell_tax_rate
        transfer_fee = (buy_notional + sell_notional) * transfer_rate
        slippage = (buy_notional + sell_notional) * slippage_rate
        estimated_cost = commission + sell_tax + transfer_fee + slippage
        adverse_impact = (buy_notional - reference_buy) + (reference_sell - sell_notional)
        rows.append({
            "scenario_id": scenario_id,
            "buy_price_shock_pct": f"{buy_shock * Decimal('100'):.2f}",
            "sell_price_shock_pct": f"{sell_shock * Decimal('100'):.2f}",
            "buy_notional_rmb": f"{buy_notional:.2f}",
            "sell_notional_rmb": f"{sell_notional:.2f}",
            "gross_notional_rmb": f"{(buy_notional + sell_notional):.2f}",
            "estimated_fees_slippage_rmb": f"{estimated_cost:.2f}",
            "adverse_price_impact_rmb": f"{adverse_impact:.2f}",
            "total_reference_impact_rmb": f"{(adverse_impact + estimated_cost):.2f}",
            "reference_gross_notional_rmb": f"{reference_total:.2f}",
            "basis": "frozen research reference prices; paper what-if only; no quote or fill observed",
        })
    path = out / "scenario_analysis.csv"
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_allocation_projection(out, orders, preflight):
    """Project paper cash and position deltas before any account observation.

    The rows describe intended order effects at frozen reference prices. They
    are not fills, broker settlement, sellable shares, or account balances.
    """
    preflight_by_key = {_order_key(row): row for row in preflight}
    rows = []
    for sequence, order in enumerate(orders, start=1):
        side = order.get("side", "")
        symbol = order.get("symbol", "")
        try:
            qty = int(order.get("qty") or 0)
            price = Decimal(str(order.get("price_rmb") or "0"))
        except (TypeError, ValueError, InvalidOperation):
            qty, price = 0, Decimal("0")
        notional = price * qty
        match = preflight_by_key.get(_order_key(order), {})
        rows.append({
            "row_type": "order",
            "sequence": sequence,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "reference_price_rmb": f"{price:.2f}",
            "reference_notional_rmb": f"{notional:.2f}",
            "projected_cash_delta_rmb": f"{(-notional if side == 'buy' else notional if side == 'sell' else Decimal('0')):.2f}",
            "projected_position_delta_shares": qty if side == "buy" else -qty if side == "sell" else 0,
            "projected_same_day_sellable_delta_shares": 0 if side == "buy" else -qty if side == "sell" else 0,
            "settlement_state": "paper_projection_only",
            "t1_dependency": "buy shares not sellable same day" if side == "buy" else "account sellable quantity and fill required" if side == "sell" else "unsupported side",
            "preflight_status": match.get("preflight_status", "unknown"),
            "basis": "frozen research reference price; no fill or account observation",
        })
    buy_notional = sum(Decimal(row["reference_notional_rmb"]) for row in rows if row["side"] == "buy")
    sell_notional = sum(Decimal(row["reference_notional_rmb"]) for row in rows if row["side"] == "sell")
    rows.append({
        "row_type": "total",
        "sequence": "",
        "symbol": "",
        "side": "",
        "qty": sum(row["qty"] for row in rows),
        "reference_price_rmb": "",
        "reference_notional_rmb": f"{(buy_notional + sell_notional):.2f}",
        "projected_cash_delta_rmb": f"{(sell_notional - buy_notional):.2f}",
        "projected_position_delta_shares": sum(row["projected_position_delta_shares"] for row in rows),
        "projected_same_day_sellable_delta_shares": sum(row["projected_same_day_sellable_delta_shares"] for row in rows),
        "settlement_state": "paper_projection_only",
        "t1_dependency": "account cash, positions, sellable shares and fills required",
        "preflight_status": "n/a",
        "basis": "aggregate of frozen order projections; not account reconciliation",
    })
    path = out / "allocation_settlement_projection.csv"
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_paper_fill_projection(out, orders, preflight, config, execution_day):
    """Replay accepted paper parents as explicit synthetic reference fills.

    This is a rehearsal artifact for the same-ledger consumer.  It deliberately
    has no account opening balance, broker acknowledgement, or live price, so
    it cannot be mistaken for an account or execution receipt.
    """
    preflight_by_key = {_order_key(row): row for row in preflight}
    config_fields = StrategyConfig.__dataclass_fields__
    ledger_config = StrategyConfig(**{
        key: value for key, value in config.items() if key in config_fields
    }).validate()
    rows = []
    lifecycle = []
    cash_delta = Decimal("0")
    position_deltas = {}
    position_projection = {}
    synthetic_count = 0

    for sequence, row in enumerate(orders, start=1):
        match = preflight_by_key.get(_order_key(row), {})
        status = match.get("preflight_status", "unknown")
        try:
            qty = int(row.get("qty") or 0)
            price = Decimal(str(row.get("price_rmb") or "0"))
        except (TypeError, ValueError, InvalidOperation):
            qty, price = 0, Decimal("0")
        gross = (price * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fees = Decimal("0")
        accepted = status == "accepted" and qty > 0 and price > 0
        order_cash_delta = Decimal("0")
        if accepted:
            # Use the same integer-cent notional and dated fee rules as the
            # shared Ledger.  The desk must not drift from replay accounting.
            gross_cents = notional_cents(cents(price), qty)
            gross = Decimal(gross_cents) / Decimal("100")
            fees = Decimal(ledger_fees(gross_cents, row.get("side"), ledger_config,
                                       day=execution_day)) / Decimal("100")
            lifecycle.extend([
                {"sequence": sequence, "event_sequence": 1, "strategy_id": row.get("strategy_id", ""),
                 "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                 "state": "paper_preflight_accepted", "source": "unified_ledger_preflight",
                 "execution_id": "", "real_broker_state": "false"},
                {"sequence": sequence, "event_sequence": 2, "strategy_id": row.get("strategy_id", ""),
                 "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                 "state": "synthetic_submission_assumed", "source": "paper_rehearsal",
                 "execution_id": f"paper:{execution_day}:{sequence}", "real_broker_state": "false"},
            ])
            synthetic_count += 1
            signed_position = qty if row.get("side") == "buy" else -qty
            position_deltas[row.get("symbol", "")] = position_deltas.get(row.get("symbol", ""), 0) + signed_position
            position = position_projection.setdefault(row.get("symbol", ""), {
                "buy_qty": 0, "sell_qty": 0, "buy_notional": Decimal("0"), "sell_notional": Decimal("0")
            })
            if row.get("side") == "buy":
                position["buy_qty"] += qty
                position["buy_notional"] += gross
            else:
                position["sell_qty"] += qty
                position["sell_notional"] += gross
            # Ledger cash movements are RMB cent-denominated entries.  Round
            # each order before accumulating so the summary reconciles to the
            # exported order-level journal rather than to hidden fractions.
            order_cash_delta = (
                (-(gross + fees) if row.get("side") == "buy" else gross - fees)
                .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
            cash_delta += order_cash_delta
            execution_status = "synthetic_full_fill_reference_price"
            availability = "next_exchange_session_required" if row.get("side") == "buy" else "sellable_quantity_not_proven"
            reason = "accepted paper parent; assumed full fill for rehearsal only"
            lifecycle.extend([
                {"sequence": sequence, "event_sequence": 3, "strategy_id": row.get("strategy_id", ""),
                 "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                 "state": "synthetic_full_fill_assumed", "source": "paper_rehearsal",
                 "execution_id": f"paper:{execution_day}:{sequence}", "real_broker_state": "false"},
                {"sequence": sequence, "event_sequence": 4, "strategy_id": row.get("strategy_id", ""),
                 "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                 "state": "settlement_pending", "source": "paper_rehearsal",
                 "execution_id": f"paper:{execution_day}:{sequence}", "real_broker_state": "false"},
            ])
        else:
            execution_status = "not_executed_preflight_rejected"
            availability = "not_applicable"
            reason = match.get("preflight_reason", "not accepted by copied ledger preflight")
            lifecycle.append({
                "sequence": sequence, "event_sequence": 1, "strategy_id": row.get("strategy_id", ""),
                "symbol": row.get("symbol", ""), "side": row.get("side", ""),
                "state": "paper_preflight_rejected", "source": "unified_ledger_preflight",
                "execution_id": "", "real_broker_state": "false",
            })
        rows.append({
            "sequence": sequence,
            "strategy_id": row.get("strategy_id", ""),
            "symbol": row.get("symbol", ""),
            "side": row.get("side", ""),
            "qty": qty,
            "reference_price_rmb": f"{price:.2f}",
            "gross_notional_rmb": f"{gross:.2f}",
            "fees_rmb": f"{fees:.2f}",
            "cash_delta_rmb": f"{order_cash_delta:.2f}",
            "position_delta_shares": signed_position if accepted else 0,
            "execution_status": execution_status,
            "availability_policy": availability,
            "reason": reason,
            "execution_admitted": "false",
        })

    path = out / "paper_fill_projection.csv"
    fields = list(rows[0]) if rows else [
        "sequence", "strategy_id", "symbol", "side", "qty", "reference_price_rmb",
        "gross_notional_rmb", "fees_rmb", "cash_delta_rmb", "position_delta_shares",
        "execution_status", "availability_policy", "reason", "execution_admitted",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    lifecycle_path = out / "paper_order_lifecycle.csv"
    with lifecycle_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["sequence", "event_sequence", "strategy_id", "symbol", "side", "state", "source", "execution_id", "real_broker_state"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(lifecycle)

    position_path = out / "paper_position_projection.csv"
    position_fields = ["symbol", "buy_qty", "sell_qty", "net_position_delta_shares",
                       "buy_notional_rmb", "sell_notional_rmb", "net_trade_notional_rmb",
                       "availability_policy", "account_position_observed"]
    with position_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=position_fields)
        writer.writeheader()
        for symbol in sorted(position_projection):
            position = position_projection[symbol]
            writer.writerow({
                "symbol": symbol,
                "buy_qty": position["buy_qty"],
                "sell_qty": position["sell_qty"],
                "net_position_delta_shares": position_deltas.get(symbol, 0),
                "buy_notional_rmb": f"{position['buy_notional']:.2f}",
                "sell_notional_rmb": f"{position['sell_notional']:.2f}",
                "net_trade_notional_rmb": f"{(position['buy_notional'] - position['sell_notional']):.2f}",
                "availability_policy": "buy_next_exchange_session; sellable_quantity_unproven",
                "account_position_observed": "false",
            })

    # Reconcile the paper projection internally before it is exposed to an
    # operator.  These checks validate arithmetic and event completeness only;
    # they deliberately do not upgrade synthetic rows into broker evidence.
    accepted_rows = [row for row in rows if row["execution_status"] == "synthetic_full_fill_reference_price"]
    rejected_rows = [row for row in rows if row["execution_status"] == "not_executed_preflight_rejected"]
    observed_cash_delta = sum((Decimal(row["cash_delta_rmb"]) for row in rows), Decimal("0"))
    expected_lifecycle_events = len(accepted_rows) * 4 + len(rejected_rows)
    reconciliation_rows = [
        {
            "check_id": "order_row_count",
            "scope": "all_orders",
            "status": "pass" if len(rows) == len(orders) else "fail",
            "expected": str(len(orders)), "actual": str(len(rows)), "difference": str(len(rows) - len(orders)),
            "basis": "paper fill rows must preserve every frozen candidate parent",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        },
        {
            "check_id": "synthetic_fill_count",
            "scope": "accepted_orders",
            "status": "pass" if len(accepted_rows) == synthetic_count else "fail",
            "expected": str(synthetic_count), "actual": str(len(accepted_rows)), "difference": str(len(accepted_rows) - synthetic_count),
            "basis": "accepted parents have one synthetic full-fill row",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        },
        {
            "check_id": "preflight_rejected_no_fill",
            "scope": "rejected_orders",
            "status": "pass" if not any(row["position_delta_shares"] or Decimal(row["cash_delta_rmb"]) != 0 for row in rejected_rows) else "fail",
            "expected": "0 rejected rows with cash or position effect", "actual": str(sum(1 for row in rejected_rows if row["position_delta_shares"] or Decimal(row["cash_delta_rmb"]) != 0)),
            "difference": "0",
            "basis": "preflight-rejected parents are excluded from synthetic execution",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        },
        {
            "check_id": "cash_delta_reconciles",
            "scope": "all_orders",
            "status": "pass" if observed_cash_delta == cash_delta else "fail",
            "expected": f"{cash_delta:.2f}", "actual": f"{observed_cash_delta:.2f}", "difference": f"{(observed_cash_delta - cash_delta):.2f}",
            "basis": "sum of order-level fee-adjusted paper cash deltas",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        },
        {
            "check_id": "lifecycle_event_count",
            "scope": "all_orders",
            "status": "pass" if len(lifecycle) == expected_lifecycle_events else "fail",
            "expected": str(expected_lifecycle_events), "actual": str(len(lifecycle)), "difference": str(len(lifecycle) - expected_lifecycle_events),
            "basis": "accepted parents have four rehearsal events; rejected parents have one terminal event",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        },
    ]
    for symbol in sorted(position_deltas):
        row_effect = sum(int(row["position_delta_shares"]) for row in accepted_rows if row["symbol"] == symbol)
        expected = int(position_deltas[symbol])
        reconciliation_rows.append({
            "check_id": "position_delta_reconciles",
            "scope": symbol,
            "status": "pass" if row_effect == expected else "fail",
            "expected": str(expected), "actual": str(row_effect), "difference": str(row_effect - expected),
            "basis": "accepted synthetic fill rows versus per-symbol position projection",
            "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
        })
    reconciliation_rows.append({
        "check_id": "live_broker_reconciliation",
        "scope": "account_orders_trades",
        "status": "unobserved",
        "expected": "same-session broker order/trade/account evidence",
        "actual": "not supplied",
        "difference": "",
        "basis": "paper projection is not a broker receipt and cannot settle the account",
        "account_observed": "false", "broker_observed": "false", "execution_admitted": "false",
    })
    reconciliation_path = out / "paper_reconciliation.csv"
    with reconciliation_path.open("w", encoding="utf-8", newline="") as handle:
        fields = list(reconciliation_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(reconciliation_rows)

    summary = {
        "schema": "paper_fill_projection_v1",
        "status": "synthetic_reference_rehearsal",
        "execution_day": execution_day,
        "source": "frozen candidate orders and copied unified-ledger preflight",
        "assumption": "accepted orders are fully filled at frozen reference prices; no account or broker state is inferred",
        "synthetic_full_fill_count": synthetic_count,
        "order_count": len(rows),
        "cash_delta_rmb": f"{cash_delta:.2f}",
        "position_deltas": {symbol: qty for symbol, qty in sorted(position_deltas.items()) if symbol},
        "t1_policy": "buy fills become sellable only on the next exchange session; current sellable shares were not inferred",
        "fees_basis": "trading.rules.fees with integer-cent notional; shared replay ledger policy",
        "account_observed": False,
        "quotes_observed": False,
        "client_interacted": False,
        "ledger_mutated": False,
        "submission_attempted": False,
        "execution_admitted": False,
        "broker_prefill_attempted": False,
        "fills_observed": False,
        "internal_reconciliation_status": "pass" if all(row["status"] != "fail" for row in reconciliation_rows) else "fail",
        "live_reconciliation_status": "unobserved",
        "artifact": str(path),
        "lifecycle_artifact": str(lifecycle_path),
        "position_artifact": str(position_path),
        "reconciliation_artifact": str(reconciliation_path),
    }
    summary_path = out / "paper_ledger_projection.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, summary_path, lifecycle_path, position_path, reconciliation_path, summary


def _write_execution_controls(out, config):
    """Expose institutional release and child-order controls without implying live support."""
    participation = config.get("participation", "")
    rows = [
        {"control_id": "sell_first_cash_release", "institutional_analogue": "basket release / cash management", "state": "paper_enabled", "trigger": "operator sequence", "parameter": "sell phase before buy phase", "live_action": "disabled"},
        {"control_id": "manual_batch_release", "institutional_analogue": "single-click batch release", "state": "paper_enabled", "trigger": "operator confirmation", "parameter": "accepted paper orders only", "live_action": "disabled"},
        {"control_id": "time_release", "institutional_analogue": "conditional release", "state": "prepared_not_connected", "trigger": "clock/time window", "parameter": "requires live session clock", "live_action": "disabled"},
        {"control_id": "limit_price_release", "institutional_analogue": "conditional release", "state": "prepared_not_connected", "trigger": "raw RMB quote limit", "parameter": "requires same-session quotes", "live_action": "disabled"},
        {"control_id": "twap_vwap_pov", "institutional_analogue": "EMS child-order algorithms", "state": "paper_schedule_ready", "trigger": "paper child-order schedule", "parameter": f"participation={participation or 'unknown'}; live market/fill feedback required", "live_action": "disabled"},
        {"control_id": "kill_switch", "institutional_analogue": "pre-trade kill switch", "state": "enabled", "trigger": "submission boundary", "parameter": "THS automatic submission false", "live_action": "disabled"},
    ]
    path = out / "execution_controls.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_recovery_plan(out):
    """Define restart and reconciliation actions without inventing broker state."""
    rows = [
        {"stage": "before_prefill", "detected_state": "plan_exists_and_no_prefill", "operator_action": "reload opening pack and latest ledger; keep THS paused", "required_evidence": "plan id and source cutoff", "next_state": "ready_for_manual_prefill"},
        {"stage": "prefill_interrupted", "detected_state": "form_write_or_readback_interrupted", "operator_action": "stop; clear only after visual confirmation; do not retry automatically", "required_evidence": "form readback or explicit cleared-form receipt", "next_state": "manual_review"},
        {"stage": "submission_unknown", "detected_state": "prefill attempted but submission state unknown", "operator_action": "query orders and trades before any new plan or close-unsubmitted action", "required_evidence": "same-session order/trade observation", "next_state": "reconcile_required"},
        {"stage": "partial_fill", "detected_state": "broker order partially filled", "operator_action": "record executions and recompute remaining quantity, cash and sellable shares", "required_evidence": "execution ids, fees, account and positions", "next_state": "reconcile_required"},
        {"stage": "end_of_day", "detected_state": "session complete", "operator_action": "capture account, orders and trades; reconcile before next opening plan", "required_evidence": "cash/positions/order/trade matched observation", "next_state": "closed_or_released"},
    ]
    path = out / "recovery_plan.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_child_order_schedule(out, orders, preflight):
    """Split accepted paper parents into deterministic, non-live time slices."""
    accepted = {_order_key(row) for row in preflight
                if row.get("preflight_status") == "accepted"}
    time_buckets = ("09:35", "10:30", "13:30")
    rows = []
    for parent_sequence, row in enumerate(orders, start=1):
        if _order_key(row) not in accepted:
            continue
        try:
            qty = int(row.get("qty", "0"))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        unit = 100 if row.get("side") == "buy" else 1
        lots, odd_lot = divmod(qty, unit)
        slice_count = min(len(time_buckets), max(1, lots))
        base_lots, remainder_lots = divmod(lots, slice_count)
        remaining = qty
        for child_sequence in range(slice_count):
            child_qty = (base_lots + (1 if child_sequence < remainder_lots else 0)) * unit
            if child_qty <= 0:
                continue
            remaining -= child_qty
            route = route_order(row.get("symbol", ""))
            rows.append({
                "parent_sequence": parent_sequence,
                "child_sequence": child_sequence + 1,
                "strategy_id": row.get("strategy_id", ""),
                "symbol": row.get("symbol", ""),
                "side": row.get("side", ""),
                "qty": child_qty,
                "planned_time": time_buckets[child_sequence],
                "algorithm": "paper_twap_style",
                "price_basis": "operator_refresh_required",
                "route_id": route.route_id,
                "route_channel": route.channel,
                "route_mode": route.mode,
                "route_status": route.status,
                "route_reason": route.reason,
                "live_submission": "disabled",
            })
        if odd_lot:
            route = route_order(row.get("symbol", ""))
            rows.append({
                "parent_sequence": parent_sequence,
                "child_sequence": "manual_review_remainder",
                "strategy_id": row.get("strategy_id", ""),
                "symbol": row.get("symbol", ""),
                "side": row.get("side", ""),
                "qty": odd_lot,
                "planned_time": "manual_review",
                "algorithm": "paper_remainder_review",
                "price_basis": "operator_refresh_required",
                "route_id": route.route_id,
                "route_channel": route.channel,
                "route_mode": route.mode,
                "route_status": route.status,
                "route_reason": route.reason,
                "live_submission": "disabled",
            })
    path = out / "child_order_schedule.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(rows[0]) if rows else [
            "parent_sequence", "child_sequence", "strategy_id", "symbol", "side", "qty",
            "planned_time", "algorithm", "price_basis", "route_id", "route_channel",
            "route_mode", "route_status", "route_reason", "live_submission",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_paper_child_order_projection(out, child_schedule_path, paper_fill_path):
    """Join deterministic paper child slices to the synthetic parent fills.

    This is an offline EMS rehearsal only.  It makes parent/child quantities
    auditable without claiming that any child reached a broker or the market.
    """
    children = read_csv(child_schedule_path)
    fills = {str(row.get("sequence", "")): row for row in read_csv(paper_fill_path)}
    grouped = {}
    for row in children:
        grouped.setdefault(str(row.get("parent_sequence", "")), []).append(row)

    rows = []
    reconciliation = []
    parent_sequences = set(grouped) | set(fills)
    for parent_sequence in sorted(parent_sequences, key=lambda value: int(value) if value.isdigit() else 10**9):
        parent_children = grouped.get(parent_sequence, [])
        parent_fill = fills.get(parent_sequence, {})
        try:
            parent_qty = int(parent_fill.get("qty") or 0)
        except (TypeError, ValueError):
            parent_qty = 0
        parent_status = parent_fill.get("execution_status", "unknown")
        assumed_parent_filled = parent_status == "synthetic_full_fill_reference_price"
        cumulative = 0
        planned_total = 0
        for child in parent_children:
            try:
                planned_qty = int(child.get("qty") or 0)
            except (TypeError, ValueError):
                planned_qty = 0
            planned_total += planned_qty
            synthetic_fill_qty = planned_qty if assumed_parent_filled else 0
            cumulative += synthetic_fill_qty
            rows.append({
                "parent_sequence": parent_sequence,
                "child_sequence": child.get("child_sequence", ""),
                "strategy_id": child.get("strategy_id", ""),
                "symbol": child.get("symbol", ""),
                "side": child.get("side", ""),
                "planned_time": child.get("planned_time", ""),
                "planned_qty": planned_qty,
                "parent_qty": parent_qty,
                "synthetic_fill_qty": synthetic_fill_qty,
                "cumulative_synthetic_fill_qty": cumulative,
                "synthetic_remaining_qty": max(parent_qty - cumulative, 0),
                "parent_execution_status": parent_status,
                "child_state": "synthetic_child_fill_assumed" if assumed_parent_filled else "paper_parent_not_filled",
                "real_broker_state": "false",
                "execution_admitted": "false",
            })
        synthetic_total = cumulative
        if not parent_children and parent_status == "not_executed_preflight_rejected":
            reconciliation_status = "not_scheduled_preflight_rejected"
        elif planned_total != parent_qty:
            reconciliation_status = "fail_parent_child_quantity_mismatch"
        elif assumed_parent_filled and synthetic_total != parent_qty:
            reconciliation_status = "fail_synthetic_fill_residual"
        else:
            reconciliation_status = "pass_schedule_only" if not assumed_parent_filled else "pass_synthetic_complete"
        reconciliation.append({
            "parent_sequence": parent_sequence,
            "strategy_id": parent_fill.get("strategy_id", ""),
            "symbol": parent_fill.get("symbol", ""),
            "side": parent_fill.get("side", ""),
            "parent_qty": parent_qty,
            "child_count": len(parent_children),
            "planned_child_qty": planned_total,
            "synthetic_child_fill_qty": synthetic_total,
            "synthetic_remaining_qty": max(parent_qty - synthetic_total, 0),
            "parent_execution_status": parent_status,
            "reconciliation_status": reconciliation_status,
            "real_broker_state": "false",
            "execution_admitted": "false",
        })

    path = out / "paper_child_order_projection.csv"
    fields = list(rows[0]) if rows else [
        "parent_sequence", "child_sequence", "strategy_id", "symbol", "side", "planned_time",
        "planned_qty", "parent_qty", "synthetic_fill_qty", "cumulative_synthetic_fill_qty",
        "synthetic_remaining_qty", "parent_execution_status", "child_state",
        "real_broker_state", "execution_admitted",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    reconciliation_path = out / "paper_child_order_reconciliation.csv"
    reconciliation_fields = list(reconciliation[0]) if reconciliation else [
        "parent_sequence", "strategy_id", "symbol", "side", "parent_qty", "child_count",
        "planned_child_qty", "synthetic_child_fill_qty", "synthetic_remaining_qty",
        "parent_execution_status", "reconciliation_status", "real_broker_state",
        "execution_admitted",
    ]
    with reconciliation_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=reconciliation_fields)
        writer.writeheader()
        writer.writerows(reconciliation)
    summary = {
        "schema": "paper_child_order_reconciliation_v1",
        "status": "paper_only",
        "parent_count": len(reconciliation),
        "scheduled_parent_count": sum(1 for row in reconciliation if row["child_count"]),
        "complete_synthetic_parent_count": sum(
            1 for row in reconciliation if row["reconciliation_status"] == "pass_synthetic_complete"
        ),
        "rejected_parent_count": sum(
            1 for row in reconciliation if row["reconciliation_status"] == "not_scheduled_preflight_rejected"
        ),
        "mismatch_count": sum(
            1 for row in reconciliation if row["reconciliation_status"].startswith("fail_")
        ),
        "real_broker_state": False,
        "execution_admitted": False,
        "artifact": str(reconciliation_path),
    }
    (out / "paper_child_order_reconciliation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path, reconciliation_path, summary


def _write_operator_action_board(out, function_matrix, release_blockers,
                                 current_snapshot, current_quotes, current_hydration):
    """Turn the public capability map into a small, paper-only operator queue."""
    input_state = {
        "account": current_snapshot is not None,
        "quotes": current_quotes is not None,
        "account_sized_preview": current_hydration is not None,
    }
    actions = {
        "portfolio_rebalance": ("P0", "supply_current_account_observation", "account"),
        "ems_blotter_lifecycle": ("P1", "review_paper_blotter_and_capture_order_trade_observations", "account_sized_preview"),
        "order_lifecycle_recovery": ("P1", "stop_and_reconcile_on_any_unknown_submission_state", "always"),
        "pre_trade_risk_compliance": ("P0", "run_account_sized_paper_risk_checks", "account_sized_preview"),
        "portfolio_risk_exposure": ("P1", "review_saved_historical_risk_views_without_promoting", "always"),
        "basket_execution_schedule": ("P1", "review_sell_first_then_buy_sequence", "always"),
        "order_release_controls": ("P1", "keep_release_manual_until_all_inputs_are_reviewed", "always"),
        "child_order_algorithms": ("P2", "review_paper_child_order_schedule_only", "always"),
        "portfolio_what_if": ("P1", "review_quote_drift_and_scenario_deltas", "account_sized_preview"),
        "broker_routing": ("P0", "keep_ths_manual_only_and_do_not_submit", "always"),
        "broker_statement_ingestion": ("P0", "import_ths_manual_statements_before_reconciliation", "always"),
        "real_time_market_data": ("P0", "run_opening_runner_after_session_probe_time", "quotes"),
        "transaction_cost_analysis": ("P1", "review_quote_drift_then_wait_for_observed_fills", "quotes"),
        "post_trade_reconciliation": ("P0", "capture_account_orders_and_trades_after_manual_activity", "account"),
        "allocation_and_settlement": ("P1", "reconcile_cash_positions_and_t1_before_next_plan", "account"),
        "audit_trail_and_alerts": ("P2", "review_open_paper_release_alerts", "always"),
        "strategy_explainability": ("P0", "review_saved_diagnostic_without_promoting_strategy", "always"),
        "strategy_review_and_promotion": ("P0", "review_capacity_aware_card_without_promoting_strategy", "always"),
        "kill_switch": ("P0", "leave_automatic_submission_disabled", "always"),
    }
    rows = []
    for row in function_matrix:
        function_id = row["function_id"]
        priority, action, prerequisite = actions.get(
            function_id, (row.get("priority", "P2"), "review_function_matrix", "always")
        )
        if prerequisite == "always":
            state = "actionable"
        elif prerequisite == "account":
            state = "ready" if input_state["account"] else "blocked"
        elif prerequisite == "quotes":
            state = "ready" if input_state["quotes"] else "blocked"
        else:
            state = "ready" if input_state["account_sized_preview"] else "blocked"
        rows.append({
            "priority": priority,
            "function_id": function_id,
            "state": state,
            "action": action,
            "prerequisite": prerequisite,
            "blockers": ";".join(release_blockers) if state == "blocked" else "",
            "evidence": row.get("artifact", ""),
            "execution_admitted": "false",
            "live_action": "disabled",
        })
    rows.sort(key=lambda item: (item["priority"], item["state"] != "blocked", item["function_id"]))
    path = out / "operator_action_board.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_offline_operator_decision_queue(
    out, orders, preflight, comparison_path, strategy_sizing_matrix_path,
    paper_lifecycle_path, current_snapshot, current_quotes,
    recovery_handoff_index_path=None,
):
    """Join strategy, ledger, route and lifecycle state into one paper queue.

    The queue is deliberately strategy-level.  It is the desk's offline
    answer to "what can the operator do next?" and never authorizes a broker
    action or infers account state.
    """
    preflight_by_key = {_order_key(row): row for row in preflight}
    comparisons = {
        row.get("strategy_id", ""): row
        for row in read_csv(comparison_path)
    }
    sizing = {
        row.get("strategy_id", ""): row
        for row in read_csv(strategy_sizing_matrix_path)
    }
    lifecycle_rows = read_csv(paper_lifecycle_path)
    lifecycle_by_strategy = {}
    for row in lifecycle_rows:
        lifecycle_by_strategy.setdefault(row.get("strategy_id", ""), []).append(row)
    recovery_by_strategy = {}
    recovery_index = None
    if recovery_handoff_index_path and Path(recovery_handoff_index_path).exists():
        try:
            recovery_index = json.loads(
                Path(recovery_handoff_index_path).read_text(encoding="utf-8-sig")
            )
            recovery_by_strategy = {
                row.get("strategy_id", ""): row
                for row in recovery_index.get("rows", [])
            }
        except (OSError, json.JSONDecodeError):
            recovery_index = None

    strategy_ids = []
    for row in orders:
        strategy_id = row.get("strategy_id", "")
        if strategy_id not in strategy_ids:
            strategy_ids.append(strategy_id)

    rows = []
    for strategy_id in strategy_ids:
        strategy_orders = [row for row in orders if row.get("strategy_id", "") == strategy_id]
        strategy_checks = [preflight_by_key.get(_order_key(row), {}) for row in strategy_orders]
        accepted_checks = [row for row in strategy_checks if row.get("preflight_status") == "accepted"]
        rejected_checks = [row for row in strategy_checks if row.get("preflight_status") != "accepted"]
        accepted_orders = [
            order for order, check in zip(strategy_orders, strategy_checks)
            if check.get("preflight_status") == "accepted"
        ]
        route_decisions = [route_order(order.get("symbol", "")) for order in accepted_orders]
        route_available = sum(route.status == "route_available" for route in route_decisions)
        route_blocked = len(route_decisions) - route_available
        buy_count = sum(row.get("side") == "buy" for row in accepted_orders)
        sell_count = sum(row.get("side") == "sell" for row in accepted_orders)

        lifecycle = lifecycle_by_strategy.get(strategy_id, [])
        recovery = recovery_by_strategy.get(strategy_id, {})
        recovery_template = {}
        recovery_handoff_path = recovery.get("handoff_json")
        if recovery_handoff_path and Path(recovery_handoff_path).exists():
            try:
                recovery_payload = json.loads(
                    Path(recovery_handoff_path).read_text(encoding="utf-8-sig")
                )
                recovery_template = recovery_payload.get("reconciliation_template") or {}
            except (OSError, json.JSONDecodeError):
                recovery_template = {}
        recovery_checks = recovery_template.get("checks") or []
        pending_evidence = sorted({
            str(item)
            for check in recovery_checks
            for item in (check.get("pending_evidence") or [])
            if str(item)
        })
        pending_evidence_count = len(pending_evidence)
        synthetic_fills = {
            (row.get("sequence", ""), row.get("symbol", ""), row.get("side", ""))
            for row in lifecycle
            if row.get("state") == "synthetic_full_fill_assumed"
        }
        settlement_pending = {
            (row.get("sequence", ""), row.get("symbol", ""), row.get("side", ""))
            for row in lifecycle
            if row.get("state") == "settlement_pending"
        }

        reasons = ["candidate_strategies_are_alternatives_do_not_pool"]
        if rejected_checks:
            reasons.append("shared_ledger_preflight_rejected")
        if route_blocked:
            reasons.append("unsupported_or_invalid_route")
        if current_snapshot is None:
            reasons.append("current_account_observation_missing")
        if current_quotes is None:
            reasons.append("same_session_quotes_missing")
        if int(recovery.get("unresolved_scenario_count") or 0) > 0:
            reasons.append("manual_recovery_scenarios_unresolved")
        if not reasons:
            reasons.append("manual_review_and_release_controls_required")

        if rejected_checks:
            decision = "hold_research_only_preflight_rejected"
            action = "repair_shared_ledger_preflight_before_any_release"
        elif route_blocked:
            decision = "hold_research_only_route_unavailable"
            action = "keep unsupported instruments out of THS prefill"
        elif current_snapshot is None or current_quotes is None:
            decision = "hold_paper_only_missing_session_inputs"
            action = "load current account and raw RMB quotes, then recalculate"
        else:
            decision = "manual_review_before_prefill"
            action = "review sell-first sequence, T+1 and human release controls"

        comparison = comparisons.get(strategy_id, {})
        risk = sizing.get(strategy_id, {})
        rows.append({
            "strategy_id": strategy_id,
            "candidate_order_count": len(strategy_orders),
            "accepted_order_count": len(accepted_checks),
            "rejected_order_count": len(rejected_checks),
            "accepted_sell_count": sell_count,
            "accepted_buy_count": buy_count,
            "route_available_count": route_available,
            "route_unavailable_count": route_blocked,
            "synthetic_full_fill_order_count": len(synthetic_fills),
            "settlement_pending_order_count": len(settlement_pending),
            "recovery_unresolved_scenario_count": recovery.get("unresolved_scenario_count", ""),
            "recovery_retry_permitted": str(recovery.get("retry_permitted", False)).lower(),
            "recovery_handoff_status": (
                "manual_reconcile_required"
                if int(recovery.get("unresolved_scenario_count") or 0) > 0
                else "not_available_or_clear"
            ),
            "recovery_pending_evidence_count": pending_evidence_count,
            "recovery_pending_evidence": ";".join(pending_evidence),
            "recovery_handoff_json": recovery.get("handoff_json", ""),
            "risk_status": risk.get("risk_status", "unknown"),
            "strategy_relationship": "alternative_candidate_not_to_be_pooled",
            "portfolio_merge_status": "blocked",
            "comparison_return": comparison.get("net_return", comparison.get("comparison_return", "")),
            "comparison_max_drawdown": comparison.get("max_drawdown", comparison.get("comparison_max_drawdown", "")),
            "comparison_sharpe": comparison.get("sharpe_zero_rf_252", comparison.get("comparison_sharpe", "")),
            "account_observed": str(current_snapshot is not None).lower(),
            "quotes_observed": str(current_quotes is not None).lower(),
            "decision": decision,
            "reason": ";".join(reasons),
            "next_operator_action": action,
            "execution_admitted": "false",
        })

    csv_path = out / "offline_operator_decision_queue.csv"
    fields = list(rows[0]) if rows else ["strategy_id", "decision", "execution_admitted"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    decision_counts = {}
    for row in rows:
        decision_counts[row["decision"]] = decision_counts.get(row["decision"], 0) + 1
    payload = {
        "schema": "offline_operator_decision_queue_v1",
        "status": "paper_only_strategy_decision_queue",
        "strategy_count": len(rows),
        "decision_counts": decision_counts,
        "portfolio_policy": "candidate_strategies_are_alternatives_and_must_not_be_merged_into_the_single_account_ledger",
        "recovery_policy": "unresolved_paper_exceptions_require_manual_reconciliation_and_never_permit_automatic_retry",
        "recovery_handoff_index": str(recovery_handoff_index_path) if recovery_index else None,
        "recovery_evidence_policy": "pending evidence is descriptive only; no broker state is inferred and no retry is authorized",
        "account_observed": current_snapshot is not None,
        "quotes_observed": current_quotes is not None,
        "execution_admitted": False,
        "source_artifacts": {
            "strategy_comparison": str(comparison_path),
            "strategy_account_sizing_matrix": str(strategy_sizing_matrix_path),
            "paper_order_lifecycle": str(paper_lifecycle_path),
        },
        "rows": rows,
    }
    json_path = out / "offline_operator_decision_queue.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path, payload


def _write_schedule(out, orders, preflight):
    """Create a paper-only operator sequence for the next session."""
    accepted = {_order_key(row) for row in preflight
                if row.get("preflight_status") == "accepted"}
    rows = []
    sequence = 0
    for phase, side in (("sell_release_cash", "sell"), ("buy_deploy_cash", "buy")):
        for row in orders:
            if row.get("side") != side:
                continue
            sequence += 1
            key = _order_key(row)
            status = "paper_ready" if key in accepted else "manual_review"
            rows.append({
                "sequence": sequence,
                "phase": phase,
                "strategy_id": row.get("strategy_id", ""),
                "symbol": row.get("symbol", ""),
                "side": side,
                "qty": row.get("qty", ""),
                "reference_price_rmb": row.get("price_rmb", ""),
                "status": status,
                "submission_mode": "manual_ths_prefill_only",
                "live_submission": "disabled",
                "operator_note": "refresh same-session quote and account before use",
            })
    path = out / "execution_schedule.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [
            "sequence", "phase", "strategy_id", "symbol", "side", "qty",
            "reference_price_rmb", "status", "submission_mode", "live_submission", "operator_note"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def _load_current_inputs(account_observation, account, quotes, quotes_csv, quote_source,
                         quote_observed_at, quote_trading_date, trading_date):
    if account_observation is not None and account is not None:
        raise ValueError("Use exactly one of --account-observation and --account")
    supplied_quotes = sum(value is not None for value in (quotes, quotes_csv))
    has_account = account_observation is not None or account is not None
    if has_account and supplied_quotes != 1:
        raise ValueError("Current paper refresh requires exactly one quote source")
    if not has_account and supplied_quotes == 0:
        return None, None

    snapshot = None
    if has_account:
        if account is not None:
            from ..ths import journal
            journal.initialize()
            snapshot = journal.latest(account, "account", trading_date=trading_date)
        else:
            snapshot = json.loads(Path(account_observation).resolve().read_text(encoding="utf-8-sig"))
        if snapshot.get("schema") != "ths_account_observation_v1":
            raise ValueError("--account-observation must be a normalized current account observation")
        if snapshot.get("trading_date") != trading_date:
            raise ValueError(
                f"Account observation trading_date {snapshot.get('trading_date')!r} "
                f"does not match opening pack execution day {trading_date!r}"
            )

    quote_observation = None
    if quotes:
        quote_observation = json.loads(Path(quotes).resolve().read_text(encoding="utf-8-sig"))
    elif quotes_csv:
        if not all((quote_source, quote_observed_at, quote_trading_date)):
            raise ValueError("--quotes-csv requires --quote-source, --quote-observed-at and --quote-trading-date")
        from ..ths.quotes import build_quote_observation
        quote_observation = build_quote_observation(
            quotes_csv,
            source=quote_source,
            observed_at=quote_observed_at,
            trading_date=quote_trading_date,
        )
    if quote_observation is not None and quote_observation.get("trading_date") != trading_date:
        raise ValueError(
            f"Quote observation trading_date {quote_observation.get('trading_date')!r} "
            f"does not match opening pack execution day {trading_date!r}"
        )
    return snapshot, quote_observation


def _quote_coverage(orders, quote_observation):
    """Measure quote readiness against every planned order symbol.

    A supplied quote file is not sufficient evidence for account sizing: every
    planned symbol needs a positive raw-RMB price.  Keep partial observations
    available for drift diagnostics, but do not let them clear the readiness
    flag or hydrate a broker-facing preview.
    """
    required = sorted({
        str(row.get("symbol", "")).upper()
        for row in orders
        if str(row.get("symbol", "")).strip()
    })
    prices = (quote_observation or {}).get("prices") or {}
    observed = []
    invalid = []
    for symbol in required:
        quote = prices.get(symbol) or {}
        try:
            price = Decimal(str(quote.get("price_rmb", "")))
        except (InvalidOperation, TypeError, ValueError):
            price = Decimal("0")
        if price > 0:
            observed.append(symbol)
        else:
            invalid.append(symbol)
    return {
        "supplied": quote_observation is not None,
        "complete": bool(required) and not invalid,
        "required_count": len(required),
        "observed_count": len(observed),
        "missing_or_invalid_count": len(invalid),
        "missing_or_invalid_symbols": invalid,
        "basis": (quote_observation or {}).get("price_basis") if quote_observation else None,
        "source": (quote_observation or {}).get("source") if quote_observation else None,
        "trading_date": (quote_observation or {}).get("trading_date") if quote_observation else None,
        "observed_at": (quote_observation or {}).get("observed_at") if quote_observation else None,
        "research_only": True,
        "execution_admitted": False,
    }


def _latest_loss_attribution(strategy_id: str | None) -> dict | None:
    """Load the latest saved-ledger diagnostic for a strategy, if one exists."""
    if not strategy_id:
        return None
    root = ROOT / "outputs" / "loss_attribution"
    if not root.exists():
        return None
    for candidate in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if not candidate.is_dir():
            continue
        run_path = candidate / "run.json"
        if not run_path.exists():
            continue
        try:
            report = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for case in report.get("cases", []):
            if case.get("case_id", "").rsplit(":", 1)[-1] != strategy_id:
                continue
            costs = case.get("costs") or {}
            slippage = case.get("raw_open_slippage") or {}
            diagnostic = {
                "schema": "strategy_explainability_v1",
                "status": "saved_account_diagnostic",
                "strategy_id": strategy_id,
                "case_id": case.get("case_id"),
                "source_report": str(candidate / "REPORT.md"),
                "source_run": str(run_path),
                "net_pnl_cny": case.get("net_pnl_cny"),
                "fees_cny": costs.get("fees_cny"),
                "minimum_commission_premium_cny": costs.get("minimum_commission_premium_cny"),
                "open_slippage_cny": slippage.get("slippage_cny"),
                "top_loss_symbols": case.get("top_loss_symbols", []),
                "top_gain_symbols": case.get("top_gain_symbols", []),
                "execution_admitted": False,
                "interpretation": "Saved-account diagnostic only; not current-session evidence, causal alpha attribution, or live approval.",
            }
            period_evidence = _strategy_period_evidence(strategy_id)
            if period_evidence:
                diagnostic["period_evidence"] = period_evidence
            return diagnostic
    return None


def _strategy_period_evidence(strategy_id: str | None) -> dict | None:
    """Summarize already-run fixed strategy windows without selecting on returns."""
    if not strategy_id:
        return None
    root = ROOT / "outputs" / "market_replay"
    if not root.exists():
        return None
    selected = {}
    for candidate in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if not candidate.is_dir():
            continue
        run_path = candidate / "run.json"
        if not run_path.exists():
            continue
        try:
            report = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("research_family") != "low_risk_stock_selection":
            continue
        for result in report.get("results", []):
            if (result.get("signal") != strategy_id or result.get("status") != "completed"
                    or not report.get("start") or not report.get("end")):
                continue
            key = (str(report["start"]), str(report["end"]))
            if key in selected:
                continue
            selected[key] = {
                "start": key[0],
                "end": key[1],
                "sessions": result.get("completed_sessions"),
                "net_return": result.get("net_return"),
                "max_drawdown": result.get("max_drawdown"),
                "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
                "fees_cny": result.get("fees"),
                "source_run": str(run_path),
            }
    periods = sorted(selected.values(), key=lambda row: (row["start"], row["end"]))
    # A carried multi-year account overlaps its component diagnostics. Keep the
    # shorter fixed windows in the desk evidence so overlap is not counted as
    # an additional independent regime observation.
    periods = [
        row for row in periods
        if not any(
            other is not row
            and row["start"] <= other["start"]
            and row["end"] >= other["end"]
            and (row["start"] < other["start"] or row["end"] > other["end"])
            for other in periods
        )
    ]
    if not periods:
        return None
    positive = sum((row.get("net_return") or 0) > 0 for row in periods)
    return {
        "schema": "strategy_period_evidence_v1",
        "status": "descriptive_fixed_windows",
        "strategy_id": strategy_id,
        "periods": periods,
        "positive_window_count": positive,
        "window_count": len(periods),
        "promotion_status": "mixed_window_evidence_no_automatic_promotion" if positive < len(periods) else "descriptive_only_no_automatic_promotion",
        "interpretation": "Already-inspected windows share fixed rules and costs but are not an untouched holdout or live-readiness proof.",
    }


def _latest_saved_robustness(source_cutoff: str | None) -> Path | None:
    """Find the newest unadmitted saved-mark diagnostic for this frozen cutoff."""
    if not source_cutoff:
        return None
    root = ROOT / "outputs" / "research_robustness"
    if not root.exists():
        return None
    for candidate in sorted(root.rglob("robustness.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (payload.get("status") == "saved_mark_robustness_diagnostic"
                and str(payload.get("end")) == str(source_cutoff)
                and payload.get("fresh_holdout_claimed") is False
                and payload.get("execution_admitted") is False):
            return candidate.resolve()
    return None


def build(opening_pack: str | Path, destination: str | Path | None = None,
          *, account_observation: str | Path | None = None,
          account: str | None = None,
          quotes: str | Path | None = None,
          quotes_csv: str | Path | None = None,
          quote_source: str | None = None,
          quote_observed_at: str | None = None,
          quote_trading_date: str | None = None,
          strategy_id: str | None = None,
          selected_strategy_id: str | None = None,
          sizing: str = "replay_shares",
           execution_scope: str = "strict",
           stockdb_forward_compare: str | Path | None = None,
           stockdb_ledger_replay: str | Path | None = None,
           ths_readonly_evidence_index: str | Path | None = None) -> tuple[Path, dict]:
    pack_path = Path(opening_pack).resolve()
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    if pack.get("status") != "paper_candidates_only" or pack.get("execution_admitted"):
        raise ValueError("Paper-only opening pack required")
    strategies = pack.get("strategies") or [{}]
    strategy = strategies[0]
    if selected_strategy_id:
        selected = next(
            (candidate for candidate in strategies
             if candidate.get("strategy_id") == selected_strategy_id),
            None,
        )
        if selected is None:
            raise ValueError(
                f"Unknown selected strategy: {selected_strategy_id!r}"
            )
        strategy = selected
    artifacts = pack.get("artifacts") or {}
    order_path = Path(artifacts["candidate_orders"]).resolve()
    preflight_artifact = artifacts.get("ledger_preflight") or artifacts.get("ledger_preflight_by_strategy")
    if not preflight_artifact:
        raise ValueError("Opening pack must contain a ledger preflight artifact")
    preflight_path = Path(preflight_artifact).resolve()
    orders = read_csv(order_path)
    preflight = read_csv(preflight_path)
    if selected_strategy_id:
        orders = [
            row for row in orders
            if row.get("strategy_id", "") == selected_strategy_id
        ]
        preflight = [
            row for row in preflight
            if row.get("strategy_id", "") == selected_strategy_id
        ]
    saved_no_trade_holdings = None
    if not orders or not preflight:
        if orders or preflight:
            raise ValueError("Opening pack must contain candidate orders and ledger preflight")
        applicable = [strategy] if selected_strategy_id else strategies
        saved_no_trade_holdings = _saved_no_trade_holdings(pack, applicable)
        if (not selected_strategy_id and any(pack.get(key) != 0 for key in (
                "candidate_order_count", "order_union_count", "order_consensus_count"))):
            raise ValueError("Empty opening pack contradicts its aggregate order counts")
    config = strategy.get("execution_config") or pack.get("ledger_preflight", {}).get("configuration") or {}
    commission_rate = Decimal(str(config.get("commission", "0")))
    minimum_commission = Decimal(str(config.get("minimum_commission", "0")))
    sell_tax_rate = Decimal(str(config.get("sell_tax", "0")))
    transfer_rate = Decimal(str(config.get("transfer_fee", "0")))
    slippage_rate = Decimal(str(config.get("slippage_bps", "0"))) / Decimal("10000")

    by_side = {side: [row for row in orders if row.get("side") == side] for side in ("buy", "sell")}
    notional = {side: sum(Decimal(row["estimated_notional_rmb"]) for row in rows)
                for side, rows in by_side.items()}
    commission = sum((max(value * commission_rate, minimum_commission) if value else Decimal("0"))
                     for value in notional.values())
    sell_tax = notional["sell"] * sell_tax_rate
    transfer_fee = sum(notional.values()) * transfer_rate
    slippage = sum(notional.values()) * slippage_rate
    estimated_cost = commission + sell_tax + transfer_fee + slippage
    accepted = [row for row in preflight if row.get("preflight_status") == "accepted"]
    rejected = [row for row in preflight if row.get("preflight_status") != "accepted"]
    if selected_strategy_id:
        staged = {
            "status": "paper_staged_only",
            "reduce_first_count": strategy.get("ledger_preflight_reduce_first", 0),
            "deferred_buy_count": strategy.get("ledger_preflight_deferred_buys", 0),
        }
    else:
        staged = pack.get("ledger_preflight", {}).get("staged_execution", {})
    if not staged:
        staged_by_strategy = pack.get("ledger_preflight_by_strategy") or {}
        staged = {
            "status": "paper_staged_only",
            "reduce_first_count": sum(
                (row.get("staged_execution") or {}).get("reduce_first_count", 0)
                for row in staged_by_strategy.values()
            ),
            "deferred_buy_count": sum(
                (row.get("staged_execution") or {}).get("deferred_buy_count", 0)
                for row in staged_by_strategy.values()
            ),
        }
    snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(destination).resolve() if destination else ROOT / "outputs" / "trader_desk_snapshots" / snapshot_id
    out.mkdir(parents=True, exist_ok=False)
    forward_compare_path = Path(stockdb_forward_compare).resolve() if stockdb_forward_compare else None
    forward_compare_payload, _ = _load_stockdb_forward_compare(forward_compare_path)
    ledger_replay_path = Path(stockdb_ledger_replay).resolve() if stockdb_ledger_replay else None
    ledger_replay_payload, _ = _load_stockdb_ledger_replay(ledger_replay_path)
    ths_readonly_payload, ths_readonly_path = _load_ths_readonly_evidence(
        ths_readonly_evidence_index
    )
    comparison_path = _write_strategy_comparison(out, pack, forward_compare_path)
    stockdb_forward_comparison_path = _write_stockdb_forward_comparison(
        out,
        forward_compare_payload,
        forward_compare_path,
        ledger_replay_payload,
        ledger_replay_path,
    )
    strategy_promotion_card_path = out / "strategy_promotion_card.csv"
    strategy_evidence_card_path = _write_strategy_evidence_card(
        out,
        pack,
        pack_path,
        comparison_path,
        strategy_promotion_card_path,
        forward_compare_path,
        ledger_replay_path,
    )
    current_snapshot, current_quotes = _load_current_inputs(
        account_observation, account, quotes, quotes_csv, quote_source,
        quote_observed_at, quote_trading_date, pack.get("execution_day"),
    )
    quote_coverage = _quote_coverage(orders, current_quotes)
    quotes_ready = quote_coverage["complete"]
    readiness_quotes = current_quotes if quotes_ready else None
    instrument_master_run_path, instrument_master_manifest, instrument_master = (
        _matching_instrument_master(pack.get("source_cutoff"))
    )
    instrument_master_coverage_path, instrument_master_coverage = (
        _latest_instrument_master_coverage(pack.get("source_cutoff"))
    )
    cutoff_master = instrument_master
    identity_master = instrument_master
    identity_source = "cutoff_matched_instrument_master"
    identity_master_is_prior = False
    if identity_master is None:
        prior_run_path, prior_manifest, prior_master = (
            _latest_prior_instrument_master(pack.get("source_cutoff"))
        )
        if prior_master is not None:
            instrument_master_run_path = prior_run_path
            instrument_master_manifest = prior_manifest
            identity_master = prior_master
            identity_source = "latest_prior_instrument_master"
            identity_master_is_prior = True
    if identity_master is None:
        identity_master = {}
        for row in orders:
            symbol = _qualified_order_identity(row.get("symbol"))
            if symbol:
                identity_master[symbol] = {
                    "symbol": symbol,
                    "identity_basis": "qualified_order_symbol",
                    "observed_status": None,
                    "pit_status": None,
                }
        identity_source = "qualified_order_symbol_fallback"
    master_identity_gaps = sorted({
        str(row.get("symbol", "")).upper()
        for row in orders
        if str(row.get("symbol", "")).upper() not in identity_master
    })
    instrument_master_summary = {
        "status": (
            "cutoff_matched" if cutoff_master is not None
            else "latest_prior_available" if identity_master_is_prior
            else "missing_cutoff_match"
        ),
        "run": str(instrument_master_run_path) if instrument_master_run_path else None,
        "master": (instrument_master_manifest or {}).get("master_path"),
        "observed_as_of": (instrument_master_manifest or {}).get("observed_as_of"),
        "instrument_count": (instrument_master_manifest or {}).get("instrument_count", 0),
        "order_identity_source": identity_source,
        "cutoff_matched": cutoff_master is not None,
        "identity_master_is_prior": identity_master_is_prior,
        "order_identity_gap_count": len(master_identity_gaps),
        "order_identity_gaps": master_identity_gaps,
        "pit_qualified": False,
        "execution_admitted": False,
        "basis": (
            "instrument_master_v1; latest verified prior-to-cutoff metadata used for identity only; "
            "not PIT or broker authority"
            if identity_master_is_prior else
            "instrument_master_v1; retrieval-time research metadata, not PIT or broker authority"
        ),
    }
    instrument_master_coverage_summary = {
        "status": (instrument_master_coverage or {}).get("status", "missing_cutoff_match"),
        "path": str(instrument_master_coverage_path) if instrument_master_coverage_path else None,
        "source_raw_count": (instrument_master_coverage or {}).get("source_raw_count", 0),
        "source_after_alias_count": (instrument_master_coverage or {}).get("source_after_alias_count", 0),
        "master_count": (instrument_master_coverage or {}).get("master_count", 0),
        "common_count": (instrument_master_coverage or {}).get("common_count", 0),
        "source_only": (instrument_master_coverage or {}).get("source_only", []),
        "master_only": (instrument_master_coverage or {}).get("master_only", []),
        "research_only": True,
        "execution_admitted": False,
        "interpretation": (
            "Scope diagnostic only; source-only CDRs or provider exclusions do not authorize orders. "
            "A prior verified master may support identity-only routing, but exact cutoff/PIT evidence "
            "remains required for release."
        ),
    }
    strategy_explainability = _latest_loss_attribution(strategy.get("strategy_id"))
    strategy_explainability_path = out / "strategy_explainability.json"
    strategy_explainability_path.write_text(
        json.dumps(strategy_explainability or {
            "schema": "strategy_explainability_v1",
            "status": "unavailable_saved_diagnostic",
            "strategy_id": strategy.get("strategy_id"),
            "execution_admitted": False,
            "interpretation": "No saved-ledger diagnostic found; current-session evidence is still required.",
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    current_hydration = None
    if orders and current_snapshot is not None and quotes_ready:
        from ..ths.handoff import prepare_opening_pack
        current_hydration = prepare_opening_pack(
            pack,
            current_snapshot,
            current_quotes,
            strategy_id=strategy_id,
            sizing_mode=sizing,
            execution_scope=execution_scope,
        )
        (out / "current_preview.json").write_text(
            json.dumps(current_hydration, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        adjustments = []
        for candidate in current_hydration.get("candidates", []):
            plan_result = candidate.get("plan") or {}
            next_plan = plan_result.get("next_plan") or {}
            for item in plan_result.get("adjustments", []):
                adjustments.append({
                    "strategy_id": candidate.get("strategy_id", ""),
                    "symbol": item.get("symbol", ""),
                    "held_qty": item.get("held_qty", ""),
                    "target_qty": item.get("target_qty", ""),
                    "delta_qty": item.get("delta_qty", ""),
                    "side": item.get("side") or "",
                    "status": item.get("status", ""),
                    "validation_reason": item.get("validation_reason", ""),
                    "next_plan": "true" if next_plan.get("symbol") == item.get("symbol") else "false",
                    "next_plan_qty": next_plan.get("qty", "") if next_plan.get("symbol") == item.get("symbol") else "",
                    "next_plan_price_rmb": (
                        f"{Decimal(next_plan['price_cents']) / Decimal('100'):.2f}"
                        if next_plan.get("symbol") == item.get("symbol") and next_plan.get("price_cents") is not None
                        else ""
                    ),
                })
        if adjustments:
            with (out / "current_adjustments.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(adjustments[0]))
                writer.writeheader()
                writer.writerows(adjustments)

    release_blockers = []
    if current_snapshot is None:
        release_blockers.append("current_user_account_observation_missing")
    if current_quotes is None:
        release_blockers.append("same_session_raw_rmb_quotes_missing")
    elif not quotes_ready:
        release_blockers.append("same_session_raw_rmb_quotes_incomplete")
    if current_hydration is None:
        release_blockers.append("account_sized_preview_not_hydrated")
    if cutoff_master is None:
        release_blockers.append("cutoff_matched_instrument_master_missing")
    elif master_identity_gaps:
        release_blockers.append("instrument_master_identity_review_required")
    release_blockers.extend(["sellable_quantity_and_t1_status_required", "manual_user_review_required"])
    exception_alerts_path = _write_exception_alerts(
        out,
        release_blockers,
        account_observed=current_snapshot is not None,
        quotes_observed=quotes_ready,
        preview_hydrated=current_hydration is not None,
    )

    risk_checks = _risk_checks(
        orders, preflight, current_snapshot, readiness_quotes, execution_scope,
        instrument_master=identity_master,
    )
    risk_checks_path = out / "risk_checks.csv"
    with risk_checks_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(risk_checks[0]) if risk_checks else [
            "strategy_id", "symbol", "side", "qty", "check", "status", "reason", "basis"])
        writer.writeheader()
        writer.writerows(risk_checks)
    risk_summary_path = _write_static_risk_summary(
        out, orders, preflight, config, current_snapshot, readiness_quotes, execution_scope
    )
    schedule_path = _write_schedule(out, orders, preflight)
    execution_controls_path = _write_execution_controls(out, config)
    recovery_plan_path = _write_recovery_plan(out)
    child_order_schedule_path = _write_child_order_schedule(out, orders, preflight)
    quote_drift_path = None
    quote_drift_summary = None
    if current_quotes is not None:
        quote_drift_path, quote_drift_summary = _write_quote_drift(out, orders, current_quotes)
    scenario_path = _write_pretrade_scenarios(out, orders, config)
    allocation_path = _write_allocation_projection(out, orders, preflight)
    paper_fill_path, paper_ledger_path, paper_lifecycle_path, paper_position_path, paper_reconciliation_path, paper_fill_summary = _write_paper_fill_projection(
        out, orders, preflight, config, pack.get("execution_day")
    )
    from .offline_strategy_plan import (
        _write_paper_account_sizing_projection,
        _write_strategy_account_sizing_matrix,
    )
    account_sizing_path, account_sizing_json_path, account_sizing_summary = (
        _write_paper_account_sizing_projection(out, orders, config, paper_fill_path)
    )
    strategy_sizing_matrix_path, strategy_sizing_matrix_json_path = (
        _write_strategy_account_sizing_matrix(
            out, pack.get("strategies") or [], orders, preflight, pack.get("execution_day")
        )
    )
    offline_portfolio_risk_path = (
        DATA_ROOT / "research" / "strategy_selection" /
        str(pack.get("execution_day")) / "offline_portfolio_risk_summary_v1.json"
    )
    offline_portfolio_risk = None
    if offline_portfolio_risk_path.is_file():
        try:
            offline_portfolio_risk = json.loads(
                offline_portfolio_risk_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            offline_portfolio_risk = None
    robustness_path = _latest_saved_robustness(pack.get("source_cutoff"))
    strategy_board_csv_path, strategy_board_json_path, strategy_board = build_strategy_board(
        comparison_path, strategy_evidence_card_path, out, strategy_sizing_matrix_path,
        robustness_path=robustness_path,
        portfolio_risk_path=offline_portfolio_risk_path if offline_portfolio_risk else None,
    )
    strategy_promotion_card_path = _write_strategy_promotion_card(
        out, comparison_path, strategy_sizing_matrix_path
    )
    from .strategy_promotion_review import build as build_strategy_promotion_review
    strategy_promotion_review_path, strategy_promotion_review_csv_path, strategy_promotion_review = (
        build_strategy_promotion_review(
            comparison_path,
            strategy_sizing_matrix_path,
            robustness_path,
            out,
            portfolio_risk_path=offline_portfolio_risk_path if offline_portfolio_risk else None,
            evidence_path=strategy_evidence_card_path,
        )
    )
    paper_child_projection_path, paper_child_reconciliation_path, paper_child_reconciliation_summary = _write_paper_child_order_projection(
        out, child_order_schedule_path, paper_fill_path
    )
    target_order_reconciliation_path = _write_target_order_reconciliation(
        out, artifacts.get("candidate_targets"), orders,
        strategy_id=selected_strategy_id,
    )
    paper_release_decision_path = _write_paper_release_decision(
        out, orders, preflight, target_order_reconciliation_path,
        account_observed=current_snapshot is not None,
        quotes_observed=quotes_ready,
    )
    recovery_handoff_index_path = (
        DATA_ROOT / "research" / "strategy_selection" /
        str(pack.get("execution_day")) / "paper_recovery_handoff_index.json"
    )
    paper_audit_event_log_path, paper_audit_event_log_jsonl_path = _write_paper_audit_event_log(
        out,
        pack_path=pack_path,
        pack=pack,
        strategy_id=selected_strategy_id or strategy.get("strategy_id"),
        orders=orders,
        preflight=preflight,
        current_snapshot=current_snapshot,
        quotes_ready=quotes_ready,
        current_hydration=current_hydration,
        release_blockers=release_blockers,
        strategy_evidence_card_path=strategy_evidence_card_path,
        paper_lifecycle_path=paper_lifecycle_path,
        paper_reconciliation_path=paper_reconciliation_path,
        paper_release_decision_path=paper_release_decision_path,
        recovery_handoff_index_path=recovery_handoff_index_path,
    )
    offline_operator_decision_queue_path, offline_operator_decision_queue_json_path, offline_operator_decision_queue = (
        _write_offline_operator_decision_queue(
            out, orders, preflight, comparison_path, strategy_sizing_matrix_path,
            paper_lifecycle_path, current_snapshot, readiness_quotes,
            recovery_handoff_index_path,
        )
    )
    paper_next_session_carry_path = _write_paper_next_session_carry(
        out, paper_fill_path, paper_position_path, paper_ledger_path,
        pack.get("execution_day"),
    )
    function_matrix = _function_matrix(
        current_snapshot, readiness_quotes, risk_checks_path, schedule_path,
        strategy_explainability_path, exception_alerts_path, quote_drift_path, scenario_path, allocation_path,
        out / "paper_execution_cost_stress_v1.csv",
        strategy_evidence_card_path,
        risk_summary_path, comparison_path, paper_fill_path, paper_lifecycle_path, paper_reconciliation_path,
        paper_child_projection_path, account_sizing_path, strategy_promotion_card_path,
        strategy_promotion_review_path,
        recovery_handoff_path=recovery_handoff_index_path,
        factor_current_path=_latest_factor_current_score(pack.get("source_cutoff"))[0],
        factor_preview_path=_latest_factor_paper_preview(pack.get("source_cutoff"))[0],
        instrument_master_path=(Path((instrument_master_manifest or {}).get("master_path"))
                                if (instrument_master_manifest or {}).get("master_path") else None),
        instrument_master_coverage_path=instrument_master_coverage_path,
        audit_event_log_path=paper_audit_event_log_path,
    )
    function_matrix_path = out / "function_matrix.csv"
    with function_matrix_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(function_matrix[0]))
        writer.writeheader()
        writer.writerows(function_matrix)
    operator_action_board_path = _write_operator_action_board(
        out, function_matrix, release_blockers,
        current_snapshot, readiness_quotes, current_hydration,
    )

    blotter = []
    for sequence, row in enumerate(orders, start=1):
        match = next((item for item in preflight if _order_key(item) == _order_key(row)), {})
        route = route_order(row.get("symbol", ""))
        route_status = route.status if match.get("preflight_status") == "accepted" else "not_routable_preflight_rejected"
        blotter.append({
            "sequence": sequence,
            "strategy_id": row.get("strategy_id", ""),
            "symbol": row.get("symbol", ""),
            "side": row.get("side", ""),
            "qty": row.get("qty", ""),
            "limit_price_rmb": row.get("price_rmb", ""),
            "estimated_notional_rmb": row.get("estimated_notional_rmb", ""),
            "preflight_status": match.get("preflight_status", "unknown"),
            "preflight_reason": match.get("preflight_reason", ""),
            "route_id": route.route_id,
            "route_channel": route.channel,
            "route_mode": route.mode,
            "route_status": route_status,
            "route_reason": route.reason,
            "lifecycle": "paper_staged_only",
            "execution_admitted": "false",
        })
    with (out / "blotter.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(blotter[0]) if blotter else [
            "sequence", "strategy_id", "symbol", "side", "qty", "limit_price_rmb",
            "estimated_notional_rmb", "preflight_status", "preflight_reason", "route_id",
            "route_channel", "route_mode", "route_status", "route_reason", "lifecycle", "execution_admitted"])
        writer.writeheader()
        writer.writerows(blotter)

    tca = [
        {"metric": "gross_buy_notional_rmb", "value": str(money(notional["buy"])), "basis": "paper candidate raw RMB close"},
        {"metric": "gross_sell_notional_rmb", "value": str(money(notional["sell"])), "basis": "paper candidate raw RMB close"},
        {"metric": "commission_rmb", "value": str(money(commission)), "basis": "configured commission and minimum commission"},
        {"metric": "sell_tax_rmb", "value": str(money(sell_tax)), "basis": "configured sell tax"},
        {"metric": "transfer_fee_rmb", "value": str(money(transfer_fee)), "basis": "configured transfer fee"},
        {"metric": "slippage_rmb", "value": str(money(slippage)), "basis": "configured slippage_bps"},
        {"metric": "estimated_all_in_cost_rmb", "value": str(money(estimated_cost)), "basis": "pre-trade estimate; no fill observed"},
    ]
    if quote_drift_summary is not None:
        tca.extend([
            {"metric": "same_session_quote_coverage", "value": f"{quote_drift_summary['coverage_count']}/{quote_drift_summary['requested_count']}", "basis": "raw RMB quote observation; no fill observed"},
            {"metric": "adverse_quote_drift_rmb", "value": str(quote_drift_summary["adverse_drift_rmb"]), "basis": "buy positive / sell negative movement against research reference"},
            {"metric": "adverse_quote_drift_bps", "value": str(quote_drift_summary["adverse_drift_bps"]), "basis": "reference-notional weighted quote drift; not execution cost"},
        ])
    with (out / "tca_pretrade.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(tca[0]))
        writer.writeheader()
        writer.writerows(tca)

    from .paper_execution_attribution import build as build_paper_execution_attribution
    paper_execution_attribution_path, paper_execution_attribution_csv_path, paper_child_pressure_path, paper_execution_stress_path, paper_execution_attribution = (
        build_paper_execution_attribution(
            paper_child_projection_path,
            paper_fill_path,
            out / "tca_pretrade.csv",
            scenario_path,
            out,
        )
    )

    gaps = [
        {
            "institutional_function": row["function_id"].replace("_", " "),
            "institutional_capability": row["institutional_capability"],
            "state": row["local_state"],
            "tomorrow_use": row["tomorrow_use"],
            "priority": row["priority"],
            "evidence": f"{row['artifact']}; next gap: {row['next_gap']}; next: {row['next_implementation']}",
        }
        for row in function_matrix
    ]
    preview_candidates = (current_hydration or {}).get("candidates", [])
    current_preview = {
        "status": (current_hydration or {}).get("status", "not_requested"),
        "ready_count": (current_hydration or {}).get("ready_count", 0),
        "candidate_count": (current_hydration or {}).get("candidate_count", 0),
        "trading_date": (current_hydration or {}).get("trading_date"),
        "quote_observed_at": (current_hydration or {}).get("quote_observed_at"),
        "strategy_statuses": [
            {"strategy_id": row.get("strategy_id"), "status": row.get("status"),
             "execution_admitted": row.get("execution_admitted", False)}
            for row in preview_candidates
        ],
    }
    next_plans = []
    for candidate in preview_candidates:
        next_plan = ((candidate.get("plan") or {}).get("next_plan") or {})
        if next_plan:
            next_plans.append({
                "strategy_id": candidate.get("strategy_id"),
                "symbol": next_plan.get("symbol"),
                "side": next_plan.get("side"),
                "qty": next_plan.get("qty"),
                "price_rmb": (
                    f"{Decimal(next_plan['price_cents']) / Decimal('100'):.2f}"
                    if next_plan.get("price_cents") is not None else None
                ),
                "status": next_plan.get("status"),
                "submission": next_plan.get("submission"),
            })
    current_preview["next_plans"] = next_plans
    release_blockers = []
    if current_snapshot is None:
        release_blockers.append("current_user_account_observation_missing")
    if current_quotes is None:
        release_blockers.append("same_session_raw_rmb_quotes_missing")
    elif not quotes_ready:
        release_blockers.append("same_session_raw_rmb_quotes_incomplete")
    if current_hydration is None:
        release_blockers.append("account_sized_preview_not_hydrated")
    if cutoff_master is None:
        release_blockers.append("cutoff_matched_instrument_master_missing")
    elif master_identity_gaps:
        release_blockers.append("instrument_master_identity_review_required")
    release_blockers.extend(["sellable_quantity_and_t1_status_required", "manual_user_review_required"])
    release_state = "blocked_missing_live_inputs" if current_hydration is None else "blocked_manual_review"
    current_preview["release_control"] = {
        "state": release_state,
        "execution_release_allowed": False,
        "blockers": release_blockers,
    }
    snapshot_strategy_id = (
        selected_strategy_id
        or pack.get("selected_strategy_id")
        or ("multi_strategy_candidates" if pack.get("strategy_count", 1) > 1 else strategy.get("strategy_id"))
    )
    snapshot = {
        "schema": "trader_desk_snapshot_v1",
        "status": "paper_desk_snapshot",
        "generated_at": datetime.now().astimezone().isoformat(),
        "opening_pack": str(pack_path),
        "source_cutoff": pack.get("source_cutoff"),
        "execution_day": pack.get("execution_day"),
        "strategy_id": snapshot_strategy_id,
        "strategy_selection": {
            "mode": "explicit_paper_candidate" if selected_strategy_id else "multi_candidate_comparison",
            "selected_strategy_id": selected_strategy_id,
            "automatic_selection": False,
            "execution_admitted": False,
        },
        "execution_scope": strategy.get("execution_scope"),
        "orders": {"total": len(orders), "buy": len(by_side["buy"]), "sell": len(by_side["sell"]),
                    "accepted": len(accepted), "rejected": len(rejected),
                    "reduce_first_count": staged.get("reduce_first_count", 0),
                    "deferred_buy_count": staged.get("deferred_buy_count", 0)},
        "notional_rmb": {"buy": str(money(notional["buy"])), "sell": str(money(notional["sell"])),
                          "gross": str(money(sum(notional.values())))},
        "pretrade_cost_estimate_rmb": str(money(estimated_cost)),
        "paper_fill_rehearsal": paper_fill_summary,
        "paper_execution_attribution": {
            "status": paper_execution_attribution.get("status"),
            "parent_count": paper_execution_attribution.get("parent_count"),
            "child_count": paper_execution_attribution.get("child_count"),
            "completed_parent_count": paper_execution_attribution.get("completed_parent_count"),
            "residual_parent_count": paper_execution_attribution.get("residual_parent_count"),
            "real_broker_state": False,
            "execution_admitted": False,
        },
        "paper_child_order_reconciliation": paper_child_reconciliation_summary,
        "paper_execution_cost_stress": {
            "path": str(paper_execution_stress_path),
            "scenario_count": len(paper_execution_attribution.get("execution_cost_stress", [])),
            "execution_admitted": False,
        },
        "paper_account_sizing": account_sizing_summary,
        "strategy_promotion_review": {
            "status": strategy_promotion_review.get("status"),
            "candidate_count": strategy_promotion_review.get("candidate_count"),
            "eligible_candidate_count": strategy_promotion_review.get("eligible_candidate_count"),
            "selection_required": strategy_promotion_review.get("selection_required"),
            "automatic_promotion": strategy_promotion_review.get("automatic_promotion"),
            "execution_admitted": strategy_promotion_review.get("execution_admitted"),
        },
        "strategy_account_sizing_matrix": str(strategy_sizing_matrix_path),
        "offline_portfolio_risk": {
            "status": offline_portfolio_risk.get("status") if offline_portfolio_risk else "not_indexed",
            "candidate_count": offline_portfolio_risk.get("candidate_count", 0) if offline_portfolio_risk else 0,
            "shared_account_scenario": offline_portfolio_risk.get("shared_account_scenario", {}) if offline_portfolio_risk else {},
            "execution_admitted": False,
        },
        "instrument_master": instrument_master_summary,
        "instrument_master_coverage": instrument_master_coverage_summary,
        "account_observed": current_snapshot is not None,
        "quote_coverage": quote_coverage,
        "quotes_observed": quotes_ready,
        "broker_prefill_attempted": False,
        "submission_attempted": False,
        "fills_observed": False,
        "execution_admitted": False,
        "release_control": {
            "state": release_state,
            "execution_release_allowed": False,
            "blockers": release_blockers,
        },
        "next_action": (
            "manual review of current preview; no broker prefill was attempted"
            if current_hydration is not None
            else "refresh same-session account and complete raw RMB quote coverage before any prefill"
        ),
        "current_preview": current_preview,
        "strategy_explainability": strategy_explainability,
        "stockdb_forward_compare": {
            "status": "loaded" if forward_compare_payload else "not_supplied",
            "path": str(forward_compare_path) if forward_compare_path else None,
            "schema": forward_compare_payload.get("schema") if forward_compare_payload else None,
            "source_cutoff": forward_compare_payload.get("source_cutoff") if forward_compare_payload else None,
            "selection_cutoff": (forward_compare_payload.get("comparison") or {}).get("selection_cutoff") if forward_compare_payload else None,
            "start_date": (forward_compare_payload.get("comparison") or {}).get("start_date") if forward_compare_payload else None,
            "end_date": (forward_compare_payload.get("comparison") or {}).get("end_date") if forward_compare_payload else None,
            "coverage_status": (forward_compare_payload.get("comparison") or {}).get("coverage_status") if forward_compare_payload else None,
            "calendar_session_count": (forward_compare_payload.get("comparison") or {}).get("calendar_session_count") if forward_compare_payload else None,
            "coverage_manifest": forward_compare_payload.get("coverage_manifest") if forward_compare_payload else None,
            "candidate_count": len(forward_compare_payload.get("candidates") or []) if forward_compare_payload else 0,
            "research_only": True,
            "execution_admitted": False,
        },
        "stockdb_shared_ledger_replay": {
            "status": "loaded" if ledger_replay_payload else "not_supplied",
            "path": str(ledger_replay_path) if ledger_replay_path else None,
            "schema": ledger_replay_payload.get("schema") if ledger_replay_payload else None,
            "snapshot": ledger_replay_payload.get("snapshot") if ledger_replay_payload else None,
            "common_path_dates": ledger_replay_payload.get("common_path_dates") if ledger_replay_payload else [],
            "coverage_manifest": ledger_replay_payload.get("coverage_manifest") if ledger_replay_payload else None,
            "candidate_count": len(ledger_replay_payload.get("candidates") or []) if ledger_replay_payload else 0,
            "research_only": True,
            "execution_admitted": False,
        },
        "ths_readonly_evidence": {
            "status": ths_readonly_payload.get("status") if ths_readonly_payload else "not_supplied",
            "path": str(ths_readonly_path) if ths_readonly_path else None,
            "account_alias": ths_readonly_payload.get("account_alias") if ths_readonly_payload else None,
            "holding_candidate_rows": ((ths_readonly_payload.get("holdings") or {}).get("candidate") or {}).get("row_count", 0) if ths_readonly_payload else 0,
            "holding_summary_cross_checks_pass": ((ths_readonly_payload.get("holdings") or {}).get("candidate") or {}).get("summary_cross_checks_pass") if ths_readonly_payload else None,
            "orders_observed": ths_readonly_payload.get("orders_observed") if ths_readonly_payload else None,
            "trades_observed": ths_readonly_payload.get("trades_observed") if ths_readonly_payload else None,
            "observation_window": ths_readonly_payload.get("observation_window") if ths_readonly_payload else None,
            "unknowns": ths_readonly_payload.get("unknowns", []) if ths_readonly_payload else [],
            "execution_admitted": False,
            "ledger_mutated": False,
        },
        "institutional_function_gap": gaps,
        "institutional_sources": INSTITUTIONAL_SOURCES,
        "artifacts": {"blotter": str(out / "blotter.csv"), "tca_pretrade": str(out / "tca_pretrade.csv"),
                       "risk_checks": str(risk_checks_path), "execution_schedule": str(schedule_path),
                       "static_risk_summary": str(risk_summary_path),
                       "paper_account_sizing_projection": str(account_sizing_path),
                       "paper_account_sizing_summary": str(account_sizing_json_path),
                       "strategy_account_sizing_matrix": str(strategy_sizing_matrix_path),
                       "strategy_account_sizing_matrix_summary": str(strategy_sizing_matrix_json_path),
                       "offline_portfolio_risk_summary": str(offline_portfolio_risk_path) if offline_portfolio_risk else None,
                       "strategy_comparison": str(comparison_path),
                       "stockdb_forward_compare": str(forward_compare_path) if forward_compare_path else None,
                       "stockdb_forward_comparison": str(stockdb_forward_comparison_path) if stockdb_forward_comparison_path else None,
                       "stockdb_ledger_replay": str(ledger_replay_path) if ledger_replay_path else None,
                       "ths_readonly_evidence_index": str(ths_readonly_path) if ths_readonly_path else None,
                       "research_robustness": str(robustness_path) if robustness_path else None,
                       "paper_execution_attribution": str(paper_execution_attribution_path),
                       "paper_execution_attribution_csv": str(paper_execution_attribution_csv_path),
                       "paper_child_pressure_scenarios": str(paper_child_pressure_path),
                       "paper_execution_cost_stress": str(paper_execution_stress_path),
                       "strategy_promotion_card": str(strategy_promotion_card_path),
                       "strategy_promotion_review": str(strategy_promotion_review_path),
                       "strategy_promotion_review_csv": str(strategy_promotion_review_csv_path),
                       "strategy_evidence_card": str(strategy_evidence_card_path),
                       "strategy_board_csv": str(strategy_board_csv_path),
                       "strategy_board_json": str(strategy_board_json_path),
                       "execution_controls": str(execution_controls_path),
                       "child_order_schedule": str(child_order_schedule_path),
                       "recovery_plan": str(recovery_plan_path),
                       "exception_alerts": str(exception_alerts_path),
                       "function_matrix": str(function_matrix_path),
                       "operator_action_board": str(operator_action_board_path),
                       "scenario_analysis": str(scenario_path),
                       "allocation_settlement_projection": str(allocation_path),
                       "paper_fill_projection": str(paper_fill_path),
                       "paper_ledger_projection": str(paper_ledger_path),
                       "paper_order_lifecycle": str(paper_lifecycle_path),
                       "paper_position_projection": str(paper_position_path),
                       "paper_reconciliation": str(paper_reconciliation_path),
                       "paper_child_order_projection": str(paper_child_projection_path),
                       "paper_child_order_reconciliation": str(paper_child_reconciliation_path),
                       "paper_child_order_reconciliation_summary": str(out / "paper_child_order_reconciliation.json"),
                       "target_order_reconciliation": str(target_order_reconciliation_path),
                       "paper_release_decision": str(paper_release_decision_path),
                       "paper_audit_event_log": str(paper_audit_event_log_path),
                       "paper_audit_event_log_jsonl": str(paper_audit_event_log_jsonl_path),
                       "offline_operator_decision_queue": str(offline_operator_decision_queue_path),
                       "offline_operator_decision_queue_json": str(offline_operator_decision_queue_json_path),
                       "paper_recovery_handoff_index": str(recovery_handoff_index_path) if recovery_handoff_index_path.exists() else None,
                       "paper_next_session_carry": str(paper_next_session_carry_path),
                       "strategy_explainability": str(strategy_explainability_path),
                       "instrument_master": instrument_master_manifest.get("master_path") if instrument_master_manifest else None,
                       "instrument_master_coverage": str(instrument_master_coverage_path) if instrument_master_coverage_path else None},
    }
    if quote_drift_path is not None:
        snapshot["artifacts"]["tca_quote_drift"] = str(quote_drift_path)
    if saved_no_trade_holdings is not None:
        snapshot["trade_status"] = "NO_TRADE"
        snapshot["saved_research_holdings"] = saved_no_trade_holdings
        snapshot["next_action"] = "retain exact saved research holdings; no operation is scheduled"
        snapshot["artifacts"]["saved_target_holdings"] = artifacts["candidate_targets"]
    if current_hydration is not None:
        snapshot["artifacts"]["current_preview"] = str(out / "current_preview.json")
        if (out / "current_adjustments.csv").exists():
            snapshot["artifacts"]["current_adjustments"] = str(out / "current_adjustments.csv")
    (out / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# Trader Desk Snapshot",
        "",
        f"- Strategy: `{snapshot['strategy_id']}`; execution day: `{snapshot['execution_day']}`.",
        f"- Paper orders: {len(orders)} ({len(by_side['buy'])} buys / {len(by_side['sell'])} sells); accepted by copied ledger: {len(accepted)}; rejected: {len(rejected)}.",
        f"- Gross notional: {money(sum(notional.values()))} RMB; estimated all-in pre-trade cost: {money(estimated_cost)} RMB.",
        f"- Synthetic paper fills: {paper_fill_summary['synthetic_full_fill_count']}/{paper_fill_summary['order_count']}; "
        f"projected cash delta {paper_fill_summary['cash_delta_rmb']} RMB; this is a rehearsal, not broker evidence.",
        f"- Internal paper reconciliation: `{paper_fill_summary['internal_reconciliation_status']}`; live broker/account reconciliation: `{paper_fill_summary['live_reconciliation_status']}`.",
        f"- Paper child-order projection: `{paper_child_projection_path}`; child fills remain synthetic and `real_broker_state=false`.",
        f"- Paper child-order reconciliation: `{paper_child_reconciliation_path}`; {paper_child_reconciliation_summary['complete_synthetic_parent_count']} synthetic parents complete, mismatches={paper_child_reconciliation_summary['mismatch_count']}.",
        f"- Paper execution attribution: `{paper_execution_attribution_path}`; {paper_execution_attribution['completed_parent_count']}/{paper_execution_attribution['parent_count']} synthetic parents complete, residual pressure: `{paper_child_pressure_path}`.",
        f"- Paper execution cost stress: `{paper_execution_stress_path}`; fixed additional slippage scenarios are diagnostic only.",
        f"- Strategy promotion review: `{strategy_promotion_review_path}`; {strategy_promotion_review['candidate_count']} candidates remain pending fresh holdout, observed account/quotes and explicit human decision.",
        f"- Target/order reconciliation: `{target_order_reconciliation_path}`; account holdings remain unobserved and execution release stays disabled.",
        f"- Instrument master: `{instrument_master_summary['status']}` at `{instrument_master_summary['observed_as_of']}`; {instrument_master_summary['instrument_count']} records, order identity gaps={instrument_master_summary['order_identity_gap_count']}; PIT qualification and execution admission remain false.",
        f"- Instrument master coverage diagnostic: `{instrument_master_coverage_summary['status']}`; source-only={instrument_master_coverage_summary['source_only']}; scope is research-only and does not authorize or block ordinary-equity order identity.",
        f"- Paper release decision: `{paper_release_decision_path}`; every row remains non-admissible until the live inputs and human review boundary are satisfied.",
        f"- Paper audit event log: `{paper_audit_event_log_path}` / `{paper_audit_event_log_jsonl_path}`; score, target, preflight, lifecycle, reconciliation, release and recovery events remain paper-only.",
        f"- Offline operator decision queue: `{offline_operator_decision_queue_path}`; strategy-level holds combine ledger, route, account, quote and lifecycle state without authorizing submission.",
        f"- Paper recovery handoff: `{recovery_handoff_index_path}`; unresolved scenarios require manual reconciliation and never permit automatic retry.",
        f"- Next-session carry: `{paper_next_session_carry_path}`; T+1 and sellable quantities remain explicit unresolved inputs.",
        f"- Paper account sizing: `{account_sizing_path}`; declared initial cash and shared paper-fill fees are projected only, not observed account state.",
        f"- Per-strategy account sizing matrix: `{strategy_sizing_matrix_path}`; candidates are checked separately and never pooled for release.",
        f"- Offline portfolio risk book: `{offline_portfolio_risk_path}`; candidate cash capacity is compared separately and the shared-account merge is explicitly rejected.",
        f"- Strategy evidence card: `{strategy_evidence_card_path}`; it joins the frozen candidate comparison with the latest same-cutoff factor replay and remains descriptive only.",
        f"- Independent StockDB forward diagnostic: `{forward_compare_path}`; integrated into strategy comparison as research-only evidence, not a promotion signal." if forward_compare_path else "- Independent StockDB forward diagnostic: not supplied; strategy comparison remains paper-only.",
        f"- Shared StockDB Engine/Ledger replay: `{ledger_replay_path}`; replay fills are paper-only and not broker evidence." if ledger_replay_path else "- Shared StockDB Engine/Ledger replay: not supplied.",
        f"- THS read-only evidence index: `{ths_readonly_path}`; holdings are candidate-only, order/trade rows remain unknown, and no Ledger mutation occurred." if ths_readonly_path else "- THS read-only evidence index: not supplied.",
        f"- Layered offline strategy board: `{strategy_board_json_path}`; paper candidates and factor diagnostics remain explicitly non-promotable.",
        f"- Release control: `{release_state}`; execution release allowed: `false`; blockers: `{', '.join(release_blockers)}`.",
        (
            "- Current account and raw-RMB quotes were supplied to the paper-only preview; "
            "THS prefill, submission and fills were not attempted."
            if current_hydration is not None else
            "- Account, live quotes, THS prefill, submission and fills: not observed or attempted."
        ),
        "",
        "## Function comparison",
        "",
        "| Institutional function | Public capability being matched | Current state | Tomorrow use | Priority | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    report.extend(
        f"| {row['institutional_function']} | {row['institutional_capability']} | {row['state']} | {row['tomorrow_use']} | "
        f"{row['priority']} | {row['evidence']} |"
        for row in gaps
    )
    report.extend(["", "## Files", "", f"- Blotter: `{out / 'blotter.csv'}`", f"- Pre-trade TCA: `{out / 'tca_pretrade.csv'}`",
                   f"- Offline portfolio what-if scenarios: `{scenario_path}`",
                   f"- Paper allocation and settlement projection: `{allocation_path}`",
                   f"- Synthetic paper fill projection: `{paper_fill_path}`",
                   f"- Synthetic paper ledger summary: `{paper_ledger_path}`",
                   f"- Synthetic paper order lifecycle: `{paper_lifecycle_path}`",
                   f"- Synthetic paper position projection: `{paper_position_path}`",
                   f"- Target/order reconciliation: `{target_order_reconciliation_path}`",
                   f"- Paper release decision: `{paper_release_decision_path}`",
                   f"- Paper audit event log: `{paper_audit_event_log_path}` / `{paper_audit_event_log_jsonl_path}`",
                   f"- Offline operator decision queue: `{offline_operator_decision_queue_path}` / `{offline_operator_decision_queue_json_path}`",
                   f"- Paper recovery handoff index: `{recovery_handoff_index_path}`",
                   f"- Next-session carry: `{paper_next_session_carry_path}`",
                   f"- Risk checks: `{risk_checks_path}`", f"- Paper execution sequence: `{schedule_path}`",
                   f"- Static risk summary: `{risk_summary_path}`",
                   f"- Paper account sizing projection: `{account_sizing_path}`",
                   f"- Paper account sizing summary: `{account_sizing_json_path}`",
                   f"- Per-strategy account sizing matrix: `{strategy_sizing_matrix_path}` / `{strategy_sizing_matrix_json_path}`",
                   f"- Offline portfolio risk book: `{offline_portfolio_risk_path}`",
                   f"- Strategy comparison: `{comparison_path}`",
                   f"- Independent StockDB forward diagnostic: `{forward_compare_path}`" if forward_compare_path else "- Independent StockDB forward diagnostic: not supplied",
                   f"- StockDB candidate comparison table: `{stockdb_forward_comparison_path}`" if stockdb_forward_comparison_path else "- StockDB candidate comparison table: not supplied",
                   f"- Shared StockDB Engine/Ledger replay: `{ledger_replay_path}`" if ledger_replay_path else "- Shared StockDB Engine/Ledger replay: not supplied",
                   f"- THS read-only evidence index: `{ths_readonly_path}`" if ths_readonly_path else "- THS read-only evidence index: not supplied",
                   f"- Strategy promotion card: `{strategy_promotion_card_path}`",
                   f"- Strategy evidence card: `{strategy_evidence_card_path}`",
                   f"- Layered strategy board: `{strategy_board_json_path}` / `{strategy_board_csv_path}`",
                   f"- Release and child-order controls: `{execution_controls_path}`",
                   f"- Paper exception alerts: `{exception_alerts_path}`",
                   f"- Paper child-order schedule: `{child_order_schedule_path}`",
                   f"- Lifecycle recovery plan: `{recovery_plan_path}`",
                   f"- Institutional function matrix: `{function_matrix_path}`",
                   f"- Operator action board: `{operator_action_board_path}`",
                   f"- Strategy explainability diagnostic: `{strategy_explainability_path}`",
                   f"- Snapshot: `{out / 'snapshot.json'}`"])
    if quote_drift_path is not None:
        report.insert(report.index(f"- Snapshot: `{out / 'snapshot.json'}`"),
                      f"- Same-session quote drift TCA: `{quote_drift_path}`")
    if strategy_explainability:
        report.extend([
            "", "## Strategy Explainability Diagnostic", "",
            f"- Saved-ledger case: `{strategy_explainability['case_id']}`; net PnL: `{strategy_explainability['net_pnl_cny']}` CNY.",
            f"- Top losses: `{', '.join(item['symbol'] for item in strategy_explainability['top_loss_symbols'])}`.",
            f"- Top gains: `{', '.join(item['symbol'] for item in strategy_explainability['top_gain_symbols'])}`.",
            "- This is a historical saved-account diagnostic only; it does not approve the current pack or prove causal factor efficacy.",
        ])
        evidence = strategy_explainability.get("period_evidence")
        if evidence:
            report.append(
                f"- Fixed-window evidence: {evidence['positive_window_count']}/{evidence['window_count']} windows positive; "
                f"promotion status=`{evidence['promotion_status']}`."
            )
            for period in evidence["periods"]:
                report.append(
                    f"- `{period['start']}..{period['end']}`: return `{period['net_return']:.2%}`, "
                    f"MDD `{period['max_drawdown']:.2%}`, Sharpe `{period['sharpe_zero_rf_252']:.3f}`."
                )
    if current_hydration is not None:
        report.append(f"- Current paper preview: `{out / 'current_preview.json'}`")
        if (out / "current_adjustments.csv").exists():
            report.append(f"- Current target adjustments: `{out / 'current_adjustments.csv'}`")
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return out, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opening-pack", required=True)
    parser.add_argument("--out")
    parser.add_argument("--account-observation", help="Current normalized THS account observation JSON")
    parser.add_argument("--account", help="Account alias with a latest normalized observation in the existing THS journal")
    quote_source = parser.add_mutually_exclusive_group()
    quote_source.add_argument("--quotes", help="Current raw-RMB quote observation JSON")
    quote_source.add_argument("--quotes-csv", help="Filled quote CSV with symbol, market and price_rmb")
    parser.add_argument("--quote-source")
    parser.add_argument("--quote-observed-at")
    parser.add_argument("--quote-trading-date")
    parser.add_argument("--strategy", help="Hydrate exactly this strategy candidate")
    parser.add_argument(
        "--selected-strategy-id",
        help="Build the paper desk for exactly one explicitly selected candidate; never auto-selects",
    )
    parser.add_argument("--sizing", choices=("replay_shares", "observed_account"), default="replay_shares")
    parser.add_argument("--execution-scope", choices=("strict", "mainboard_only"), default="strict")
    parser.add_argument(
        "--stockdb-forward-compare",
        help="Independent stockdb_forward_compare_v1 result.json to attach as research-only desk evidence",
    )
    parser.add_argument(
        "--stockdb-ledger-replay",
        help="Shared stockdb_shared_ledger_replay_v1 summary.json to attach as research-only desk evidence",
    )
    parser.add_argument(
        "--ths-readonly-evidence-index",
        help="Static THS read-only evidence index; descriptive only, never imported into the Ledger",
    )
    args = parser.parse_args(argv)
    out, snapshot = build(
        args.opening_pack,
        args.out,
        account_observation=args.account_observation,
        account=args.account,
        quotes=args.quotes,
        quotes_csv=args.quotes_csv,
        quote_source=args.quote_source,
        quote_observed_at=args.quote_observed_at,
        quote_trading_date=args.quote_trading_date,
        strategy_id=args.strategy,
        selected_strategy_id=args.selected_strategy_id,
        sizing=args.sizing,
        execution_scope=args.execution_scope,
        stockdb_forward_compare=args.stockdb_forward_compare,
        stockdb_ledger_replay=args.stockdb_ledger_replay,
        ths_readonly_evidence_index=args.ths_readonly_evidence_index,
    )
    print(json.dumps({"output": str(out), "status": snapshot["status"], "orders": snapshot["orders"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
