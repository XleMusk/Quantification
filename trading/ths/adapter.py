"""Bounded prefill. A human owns the final submission in the THS client."""
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from decimal import Decimal
import re
from typing import Any, Protocol
import uuid

from ..config import StrategyConfig
from ..ledger import Ledger, new_state
from ..rules import cents


Selector = Mapping[str, Any]


class PrefillWindow(Protocol):
    """Narrow GUI boundary consumed by the paper-only prefill adapter."""

    def ready(self) -> None: ...

    def check_fields(self, selectors: Sequence[Selector]) -> None: ...

    def text(self, selector: Selector) -> str: ...

    def write(self, selector: Selector, value: str) -> None: ...

    def read_grid(self, grid: Selector) -> list[dict[str, Any]]: ...


def now():
    return datetime.now(timezone.utc)


def money(value):
    text = str(value).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", text):
        raise ValueError("Expected a plain RMB amount, not a formula or rounded research price")
    number = Decimal(text.replace(",", ""))
    if not number.is_finite() or number < 0 or number != number.quantize(Decimal(".01")):
        raise ValueError("Expected a nonnegative RMB amount with at most two decimal places")
    return cents(number)


def fresh(stamp, current=None, seconds=300):
    stamp = datetime.fromisoformat(stamp)
    current = current or now()
    if stamp.tzinfo is None or not 0 <= (current - stamp).total_seconds() <= seconds:
        raise ValueError("Missing, future or stale observation timestamp")


def validate_cash_balance(snapshot):
    keys = {"cash_total", "cash_frozen", "cash_basis"}
    if not keys.intersection(snapshot):
        return False
    if not keys <= snapshot.keys() or snapshot["cash_basis"] != "cash_balance_includes_frozen":
        raise ValueError("Explicit cash balance including frozen funds is required; not assets/withdrawable cash")
    if (any(type(snapshot[k]) is not int for k in ("cash_total", "cash_frozen", "cash_available"))
            or not 0 <= snapshot["cash_frozen"] <= snapshot["cash_total"]
            or snapshot["cash_total"] - snapshot["cash_frozen"] != snapshot["cash_available"]):
        raise ValueError("Cash total/frozen/available balance does not reconcile")
    return True


def validate_snapshot(snapshot, current=None):
    current = current or now()
    if snapshot.get("source") not in {"ths_controls", "ths_user_export"} or not snapshot.get("account_alias"):
        raise ValueError("Use a THS account observation, not a paper/replay balance")
    fresh(snapshot["observed_at"], current)
    if snapshot.get("trading_date") != current.astimezone(timezone(timedelta(hours=8))).date().isoformat():
        raise ValueError("The account trading date must match today's local date; refresh the account view")
    if type(snapshot["cash_available"]) is not int or snapshot["cash_available"] < 0:
        raise ValueError("Cash must be nonnegative integer cents")
    validate_cash_balance(snapshot)
    for symbol, position in snapshot["positions"].items():
        if not re.fullmatch(r"\d{6}\.(?:SSE|SZSE|BSE)", symbol):
            raise ValueError("Holdings require explicit exchange-qualified symbols")
        total, available = position["quantity"], position["sellable"]
        if type(total) is not int or type(available) is not int or not 0 <= available <= total:
            raise ValueError("Unknown/invalid total or sellable quantity")


def create_plan(snapshot, symbol, side, price, qty, *, price_basis, current=None, config=None):
    current = current or now()
    config = (config or StrategyConfig()).validate()
    validate_snapshot(snapshot, current)
    if price_basis != "raw_rmb":
        raise ValueError("Adjusted/research prices cannot enter the RMB order form")
    if not re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", symbol):
        raise ValueError("Use an explicit supported exchange-qualified symbol")
    price_cents = money(price)
    state = new_state(0)
    state["cash"] = snapshot["cash_available"]
    day = current.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    for code, position in snapshot["positions"].items():
        state["positions"][code] = [{"qty": position["sellable"], "available": day},
                                    {"qty": position["quantity"] - position["sellable"], "available": "9999-12-31"}]
    identifier = uuid.uuid4().hex
    # Reuse the existing ledger's lot/cash/T+1 checks, without persisting a fake fill.
    order = Ledger(state, config).submit(identifier, day, symbol, side, qty, price_cents)
    if order["status"] == "rejected":
        raise ValueError(order["reason"])
    return {"id": identifier, "account_alias": snapshot["account_alias"], "symbol": symbol, "side": side,
            "price_cents": price_cents, "qty": qty, "price_basis": price_basis,
            "created_at": current.isoformat(), "expires_at": (current + timedelta(minutes=5)).isoformat(),
            "snapshot": deepcopy(snapshot), "execution_config": config.to_dict(),
            "status": "planned", "submission": "manual_only"}


