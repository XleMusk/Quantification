"""Daily replay uses the same targets, order ledger and fills as paper execution."""
from copy import deepcopy
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import math
from numbers import Real

from .contracts import (ExecutionEvent, IntentKind, OrderIntent, PortfolioIntent, PreopenPortfolioIntent, Price, PriceBasis, RunContext,
                        SecurityId, ShareQuantity, SignalFrame, SignalKind, SignalUnavailable, TradingMode, decision_time, MARKET_TZ)
from .expected_returns import checked_return_intent
from .ledger import ACTIVE, Ledger
from .portfolio_allocation import LOT_ALLOCATION, OPEN_LOT_ALLOCATION, allocate_close_lots, allocate_open_lots
from .entry_risk import entry_risk_contract, notice_entry_block, raw_quote_entry_block, validate_entry_risk_binding
from .rules import cents, holder_bonus_ratio_bounds, listing_limit_reason, notional_cents, quote_value
from .rights_issue import apply_rights_issue, validate_rights_binding
from .simulated_broker import SimulatedBrokerAdapter
from .event_frontier import validate_signal_frontier
from . import target_exit_lifecycle as full_exits
from .research import order_capacity_replay as order_capacity
from .published_close_router import (PublishedCloseRouterEnvelope, BINDING_SCHEMA as ROUTER_BINDING_SCHEMA,
    RECEIPT_SCHEMA as ROUTER_RECEIPT_SCHEMA, OWNER_STATE_KEY as ROUTER_OWNER_STATE,
    checked_receipt as checked_router_receipt)


PUBLISHED_CLOSE_INTENT_SCHEMA = "replay_published_close_portfolio_intent_v1"
PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA = "replay_published_close_reduced_portfolio_intent_v1"
PUBLISHED_CLOSE_TARGET_POLICY_SCHEMA = "replay_published_close_target_policy_v1"
PREOPEN_BINDING_SCHEMA = "simulation_preopen_execution_binding_v1"
PREOPEN_RECEIPT_SCHEMA = "simulation_preopen_portfolio_intent_receipt_v1"


def _published_now():
    return datetime.now(MARKET_TZ)


def _checked_preopen(envelope, day, feed, config, context):
    if (not isinstance(envelope, PreopenPortfolioIntent) or not isinstance(context, RunContext)
            or context.mode not in {TradingMode.REPLAY, TradingMode.PAPER}
            or envelope.intent.context != context or envelope.execution_day != day):
        raise ValueError("Preopen intent must match this exact simulated account, mode and execution day")
    calendar = list(feed.calendar)
    if day not in calendar or calendar != sorted(set(calendar)):
        raise ValueError("Preopen execution requires a pinned ordered exact session calendar")
    index = calendar.index(day)
    if (index == 0 or envelope.source_day != calendar[index - 1]
            or feed.next_day(envelope.source_day) != day):
        raise ValueError("Preopen source close must be the exact previous exchange session")
    if datetime.fromisoformat(envelope.published_at.replace("Z", "+00:00")) > _published_now():
        raise ValueError("A future preopen publication cannot be executed")
    if (any(weight > config.single_limit + 1e-12 for _, weight in envelope.intent.weights)
            or sum(weight for _, weight in envelope.intent.weights) > config.gross_limit + 1e-12):
        raise ValueError("Preopen target exceeds the unchanged position caps")
    return {"schema": PREOPEN_RECEIPT_SCHEMA, "source_day": envelope.source_day,
        "execution_day": day, "published_at": envelope.published_at, "envelope": envelope.to_dict(),
        "input_kind": "explicit_PreopenPortfolioIntent_not_EOD_SignalFrame",
        "source_availability_verified_by_engine": False, "source_verified": False,
        "pit_verified": False, "execution_admitted": False}


def _preopen_frontier(state, envelope, day, feed, config, context, *, opening_only=False):
    saved = state.get("preopen_execution_binding")
    if envelope is None:
        if saved is not None or "last_preopen_intent" in state:
            raise ValueError("An established preopen account needs a new explicit envelope every session")
        return None, None
    receipt = _checked_preopen(envelope, day, feed, config, context)
    binding = {"schema": PREOPEN_BINDING_SCHEMA, "context": context.to_dict(), "config": config.to_dict()}
    if (state.get("pending_targets") is not None or state.get("pending_published_close_intent") is not None
            or state.get("pending_risk_exits") is not None
            or state.get("published_close_target_policy") is not None
            or state.get("replay_risk_exit_policy") is not None):
        raise ValueError("Preopen execution cannot replace an existing pending target, published-close path or risk policy")
    if state["day"] is not None and state["day"] != envelope.source_day:
        raise ValueError("A resumed preopen account cannot skip the exact source session")
    if saved is not None:
        if saved != binding or not isinstance(state.get("last_preopen_intent"), dict) or state["day"] is None:
            raise ValueError("The restored preopen context/configuration or receipt changed")
        prior = state["last_preopen_intent"]
        if prior != _checked_preopen(PreopenPortfolioIntent.from_dict(prior["envelope"]), state["day"], feed, config, context):
            raise ValueError("The last preopen receipt does not match the processed checkpoint session")
    elif "last_preopen_intent" in state:
        raise ValueError("A restored preopen receipt cannot lose its execution binding")
    required = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    required.update(symbol for symbol, _ in envelope.intent.weights)
    residual_exits = (state.get("full_exit_lifecycle") or {}).get("remaining_full_exits", {})
    rows = feed.rows.get(day, {})
    required_prices = ("open", "preclose") if opening_only else ("open", "close", "preclose")
    for symbol in required:
        row = rows.get(symbol)
        if (row is None or symbol not in feed.instruments or str(row.get("adjustflag")) != "3"
                or row.get("tradestatus") not in {0, 1} or row.get("isST") not in {0, 1}
                or any(not isinstance(row.get(field), Real) or isinstance(row[field], bool)
                    or not math.isfinite(row[field]) or row[field] <= 0
                    for field in required_prices)):
            raise ValueError("Preopen holdings and targets require explicit current raw prices and known status before mutation")
        # A reported suspension is valuation-only. Pure NO_CHANGE retains
        # exact shares unless a saved exit or ST rule can actually sell them.
        # Neither case uses a participation budget, so missing native volume
        # must stay missing rather than stop an otherwise complete session.
        if row["tradestatus"] == 0 or (envelope.intent.kind == IntentKind.NO_CHANGE
                and symbol not in residual_exits and row["isST"] == 0):
            continue
        try:
            volume = feed.volumes.at[envelope.source_day, symbol]
        except (KeyError, AttributeError) as error:
            raise ValueError("Preopen execution needs the exact prior-session participation volume") from error
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Preopen prior-session participation volume must be known and nonnegative")
    return receipt, binding


def _published_close_receipt(intent, day, feed, config, context):
    """A real evening decision is separate from the fixed 15:05 SignalFrame.

    Source/model proof remains the publisher consumer's responsibility. This
    receipt preserves the typed intent and actual decision clock, not a claim
    that source availability, PIT or account execution has been admitted.
    """
    if (not isinstance(intent, PortfolioIntent) or not isinstance(context, RunContext) or context.mode != TradingMode.REPLAY
            or intent.context != context or intent.kind not in {IntentKind.FULL_TARGET, IntentKind.NO_CHANGE}
            or not intent.decided_at or not intent.execution_not_before or not intent.source):
        raise ValueError("Published close intent requires explicit replay context, supported kind and complete metadata")
    calendar = list(feed.calendar)
    index = calendar.index(day)
    if index + 1 >= len(calendar):
        raise ValueError("Published close intent requires the exact next feed session")
    execution_day = calendar[index + 1]
    if intent.execution_not_before != execution_day or feed.next_day(day) != execution_day or execution_day <= day:
        raise ValueError("Published close intent does not match the exact next feed session")
    decided = datetime.fromisoformat(intent.decided_at.replace("Z", "+00:00")).astimezone(MARKET_TZ)
    opening = datetime.fromisoformat(execution_day + "T09:30:00+08:00")
    if (decided.date().isoformat() != day or decided < datetime.fromisoformat(decision_time(day))
            or decided >= opening or decided > _published_now()):
        raise ValueError("Published close intent violates its actual decision/next-open clock")
    if (any(weight > config.single_limit + 1e-12 for _, weight in intent.weights)
            or sum(weight for _, weight in intent.weights) > config.gross_limit + 1e-12):
        raise ValueError("Published close intent exceeds the unchanged position caps")
    return {"schema": PUBLISHED_CLOSE_INTENT_SCHEMA, "decision_day": day, "decision_at": intent.decided_at,
            "execution_not_before": execution_day, "intent": intent.to_dict(),
            "input_kind": "explicit_published_close_PortfolioIntent_not_SignalFrame",
            "source_availability_verified_by_engine": False, "source_verified": False,
            "pit_verified": False, "execution_admitted": False}


