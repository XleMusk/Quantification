"""Explicit export mappings; no OCR, clipboard access or inferred account state."""
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re

from .adapter import money, now, validate_cash_balance, validate_snapshot
from ..contracts import BrokerCashMovement, BrokerRestrictionRelease, BrokerShareCredit, SecurityId, ShareQuantity


def csv_rows(path, columns, *, encoding="utf-8-sig", delimiter=","):
    if delimiter not in {",", "\t"}:
        raise ValueError("Only comma/tab exports are supported")
    with Path(path).open(encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream, delimiter=delimiter)
        headers = reader.fieldnames or []
        if not headers or len(set(headers)) != len(headers) or not set(columns.values()) <= set(headers):
            raise ValueError("Missing, duplicate or unmapped export headers")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("Malformed export row")
    return rows


def integer(value):
    text = str(value).strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError("Expected an integer; no float/NaN/blank coercion")
    return int(text)


def identifier(value):
    text = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", text):
        raise ValueError("Invalid broker identifier")
    return text


def normalize(kind, rows, mapping):
    if kind == "share_credits":
        columns = mapping["columns"]
        fields = {"credit_id", "symbol", "market", "credited_qty", "credit_day", "available_day", "kind"}
        if (set(columns) != fields or len(set(columns.values())) != len(fields)
                or mapping.get("basis") != "account_bonus_share_credits" or mapping.get("quantity_unit") != "share"):
            raise ValueError("Use explicit account share-credit export columns and share units")
        result, seen = [], set()
        for raw in rows:
            row = {key: str(raw[column]).strip() for key, column in columns.items()}
            market = mapping.get("markets", {}).get(row["market"])
            if market not in {"SSE", "SZSE", "BSE"} or not re.fullmatch(r"\d{6}", row["symbol"]):
                raise ValueError("Share credit requires explicit mapped market and six-digit security code")
            credit = BrokerShareCredit(identifier(row["credit_id"]), SecurityId(f"{row['symbol']}.{market}"),
                ShareQuantity(integer(row["credited_qty"])), row["credit_day"], row["available_day"],
                mapping.get("kinds", {}).get(row["kind"]))
            if credit.id in seen:
                raise ValueError("Duplicate share credit ID")
            seen.add(credit.id)
            result.append({"credit_id": credit.id, "symbol": credit.security.symbol,
                           "credited_qty": credit.quantity.shares, "credit_day": credit.day,
                           "available_day": credit.available_day, "kind": credit.kind})
        return result
    if kind == "restriction_releases":
        columns = mapping["columns"]
        fields = {"release_id", "symbol", "market", "released_qty", "release_day", "reason"}
        if (set(columns) != fields or len(set(columns.values())) != len(fields)
                or mapping.get("basis") != "opening_restriction_releases" or mapping.get("quantity_unit") != "share"):
            raise ValueError("Use explicit opening restriction release export columns and share units")
        result, seen = [], set()
        for raw in rows:
            row = {key: str(raw[column]).strip() for key, column in columns.items()}
            market = mapping.get("markets", {}).get(row["market"])
            if market not in {"SSE", "SZSE", "BSE"} or not re.fullmatch(r"\d{6}", row["symbol"]):
                raise ValueError("Release requires explicit mapped market and six-digit security code")
            release = BrokerRestrictionRelease(identifier(row["release_id"]), SecurityId(f"{row['symbol']}.{market}"),
                ShareQuantity(integer(row["released_qty"])), row["release_day"], mapping.get("reasons", {}).get(row["reason"]))
            if release.id in seen:
                raise ValueError("Duplicate restriction release ID")
            seen.add(release.id)
            result.append({"release_id": release.id, "symbol": release.security.symbol,
                           "released_qty": release.quantity.shares, "release_day": release.day, "reason": release.reason})
        return result
    if kind == "movements":
        columns = mapping["columns"]
        if (set(columns) != {"movement_id", "kind", "cash_change"} or len(set(columns.values())) != 3
                or mapping.get("basis") != "nontrade_cash_movements"
                or mapping.get("currency") != "CNY" or mapping.get("amount_unit") != "rmb"):
            raise ValueError("Use an exact explicit nontrade cash movement statement mapping")
        result, seen = [], set()
        for raw in rows:
            row = {key: str(raw[column]).strip() for key, column in columns.items()}
            change = row["cash_change"]
            change = -money(change[1:]) if change.startswith("-") else money(change)
            movement = BrokerCashMovement(identifier(row["movement_id"]), mapping.get("kinds", {}).get(row["kind"]), change)
            if movement.id in seen:
                raise ValueError("Duplicate cash movement statement ID")
            seen.add(movement.id)
            result.append({"movement_id": movement.id, "kind": movement.kind, "cash_change_cents": change})
        return result
    required = {"account": {"symbol", "market", "quantity", "sellable"},
                "orders": {"symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"},
                "trades": {"symbol", "market", "side", "order_id", "execution_id", "qty", "price"},
                "cashflows": {"symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"}}[kind]
    columns = mapping["columns"]
    priced_account = kind == "account" and set(columns) == required | {"price"}
    if priced_account:
        if mapping.get("price_basis") != "raw_rmb":
            raise ValueError("Account valuation must explicitly declare raw RMB prices")
        required = required | {"price"}
    if set(columns) != required or len(set(columns.values())) != len(columns):
        raise ValueError("Use an exact, unambiguous column map for this view")
    if kind == "cashflows" and mapping.get("basis") != "cumulative_order_cashflow":
        raise ValueError("Cashflow mapping must declare one actual cumulative statement per broker contract")
    result, identities = [], set()
    for raw in rows:
        row = {key: str(raw[column]).strip() for key, column in columns.items()}
        if not re.fullmatch(r"\d{6}", row["symbol"]):
            raise ValueError("Stock codes must retain all six digits")
        market = mapping["markets"].get(row.pop("market"))
        if market not in {"SSE", "SZSE", "BSE"}:
            raise ValueError("Unmapped exchange; do not infer it from ticker prefix")
        row["symbol"] += "." + market
        if kind == "account":
            row["quantity"], row["sellable"] = integer(row["quantity"]), integer(row["sellable"])
            if row["sellable"] > row["quantity"]:
                raise ValueError("Sellable quantity exceeds holdings")
            key = row["symbol"]
            if priced_account:
                row["price_cents"] = money(row.pop("price"))
                if not row["price_cents"]:
                    raise ValueError("Account valuation must be positive")
        else:
            row["side"] = mapping["sides"].get(row["side"])
            if row["side"] not in {"buy", "sell"}:
                raise ValueError("Unsupported or unknown order side")
            row["order_id"] = identifier(row["order_id"])
            key = row["order_id"]
        if kind == "cashflows":
            change = row.pop("cash_change")
            row["cash_change_cents"] = -money(change[1:]) if change.startswith("-") else money(change)
            row["gross_cents"], row["fees_cents"] = money(row.pop("gross_amount")), money(row.pop("fees"))
            expected = row["gross_cents"] * (1 if row["side"] == "sell" else -1) - row["fees_cents"]
            if expected != row["cash_change_cents"]:
                raise ValueError("Actual statement gross/fees/signed cash do not reconcile")
        elif kind != "account":
            row["qty"], row["price_cents"] = integer(row["qty"]), money(row.pop("price"))
            if not row["qty"] or not row["price_cents"]:
                raise ValueError("Quantity and price must be positive")
            key = row["order_id"]
            if kind == "orders":
                row["filled_qty"] = integer(row["filled_qty"])
                if row["filled_qty"] > row["qty"]:
                    raise ValueError("Order fill quantity exceeds order size")
                row["broker_status"] = row["status"]
                row["status"] = mapping.get("statuses", {}).get(row["status"], "unknown")
                if row["status"] not in {"accepted", "partial", "filled", "cancelled", "rejected", "unknown"}:
                    raise ValueError("Invalid status mapping")
            else:
                row["execution_id"] = identifier(row["execution_id"])
                key = (row["order_id"], row["execution_id"])
        if key in identities:
            raise ValueError("Duplicate holdings/order/execution rows; export must be unambiguous")
        identities.add(key)
        result.append(row)
    return result