def from_target(snapshot, target, *, current=None, config=None):
    """A current strategy's absolute share target, not historical replay weights."""
    current = current or now()
    if target.get("mode") != "current_preview" or not target.get("strategy_id"):
        raise ValueError("Only explicitly reviewed current-preview targets are accepted")
    fresh(target["generated_at"], current)
    if type(target["target_qty"]) is not int or target["target_qty"] < 0:
        raise ValueError("Target must be a nonnegative absolute share quantity")
    held = snapshot["positions"].get(target["symbol"], {}).get("quantity", 0)
    delta = target["target_qty"] - held
    if not delta:
        raise ValueError("No position change required")
    plan = create_plan(snapshot, target["symbol"], "buy" if delta > 0 else "sell",
                       target["limit_price_rmb"], abs(delta), price_basis=target["price_basis"],
                       current=current, config=config)
    plan["target"] = deepcopy(target)
    return plan


def from_portfolio(snapshot, portfolio, *, current=None, config=None):
    """Preview a complete share target; only its next step can be saved."""
    current = current or now()
    config = (config or StrategyConfig()).validate()
    validate_snapshot(snapshot, current)
    if (portfolio.get("mode") != "current_preview"
            or not isinstance(portfolio.get("strategy_id"), str)
            or not portfolio["strategy_id"].strip()):
        raise ValueError("Only explicitly reviewed current-preview portfolios are accepted")
    if portfolio.get("account_alias") != snapshot["account_alias"]:
        raise ValueError("Portfolio account alias mismatch")
    fresh(portfolio["generated_at"], current)
    targets = portfolio.get("targets")
    if not isinstance(targets, list):
        raise ValueError("Portfolio targets must be a complete list of absolute share quantities")
    indexed = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Invalid portfolio target")
        symbol = target.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}\.(?:SSE|SZSE|BSE)", symbol):
            raise ValueError("Targets require explicit exchange-qualified symbols")
        if symbol in indexed:
            raise ValueError("Duplicate portfolio target")
        if type(target.get("target_qty")) is not int or target["target_qty"] < 0:
            raise ValueError("Target must be a nonnegative absolute share quantity")
        indexed[symbol] = target
    missing = {symbol for symbol, value in snapshot["positions"].items()
               if value["quantity"] > 0} - indexed.keys()
    if missing:
        raise ValueError(f"Explicit targets required for all held securities: {', '.join(sorted(missing))}")

    adjustments, plans = [], {}
    rejections = {"insufficient_cash", "insufficient_sellable_T1", "buy_lot_must_be_100",
                  "odd_lot_must_be_full_exit", "quantity_above_board_order_limit"}
    for symbol, target in sorted(indexed.items()):
        held = snapshot["positions"].get(symbol, {}).get("quantity", 0)
        delta = target["target_qty"] - held
        item = {"symbol": symbol, "held_qty": held, "target_qty": target["target_qty"],
                "delta_qty": delta, "side": "buy" if delta > 0 else "sell" if delta < 0 else None,
                "status": "no_change" if not delta else "waiting_for_prior_step"}
        adjustments.append(item)
        if not delta:
            continue
        # Only the observed account funds this check; never credit hypothetical sells.
        single = {**target, "mode": portfolio["mode"], "strategy_id": portfolio["strategy_id"],
                  "generated_at": portfolio["generated_at"]}
        try:
            plans[symbol] = from_target(snapshot, single, current=current, config=config)
        except ValueError as error:
            if str(error) not in rejections:
                raise
            item["validation_reason"] = str(error)
    adjustments.sort(key=lambda item: (0 if item["side"] == "sell" else 1 if item["side"] == "buy" else 2,
                                      item["symbol"]))
    changes = [item for item in adjustments if item["delta_qty"]]
    result = {"account_alias": snapshot["account_alias"], "strategy_id": portfolio["strategy_id"],
              "generated_at": portfolio["generated_at"], "account_observed_at": snapshot["observed_at"],
              "cash_available_cents": snapshot["cash_available"], "adjustments": adjustments,
              "status": "no_change", "next_plan": None, "saved": False,
              "execution_config": config.to_dict(),
              "submission": "manual_only", "client_interacted": False, "execution_admitted": False}
    if not changes:
        return result
    if changes[0]["side"] == "sell":
        for item in changes:
            if item["side"] == "buy":
                item["status"] = "waiting_for_sales"
    first = changes[0]
    if "validation_reason" in first:
        first["status"] = "blocked"
        result.update(status="blocked", reason=first["validation_reason"])
        return result
    first["status"] = "next_plan"
    plan = plans[first["symbol"]]
    plan["portfolio"] = deepcopy(portfolio)
    result.update(status="awaiting_review", next_plan=plan)
    return result