def _pending_published_close_receipt(state, feed, day, config, context):
    receipt = state.get("pending_published_close_intent")
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise ValueError("Invalid persisted published close intent")
    if receipt.get("schema") == ROUTER_RECEIPT_SCHEMA:
        envelope = PublishedCloseRouterEnvelope.from_dict(receipt.get("envelope"))
        result = PortfolioIntent.from_dict(receipt.get("intent"))
        binding = {"schema": ROUTER_BINDING_SCHEMA, "policy_id": envelope.policy_id,
                   "context": context.to_dict(), "config": config.to_dict()}
        _checked_router_envelope(envelope, envelope.source_day, feed, config, context)
        if (envelope.source_day != state["day"] or envelope.execution_day != day
                or result.kind != IntentKind.FULL_TARGET or state.get("published_close_router_policy") != binding
                or receipt != checked_router_receipt(envelope, result)
                or state["pending_targets"] != result.to_legacy_pending()):
            raise ValueError("Pending router publication, binding or exact target changed")
        return receipt
    if receipt.get("schema") == PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA:
        original = PortfolioIntent.from_dict(receipt.get("original_intent"))
        reduced = PortfolioIntent.from_dict(receipt.get("reduced_intent"))
        decision_day = receipt.get("decision_day")
        binding = {"schema": PUBLISHED_CLOSE_TARGET_POLICY_SCHEMA, "policy_id": receipt.get("policy_id"),
                   "context": context.to_dict()}
        if (decision_day != state["day"] or receipt.get("execution_not_before") != day
                or reduced.kind != IntentKind.FULL_TARGET):
            raise ValueError("Pending published close intent cannot skip or change its exact opening session")
        checked = _published_close_reduction_receipt(original, reduced, decision_day, feed, config, context,
                                                    receipt.get("policy_id"))
        if (state.get("published_close_target_policy") != binding or receipt != checked
                or state["pending_targets"] != reduced.to_legacy_pending()):
            raise ValueError("Pending published close reduction or legacy target weights changed")
        return receipt
    if receipt.get("schema") != PUBLISHED_CLOSE_INTENT_SCHEMA:
        raise ValueError("Invalid persisted published close intent")
    intent = PortfolioIntent.from_dict(receipt.get("intent"))
    decision_day = receipt.get("decision_day")
    if (decision_day != state["day"] or receipt.get("execution_not_before") != day
            or intent.kind != IntentKind.FULL_TARGET):
        raise ValueError("Pending published close intent cannot skip or change its exact opening session")
    checked = _published_close_receipt(intent, decision_day, feed, config, context)
    if receipt != checked or state["pending_targets"] != intent.to_legacy_pending():
        raise ValueError("Pending published close metadata or legacy target weights changed")
    return receipt


def _checked_router_envelope(envelope, day, feed, config, context):
    if (not isinstance(envelope, PublishedCloseRouterEnvelope) or envelope.source_day != day
            or envelope.original_intent.context != context or context.mode != TradingMode.REPLAY):
        raise ValueError("Router publication must match this exact simulated account and source day")
    calendar = list(feed.calendar)
    if day not in calendar or calendar != sorted(set(calendar)):
        raise ValueError("Router publication needs an exact ordered session calendar")
    index = calendar.index(day)
    if (index + 1 >= len(calendar) or calendar[index + 1] != envelope.execution_day
            or feed.next_day(day) != envelope.execution_day):
        raise ValueError("Router E must be the exact next exchange session; E quotes are not required for D")
    if datetime.fromisoformat(envelope.published_at.replace("Z", "+00:00")) > _published_now():
        raise ValueError("A future router publication cannot be consumed")
    if (config.holdings != 5 or config.gross_limit != .8 or config.single_limit < .16
            or any(weight > config.single_limit + 1e-12 for _, weight in envelope.original_intent.weights)):
        raise ValueError("Router publication requires the registered unchanged five-slot capital limits")


def _router_close_binding(state, envelope, policy, day, feed, config, context):
    saved = state.get("published_close_router_policy")
    if envelope is None and policy is None:
        if saved is not None:
            raise ValueError("An established published-close router needs its explicit envelope and callback every session")
        return None
    _checked_router_envelope(envelope, day, feed, config, context)
    if not callable(policy) or getattr(policy, "policy_id", None) != envelope.policy_id:
        raise ValueError("An explicit registered post-open router callback is required")
    binding = {"schema": ROUTER_BINDING_SCHEMA, "policy_id": envelope.policy_id,
               "context": context.to_dict(), "config": config.to_dict()}
    if saved != binding and (saved is not None or state["day"] is not None):
        raise ValueError("Published-close router method cannot change or be introduced into an existing account")
    prior = state.get("last_published_close_router")
    if saved is not None:
        if not isinstance(prior, dict):
            raise ValueError("Restored router account requires its original last publication receipt")
        previous = PublishedCloseRouterEnvelope.from_dict(prior.get("envelope"))
        previous_result = PortfolioIntent.from_dict(prior.get("intent"))
        _checked_router_envelope(previous, previous.source_day, feed, config, context)
        if previous.source_day != state["day"] or prior != checked_router_receipt(previous, previous_result):
            raise ValueError("Restored router publication receipt changed")
    elif prior is not None:
        raise ValueError("A router receipt cannot lose its registered binding")
    owner = (state.get(ROUTER_OWNER_STATE) or {}).get("active_type")
    exposed = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
    exposed.update(symbol for symbol, weight in (state.get("pending_targets") or {}).items() if weight > 0)
    if owner != envelope.owner_before or exposed != set(envelope.owner_symbols):
        raise ValueError("Router publication is not bound to the original owner exposure before this opening")
    return binding


def _published_close_target_binding(state, policy, context, intent, *, allow_absent_intent=False):
    """A published-target reduction is an explicit, immutable replay method."""
    saved = state.get("published_close_target_policy")
    if policy is None:
        if saved is not None:
            raise ValueError("An existing published-close target policy cannot be removed")
        return None
    policy_id = getattr(policy, "policy_id", None)
    if (context.mode != TradingMode.REPLAY or not callable(policy)
            or not isinstance(policy_id, str) or not policy_id.strip()):
        raise ValueError("Published-close target policy must be identified, callable and replay-only")
    binding = {"schema": PUBLISHED_CLOSE_TARGET_POLICY_SCHEMA, "policy_id": policy_id,
               "context": context.to_dict()}
    if saved != binding and (saved is not None or state["day"] is not None):
        raise ValueError("An existing published-close target policy cannot be changed or introduced")
    if intent is None and not allow_absent_intent:
        raise ValueError("A published-close target policy requires an explicit publication on every session")
    return binding


def _published_close_reduction_receipt(original, reduced, day, feed, config, context, policy_id):
    """Preserve the real publication separately from its reduce-only execution target."""
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("Published-close target reduction requires its stable policy identity")
    receipt = _published_close_receipt(original, day, feed, config, context)
    # Reuse the ordinary replay overlay's reduction contract without calling the
    # policy again during checkpoint validation.
    reduce_replay_target(original, lambda value: reduced)
    if reduced.execution_not_before != original.execution_not_before or reduced.source != original.source:
        raise ValueError("Published-close target policy changed publication source or exact execution session")
    receipt["schema"] = PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA
    receipt["original_intent"] = receipt.pop("intent")
    receipt["reduced_intent"] = reduced.to_dict()
    receipt["policy_id"] = policy_id
    return receipt


@dataclass(frozen=True)
class ReplayRiskExitIntent:
    """A separate next-session reduction of existing shares, never a weight target."""
    context: RunContext
    decided_on: str
    execution_on: str
    symbols: tuple[str, ...]
    source: str
    reason: str

    def __post_init__(self):
        if not isinstance(self.context, RunContext) or self.context.mode != TradingMode.REPLAY:
            raise ValueError("Risk exits require an explicit replay context")
        for value in (self.decided_on, self.execution_on):
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Risk exit dates must be canonical sessions")
        if self.execution_on <= self.decided_on:
            raise ValueError("Risk exits must execute after their decision session")
        if (not isinstance(self.symbols, tuple) or not self.symbols
                or len(set(self.symbols)) != len(self.symbols)):
            raise ValueError("Risk exits need unique immutable held symbols")
        for symbol in self.symbols:
            SecurityId(symbol)
        if any(not isinstance(value, str) or not value for value in (self.source, self.reason)):
            raise ValueError("Risk exits require an independent source and reason")

    def to_dict(self):
        return {"schema": "replay_risk_exit_v1", "context": self.context.to_dict(),
                "decided_on": self.decided_on, "execution_on": self.execution_on,
                "symbols": list(self.symbols), "source": self.source, "reason": self.reason}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("schema") != "replay_risk_exit_v1":
            raise ValueError("Unsupported pending replay risk exit")
        return cls(RunContext.from_dict(value["context"]), value["decided_on"], value["execution_on"],
                   tuple(value["symbols"]), value["source"], value["reason"])