def observation(kind, rows, mapping, *, account_alias, observed_at, trading_date, source, cash=None,
                cash_total=None, cash_frozen=None):
    if source not in {"ths_controls", "ths_user_export"} or not account_alias:
        raise ValueError("Unknown observation source/account")
    stamp = datetime.fromisoformat(observed_at)
    if stamp.tzinfo is None or stamp > now() or date.fromisoformat(trading_date) > stamp.astimezone(timezone(timedelta(hours=8))).date():
        raise ValueError("Observation requires an explicit, non-future timestamp and trading date")
    value = {"kind": kind, "account_alias": account_alias, "observed_at": observed_at,
             "trading_date": trading_date, "source": source, "rows": normalize(kind, rows, mapping)}
    if kind != "account" and any(v is not None for v in (cash, cash_total, cash_frozen)):
        raise ValueError("Cash balances belong to the account view only")
    if kind in {"cashflows", "movements", "restriction_releases", "share_credits"}:
        if source != "ths_user_export":
            raise ValueError("Cashflow control capture is not verified; use an explicit actual statement export")
        value["basis"] = mapping["basis"]
        if kind == "movements":
            value.update(currency="CNY", cash_unit="cent")
        if kind == "restriction_releases":
            if any(r["release_day"] != trading_date for r in value["rows"]):
                raise ValueError("Restriction release effective day must match the explicit statement session")
            value["quantity_unit"] = "share"
        if kind == "share_credits":
            if any(c["credit_day"] != trading_date for c in value["rows"]):
                raise ValueError("Share credit effective day must match the explicit statement session")
            value["quantity_unit"] = "share"
    if kind == "account":
        value["cash_available"] = money(cash)
        if cash_total is not None or cash_frozen is not None:
            value.update(cash_total=money(cash_total), cash_frozen=money(cash_frozen),
                         cash_basis="cash_balance_includes_frozen")
        value["positions"] = {row["symbol"]: {k: row[k] for k in ("quantity", "sellable")} for row in value["rows"]}
        if "price" in mapping["columns"]:
            value["marks"] = {row["symbol"]: row["price_cents"] for row in value["rows"] if row["quantity"]}
        # An explicit broker export may be imported after its trading day.
        # Keep live control captures strict, while plan creation still calls
        # validate_snapshot with the current clock and therefore rejects stale
        # exported balances for prefill.
        validate_snapshot(value, current=stamp if source == "ths_user_export" else None)
    return value