def check_plan(plan, current=None):
    current = current or now()
    if plan.get("submission") != "manual_only" or plan.get("status") not in {"planned", "prefilling"}:
        raise ValueError("Plan is not eligible for one-time prefill")
    if type(plan["price_cents"]) is not int or not re.fullmatch(r"[a-f0-9]{32}", plan["id"]):
        raise ValueError("Invalid stored plan identity or price")
    fresh(plan["created_at"], current)
    if "target" in plan:
        fresh(plan["target"]["generated_at"], current)
    if current > datetime.fromisoformat(plan["expires_at"]):
        raise ValueError("Expired preview plan")
    config = StrategyConfig(**plan.get("execution_config", StrategyConfig().to_dict())).validate()
    if ("strategy_signal" in plan
            and config.to_dict() != plan["strategy_signal"]["selector_config"]):
        raise ValueError("Signal execution config differs from its selector; regenerate the plan")
    verified = create_plan(plan["snapshot"], plan["symbol"], plan["side"],
                           Decimal(plan["price_cents"]) / 100, plan["qty"], price_basis=plan["price_basis"],
                           current=current, config=config)
    if verified["account_alias"] != plan["account_alias"]:
        raise ValueError("Account mismatch")


def _prefill_values(plan):
    return {
        "symbol": plan["symbol"].split(".")[0],
        "price": f"{Decimal(plan['price_cents']) / 100:.2f}",
        "qty": str(plan["qty"]),
    }


def build_prefill_payload(plan, current=None):
    """Build the exact three-field THS payload without touching a client window."""
    check_plan(plan, current=current)
    return _prefill_values(plan)


def _readback_mismatch_category(field):
    return {
        "symbol": "code_mismatch",
        "price": "price_mismatch",
        "qty": "quantity_mismatch",
    }.get(field, "other_mismatch")


def compare_prefill_readback(plan, readback, current=None):
    """Return field-level differences without treating a mismatch as a fill."""
    expected = build_prefill_payload(plan, current=current)
    received = readback if isinstance(readback, dict) else {}
    missing_fields = [field for field in expected if field not in received]
    field_status = {}
    mismatch_categories = {}
    mismatches = [
        {"field": field, "category": (
            "missing" if field not in received else _readback_mismatch_category(field)
        ), "expected": expected[field], "received": received.get(field)}
        for field in ("symbol", "price", "qty")
        if received.get(field) != expected[field]
    ]
    for field in expected:
        if field not in received:
            field_status[field] = "missing"
        elif received[field] == expected[field]:
            field_status[field] = "matched"
        else:
            category = _readback_mismatch_category(field)
            field_status[field] = category
            mismatch_categories.setdefault(category, []).append(field)
    for field in missing_fields:
        mismatch_categories.setdefault("missing", []).append(field)
    unexpected_fields = sorted(set(received) - set(expected))
    return {"status": "readback_verified" if not mismatches else "readback_mismatch",
            "expected": deepcopy(expected), "readback": deepcopy(readback),
            "mismatches": mismatches, "mismatch_count": len(mismatches),
            "readback_complete": not missing_fields,
            "missing_fields": missing_fields, "unexpected_fields": unexpected_fields,
            "field_status": field_status, "mismatch_categories": mismatch_categories,
            "submitted": False, "execution_admitted": False}


def verify_prefill_readback(plan, readback, current=None):
    """Verify a captured form readback against the reviewed plan; never submit."""
    result = compare_prefill_readback(plan, readback, current=current)
    if result["mismatches"]:
        details = "; ".join(
            f"{item['field']} expected={item['expected']!r} received={item['received']!r}"
            for item in result["mismatches"]
        )
        raise ValueError(f"Final form mismatch: {details}")
    return result