def _replay_risk_exit_binding(state, policy, context):
    """Opt-in identity cannot be introduced, changed or dropped mid-account."""
    saved = state.get("replay_risk_exit_policy")
    if policy is None:
        if saved is not None or state.get("pending_risk_exits") is not None:
            raise ValueError("An existing replay risk-exit policy cannot be removed")
        return None
    if (context.mode != TradingMode.REPLAY or not callable(policy)
            or not isinstance(getattr(policy, "policy_id", None), str) or not policy.policy_id):
        raise ValueError("Independent risk-exit policy is explicit, identified and replay-only")
    binding = {"schema": "replay_risk_exit_policy_v1", "policy_id": policy.policy_id,
               "context": context.to_dict()}
    if saved != binding and (saved is not None or state["day"] is not None):
        raise ValueError("An existing replay risk-exit policy cannot be changed")
    return binding


def _schedule_replay_risk_exit(book, feed, day, policy):
    """Queue the existing typed risk decision without executing any orders."""
    if policy is None:
        return
    state, context = book.state, book.context
    risk_intent = policy(day, book, feed)
    if isinstance(risk_intent, SignalUnavailable):
        book.emit(day, "risk_exit_deferred", reason=risk_intent.reason)
    elif risk_intent is not None:
        if not isinstance(risk_intent, ReplayRiskExitIntent):
            raise ValueError("Independent risk-exit policy must return a typed share-reduction intent")
        next_index = feed.calendar.index(day) + 1
        if (risk_intent.context != context or risk_intent.decided_on != day
                or next_index >= len(feed.calendar) or risk_intent.execution_on != feed.calendar[next_index]
                or risk_intent.execution_on != feed.next_day(day)
                or any(book.quantity(symbol) <= 0 for symbol in risk_intent.symbols)):
            raise ValueError("Risk exit changed account/session identity or included a nonheld symbol")
        state["pending_risk_exits"] = risk_intent.to_dict()
        book.emit(day, "risk_exit_scheduled", execution_not_before=risk_intent.execution_on,
                  symbols=list(risk_intent.symbols), source=risk_intent.source, reason=risk_intent.reason)


def choose_targets(candidates, held, config):
    ranks = {row["symbol"]: i + 1 for i, row in enumerate(candidates)}
    selected = [s for s in held if ranks.get(s, math.inf) <= config.retain_rank]
    selected.sort(key=lambda s: ranks[s])
    selected = selected[:config.holdings]
    for row in candidates:
        if len(selected) >= config.holdings:
            break
        if row["symbol"] not in selected:
            selected.append(row["symbol"])
    # A shortage of candidates leaves cash rather than enlarging the survivors.
    weight = min(config.single_limit, config.gross_limit / config.holdings)
    return {symbol: weight for symbol in selected}


def _close_candidate_binding(state, feed, context):
    method = getattr(type(feed), "close_signal_frame", None)
    policy = getattr(feed, "close_candidate_policy", None)
    binding = None
    if method is not None or policy is not None:
        if (context.mode != TradingMode.REPLAY or not callable(method)
                or type(policy) is not dict or set(policy) != {"id", "capacity", "capital_policy"}
                or any(type(value) is not str or not value.strip() for value in policy.values())):
            raise ValueError("Close candidate filtering requires a declared replay hook and explicit policy binding")
        binding = deepcopy(policy)
    saved = state.get("close_candidate_policy")
    if saved != binding and (saved is not None or state["day"] is not None):
        raise ValueError("Close candidate policy cannot change within an existing account")
    return method, binding


def _checked_close_signal_frame(original, filtered):
    if isinstance(filtered, SignalUnavailable):
        return filtered
    if not isinstance(filtered, SignalFrame):
        raise TypeError("Close candidate hook must return SignalFrame or SignalUnavailable")
    if (filtered.day != original.day or filtered.source != original.source
            or filtered.context != original.context):
        raise ValueError("Close candidate hook changed the source, session or account")
    remaining = iter(original.signals)
    for signal in filtered.signals:
        if not any(signal == candidate for candidate in remaining):
            raise ValueError("Close candidates must be an unchanged ordered subsequence of the original signals")
    return filtered


def shares_for_target(equity_cents, weight, price, rule):
    """Absolute share target using the engine's existing quote/lot arithmetic."""
    if not price:
        return 0
    step = rule.quantity.step
    if rule.price_unit == "mill":
        return int(Decimal(equity_cents) * Decimal(str(weight)) * 10 / price / step) * step
    return int(equity_cents * weight / price / step) * step


def reduce_replay_target(intent, policy):
    """An optional research overlay may reduce a target, never change its identity."""
    if (not isinstance(intent, PortfolioIntent) or intent.context is None
            or intent.context.mode != TradingMode.REPLAY or intent.kind != IntentKind.FULL_TARGET):
        raise ValueError("Target policy requires a replay full target")
    result = policy(intent)
    if not isinstance(result, PortfolioIntent):
        raise ValueError("Target policy requires a typed portfolio intent")
    if result.context != intent.context or result.decided_at != intent.decided_at:
        raise ValueError("Target policy changed account/mode or decision time")
    if result.kind == IntentKind.NO_CHANGE:
        return result
    if result.kind != IntentKind.FULL_TARGET or result.execution_not_before != intent.execution_not_before:
        raise ValueError("Target policy changed kind or next-session execution")
    original = dict(intent.weights)
    if any(symbol not in original or weight > original[symbol] for symbol, weight in result.weights):
        raise ValueError("Target policy may only reduce existing weights")
    return result


def allocate_replay_target(intent, policy, config):
    """Explicit replay allocation within the admitted candidates and risk caps."""
    if (not isinstance(intent, PortfolioIntent) or intent.context is None
            or intent.context.mode != TradingMode.REPLAY or intent.kind != IntentKind.FULL_TARGET):
        raise ValueError("Portfolio allocation requires a replay full target")
    result = policy(intent)
    if not isinstance(result, PortfolioIntent):
        raise ValueError("Portfolio allocation requires a typed portfolio intent")
    if result.context != intent.context or result.decided_at != intent.decided_at:
        raise ValueError("Portfolio allocation changed account/mode or decision time")
    if result.kind == IntentKind.NO_CHANGE:
        return result
    if result.kind != IntentKind.FULL_TARGET or result.execution_not_before != intent.execution_not_before:
        raise ValueError("Portfolio allocation changed kind or next-session execution")
    admitted = dict(intent.weights)
    if (any(symbol not in admitted or weight > config.single_limit + 1e-12 for symbol, weight in result.weights)
            or sum(weight for _, weight in result.weights) > config.gross_limit + 1e-12):
        raise ValueError("Portfolio allocation exceeds admitted candidates or position caps")
    return result