def reconcile_account(plan, result, account, cashflows, *, interval_closed=False):
    """Check actual balances/fees against immutable pre-submit holdings; no ledger writes."""
    previous = (plan.get("result") or {}).get("confirmed_account_reconciliation")
    value = {"status": "pending_evidence", "ledger_imported": False}
    result = {**result}
    for observation, key in ((account, "account_observed_at"), (cashflows, "cashflows_observed_at")):
        if observation is not None:
            result[key] = observation["observed_at"]
    def finish(reason=None, conflict=False):
        if reason:
            value["reason"] = reason
        if conflict:
            value["status"] = "reconciliation_required"
        matched = value["status"] == "matched_cash_and_positions"
        return {**result, **({"status": "reconciliation_required"} if conflict else {}),
                "account_reconciliation": value, "account_cash_reconciled": matched,
                "account_positions_reconciled": matched,
                "confirmed_account_reconciliation": value if matched else previous}

    if result["status"] not in {"observed_partial", "observed_filled", "observed_cancelled", "observed_rejected"}:
        return finish("trade_evidence_not_reconciled")
    if account is None or cashflows is None:
        return finish("account_or_actual_cashflow_missing", conflict=previous is not None)
    before = plan["snapshot"]
    for observation, kind in ((account, "account"), (cashflows, "cashflows")):
        if (observation.get("kind") != kind or observation.get("account_alias") != plan["account_alias"]
                or observation.get("trading_date") != plan["trading_date"]):
            return finish("account_or_cashflow_identity_conflict", True)
    if cashflows.get("source") != "ths_user_export" or cashflows.get("basis") != "cumulative_order_cashflow":
        return finish("unknown_cashflow_source_or_basis", True)
    try:
        # Historical receipt checking is not authorization to prefill a stale account.
        validate_snapshot(before, current=datetime.fromisoformat(before["observed_at"]))
        validate_snapshot(account, current=datetime.fromisoformat(account["observed_at"]))
        if not validate_cash_balance(before) or not validate_cash_balance(account):
            return finish("total_frozen_cash_basis_missing", conflict=previous is not None)
    except ValueError as error:
        return finish(str(error), True)
    matched = [r for r in cashflows["rows"] if r["order_id"] == plan["contract_id"]]
    if len(matched) != 1:
        return finish("unique_cumulative_order_statement_missing", conflict=previous is not None)
    flow = matched[0]
    if (any(flow[k] != plan[k] for k in ("symbol", "side"))
            or flow["gross_cents"] != result["gross_executed_cents"]):
        return finish("statement_conflicts_with_confirmed_executions", True)
    if (any(type(flow[k]) is not int for k in ("gross_cents", "fees_cents", "cash_change_cents"))
            or flow["fees_cents"] < 0 or flow["cash_change_cents"] !=
            flow["gross_cents"] * (1 if flow["side"] == "sell" else -1) - flow["fees_cents"]):
        return finish("invalid_actual_statement_economics", True)
    if (previous and previous["filled_qty"] == result["filled_qty"]
            and any(previous[k] != flow[k] for k in ("gross_cents", "fees_cents", "cash_change_cents"))):
        return finish("previously_confirmed_statement_changed", True)
    if interval_closed:
        # Keep the historical balance proof, but current fill/fee revisions still stop.
        if (previous and previous["filled_qty"] == result["filled_qty"]
                and previous["execution_ids"] == result["execution_ids"]):
            value = dict(previous)
            return finish()
        return finish("account_interval_closed_before_full_cash_proof", conflict=previous is not None)
    stamps = [result["orders_observed_at"], result["trades_observed_at"], cashflows["observed_at"]]
    if (datetime.fromisoformat(cashflows["observed_at"]) < datetime.fromisoformat(plan["created_at"])
            or any(datetime.fromisoformat(account["observed_at"]) < datetime.fromisoformat(s) for s in stamps)):
        return finish("refresh_account_after_orders_trades_and_cashflow", conflict=previous is not None)
    expected = {s: dict(p) for s, p in before["positions"].items()}
    symbol, side, qty = plan["symbol"], plan["side"], result["filled_qty"]
    position = expected.setdefault(symbol, {"quantity": 0, "sellable": 0})
    position["quantity"] += qty if side == "buy" else -qty
    if side == "sell":
        position["sellable"] -= qty
        if result["status"] == "observed_partial":
            position["sellable"] -= plan["qty"] - qty
    # Main-board same-day buy is T1. This does not expand native ETF support.
    expected = {s: p for s, p in expected.items() if p["quantity"]}
    observed = {s: p for s, p in account["positions"].items() if p["quantity"]}
    residual = account["cash_total"] - before["cash_total"] - flow["cash_change_cents"]
    value.update(filled_qty=qty, **{k: flow[k] for k in ("gross_cents", "fees_cents", "cash_change_cents")},
                 cash_basis=account["cash_basis"], cash_residual_cents=residual,
                 before_cash_total=before["cash_total"], after_cash_total=account["cash_total"],
                 after_cash_available=account["cash_available"], after_cash_frozen=account["cash_frozen"],
                 expected_positions=expected, observed_positions=observed,
                 account_observed_at=account["observed_at"], cashflows_observed_at=cashflows["observed_at"],
                 execution_ids=result["execution_ids"])
    if residual or expected != observed:
        return finish("cash_or_positions_do_not_reconcile", True)
    value["status"] = "matched_cash_and_positions"
    return finish()