def gui_prefill_profile_status(profile):
    """Classify a profile without upgrading it into a verified GUI mapping."""
    if not isinstance(profile, dict):
        return {"status": "invalid_profile", "schema": None, "missing_fields": ["symbol", "price", "qty"],
                "verified_local": False, "account_alias_present": False}
    fields = profile.get("fields") if isinstance(profile.get("fields"), dict) else {}
    missing = [key for key in ("symbol", "price", "qty") if key not in fields]
    if profile.get("schema") == "ths_observation_bundle_profile_v1":
        status = "observation_only_profile"
    elif missing:
        status = "missing_gui_field_mappings"
    elif profile.get("verified_local") is not True:
        status = "unverified_local_mapping"
    elif not profile.get("account_alias"):
        status = "missing_account_alias"
    else:
        status = "ready"
    return {
        "status": status,
        "schema": profile.get("schema"),
        "missing_fields": missing,
        "verified_local": profile.get("verified_local") is True,
        "account_alias_present": bool(profile.get("account_alias")),
    }


def validate_gui_prefill_profile(profile):
    """Reject observation-table profiles before they can be used as GUI maps."""
    status = gui_prefill_profile_status(profile)
    if status["status"] == "invalid_profile":
        raise ValueError("THS GUI prefill profile must be a JSON object")
    if status["status"] == "observation_only_profile":
        raise ValueError(
            "A structured observation profile cannot drive GUI prefill; provide a verified_local "
            "THS GUI profile with account_alias and fields.symbol/price/qty"
        )
    if status["missing_fields"]:
        raise ValueError(
            "THS GUI prefill profile is missing field mappings: "
            + ", ".join(sorted(status["missing_fields"]))
        )
    return profile


class PrefillAdapter:
    """Validate and write a reviewed THS form without submitting it."""

    def __init__(self, window: PrefillWindow, profile: Mapping[str, Any]) -> None:
        self.window, self.profile = window, profile

    def guard(self, plan: Mapping[str, Any]) -> None:
        self.window.ready()
        if self.profile.get("verified_local") is not True:
            raise ValueError("Local control mapping has not been verified")
        if self.profile["account_alias"] != plan["account_alias"]:
            raise ValueError("Account alias mismatch")
        fields = validate_gui_prefill_profile(self.profile)["fields"]
        if len({str(sorted(fields[k].items())) for k in ("symbol", "price", "qty")}) != 3:
            raise ValueError("Order fields must be three distinct controls")
        self.window.check_fields([fields[k] for k in ("symbol", "price", "qty")])
        guards = self.profile["guards"]
        for key in ("account", "side", "price_mode"):
            expected = guards[key]["expected"]
            if key == "side":
                expected = expected[plan["side"]]
            if not expected or self.window.text(guards[key]["selector"]) != expected:
                raise ValueError(f"{key} guard mismatch; user must select the correct account/order page")

    def prefill(
        self,
        plan: Mapping[str, Any],
        stop_check: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        check_plan(plan)
        values = _prefill_values(plan)
        for field, value in values.items():
            if stop_check():
                raise InterruptedError("Paused; no further desktop writes")
            check_plan(plan)
            self.guard(plan)
            self.window.write(self.profile["fields"][field], value)
            if self.window.text(self.profile["fields"][field]) != value:
                raise ValueError(f"{field} readback mismatch; partial form may remain, do not submit")
        if stop_check():
            raise InterruptedError("Paused before final verification")
        self.guard(plan)
        readback = {key: self.window.text(selector) for key, selector in self.profile["fields"].items() if key in values}
        verified = verify_prefill_readback(plan, readback)
        return {**verified, "status": "awaiting_manual_submission", "verified_at": now().isoformat()}

    def read(self, kind: str) -> dict[str, Any]:
        self.window.ready()
        if self.profile.get("verified_local") is not True:
            raise ValueError("Local control mapping has not been verified")
        view = self.profile["views"][kind]
        for guard in (self.profile["guards"]["account"], view["guard"]):
            if self.window.text(guard["selector"]) != guard["expected"]:
                raise ValueError("Account/query page mismatch")
        rows = self.window.read_grid(view["grid"])
        cash = self.window.text(view["cash_available"]) if kind == "account" else None
        balances = {}
        if kind == "account" and ("cash_total" in view or "cash_frozen" in view):
            if not view.get("cash_total") or not view.get("cash_frozen"):
                raise ValueError("Both inspected total/frozen cash selectors are required")
            balances = {key: self.window.text(view[key]) for key in ("cash_total", "cash_frozen")}
        self.window.ready()
        for guard in (self.profile["guards"]["account"], view["guard"]):
            if self.window.text(guard["selector"]) != guard["expected"]:
                raise ValueError("Account/query page changed during read")
        return {"rows": rows, "cash_available": cash, **balances}
