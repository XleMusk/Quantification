"""Account-specific rights receipts; public ex-price ratios are not allocations."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from .contracts import TradingMode
from .rules import cents


WAIVE_POLICY = {"id": "rights_replay_waive_all_v1", "decision": "waive_all",
                "source_kind": "explicit_replay_assumption", "execution_admitted": False}


def rights_replay_contract(policy):
    if policy is None:
        return None
    if policy != "waive":
        raise ValueError("Unknown explicit rights replay policy")
    return dict(WAIVE_POLICY)


def validate_rights_binding(state, policy, context=None):
    contract = rights_replay_contract(policy)
    if state.get("rights_replay_policy") != contract:
        raise ValueError("Rights replay policy changed; fresh replay required")
    if contract is not None and (state.get("broker_binding") or
                                context is not None and context.mode != TradingMode.REPLAY):
        raise PermissionError("Rights assumption requires an unbound replay account")
    return contract


def iso_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Explicit ISO rights date required")
    return value


def positive_decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Known positive rights amount required") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("Known positive rights amount required")
    return result


def reference_terms(reference):
    if reference.get("kind") != "rights_issue":
        raise ValueError("Rights reference required")
    registration, ex_day = iso_day(reference["registration_date"]), iso_day(reference["date"])
    if registration >= ex_day or not reference.get("source_sha256"):
        raise ValueError("Dated reviewed rights reference required")
    return {"symbol": reference["symbol"], "date": ex_day, "registration_date": registration,
            "reference_rights_ratio": str(positive_decimal(reference["rights_ratio"]).normalize()),
            "subscription_price": str(positive_decimal(reference["subscription_price"]).normalize()),
            "reference_sha256": reference["source_sha256"]}


def waiver_receipt(reference, quantity, context, policy):
    if rights_replay_contract(policy) is None:
        return None
    if context.mode != TradingMode.REPLAY:
        raise PermissionError("Rights assumption is replay-only")
    return {"kind": "rights_issue_resolution", "account_id": context.account_id,
            "mode": context.mode.value, "registered_quantity": quantity,
            "action_terms": reference_terms(reference), "decision": "waive",
            "subscribed_shares": 0, "allocated_shares": 0, "cash_debit_cents": 0,
            "source_kind": "explicit_replay_assumption", "source_ref": WAIVE_POLICY["id"],
            "policy": dict(WAIVE_POLICY)}


def checked_receipt(book, reference, quantity, receipt):
    terms = reference_terms(reference)
    if (not isinstance(receipt, dict) or receipt.get("kind") != "rights_issue_resolution"
            or receipt.get("account_id") != book.context.account_id
            or receipt.get("mode") != book.context.mode.value
            or type(receipt.get("registered_quantity")) is not int
            or receipt["registered_quantity"] != quantity or receipt.get("action_terms") != terms
            or not isinstance(receipt.get("source_ref"), str) or not receipt["source_ref"].strip()
            or receipt.get("source_kind") not in {"broker_account_record", "explicit_replay_assumption"}):
        raise ValueError("Rights issue needs a matching account-specific receipt")
    fields = ("subscribed_shares", "allocated_shares", "cash_debit_cents")
    if any(type(receipt.get(key)) is not int or receipt[key] < 0 for key in fields):
        raise ValueError("Explicit integer rights shares/cash required")
    if receipt.get("decision") == "waive":
        if any(receipt[key] for key in fields):
            raise ValueError("A waiver cannot contain subscribed shares or cash")
        if receipt["source_kind"] == "explicit_replay_assumption" and (
                book.context.mode != TradingMode.REPLAY or book.state.get("broker_binding")
                or receipt.get("policy") != WAIVE_POLICY or book.state.get("rights_replay_policy") != WAIVE_POLICY):
            raise PermissionError("Rights waiver requires its predeclared unbound replay policy")
        return {"waive": terms["date"]}, 0, 0
    if receipt.get("decision") != "subscribe" or receipt["source_kind"] != "broker_account_record":
        raise ValueError("Subscription requires an observed account record, not inferred ex-price shares")
    holder = receipt.get("holder_terms")
    if (not isinstance(holder, dict) or not isinstance(holder.get("source_ref"), str)
            or not holder["source_ref"].strip()):
        raise ValueError("Holder subscription terms required separately from ex-price ratio")
    ratio = positive_decimal(holder["rights_ratio"])
    start, end = iso_day(holder["subscription_start"]), iso_day(holder["subscription_end"])
    payment = iso_day(receipt["payment_date"])
    if not terms["registration_date"] < start <= payment <= end < terms["date"]:
        raise ValueError("Rights payment is outside the declared subscription window")
    nominal = quantity * ratio
    entitled = receipt.get("entitled_shares")
    bounds = {int(nominal.to_integral_value(rounding=ROUND_FLOOR)),
              int(nominal.to_integral_value(rounding=ROUND_CEILING))}
    subscribed, allocated = receipt["subscribed_shares"], receipt["allocated_shares"]
    if type(entitled) is not int or entitled not in bounds or not 0 < subscribed <= entitled or allocated > subscribed:
        raise ValueError("Observed entitlement/subscription/allocation quantities disagree")
    price = positive_decimal(terms["subscription_price"])
    principal, allocation_cost = cents(price * subscribed), cents(price * allocated)
    money = {key: receipt.get(key) for key in ("subscription_fees_cents", "refund_cents",
                                             "fee_refund_cents", "net_refund_interest_cents")}
    if (any(type(value) is not int or value < 0 for value in money.values())
            or money["fee_refund_cents"] > money["subscription_fees_cents"]
            or receipt["cash_debit_cents"] != principal + money["subscription_fees_cents"]
            or money["refund_cents"] != principal - allocation_cost + money["fee_refund_cents"] + money["net_refund_interest_cents"]):
        raise ValueError("Rights payment/allocation/refund cash does not reconcile")
    phases = {"payment": payment}
    if allocated:
        allocation, listing = iso_day(receipt["allocation_date"]), iso_day(receipt["listing_date"])
        if not terms["date"] <= allocation <= listing:
            raise ValueError("Known post-ex allocation and listing dates required")
        phases["allocation"] = allocation
    elif receipt.get("allocation_date") is not None or receipt.get("listing_date") is not None:
        raise ValueError("Zero allocation must not invent listing dates")
    if money["refund_cents"]:
        refund = iso_day(receipt["refund_date"])
        if refund < payment:
            raise ValueError("Rights refund precedes payment")
        phases["refund"] = refund
    elif receipt.get("refund_date") is not None:
        raise ValueError("Zero refund must not invent a payment date")
    return phases, principal, allocation_cost


def apply_rights_issue(book, feed, reference, day):
    """Apply known payment/refund/allocation once, leaving absent receipts unresolved."""
    symbol, ex_day = reference["symbol"], reference["date"]
    identifier = f"rights:{symbol}:{ex_day}"
    quantity = book.state["entitlements"].get(identifier)
    if quantity is None and day < ex_day:
        return
    if type(quantity) is not int or quantity < 0:
        raise ValueError(f"Unresolved rights-issue entitlement: {symbol} {day}; registered quantity={quantity}")
    if quantity == 0:
        return
    saved = book.state.get("corporate_action_reconciliations", {}).get(identifier)
    receipt = saved
    if receipt is None:
        reader = getattr(feed, "rights_issue_receipt", None)
        receipt = reader(reference, quantity, book.context) if callable(reader) else None
    if receipt is None:
        if day < ex_day:
            return
        raise ValueError(f"Unresolved rights-issue entitlement: {symbol} {day}; registered quantity={quantity}; "
                         "subscription/allocation reconciliation required")
    if book.state.get("broker_binding"):
        raise PermissionError("Imported broker balances cannot receive simulator rights mutations")
    phases, principal, allocation_cost = checked_receipt(book, reference, quantity, receipt)
    processed = book.state.get("rights_issue_progress", {}).get(identifier, [])
    if any(phase not in phases for phase in processed) or len(set(processed)) != len(processed):
        raise ValueError("Rights processed phases disagree with the receipt")
    if any(when < day and phase not in processed for phase, when in phases.items()):
        raise ValueError("Skipped rights settlement session; full account replay required")
    due = [phase for phase, when in phases.items() if when == day and phase not in processed]
    if "payment" in due and receipt["cash_debit_cents"] > book.cash_available():
        raise ValueError("Insufficient unreserved cash for observed rights subscription")
    receivable = book.state["receivables"].get(identifier, 0) + (principal if "payment" in due else 0)
    released = (allocation_cost if "allocation" in due else 0) + (
        principal - allocation_cost if "refund" in due else 0)
    if released > receivable:
        raise ValueError("Rights allocation/refund exceeds paid subscription receivable")
    book.state.setdefault("corporate_action_reconciliations", {})[identifier] = deepcopy(receipt)
    progress = book.state.setdefault("rights_issue_progress", {}).setdefault(identifier, [])
    for phase in due:
        if phase == "payment":
            book.state["cash"] -= receipt["cash_debit_cents"]
            book.state["receivables"][identifier] = principal
        elif phase == "allocation":
            book.state["receivables"][identifier] -= allocation_cost
            book.state["positions"].setdefault(symbol, []).append(
                {"qty": receipt["allocated_shares"], "available": receipt["listing_date"]})
        elif phase == "refund":
            book.state["receivables"][identifier] -= principal - allocation_cost
            book.state["cash"] += receipt["refund_cents"]
        if identifier in book.state["receivables"] and not book.state["receivables"][identifier]:
            book.state["receivables"].pop(identifier)
        progress.append(phase)
        book.emit(day, "rights_" + phase, symbol=symbol, action_id=identifier,
                  registered_quantity=quantity, decision=receipt["decision"],
                  subscribed_shares=receipt["subscribed_shares"], allocated_shares=receipt["allocated_shares"],
                  cash_debit_cents=receipt["cash_debit_cents"] if phase == "payment" else 0,
                  cash_credit_cents=receipt.get("refund_cents", 0) if phase == "refund" else 0,
                  receivable_cents=book.state["receivables"].get(identifier, 0),
                  source_kind=receipt["source_kind"], source_ref=receipt["source_ref"],
                  valuation_basis="paid_principal_until_observed_allocation_or_refund")