def reconcile(plan, orders, trades):
    """Broker contract binding is explicit; never guess by ticker/time proximity."""
    contract = plan.get("contract_id")
    if not contract:
        return {"status": "submission_unknown", "reason": "Bind the actual broker contract ID after manual submission"}
    for value, kind in ((orders, "orders"), (trades, "trades")):
        if not value or value["kind"] != kind or value["account_alias"] != plan["account_alias"] or value["trading_date"] != plan["trading_date"]:
            raise ValueError("Matching account/date order and trade observations are required")
        if datetime.fromisoformat(value["observed_at"]) < datetime.fromisoformat(plan["created_at"]):
            raise ValueError("Broker observation predates the plan; query the current order and trade views")
    matched = [row for row in orders["rows"] if row["order_id"] == contract]
    fills = [row for row in trades["rows"] if row["order_id"] == contract]
    seen, quantity, executions = set(), 0, []
    for fill in fills:
        key = fill["execution_id"]
        if key in seen or fill["symbol"] != plan["symbol"] or fill["side"] != plan["side"]:
            raise ValueError("Duplicate or mismatched execution")
        if (plan["side"] == "buy" and fill["price_cents"] > plan["price_cents"]) or (plan["side"] == "sell" and fill["price_cents"] < plan["price_cents"]):
            raise ValueError("Observed execution violates limit")
        seen.add(key)
        quantity += fill["qty"]
        executions.append({key: fill[key] for key in (
            "order_id", "execution_id", "symbol", "side", "qty", "price_cents")})
        executions[-1]["notional_cents"] = fill["qty"] * fill["price_cents"]
    evidence = {"orders_observed_at": orders["observed_at"], "trades_observed_at": trades["observed_at"],
                "executions": sorted(executions, key=lambda row: row["execution_id"]),
                "execution_ids": sorted(seen), "filled_qty": quantity,
                "gross_executed_cents": sum(row["notional_cents"] for row in executions),
                "account_cash_reconciled": False, "submitted_by_adapter": False}
    if len(matched) != 1:
        return {**evidence, "status": "submission_unknown", "reason": "Broker order not uniquely observed; query, never resend"}
    order = matched[0]
    if any(order[key] != plan[key] for key in ("symbol", "side", "qty", "price_cents")):
        raise ValueError("Broker order does not match the prefilled plan")
    if quantity != order["filled_qty"] or quantity > plan["qty"]:
        return {**evidence, "status": "reconciliation_required", "order_filled_qty": order["filled_qty"], "trade_qty": quantity}
    if order["status"] == "rejected" and quantity:
        status = "reconciliation_required"
    elif quantity == plan["qty"]:
        status = "observed_filled"
    elif order["status"] in {"cancelled", "rejected"}:
        status = "observed_" + order["status"]
    elif order["status"] == "filled":
        status = "reconciliation_required"
    elif quantity:
        status = "observed_partial"
    else:
        status = "observed_accepted" if order["status"] == "accepted" else "reconciliation_required"
    return {**evidence, "status": status, "broker_status": order["broker_status"]}


def retain_execution_evidence(previous, result):
    """A clipped query or changed execution ID cannot erase confirmed economics."""
    previous = previous or {}
    known = previous.get("confirmed_executions", [])
    known_ids = set(previous.get("confirmed_execution_ids", previous.get("execution_ids", [])))
    known_qty = previous.get("confirmed_filled_qty", previous.get("filled_qty", 0))
    current = {row["execution_id"]: row for row in result.get("executions", [])}
    reason = None
    if any(row["execution_id"] in current and row != current[row["execution_id"]] for row in known):
        reason = "previously_confirmed_execution_changed"
    elif known_ids - set(current):
        reason = "previously_confirmed_execution_missing"
    elif result.get("filled_qty", 0) < known_qty:
        reason = "previously_confirmed_quantity_decreased"
    if reason:
        result = {**result, "status": "reconciliation_required", "reason": reason}
    elif result["status"] in {"observed_accepted", "observed_partial", "observed_filled",
                              "observed_cancelled", "observed_rejected"}:
        known = result.get("executions", [])
        known_ids, known_qty = set(current), result.get("filled_qty", 0)
    return {**result, "confirmed_executions": known, "confirmed_execution_ids": sorted(known_ids),
            "confirmed_filled_qty": known_qty}