def ordinary_share_terms(action):
    try:
        amounts = {field: Decimal(str(action[field])) for field in
                   ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")}
    except (KeyError, InvalidOperation) as error:
        raise ValueError("Unresolved ordinary allocation terms") from error
    if any(not value.is_finite() or value < 0 for value in amounts.values()):
        raise ValueError("Unresolved ordinary allocation terms")
    return {"symbol": action["symbol"], "date": action["dividOperateDate"],
            "registration_date": action["dividRegistDate"], "listing_date": action["dividStockMarketDate"],
            **{field: str(value.normalize()) for field, value in amounts.items()}}


def ordinary_share_allocation(book, feed, action, quantity, nominal):
    """A fractional ratio is not this account's observed integer allocation."""
    symbol, day = action["symbol"], action["dividOperateDate"]
    if (action.get("action_kind") == "restricted_restructuring" or book.rule(symbol, day).is_fund
            or not nominal.is_finite() or nominal <= 0 or type(quantity) is not int or quantity <= 0):
        raise ValueError("Unsupported ordinary fractional share allocation")
    terms = ordinary_share_terms(action)
    listing = date.fromisoformat(terms["listing_date"]).isoformat()
    registration = date.fromisoformat(terms["registration_date"]).isoformat()
    if (listing != terms["listing_date"] or registration != terms["registration_date"]
            or listing < day or registration >= day):
        raise ValueError("Invalid allocated-share listing date")
    identifier = f"{symbol}:{day}"
    saved = book.state.get("corporate_action_reconciliations", {}).get(identifier)
    receipt = saved
    if receipt is None:
        reader = getattr(feed, "ordinary_share_allocation_receipt", None)
        receipt = reader(action, quantity, book.context) if callable(reader) else None
    if (not isinstance(receipt, dict) or receipt.get("kind") != "ordinary_share_allocation"
            or receipt.get("account_id") != book.context.account_id
            or receipt.get("mode") != book.context.mode.value
            or type(receipt.get("registered_quantity")) is not int
            or receipt["registered_quantity"] != quantity or receipt.get("action_terms") != terms
            or not isinstance(receipt.get("source_ref"), str) or not receipt["source_ref"].strip()
            or receipt.get("source_kind") not in {"broker_account_record", "explicit_replay_assumption"}):
        raise ValueError(f"Unresolved fractional ordinary allocation: {symbol}; account-specific receipt required")
    lower = int(nominal.to_integral_value(rounding=ROUND_FLOOR))
    upper = int(nominal.to_integral_value(rounding=ROUND_CEILING))
    allocation = receipt.get("allocated_shares")
    if type(allocation) is not int or allocation not in {lower, upper}:
        raise ValueError("Ordinary share allocation is outside the integer remainder bounds")
    if receipt["source_kind"] == "explicit_replay_assumption":
        policy = receipt.get("allocation_policy")
        if (book.context.mode != TradingMode.REPLAY or book.state.get("broker_binding")
                or policy not in {"ordinary_fraction_floor_v1", "ordinary_fraction_ceil_v1"}
                or allocation != (lower if policy == "ordinary_fraction_floor_v1" else upper)):
            raise ValueError("Ordinary allocation assumption requires its fixed unbound replay policy")
    book.state.setdefault("corporate_action_reconciliations", {})[identifier] = dict(receipt)
    return {"allocated_shares": allocation, "nominal_bonus_shares": str(nominal),
            "allocation_source_kind": receipt["source_kind"], "allocation_source_ref": receipt["source_ref"],
            "allocation_policy": receipt.get("allocation_policy")}


def corporate_actions(book, feed, day, at_close=False):
    manifest = getattr(feed, "manifest", {})
    validate_rights_binding(book.state, getattr(feed, "rights_replay_policy", None), book.context)
    restricted_allocations = {}
    # A verified ex-price is not proof of this account's subscription or allocation.
    for reference in manifest.get("historical_price_adjustments", []):
        if (reference["kind"] == "restructuring_holder_distribution"
                and reference["date"] >= manifest.get("start", "") and not at_close
                and reference["date"] <= day
                and (book.state["day"] is None or book.state["day"] < reference["date"])):
            symbol = reference["symbol"]
            identifier = f"{symbol}:{reference['date']}"
            quantity = book.state["entitlements"].get(identifier)
            if type(quantity) is not int or quantity < 0:
                raise ValueError(f"Unresolved holder distribution: {symbol}; registered quantity={quantity}")
            if quantity == 0:
                restricted_allocations[identifier] = 0
                continue
            if day != reference["date"]:
                raise ValueError(f"Skipped holder-distribution session: {symbol}; reconciliation required")
            receipt = book.state.get("corporate_action_reconciliations", {}).get(identifier, {})
            source_kind = receipt.get("source_kind")
            if (receipt.get("account_id") != book.context.account_id
                    or receipt.get("mode") != book.context.mode.value
                    or type(receipt.get("registered_quantity")) is not int
                    or receipt["registered_quantity"] != quantity
                    or receipt.get("reference_sha256") != reference["source_sha256"]
                    or receipt.get("holder_source_sha256") != reference["holder_source_sha256"]
                    or receipt.get("eligibility") not in {"eligible", "excluded"}
                    or not isinstance(receipt.get("source_ref"), str) or not receipt["source_ref"]
                    or source_kind not in {"broker_account_record", "explicit_replay_assumption"}
                    or (source_kind == "explicit_replay_assumption" and book.context.mode != TradingMode.REPLAY)):
                raise ValueError(f"Unresolved holder eligibility: {symbol}; account-specific receipt required")
            lower_ratio, upper_ratio = holder_bonus_ratio_bounds(reference)
            if lower_ratio != upper_ratio:
                allocation = receipt.get("allocated_shares")
                eligible = receipt["eligibility"] == "eligible"
                lower = int((quantity * lower_ratio).to_integral_value(rounding=ROUND_FLOOR)) if eligible else 0
                upper = int((quantity * upper_ratio).to_integral_value(rounding=ROUND_CEILING)) if eligible else 0
                if (source_kind != "broker_account_record" or type(allocation) is not int
                        or not lower <= allocation <= upper
                        or (eligible and quantity > Decimal(str(reference["eligible_share_base"])))
                        or allocation > Decimal(str(reference["holder_shares"]))):
                    raise ValueError(f"Unresolved approximate holder allocation: {symbol}; actual allocation required")
            else:
                exact = quantity * lower_ratio if receipt["eligibility"] == "eligible" else Decimal(0)
                allocation = receipt.get("allocated_shares", int(exact) if exact == int(exact) else None)
                if (type(allocation) is not int or allocation < 0
                        or allocation not in {int(exact), int(exact) + (exact != int(exact))}
                        or (exact != int(exact) and source_kind != "broker_account_record")):
                    raise ValueError(f"Unresolved fractional holder allocation: {symbol}; actual allocation required")
            restricted_allocations[identifier] = allocation
            continue
        if reference["kind"] != "rights_issue" or reference["date"] < manifest.get("start", ""):
            continue
        symbol = reference["symbol"]
        identifier = f"rights:{symbol}:{reference['date']}"
        if at_close:
            if reference["registration_date"] == day:
                book.state["entitlements"][identifier] = book.quantity(symbol)
        elif day > reference["registration_date"]:
            apply_rights_issue(book, feed, reference, day)
    for action in feed.actions:
        symbol = action["symbol"]
        identifier = f"{symbol}:{action['dividOperateDate']}"
        if at_close:
            if action["dividRegistDate"] == day:
                book.state["entitlements"][identifier] = book.quantity(symbol)
            continue
        qty = book.state["entitlements"].get(identifier, 0)
        if not qty:
            continue
        if action["dividOperateDate"] == day:
            dividend = Decimal(str(action.get("dividCashPsBeforeTax") or "0"))
            ordinary_bonus = Decimal(str(action.get("dividStocksPs") or "0"))
            bonus = ordinary_bonus + Decimal(str(action.get("dividReserveToStockPs") or "0"))
            reserve = Decimal(0) if book.rule(symbol, day).is_fund else Decimal(str(book.config.dividend_tax_reserve))
            bonus_basis = Decimal(0)
            if ordinary_bonus > 0:
                bonus_basis = Decimal(str(action.get("bonus_taxable_cash_ps") or "NaN"))
                if not bonus_basis.is_finite() or bonus_basis <= 0:
                    raise ValueError(f"Unresolved bonus taxable basis: {symbol} {day}")
            tax_reserve = (dividend + bonus_basis) * qty * reserve
            net_cash = dividend * qty - tax_reserve
            if net_cash < 0:
                raise ValueError(f"Bonus tax reserve exceeds cash dividend: {symbol}; reconciliation required")
            if dividend and (not action.get("dividPayDate") or action["dividPayDate"] < day):
                raise ValueError("Invalid dividend payment date")
            allocation_detail = {}
            if bonus:
                restricted = action.get("action_kind") == "restricted_restructuring"
                new_shares = restricted_allocations[identifier] if restricted else qty * bonus
                if not action.get("dividStockMarketDate"):
                    raise ValueError("Undated bonus shares require reconciliation")
                if not restricted and new_shares != int(new_shares):
                    allocation_detail = ordinary_share_allocation(book, feed, action, qty, new_shares)
                    new_shares = allocation_detail["allocated_shares"]
                elif restricted:
                    allocation_detail = {"allocated_shares": restricted_allocations[identifier]}
                if new_shares:
                    book.state["positions"].setdefault(symbol, []).append({"qty": int(new_shares), "available": action["dividStockMarketDate"]})
            if dividend:
                book.state["receivables"][identifier] = cents(net_cash)
            book.emit(day, "corporate_action", symbol=symbol, entitlement=qty,
                      receivable=book.state["receivables"].get(identifier, 0), **allocation_detail,
                      **({"bonus_taxable_cash_ps": str(bonus_basis), "modeled_tax_reserve": cents(tax_reserve)}
                         if ordinary_bonus > 0 else {}))
        if action.get("dividPayDate") and action["dividPayDate"] <= day and identifier in book.state["receivables"]:
            amount = book.state["receivables"].pop(identifier)
            book.state["cash"] += amount
            book.emit(day, "dividend_paid", symbol=symbol, cash=amount)


def _execution_listing_reason(feed, symbol, day, context):
    """Optional dated REPLAY metadata; existing listing guard stays default."""
    overlay = getattr(feed, "research_listing_limit_reason", None)
    if overlay is None:
        return listing_limit_reason(feed.instruments[symbol].get("ipoDate"), day, feed.calendar)
    if context.mode != TradingMode.REPLAY or not callable(overlay):
        raise ValueError("Dated research IPO overlay is replay-only and must be callable")
    reason = overlay(symbol, day, context)
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise ValueError("IPO overlay must return an explicit block reason or None")
    return reason


def simulate_order(book, feed, day, symbol, side, qty, quote, prior_volume):
    if str(quote.get("adjustflag", "3")) != "3":
        raise ValueError("Simulation cannot execute adjusted quotes")
    identifier = f"{day}:{symbol}:{side}"
    rule = book.rule(symbol, day)
    quantity_rule = rule.quantity
    entry_policy = getattr(feed, "entry_risk_policy", None)
    notice_view = getattr(feed, "risk_notice_view", None)
    entry_contract = entry_risk_contract(entry_policy, notice_view)
    if notice_view is not None and book.context.mode != TradingMode.REPLAY:
        raise ValueError("Partial historical risk-notice view is replay-only")
    validate_entry_risk_binding(book.state, entry_contract)
    entry_block = raw_quote_entry_block(entry_policy, side, quote, rule, notice_view)
    if entry_block is None:
        entry_block = notice_entry_block(entry_policy, notice_view, symbol, day, side, rule)
    if entry_contract is not None:
        book.state["entry_risk_policy"] = entry_contract
    if entry_block:
        book.emit(day, "order_blocked", symbol=symbol, side=side, qty=qty, **entry_block)
        return
    if rule.is_fund:
        if str(quote.get("adjustflag")) != "3":
            raise ValueError("ETF simulation needs explicit raw quote semantics")
        for field in ("open", "preclose"):
            Price.from_rmb(quote[field], PriceBasis.RAW, rule.price_unit)
    listing_reason = _execution_listing_reason(feed, symbol, day, book.context)
    if listing_reason:
        book.emit(day, "order_blocked", symbol=symbol, side=side, qty=qty, reason=listing_reason)
        return
    lower, upper = rule.limits(quote["preclose"], day, bool(quote["isST"]))
    opening = quote_value(quote["open"], rule.price_unit)
    blocked = None
    if quote["tradestatus"] != 1:
        blocked = "suspended"
    elif side == "buy" and quote["isST"]:
        blocked = "risk_warning_buy_blocked"
    elif side == "buy" and opening >= upper:
        blocked = "limit_up_queue_unknown"
    elif side == "sell" and opening <= lower:
        blocked = "limit_down_queue_unknown"
    elif not lower <= opening <= upper:
        blocked = "unsupported_limit_exception"
    if blocked:
        book.emit(day, "order_blocked", symbol=symbol, side=side, qty=qty, reason=blocked)
        return
    # Current suspension/price-limit constraints take precedence. Unknown exact
    # preceding-session activity is not a known zero budget and must not create
    # an order, reserve cash, or stop an unrelated symbol in this explicit mode.
    capacity_policy = getattr(feed, "prior_volume_policy", None)
    if capacity_policy is not None or book.state.get("prior_volume_execution_policy") is not None:
        order_capacity.validate_binding(book.state, capacity_policy, book.context, book.config,
            (book.state.get("full_exit_lifecycle") or {}).get("method_id"))
        if order_capacity.unknown_volume(prior_volume):
            index = feed.calendar.index(day)
            book.emit(day, "order_blocked", symbol=symbol, side=side, qty=qty,
                reason="prior_session_volume_unknown",
                previous_day_required=feed.calendar[index - 1] if index else None,
                capacity_method=order_capacity.METHOD_ID,
                prior_volume=(float(prior_volume) if isinstance(prior_volume, Real)
                    and not isinstance(prior_volume, bool) and math.isfinite(prior_volume) else None),
                order_created=False, capacity_known=False)
            return
    slip = book.config.slippage_bps / 10000
    price = min(upper, max(lower, quote_value(quote["open"] * (1 + slip if side == "buy" else 1 - slip), rule.price_unit)))
    qty = quantity_rule.rounded(qty, side, book.quantity(symbol))
    if side == "buy":
        while qty > 0 and book.order_cost(qty, price, symbol, day) > book.cash_available():
            qty -= quantity_rule.step
        qty = quantity_rule.rounded(qty, side)
    if qty <= 0:
        book.emit(day, "order_blocked", symbol=symbol, side=side, reason="cash_below_one_lot")
        return
    limit_price = Price(price, PriceBasis.RAW) if rule.price_unit == "cent" else Price.from_mills(price, PriceBasis.RAW)
    intent = OrderIntent(identifier, book.context, day, SecurityId(symbol), side, ShareQuantity(qty),
                         limit_price, "target_rebalance")
    broker = SimulatedBrokerAdapter(book)
    order = broker.submit(intent)
    if order["status"] == "rejected":
        return
    step = quantity_rule.step
    capacity = int(max(0, prior_volume) * book.config.participation) // step * step
    fill_qty = min(qty, capacity)
    if side == "sell" and qty % 100 and capacity >= qty:
        fill_qty = qty
    # Two callbacks exercise partial -> complete with an unchanged total capacity.
    first = (fill_qty // (2 * step)) * step
    for index, part in enumerate((first, fill_qty - first)):
        if part:
            event = ExecutionEvent(f"{identifier}:{index}", identifier, book.context, day, intent.security,
                                   side, ShareQuantity(part), intent.limit_price,
                                   day if rule.same_day_resale else feed.next_day(day),
                                   same_day_resale=rule.same_day_resale)
            broker.apply_execution(event)
    if order["status"] in ACTIVE:
        broker.cancel(identifier, day=day)


def commission_deadband_skip(book, symbol, day, side, current, desired, quantity, raw_price):
    """Ignore uneconomic internal reweights, never entries/exits or risk reductions."""
    if current <= 0 or desired <= 0 or quantity >= current and side == "sell":
        return False
    if side == "sell":
        equity = book.equity()
        invested = sum(book.position_value(s) for s in book.state["positions"])
        if (book.position_value(symbol) > equity * book.config.single_limit
                or invested > equity * book.config.gross_limit):
            return False
    rule = book.rule(symbol, day)
    value = notional_cents(quote_value(raw_price, rule.price_unit), quantity, rule.price_unit)
    return value * book.config.commission < cents(book.config.minimum_commission)


def _execute_session_opening(book, feed, day, rows, *, before_events, preopen_intent=None,
        preopen_receipt=None, preopen_full_exits=False, exit_lifecycle=None,
        pending_risk=None, pending_published=None, lot_allocation=None,
        commission_deadband=False, capacity_binding=None, risk_compatible_exits=False):
    """The unchanged opening execution phase used by daily and explicit split sessions."""
    state, config, context = book.state, book.config, book.context
    corporate_actions(book, feed, day)
    prior_index = feed.calendar.index(day) - 1
    previous_day = feed.calendar[prior_index] if prior_index >= 0 else None
    for symbol, row in rows.items():
        if row["open"] > 0:
            unit = book.rule(symbol, day).price_unit
            book.mark_price(symbol, quote_value(row["open"], unit), day, price_unit=unit)
    targets = (preopen_intent.intent.to_legacy_pending() if preopen_intent is not None
        else PortfolioIntent.from_legacy_pending(state["pending_targets"]).to_legacy_pending())
    if preopen_receipt is not None:
        if preopen_intent.intent.kind == IntentKind.NO_CHANGE:
            book.emit(day, "preopen_no_decision", reason=preopen_intent.intent.reason,
                preopen_intent_frontier=preopen_receipt, quantity_retention=True)
        else:
            state["targets"], state["candidates"] = dict(targets), []
            if preopen_full_exits:
                full_exits.replace_preopen_target(exit_lifecycle, book, day, preopen_intent)
            book.emit(day, "preopen_targets_received", source_day=preopen_intent.source_day,
                published_at=preopen_intent.published_at, targets=dict(targets),
                preopen_intent_frontier=preopen_receipt)
    held = [s for s in state["positions"] if book.quantity(s)]
    forced = [s for s in held if (s in rows and rows[s]["isST"] == 1)
              or (feed.instruments[s].get("outDate") and day >= feed.instruments[s]["outDate"])]
    preopen_forced_only = bool(forced and preopen_intent is not None
        and preopen_intent.intent.kind == IntentKind.NO_CHANGE)
    if forced and not preopen_forced_only:
        if targets is None:
            equity = book.equity()
            targets = {s: book.position_value(s) / equity for s in held}
        targets = {s: w for s, w in targets.items() if s not in forced}
    risk_symbols = set(pending_risk.symbols) if pending_risk is not None else set()
    residual_symbols = full_exits.exit_symbols(exit_lifecycle, book) if exit_lifecycle is not None else set()
    risk_symbols.update(residual_symbols)
    if preopen_forced_only:
        # Retain every other exact quantity; rebuilding weights would turn an
        # unknown/no-change publication into an unintended rebalance.
        risk_symbols.update(forced)
    if targets is not None or risk_symbols:
        equity = book.equity()
        open_quantities = (allocate_open_lots(targets, book, day, rows)
                           if targets is not None and lot_allocation == OPEN_LOT_ALLOCATION else None)
        desired = {}
        diagnostics = {}
        for symbol in set(held) | set(targets or {}) | risk_symbols:
            price = state["prices"].get(symbol, 0)
            weight = min(targets.get(symbol, 0), config.single_limit) if targets is not None else None
            rule = book.rule(symbol, day)
            current = book.quantity(symbol)
            # NO_CHANGE plus independent risk reduction retains exact shares.
            # Reconstructing weights here would trade the nonexpired positions.
            desired[symbol] = (0 if symbol in risk_symbols else current if targets is None else
                               open_quantities.get(symbol, 0) if open_quantities is not None else
                               shares_for_target(equity, weight, price, rule))
            delta = desired[symbol] - current
            reason = ("forced_risk_exit" if preopen_forced_only and symbol in forced else
                      "residual_full_exit" if symbol in residual_symbols else
                      "independent_replay_risk_exit" if symbol in risk_symbols else
                      "independent_risk_only_hold_quantity" if targets is None else
                      "forced_risk_exit" if symbol in forced else
                      "target_removed_exit" if current and not weight else
                      "target_below_minimum_exit" if current and weight and not desired[symbol] else
                      "new_entry" if not current and desired[symbol] else
                      "below_minimum_no_entry" if not current and weight else
                      "exposure_cap_reduction" if delta < 0 and (
                          book.position_value(symbol) > equity * config.single_limit
                          or sum(book.position_value(s) for s in held) > equity * config.gross_limit) else
                      "internal_reweight" if delta else "unchanged_quantity")
            diagnostics[symbol] = {
                "symbol": symbol, "mechanical_reason": reason, "target_weight": weight,
                "open_equity_cents": equity, "engine_mark": price, "price_unit": rule.price_unit,
                "has_execution_open": bool(rows.get(symbol, {}).get("open", 0) > 0),
                "ideal_fractional_quantity": float(Decimal(equity) * Decimal(str(weight))
                    * (10 if rule.price_unit == "mill" else 1) / price) if price and weight is not None else None,
                "engine_target_quantity": desired[symbol], "held_before": current,
                "sellable_before": book.available(symbol, day), "minimum_buy_quantity": rule.quantity.minimum,
                "quantity_step": rule.quantity.step, "constraint_flags": [],
                "cash_available_before_cents": book.cash_available(),
            }
        for side in ("sell", "buy"):
            symbols = sorted(desired, key=lambda s: (-(targets or {}).get(s, 0), s))
            for symbol in symbols:
                current = book.quantity(symbol)
                delta = desired[symbol] - current
                if (side == "sell" and delta >= 0) or (side == "buy" and delta <= 0):
                    continue
                row = rows.get(symbol)
                if row is None or not row["open"] > 0:
                    book.emit(day, "order_blocked", symbol=symbol, side=side, reason="missing_quote")
                    continue
                qty = abs(delta)
                if side == "sell":
                    qty = min(qty, book.available(symbol, day))
                    if qty < abs(delta):
                        diagnostics[symbol]["constraint_flags"].append("sellable_T1")
                else:
                    invested = sum(book.position_value(s) for s in state["prices"])
                    rule = book.rule(symbol, day)
                    worst_price = quote_value(row["open"] * (1 + config.slippage_bps / 10000), rule.price_unit)
                    allowance = min(equity * config.gross_limit - invested,
                                    equity * config.single_limit - book.position_value(symbol))
                    step = rule.quantity.step
                    if rule.price_unit == "mill":
                        allowed = int(Decimal(str(allowance)) * 10 / worst_price / step) * step
                    else:
                        allowed = int(allowance / worst_price / step) * step
                    qty = min(qty, max(0, allowed))
                    if qty < abs(delta):
                        diagnostics[symbol]["constraint_flags"].append("exposure_caps")
                before_rounding = qty
                qty = book.rule(symbol, day).quantity.rounded(qty, side, current)
                if qty < before_rounding:
                    diagnostics[symbol]["constraint_flags"].append("legal_order_quantity")
                if qty:
                    if commission_deadband and commission_deadband_skip(
                            book, symbol, day, side, current, desired[symbol], qty, row["open"]):
                        book.emit(day, "rebalance_deferred", symbol=symbol, side=side, qty=qty,
                                  reason="minimum_commission_internal_reweight",
                                  minimum_trade_cny=config.minimum_commission / config.commission)
                        continue
                    if capacity_binding is not None:
                        try:
                            prior_volume = feed.volumes.at[previous_day, symbol] if previous_day else float("nan")
                        except KeyError:
                            prior_volume = float("nan")
                    else:
                        prior_volume = feed.volumes.at[previous_day, symbol] if previous_day else 0
                        prior_volume = float(prior_volume) if math.isfinite(prior_volume) else 0
                    simulate_order(book, feed, day, symbol, side, qty, row, prior_volume)
                    order = state["orders"].get(f"{day}:{symbol}:{side}")
                    if order and order["qty"] < qty:
                        diagnostics[symbol]["constraint_flags"].append("cash_fee_reservation")
                    if order and order["filled"] < order["qty"] and order["status"] == "cancelled":
                        diagnostics[symbol]["constraint_flags"].append("prior_volume_participation")
        execution_events = state["events"][before_events:]
        for symbol in sorted(diagnostics):
            diagnostic = diagnostics[symbol]
            symbol_events = [e for e in execution_events if e.get("symbol") == symbol]
            diagnostic["constraint_flags"] = sorted(set(diagnostic["constraint_flags"] + [
                e["reason"] for e in symbol_events if e["kind"] in {"order_blocked", "rejected", "rebalance_deferred"}
                and e.get("reason")]))
            diagnostic["order_ids"] = [o["id"] for o in state["orders"].values()
                                       if o["day"] == day and o["symbol"] == symbol]
            diagnostic["requested_quantity"] = sum(state["orders"][key]["qty"] for key in diagnostic["order_ids"])
            diagnostic["filled_quantity"] = sum(state["orders"][key]["filled"] for key in diagnostic["order_ids"])
            diagnostic["held_after"] = book.quantity(symbol)
            diagnostic["remaining_quantity_gap"] = desired[symbol] - book.quantity(symbol)
            diagnostic["cash_available_after_cents"] = book.cash_available()
            book.emit(day, "target_execution_diagnostic", schema_version=1, **diagnostic)
        if exit_lifecycle is not None:
            full_exits.record_opening_attempts(exit_lifecycle, book, day, diagnostics,
                risk_intent=pending_risk if risk_compatible_exits else None)
        state["pending_targets"] = None
        if pending_published is not None:
            book.emit(day, "published_close_intent_consumed", published_close_intent=pending_published)
            state.pop("pending_published_close_intent")
        if pending_risk is not None:
            book.emit(day, "risk_exit_execution", decided_on=pending_risk.decided_on,
                      symbols=list(pending_risk.symbols), source=pending_risk.source, reason=pending_risk.reason,
                      remaining_quantities={symbol: book.quantity(symbol) for symbol in pending_risk.symbols})
            state["pending_risk_exits"] = None


def _mark_session_close(book, feed, day, rows, action_view=None):
    """The unchanged closing valuation/action phase; return stale held symbols."""
    state = book.state
    stale = []
    for symbol in state["positions"]:
        row = rows.get(symbol)
        if row and row["close"] > 0:
            unit = book.rule(symbol, day).price_unit
            book.mark_price(symbol, quote_value(row["close"], unit), day, price_unit=unit)
            if book.quantity(symbol) and row["tradestatus"] != 1:
                stale.append(symbol)
        elif book.quantity(symbol):
            stale.append(symbol)
    for symbol, row in rows.items():
        if row["close"] > 0:
            unit = book.rule(symbol, day).price_unit
            book.mark_price(symbol, quote_value(row["close"], unit), day, price_unit=unit)
    corporate_actions(book, feed, day, at_close=True)
    if action_view is not None:
        action_view.record_entitlements(book, day)
    return stale


def advance(state, feed, day, config, session_number, *, context=None, target_policy=None, portfolio_policy=None,
            commission_deadband=False, lot_allocation=None, replay_risk_exit_policy=None,
            published_close_intent=None, published_close_target_policy=None, preopen_intent=None,
            full_exit_lifecycle=None, published_close_router=None, published_close_router_policy=None,
            published_close_session_method=None):
    if state.get("intraday_open_session") is not None or state.get("intraday_session_method") is not None:
        raise ValueError("A two-phase intraday account must use its explicit opening/closing path, not legacy advance")
    if state.get("broker_binding"):
        raise PermissionError("Broker-bound accounts cannot advance through the simulated execution engine")
    if state["day"] and day <= state["day"]:
        raise ValueError("A session cannot be processed twice or backwards")
    notice_view = getattr(feed, "risk_notice_view", None)
    entry_contract = entry_risk_contract(getattr(feed, "entry_risk_policy", None), notice_view)
    validate_entry_risk_binding(state, entry_contract)
    context = context if context is not None else RunContext("standalone_replay", TradingMode.REPLAY)
    from .published_close_session import validate_binding as validate_published_session, marker as published_session_marker
    split_binding = validate_published_session(state, published_close_session_method, day, feed,
        config, context, published_close_intent)
    if split_binding is not None and (any(value is not None for value in (
            target_policy, portfolio_policy, lot_allocation, preopen_intent, full_exit_lifecycle,
            published_close_router, published_close_router_policy, getattr(feed, "prior_volume_policy", None)))
            or commission_deadband or published_close_target_policy is None or replay_risk_exit_policy is None
            or config.rebalance_sessions != 1):
        raise ValueError("Split published-close sessions require their fixed target/risk callbacks without other overlays")
    close_frame_method, close_candidate_binding = _close_candidate_binding(state, feed, context)
    router_requested = published_close_router is not None or published_close_router_policy is not None
    if router_requested and (close_frame_method is not None or any(value is not None for value in (
            preopen_intent, published_close_intent, published_close_target_policy, target_policy,
            portfolio_policy, lot_allocation, full_exit_lifecycle)) or commission_deadband
            or session_number % config.rebalance_sessions != 0):
        raise ValueError("Published-close router is explicit and separate from all legacy target/close overlays")
    router_binding = _router_close_binding(state, published_close_router, published_close_router_policy,
        day, feed, config, context)
    if close_frame_method is not None and any(value is not None for value in (
            preopen_intent, published_close_intent, published_close_target_policy,
            target_policy, portfolio_policy, lot_allocation)):
        raise ValueError("Close candidate filtering cannot be mixed with preopen, published or target/portfolio/lot overlays")
    order_capacity.validate_seed_session(state, feed.calendar, day)
    exit_lifecycle = full_exits.validate_binding(state, full_exit_lifecycle, context)
    capacity_binding = order_capacity.validate_binding(state, getattr(feed, "prior_volume_policy", None),
        context, config, full_exit_lifecycle)
    risk_compatible_exits = (exit_lifecycle is not None
        and exit_lifecycle["method_id"] == full_exits.RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    preopen_full_exits = (exit_lifecycle is not None
        and exit_lifecycle["method_id"] == full_exits.PREOPEN_FULL_EXIT_LIFECYCLE)
    if preopen_full_exits and (preopen_intent is None or published_close_intent is not None
            or state["day"] is not None and state.get("preopen_execution_binding") is None):
        raise ValueError("Preopen full-exit lifecycle needs its explicit envelope and original preopen binding every session")
    if exit_lifecycle is not None and any(value is not None for value in (
            preopen_intent if not preopen_full_exits else None,
            target_policy if not risk_compatible_exits else None, portfolio_policy, lot_allocation,
            replay_risk_exit_policy if not risk_compatible_exits else None, published_close_target_policy)):
        raise ValueError("Fixed full-exit lifecycle cannot be mixed with other execution overlays")
    if preopen_intent is not None and (target_policy is not None or portfolio_policy is not None
            or lot_allocation is not None or replay_risk_exit_policy is not None
            or published_close_intent is not None or published_close_target_policy is not None
            or commission_deadband):
        raise ValueError("Preopen execution is explicit and cannot be mixed with close-target or risk overlays")
    preopen_receipt, preopen_binding = _preopen_frontier(state, preopen_intent, day, feed, config, context)
    action_view = getattr(feed, "research_corporate_action_view", None)
    action_binding = None
    if action_view is not None or "research_corporate_action_binding" in state:
        from .research.research_corporate_action_view import validate_research_action_binding
        action_binding = validate_research_action_binding(state, action_view, context, config, day=day)
    published_receipt = None
    if published_close_intent is not None:
        if (session_number % config.rebalance_sessions != 0 or target_policy is not None
                or portfolio_policy is not None or lot_allocation is not None):
            raise ValueError("Published close intent requires a due session without target/portfolio/lot overlays")
        published_receipt = _published_close_receipt(published_close_intent, day, feed, config, context)
    published_target_binding = _published_close_target_binding(
        state, published_close_target_policy, context, published_close_intent,
        allow_absent_intent=split_binding is not None)
    pending_published = _pending_published_close_receipt(state, feed, day, config, context)
    risk_binding = _replay_risk_exit_binding(state, replay_risk_exit_policy, context)
    pending_risk = (ReplayRiskExitIntent.from_dict(state["pending_risk_exits"])
                    if state.get("pending_risk_exits") is not None else None)
    if pending_risk is not None and (pending_risk.context != context or pending_risk.execution_on != day):
        raise ValueError("Pending risk exit does not match this account or exact execution session")
    if lot_allocation not in {None, LOT_ALLOCATION, OPEN_LOT_ALLOCATION} or (lot_allocation and context.mode != TradingMode.REPLAY):
        raise ValueError("Close lot allocation is an explicit versioned replay-only policy")
    if lot_allocation and target_policy is not None:
        raise ValueError("Close lot allocation cannot enlarge a reduction-only overlay")
    if state["day"] and state.get("lot_allocation") != lot_allocation:
        raise ValueError("Close lot allocation cannot change within an existing account")
    if type(commission_deadband) is not bool or (commission_deadband and (
            context.mode != TradingMode.REPLAY or config.commission <= 0)):
        raise ValueError("Commission deadband is explicit replay-only with a positive commission rate")
    if state["day"] and state.get("commission_deadband", False) != commission_deadband:
        raise ValueError("Commission deadband cannot change within an existing account")
    if notice_view is not None and context.mode != TradingMode.REPLAY:
        raise ValueError("Partial historical risk-notice view is replay-only")
    if getattr(feed, "manifest", {}).get("replay_only") and context.mode != TradingMode.REPLAY:
        raise ValueError("Diagnostic market snapshot is historical-replay-only")
    if target_policy is not None and (context.mode != TradingMode.REPLAY or not callable(target_policy)):
        raise ValueError("Optional target policy is replay-only and must be callable")
    if portfolio_policy is not None and (context.mode != TradingMode.REPLAY or not callable(portfolio_policy)
                                         or target_policy is not None):
        raise ValueError("Portfolio policy is replay-only, callable and separate from the reduction overlay")
    opening_targets = ({symbol: weight for symbol, weight in preopen_intent.intent.weights if weight > 0}
        if preopen_intent is not None and preopen_intent.intent.kind == IntentKind.FULL_TARGET else {})
    validate_preopen_targets = getattr(feed, "validate_preopen_targets", None)
    if opening_targets and validate_preopen_targets is not None:
        validate_preopen_targets(day, opening_targets)
    validate_session = getattr(feed, "validate_session", None)
    if validate_session is not None:
        validation_state = state
        if opening_targets:
            # The envelope is already validated. Expose its new-entry scope
            # to pure feed guards before opening fills, without queuing a
            # second target or changing the actual account state.
            validation_state = deepcopy(state)
            validation_state["pending_targets"] = opening_targets
        validate_session(validation_state, day)
    validate_risk_session = getattr(replay_risk_exit_policy, "validate_session", None)
    if validate_risk_session is not None:
        validate_risk_session(state, feed, day, context)
    book = Ledger(state, config, context, instrument_profiles=getattr(feed, "instrument_profiles", None))
    # Reject invalid/future score batches before this session mutates the account.
    signal_batch, allocated_intent = None, None
    event_frontier = None
    return_policy = (portfolio_policy is not None
                     and getattr(portfolio_policy, "signal_kind", None) == SignalKind.EXPECTED_RETURN)
    if return_policy and (target_policy is not None or lot_allocation is not None
                          or not callable(getattr(portfolio_policy, "validate_frame", None))):
        raise ValueError("Expected-return selection is explicit and separate from target/lot overlays")
    if (session_number % config.rebalance_sessions == 0 and published_close_intent is None
            and preopen_intent is None and not router_requested and split_binding is None):
        # A proxy's __getattr__ may expose its base feed's rule method and bypass
        # its own model/trend scores. Only opt in through a declared consumer.
        frame_method = getattr(type(feed), "signal_frame", None)
        if frame_method is not None:
            signal_batch = frame_method(feed, day, config, context)
        else:
            legacy = feed.scores(day, config)
            signal_batch = (legacy if isinstance(legacy, (SignalFrame, SignalUnavailable))
                            else SignalFrame.from_legacy(legacy, day, "legacy_ranked_source", context))
        if not isinstance(signal_batch, (SignalFrame, SignalUnavailable)):
            raise TypeError("Signal source must return SignalFrame or SignalUnavailable")
        if isinstance(signal_batch, SignalFrame):
            if signal_batch.day != day or signal_batch.context != context:
                raise ValueError("Signal session/account/mode does not match replay")
            event_frontier = validate_signal_frontier(
                signal_batch, decision_at=decision_time(day), execution_day=feed.next_day(day))
            if return_policy:
                portfolio_policy.validate_frame(signal_batch)
                candidates = [{"symbol": s.security.symbol, "expected_return_fraction": s.value,
                               "horizon_sessions": s.horizon_sessions} for s in signal_batch.signals]
            else:
                candidates = signal_batch.ranked_candidates()
            if portfolio_policy is not None and not return_policy:
                original = PortfolioIntent.full_target(
                    choose_targets(candidates, [s for s in state["positions"] if book.quantity(s)], config),
                    context=context, decided_at=decision_time(day), execution_not_before=feed.next_day(day),
                    source=signal_batch.source)
                allocated_intent = allocate_replay_target(original, portfolio_policy, config)
    rows = feed.rows.get(day, {})
    # A saved mill quote cannot be processed by an old cent-only feed/profile.
    for symbol, unit in state.get("price_units", {}).items():
        if book.rule(symbol, day).price_unit != unit:
            raise ValueError("Account quote units do not match feed instrument profiles")
    if commission_deadband:
        state["commission_deadband"] = True
    if lot_allocation:
        state["lot_allocation"] = lot_allocation
    for symbol, row in rows.items():
        rule = book.rule(symbol, day)
        if rule.is_fund:
            if (row.get("tradestatus") not in {0, 1} or row.get("isST") != 0
                    or str(row.get("adjustflag")) != "3"):
                raise ValueError("ETF simulation needs explicit status and raw quote semantics")
            for field in ("open", "close", "preclose"):
                Price.from_rmb(row[field], PriceBasis.RAW, rule.price_unit)
    # A carried price is not a delisting settlement or a substitute for missing data.
    # Check before touching the account so an unresolved session can be resumed.
    for symbol in state["positions"]:
        if not book.quantity(symbol):
            continue
        out_date = feed.instruments[symbol].get("outDate")
        if out_date and day >= out_date:
            raise ValueError(f"Unresolved delisted holding: {symbol} {day}; settlement required")
        row = rows.get(symbol)
        if row is None or not math.isfinite(row["close"]) or row["close"] <= 0:
            raise ValueError(f"Unpriceable holding: {symbol} {day}; source reconciliation required")
    before_orders, before_events = len(state["orders"]), len(state["events"])
    if close_candidate_binding is not None:
        state["close_candidate_policy"] = close_candidate_binding
    if risk_binding is not None:
        state["replay_risk_exit_policy"] = risk_binding
    if published_target_binding is not None:
        state["published_close_target_policy"] = published_target_binding
    if router_binding is not None:
        state["published_close_router_policy"] = router_binding
    if entry_contract is not None:
        state["entry_risk_policy"] = entry_contract
    if action_binding is not None:
        state["research_corporate_action_binding"] = action_binding
    if preopen_binding is not None:
        state["preopen_execution_binding"] = preopen_binding
    if exit_lifecycle is not None:
        state["full_exit_lifecycle"] = exit_lifecycle
    if capacity_binding is not None:
        state["prior_volume_execution_policy"] = capacity_binding
    if split_binding is not None:
        state["published_close_session_binding"] = split_binding
    _execute_session_opening(book, feed, day, rows, before_events=before_events,
        preopen_intent=preopen_intent, preopen_receipt=preopen_receipt,
        preopen_full_exits=preopen_full_exits, exit_lifecycle=exit_lifecycle,
        pending_risk=pending_risk, pending_published=pending_published,
        lot_allocation=lot_allocation, commission_deadband=commission_deadband,
        capacity_binding=capacity_binding, risk_compatible_exits=risk_compatible_exits)
    stale = _mark_session_close(book, feed, day, rows, action_view=action_view)
    published_reduction, router_receipt = None, None
    if (session_number % config.rebalance_sessions == 0 and preopen_intent is None
            and not (split_binding is not None and published_close_intent is None)):
        if close_frame_method is not None and isinstance(signal_batch, SignalFrame):
            # The original complete rank/frontier was checked before opening.
            # Admission now sees this same book after D's fills, marks and actions.
            original_batch = deepcopy(signal_batch)
            signal_batch = _checked_close_signal_frame(original_batch,
                close_frame_method(feed, day, config, context, signal_batch, book))
            if isinstance(signal_batch, SignalFrame):
                event_frontier = validate_signal_frontier(signal_batch,
                    decision_at=decision_time(day), execution_day=feed.next_day(day))
                candidates = signal_batch.ranked_candidates()
        if router_requested:
            intent = published_close_router_policy(published_close_router, book, feed, config)
            router_receipt = checked_router_receipt(published_close_router, intent,
                held_symbols={symbol for symbol in state["positions"] if book.quantity(symbol)})
            candidates = []
        elif published_close_intent is not None:
            intent = published_close_intent
            candidates = []  # Published weights do not manufacture ranked scores.
            if published_close_target_policy is not None and intent.kind == IntentKind.FULL_TARGET:
                # This must run after D's old pending target, marks and actions:
                # expiry/structure policies need the actual post-opening book.
                intent = reduce_replay_target(intent, published_close_target_policy)
                published_reduction = _published_close_reduction_receipt(
                    published_close_intent, intent, day, feed, config, context,
                    published_target_binding["policy_id"])
        elif isinstance(signal_batch, SignalUnavailable):
            intent = PortfolioIntent.no_change(signal_batch.reason, context=context, decided_at=decision_time(day))
        else:
            if return_policy:
                intent = checked_return_intent(signal_batch, portfolio_policy(signal_batch, book, feed, config),
                                               config, feed.next_day(day))
            else:
                intent = allocated_intent if allocated_intent is not None else PortfolioIntent.full_target(
                    choose_targets(candidates, [s for s in state["positions"] if book.quantity(s)], config),
                    context=context, decided_at=decision_time(day), execution_not_before=feed.next_day(day), source=signal_batch.source)
            if target_policy is not None:
                intent = reduce_replay_target(intent, target_policy)
            if lot_allocation == LOT_ALLOCATION and intent.kind == IntentKind.FULL_TARGET:
                intent = allocate_close_lots(intent, book, day, rows)
        published_metadata = {"published_close_intent_frontier": published_receipt} if published_receipt is not None else {}
        if published_reduction is not None:
            published_metadata["published_close_reduction"] = published_reduction
        if router_receipt is not None:
            published_metadata["published_close_router"] = router_receipt
        if intent.kind == IntentKind.NO_CHANGE:
            book.emit(day, "no_decision", signal_date=day, reason=intent.reason, **published_metadata)
        else:
            state["candidates"] = candidates
            state["targets"] = intent.to_legacy_pending()
            state["pending_targets"] = intent.to_legacy_pending()
            if exit_lifecycle is not None:
                full_exits.replace_target(exit_lifecycle, book, day, intent)
            if published_receipt is not None:
                state["pending_published_close_intent"] = (published_reduction if published_reduction is not None
                                                           else published_receipt)
            elif router_receipt is not None:
                state["pending_published_close_intent"] = router_receipt
            book.emit(day, "targets_generated", signal_date=day, execution_not_before=feed.next_day(day),
                      targets=state["targets"], signal_source=intent.source, decision_reason=intent.reason or None,
                      **published_metadata)
    if split_binding is not None and published_receipt is None:
        book.emit(day, "close_publication_missing", execution_not_before=feed.next_day(day),
                  decision_not_manufactured=True)
    _schedule_replay_risk_exit(book, feed, day, replay_risk_exit_policy)
    if exit_lifecycle is not None:
        full_exits.refresh_remaining(exit_lifecycle, book)
    state["day"] = day
    if split_binding is not None:
        state["last_published_close_session"] = published_session_marker(day, published_receipt)
    if preopen_receipt is not None:
        state["last_preopen_intent"] = preopen_receipt
    if router_receipt is not None:
        state["last_published_close_router"] = router_receipt
    if state["cash"] < 0 or any(book.quantity(s) < 0 for s in state["positions"]):
        raise ValueError("Ledger invariant violated")
    today_fills = [f for f in state["fills"].values() if f["day"] == day]
    result = {"date": day, "equity": book.equity() / 100, "cash": state["cash"] / 100,
            "receivables": sum(state["receivables"].values()) / 100,
            "holdings": sum(book.quantity(s) > 0 for s in state["positions"]),
            "fees": sum(f["fees"] for f in today_fills) / 100,
            "turnover_value": sum(f.get("notional", f["qty"] * f["price"]) for f in today_fills) / 100,
            "new_orders": len(state["orders"]) - before_orders, "stale_symbols": stale,
            "events": state["events"][before_events:], "event_time_frontier": event_frontier}
    if published_receipt is not None:
        result["published_close_intent_frontier"] = published_receipt
    if published_reduction is not None:
        result["published_close_reduction"] = published_reduction
    if router_receipt is not None:
        result["published_close_router"] = router_receipt
    if pending_published is not None:
        result["consumed_published_close_intent"] = pending_published
    if preopen_receipt is not None:
        result["preopen_intent_frontier"] = preopen_receipt
    return result
