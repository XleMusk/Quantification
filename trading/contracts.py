"""Vendor-independent, versioned contracts for the research-to-paper path.

The persisted V1 account keeps None or a target mapping until its state schema
is migrated. Conversion is explicit so an absent decision cannot mean exit.
"""

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from decimal import Decimal
import math
from collections.abc import Mapping
import re


SCHEMA_VERSION = 1
MARKET_TZ = timezone(timedelta(hours=8))


def _payload(contract_type, **values):
    return {"schema_version": SCHEMA_VERSION, "contract_type": contract_type, **values}


def _read(payload, kind):
    if (not isinstance(payload, Mapping) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != SCHEMA_VERSION or payload.get("contract_type") != kind):
        raise ValueError(f"Unsupported {kind} schema")
    return payload


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Session day must be YYYY-MM-DD")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an offset-aware ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Timestamp must be offset-aware")
    return parsed


def decision_time(day):
    return _day(day) + "T15:05:00+08:00"


class TradingMode(str, Enum):
    REPLAY = "historical_replay"
    PAPER = "end_of_day_paper"


@dataclass(frozen=True)
class RunContext:
    account_id: str
    mode: TradingMode

    def __post_init__(self):
        if not isinstance(self.account_id, str) or not self.account_id or not isinstance(self.mode, TradingMode):
            raise ValueError("A simulation account and explicit mode are required")

    def to_dict(self):
        return _payload("RunContext", account_id=self.account_id, mode=self.mode.value)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "RunContext")
        return cls(data["account_id"], TradingMode(data["mode"]))


@dataclass(frozen=True)
class SecurityId:
    """A canonical code reference, not the permanent instrument master (D01)."""
    symbol: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[0-9]{6}\.(SSE|SZSE|BSE)", self.symbol):
            raise ValueError("Security needs a six-digit code and explicit exchange")

    def to_dict(self):
        code, exchange = self.symbol.split(".")
        return _payload("SecurityId", code=code, exchange=exchange)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "SecurityId")
        if not isinstance(data["code"], str) or not isinstance(data["exchange"], str):
            raise ValueError("Security code/exchange must be strings")
        return cls(data["code"] + "." + data["exchange"])


class PriceBasis(str, Enum):
    RAW = "unadjusted"
    FORWARD_ADJUSTED = "forward_adjusted"
    BACKWARD_ADJUSTED = "backward_adjusted"


@dataclass(frozen=True)
class Price:
    cents: int | None
    basis: PriceBasis
    mills: int | None = None

    def __post_init__(self):
        value = self.cents if self.mills is None else self.mills
        if (type(value) is not int or value <= 0 or not isinstance(self.basis, PriceBasis)
                or (self.mills is not None and self.cents is not None)):
            raise ValueError("Price requires one positive integer CNY cent/mill value and an explicit basis")

    @property
    def unit(self):
        return "cent" if self.mills is None else "mill"

    @property
    def value(self):
        return self.cents if self.mills is None else self.mills

    @property
    def rmb(self):
        return Decimal(self.value) / (100 if self.unit == "cent" else 1000)

    @classmethod
    def from_mills(cls, value, basis):
        return cls(None, basis, value)

    @classmethod
    def from_rmb(cls, value, basis, unit="cent"):
        if unit not in {"cent", "mill"}:
            raise ValueError("Unknown CNY quote unit")
        amount = Decimal(str(value)) * (100 if unit == "cent" else 1000)
        if not amount.is_finite() or amount <= 0 or amount != int(amount):
            raise ValueError("Quote is not on the declared price tick")
        return cls(int(amount), basis) if unit == "cent" else cls.from_mills(int(amount), basis)

    def to_dict(self):
        payload = _payload("Price", value=self.value, unit=self.unit, currency="CNY", basis=self.basis.value)
        if self.unit == "mill":
            payload["schema_version"] = 2
        return payload

    @classmethod
    def from_dict(cls, payload):
        if (isinstance(payload, Mapping) and type(payload.get("schema_version")) is int
                and payload["schema_version"] == 2 and payload.get("contract_type") == "Price"
                and payload.get("unit") == "mill"):
            if payload["currency"] != "CNY":
                raise ValueError("Price unit/currency mismatch")
            return cls.from_mills(payload["value"], PriceBasis(payload["basis"]))
        data = _read(payload, "Price")
        if data["unit"] != "cent" or data["currency"] != "CNY":
            raise ValueError("Price unit/currency mismatch")
        return cls(data["value"], PriceBasis(data["basis"]))


@dataclass(frozen=True)
class ShareQuantity:
    shares: int

    def __post_init__(self):
        if type(self.shares) is not int or self.shares <= 0:
            raise ValueError("Quantity requires positive integer shares, not lots or money")

    def to_dict(self):
        return _payload("ShareQuantity", value=self.shares, unit="share")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "ShareQuantity")
        if data["unit"] != "share":
            raise ValueError("Quantity unit mismatch")
        return cls(data["value"])


@dataclass(frozen=True)
class SignalTime:
    event_time: str
    available_at: str
    published_at: str | None = None
    received_at: str | None = None
    computed_at: str | None = None
    availability_evidence: str = "provided"

    def __post_init__(self):
        available = _timestamp(self.available_at)
        if available < _timestamp(self.event_time):
            raise ValueError("Signal cannot be available before its market event")
        for value in (self.published_at, self.received_at, self.computed_at):
            if value is not None and _timestamp(value) > available:
                raise ValueError("Signal availability precedes a required input/computation")
        if self.availability_evidence not in {"provided", "historical_eod_assumption"}:
            raise ValueError("Unknown availability evidence")

    def to_dict(self):
        return _payload("SignalTime", **asdict(self))

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "SignalTime")
        return cls(data["event_time"], data["available_at"], data.get("published_at"),
                   data.get("received_at"), data.get("computed_at"), data["availability_evidence"])


class SignalKind(str, Enum):
    SCORE = "ranking_score"
    EXPECTED_RETURN = "expected_return_fraction"


@dataclass(frozen=True)
class Signal:
    security: SecurityId
    value: float
    kind: SignalKind
    time: SignalTime
    horizon_sessions: int | None = None
    diagnostics: tuple[tuple[str, float], ...] = ()

    def __post_init__(self):
        if not isinstance(self.security, SecurityId) or not isinstance(self.time, SignalTime) or not isinstance(self.kind, SignalKind):
            raise ValueError("Invalid signal contract")
        if type(self.value) not in (int, float) or not math.isfinite(self.value):
            raise ValueError("Signal value must be finite")
        if self.horizon_sessions is not None and (type(self.horizon_sessions) is not int or self.horizon_sessions < 1):
            raise ValueError("Invalid signal horizon")
        if self.kind == SignalKind.EXPECTED_RETURN and self.horizon_sessions is None:
            raise ValueError("Expected return needs a prediction horizon")
        keys = [key for key, _ in self.diagnostics]
        if not isinstance(self.diagnostics, tuple) or any(not isinstance(pair, tuple) for pair in self.diagnostics):
            raise ValueError("Signal diagnostics must be immutable")
        if len(set(keys)) != len(keys) or any(not isinstance(k, str) or not k or k in {"symbol", "score"} for k in keys):
            raise ValueError("Invalid diagnostic names")
        if any(type(v) not in (int, float) or not math.isfinite(v) for _, v in self.diagnostics):
            raise ValueError("Invalid signal diagnostic")

    def to_dict(self):
        return _payload("Signal", security=self.security.to_dict(), value=self.value, kind=self.kind.value,
                        time=self.time.to_dict(), horizon_sessions=self.horizon_sessions, diagnostics=dict(self.diagnostics))

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "Signal")
        return cls(SecurityId.from_dict(data["security"]), data["value"], SignalKind(data["kind"]),
                   SignalTime.from_dict(data["time"]), data.get("horizon_sessions"), tuple(data.get("diagnostics", {}).items()))


@dataclass(frozen=True)
class SignalFrame:
    day: str
    signals: tuple[Signal, ...]
    source: str
    context: RunContext

    def __post_init__(self):
        _day(self.day)
        if not isinstance(self.context, RunContext) or not isinstance(self.source, str) or not self.source:
            raise ValueError("Signal batch needs a source and run context")
        if not isinstance(self.signals, tuple) or not all(isinstance(s, Signal) for s in self.signals):
            raise ValueError("Signal batch requires immutable typed signals")
        if len({s.security for s in self.signals}) != len(self.signals):
            raise ValueError("Duplicate signal security")
        cutoff = _timestamp(decision_time(self.day))
        for signal in self.signals:
            if (_timestamp(signal.time.event_time).astimezone(cutoff.tzinfo).date().isoformat() != self.day
                    or _timestamp(signal.time.available_at) > cutoff):
                raise ValueError("Signal was unavailable at this session decision")

    @classmethod
    def from_legacy(cls, candidates, day, source, context, available_at=None):
        if not isinstance(candidates, list):
            raise TypeError("Legacy scores must be a ranked list")
        if available_at is not None and len(available_at) != len(candidates):
            raise ValueError("Score timestamps do not align with candidates")
        signals = []
        for index, row in enumerate(candidates):
            stamp = SignalTime(_day(day) + "T15:00:00+08:00",
                               available_at[index] if available_at is not None else decision_time(day),
                               availability_evidence="provided" if available_at is not None else "historical_eod_assumption")
            signals.append(Signal(SecurityId(row["symbol"]), row["score"], SignalKind.SCORE, stamp,
                                  diagnostics=tuple((k, v) for k, v in row.items() if k not in {"symbol", "score"})))
        return cls(day, tuple(signals), source, context)

    def ranked_candidates(self):
        if any(s.kind != SignalKind.SCORE for s in self.signals):
            raise ValueError("Rank selector requires scores; expected returns need an explicit portfolio policy")
        return [{"symbol": s.security.symbol, "score": s.value, **dict(s.diagnostics)} for s in self.signals]

    def with_context(self, context):
        return replace(self, context=context)

    def to_dict(self):
        return _payload("SignalFrame", day=self.day, signals=[s.to_dict() for s in self.signals],
                        source=self.source, context=self.context.to_dict())

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "SignalFrame")
        return cls(data["day"], tuple(Signal.from_dict(s) for s in data["signals"]), data["source"],
                   RunContext.from_dict(data["context"]))


class IntentKind(str, Enum):
    FULL_TARGET = "FULL_TARGET"
    DELTA = "DELTA"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True)
class SignalUnavailable:
    reason: str

    def __post_init__(self):
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("Unavailable signal needs a reason")


@dataclass(frozen=True)
class PortfolioIntent:
    kind: IntentKind
    weights: tuple[tuple[str, float], ...] = ()
    reason: str = ""
    context: RunContext | None = None
    decided_at: str | None = None
    execution_not_before: str | None = None
    source: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, IntentKind):
            raise ValueError("Unknown intent kind")
        if not isinstance(self.reason, str) or (self.source is not None and (not isinstance(self.source, str) or not self.source)):
            raise ValueError("Invalid intent reason/source")
        if self.kind == IntentKind.NO_CHANGE and self.weights:
            raise ValueError("NO_CHANGE cannot carry target weights")
        if self.kind == IntentKind.NO_CHANGE and not self.reason:
            raise ValueError("NO_CHANGE needs a reason")
        if not isinstance(self.weights, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in self.weights):
            raise ValueError("Intent weights must be immutable")
        if len({symbol for symbol, _ in self.weights}) != len(self.weights):
            raise ValueError("Duplicate intent symbol")
        for symbol, weight in self.weights:
            SecurityId(symbol)
            if type(weight) not in (int, float):
                raise ValueError("Invalid intent weight")
            if not math.isfinite(weight) or abs(weight) > 1 or (self.kind == IntentKind.FULL_TARGET and weight < 0):
                raise ValueError("Invalid intent weight")
        if self.kind == IntentKind.FULL_TARGET and sum(w for _, w in self.weights) > 1 + 1e-12:
            raise ValueError("Full target exceeds portfolio equity")
        if self.context is not None and not isinstance(self.context, RunContext):
            raise ValueError("Invalid intent context")
        if self.decided_at is not None:
            _timestamp(self.decided_at)
        if self.execution_not_before is not None:
            _day(self.execution_not_before)
            if self.decided_at is not None and self.execution_not_before <= _timestamp(self.decided_at).astimezone(MARKET_TZ).date().isoformat():
                raise ValueError("EOD targets cannot execute on the decision day")

    @classmethod
    def full_target(cls, targets: Mapping[str, float], **metadata):
        return cls(IntentKind.FULL_TARGET, tuple(targets.items()), **metadata)

    @classmethod
    def no_change(cls, reason: str, **metadata):
        if not reason:
            raise ValueError("NO_CHANGE needs a reason")
        return cls(IntentKind.NO_CHANGE, reason=reason, **metadata)

    @classmethod
    def delta(cls, changes: Mapping[str, float]):
        return cls(IntentKind.DELTA, tuple(changes.items()))

    @classmethod
    def from_legacy_pending(cls, pending):
        if pending is None:
            return cls.no_change("no_pending_target")
        if not isinstance(pending, Mapping):
            raise ValueError("Invalid persisted pending target")
        return cls.full_target(pending)

    def to_legacy_pending(self):
        if self.kind == IntentKind.NO_CHANGE:
            return None
        if self.kind == IntentKind.FULL_TARGET:
            return dict(self.weights)
        raise ValueError("DELTA is not supported by the V1 account state")

    def to_dict(self):
        return _payload("PortfolioIntent", kind=self.kind.value, weights=dict(self.weights),
                        unit="equity_fraction", reason=self.reason,
                        context=self.context.to_dict() if self.context is not None else None,
                        decided_at=self.decided_at, execution_not_before=self.execution_not_before, source=self.source)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "PortfolioIntent")
        if data["unit"] != "equity_fraction":
            raise ValueError("Target unit mismatch")
        return cls(IntentKind(data["kind"]), tuple(data["weights"].items()), data.get("reason", ""),
                   RunContext.from_dict(data["context"]) if data.get("context") is not None else None,
                   data.get("decided_at"), data.get("execution_not_before"), data.get("source"))


@dataclass(frozen=True)
class PreopenPortfolioIntent:
    """A published overnight target for one exact simulated opening.

    This envelope is separate from an EOD PortfolioIntent's decision clock.
    Source/model evidence is checked by its publisher/consumer, not inferred
    from the existence of this target contract.
    """
    source_day: str
    execution_day: str
    published_at: str
    intent: PortfolioIntent
    explicit_exit_all: bool = False

    def __post_init__(self):
        _day(self.source_day)
        _day(self.execution_day)
        if self.source_day >= self.execution_day:
            raise ValueError("Preopen execution must follow its source close")
        if (not isinstance(self.intent, PortfolioIntent)
                or self.intent.kind not in {IntentKind.FULL_TARGET, IntentKind.NO_CHANGE}
                or not isinstance(self.intent.context, RunContext) or not self.intent.source
                or self.intent.decided_at is not None or self.intent.execution_not_before is not None):
            raise ValueError("Preopen intent needs a bound full/no-change target without EOD decision metadata")
        published = _timestamp(self.published_at).astimezone(MARKET_TZ)
        if (not _timestamp(self.source_day + "T15:00:00+08:00") < published
                < _timestamp(self.execution_day + "T09:30:00+08:00")):
            raise ValueError("Preopen publication must be after the source close and strictly before execution opening")
        empty_full = self.intent.kind == IntentKind.FULL_TARGET and not self.intent.weights
        if (type(self.explicit_exit_all) is not bool or self.explicit_exit_all != empty_full
                or (empty_full and not self.intent.reason)):
            raise ValueError("An empty preopen full target needs an explicit, reasoned exit-all confirmation")

    def to_dict(self):
        return _payload("PreopenPortfolioIntent", source_day=self.source_day,
            execution_day=self.execution_day, published_at=self.published_at,
            intent=self.intent.to_dict(), explicit_exit_all=self.explicit_exit_all)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "PreopenPortfolioIntent")
        return cls(data["source_day"], data["execution_day"], data["published_at"],
            PortfolioIntent.from_dict(data["intent"]), data["explicit_exit_all"])


@dataclass(frozen=True)
class OrderIntent:
    id: str
    context: RunContext
    day: str
    security: SecurityId
    side: str
    quantity: ShareQuantity
    limit_price: Price
    reason: str

    def __post_init__(self):
        _day(self.day)
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.reason, str) or not self.reason
                or not isinstance(self.context, RunContext) or not isinstance(self.security, SecurityId)
                or self.side not in {"buy", "sell"} or not isinstance(self.quantity, ShareQuantity)
                or not isinstance(self.limit_price, Price)):
            raise ValueError("Invalid order intent")
        if self.limit_price.basis != PriceBasis.RAW:
            raise ValueError("Execution requires unadjusted CNY prices")

    def to_dict(self):
        return _payload("OrderIntent", id=self.id, context=self.context.to_dict(), day=self.day,
                        security=self.security.to_dict(), side=self.side, quantity=self.quantity.to_dict(),
                        limit_price=self.limit_price.to_dict(), price_type="limit", reason=self.reason)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "OrderIntent")
        if data["price_type"] != "limit":
            raise ValueError("Only explicit limit orders are supported")
        return cls(data["id"], RunContext.from_dict(data["context"]), data["day"], SecurityId.from_dict(data["security"]),
                   data["side"], ShareQuantity.from_dict(data["quantity"]), Price.from_dict(data["limit_price"]), data["reason"])


@dataclass(frozen=True)
class ExecutionEvent:
    id: str
    order_id: str
    context: RunContext
    day: str
    security: SecurityId
    side: str
    quantity: ShareQuantity
    price: Price
    available_day: str
    same_day_resale: bool = False

    def __post_init__(self):
        _day(self.day)
        _day(self.available_day)
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.order_id, str) or not self.order_id
                or not isinstance(self.context, RunContext) or not isinstance(self.security, SecurityId)
                or self.side not in {"buy", "sell"} or not isinstance(self.quantity, ShareQuantity)
                or not isinstance(self.price, Price)):
            raise ValueError("Invalid execution event")
        if type(self.same_day_resale) is not bool:
            raise ValueError("Fill resale rule must be explicit")
        if (self.price.basis != PriceBasis.RAW
                or (self.available_day != self.day if self.same_day_resale else self.available_day <= self.day)):
            raise ValueError("Fill requires raw CNY price and a date matching its declared resale rule")

    def to_dict(self):
        result = _payload("ExecutionEvent", id=self.id, order_id=self.order_id, context=self.context.to_dict(),
                        day=self.day, security=self.security.to_dict(), side=self.side,
                        quantity=self.quantity.to_dict(), price=self.price.to_dict(),
                        available_day=self.available_day, kind="fill", source="simulator")
        if self.same_day_resale:
            result.update(schema_version=2, availability_rule="same_day_resale")
        return result

    @classmethod
    def from_dict(cls, payload):
        same_day = (isinstance(payload, Mapping) and type(payload.get("schema_version")) is int
                    and payload["schema_version"] == 2 and payload.get("contract_type") == "ExecutionEvent")
        if same_day:
            if payload.get("availability_rule") != "same_day_resale":
                raise ValueError("Unsupported fill resale rule")
            data = payload
        else:
            data = _read(payload, "ExecutionEvent")
        if data["kind"] != "fill" or data["source"] != "simulator":
            raise ValueError("Only simulated fill events are supported here")
        return cls(data["id"], data["order_id"], RunContext.from_dict(data["context"]), data["day"],
                   SecurityId.from_dict(data["security"]), data["side"], ShareQuantity.from_dict(data["quantity"]),
                   Price.from_dict(data["price"]), data["available_day"], same_day_resale=same_day)


@dataclass(frozen=True)
class BrokerFill:
    id: str
    quantity: ShareQuantity
    price: Price

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.quantity, ShareQuantity)
                or not isinstance(self.price, Price) or self.price.basis != PriceBasis.RAW or self.price.unit != "cent"):
            raise ValueError("Broker fill requires explicit ID, shares and raw cent quote")

    def to_dict(self):
        return _payload("BrokerFill", id=self.id, quantity=self.quantity.to_dict(), price=self.price.to_dict(),
                        source="broker_observation")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerFill")
        if data["source"] != "broker_observation":
            raise ValueError("Unknown broker fill source")
        return cls(data["id"], ShareQuantity.from_dict(data["quantity"]), Price.from_dict(data["price"]))


def _broker_holdings(positions, marks):
    if (not isinstance(positions, tuple) or any(not isinstance(p, tuple) or len(p) != 3 for p in positions)
            or len({p[0] for p in positions}) != len(positions)):
        raise ValueError("Broker holdings must be immutable and unique")
    for symbol, total, sellable in positions:
        SecurityId(symbol)
        if type(total) is not int or type(sellable) is not int or not 0 <= sellable <= total or not total:
            raise ValueError("Invalid broker holdings quantities")
    if (not isinstance(marks, tuple) or any(not isinstance(p, tuple) or len(p) != 2 for p in marks)
            or {p[0] for p in marks} != {p[0] for p in positions} or len({p[0] for p in marks}) != len(marks)):
        raise ValueError("Every held security needs a dated observed raw valuation")
    for symbol, price in marks:
        SecurityId(symbol)
        Price(price, PriceBasis.RAW)


@dataclass(frozen=True)
class BrokerFillBatch:
    order: OrderIntent
    account_alias: str
    contract_id: str
    fills: tuple[BrokerFill, ...]
    fees_cents: int
    status: str
    available_day: str
    cash_total: int
    cash_frozen: int
    positions: tuple[tuple[str, int, int], ...]
    marks: tuple[tuple[str, int], ...]
    observed_at: str
    source_ref: str
    broker_account_kind: str = "simulated"

    def __post_init__(self):
        if (not isinstance(self.order, OrderIntent) or self.order.context.mode != TradingMode.PAPER
                or self.order.limit_price.unit != "cent"
                or self.broker_account_kind != "simulated"
                or not all(isinstance(v, str) and v for v in (self.account_alias, self.contract_id, self.source_ref))):
            raise ValueError("Only explicitly bound simulated broker accounts can supply batches")
        if (not isinstance(self.fills, tuple) or any(not isinstance(f, BrokerFill) for f in self.fills)
                or len({f.id for f in self.fills}) != len(self.fills)):
            raise ValueError("Broker batch needs immutable unique executions")
        qty = sum(f.quantity.shares for f in self.fills)
        if (self.status not in {"partial", "filled", "cancelled", "rejected"}
                or qty > self.order.quantity.shares
                or (self.status == "filled" and qty != self.order.quantity.shares)
                or (self.status == "partial" and not 0 < qty < self.order.quantity.shares)
                or (self.status == "cancelled" and qty >= self.order.quantity.shares)
                or (self.status == "rejected" and qty != 0)):
            raise ValueError("Broker order status and cumulative executions disagree")
        _day(self.available_day)
        if self.available_day <= self.order.day or _timestamp(self.observed_at).astimezone(MARKET_TZ).date().isoformat() != self.order.day:
            raise ValueError("Broker batch requires same-day observation and following-session availability")
        if (any(type(v) is not int or v < 0 for v in (self.fees_cents, self.cash_total, self.cash_frozen))
                or self.cash_frozen > self.cash_total):
            raise ValueError("Broker money must be nonnegative exact cents")
        _broker_holdings(self.positions, self.marks)

    def to_dict(self):
        return _payload("BrokerFillBatch", order=self.order.to_dict(), account_alias=self.account_alias,
                        contract_id=self.contract_id, fills=[f.to_dict() for f in self.fills], fees_cents=self.fees_cents,
                        status=self.status, available_day=self.available_day, cash_total=self.cash_total,
                        cash_frozen=self.cash_frozen, positions=[list(p) for p in self.positions], marks=[list(p) for p in self.marks],
                        observed_at=self.observed_at, source_ref=self.source_ref,
                        broker_account_kind=self.broker_account_kind, source="broker_observation")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerFillBatch")
        if data["source"] != "broker_observation":
            raise ValueError("Unknown broker receipt source")
        return cls(OrderIntent.from_dict(data["order"]), data["account_alias"], data["contract_id"],
                   tuple(BrokerFill.from_dict(f) for f in data["fills"]), data["fees_cents"], data["status"],
                   data["available_day"], data["cash_total"], data["cash_frozen"],
                   tuple(tuple(p) for p in data["positions"]), tuple(tuple(p) for p in data["marks"]),
                   data["observed_at"], data["source_ref"], data["broker_account_kind"])


@dataclass(frozen=True)
class BrokerCashMovement:
    id: str
    kind: str
    cash_change_cents: int

    def __post_init__(self):
        credit = {"cash_dividend", "interest", "deposit"}
        debit = {"tax", "fee", "withdrawal"}
        if (not isinstance(self.id, str) or not self.id or self.kind not in credit | debit
                or type(self.cash_change_cents) is not int or not self.cash_change_cents
                or (self.kind in credit) != (self.cash_change_cents > 0)):
            raise ValueError("Nontrade cash movement needs an ID, explicit kind and correctly signed exact cents")

    def to_dict(self):
        return _payload("BrokerCashMovement", id=self.id, kind=self.kind, cash_change_cents=self.cash_change_cents,
                        source="broker_observation", currency="CNY", unit="cent")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerCashMovement")
        if data["source"] != "broker_observation" or data["currency"] != "CNY" or data["unit"] != "cent":
            raise ValueError("Unknown cash movement source")
        return cls(data["id"], data["kind"], data["cash_change_cents"])


@dataclass(frozen=True)
class BrokerRestrictionRelease:
    id: str
    security: SecurityId
    quantity: ShareQuantity
    day: str
    reason: str = "opening_unknown_restriction_release"

    def __post_init__(self):
        _day(self.day)
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.security, SecurityId)
                or not isinstance(self.quantity, ShareQuantity)
                or self.reason != "opening_unknown_restriction_release"):
            raise ValueError("Release needs an ID, explicit security/shares/date and opening restriction evidence")

    def to_dict(self):
        return _payload("BrokerRestrictionRelease", id=self.id, security=self.security.to_dict(),
                        quantity=self.quantity.to_dict(), day=self.day, reason=self.reason, source="broker_observation")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerRestrictionRelease")
        if data["source"] != "broker_observation":
            raise ValueError("Unknown restriction release source")
        return cls(data["id"], SecurityId.from_dict(data["security"]), ShareQuantity.from_dict(data["quantity"]),
                   data["day"], data["reason"])


@dataclass(frozen=True)
class BrokerShareCredit:
    id: str
    security: SecurityId
    quantity: ShareQuantity
    day: str
    available_day: str
    kind: str

    def __post_init__(self):
        _day(self.day)
        _day(self.available_day)
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.security, SecurityId)
                or not isinstance(self.quantity, ShareQuantity) or self.available_day < self.day
                or self.kind not in {"bonus_shares", "capital_transfer"}):
            raise ValueError("Share credit needs explicit account ID/security/shares/availability and bonus or capital-transfer meaning")

    def to_dict(self):
        return _payload("BrokerShareCredit", id=self.id, security=self.security.to_dict(),
                        quantity=self.quantity.to_dict(), day=self.day, available_day=self.available_day,
                        kind=self.kind, source="broker_observation")

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerShareCredit")
        if data["source"] != "broker_observation":
            raise ValueError("Unknown share credit source")
        return cls(data["id"], SecurityId.from_dict(data["security"]), ShareQuantity.from_dict(data["quantity"]),
                   data["day"], data["available_day"], data["kind"])


@dataclass(frozen=True)
class BrokerSessionBalance:
    context: RunContext
    account_alias: str
    day: str
    available_day: str
    cash_total: int
    positions: tuple[tuple[str, int, int], ...]
    marks: tuple[tuple[str, int], ...]
    movements: tuple[BrokerCashMovement, ...]
    terminalizations: tuple[tuple[str, str, str], ...]
    observed_at: str
    source_ref: str
    restriction_releases: tuple[BrokerRestrictionRelease, ...] = ()
    share_credits: tuple[BrokerShareCredit, ...] = ()

    def __post_init__(self):
        _day(self.day)
        _day(self.available_day)
        if (not isinstance(self.context, RunContext) or self.context.mode != TradingMode.PAPER
                or not all(isinstance(v, str) and v for v in (self.account_alias, self.source_ref))
                or type(self.cash_total) is not int or self.cash_total < 0 or self.available_day <= self.day
                or _timestamp(self.observed_at).astimezone(MARKET_TZ).date().isoformat() != self.day):
            raise ValueError("Session receipt needs explicit simulated account, same-day cash/valuation and following session")
        _broker_holdings(self.positions, self.marks)
        if (not isinstance(self.movements, tuple) or any(not isinstance(m, BrokerCashMovement) for m in self.movements)
                or len({m.id for m in self.movements}) != len(self.movements)):
            raise ValueError("Session cash movement IDs must be unique")
        if (not isinstance(self.terminalizations, tuple)
                or any(not isinstance(t, tuple) or len(t) != 3 or not all(isinstance(v, str) and v for v in t)
                       or t[1] not in {"filled", "cancelled", "rejected"} for t in self.terminalizations)
                or len({t[0] for t in self.terminalizations}) != len(self.terminalizations)):
            raise ValueError("Session terminal order proof is invalid or duplicated")
        if (not isinstance(self.restriction_releases, tuple)
                or any(not isinstance(r, BrokerRestrictionRelease) or r.day != self.day for r in self.restriction_releases)
                or len({r.id for r in self.restriction_releases}) != len(self.restriction_releases)):
            raise ValueError("Session restriction releases must have unique IDs and the same effective day")
        if (not isinstance(self.share_credits, tuple)
                or any(not isinstance(c, BrokerShareCredit) or c.day != self.day for c in self.share_credits)
                or len({c.id for c in self.share_credits}) != len(self.share_credits)):
            raise ValueError("Session share credits must have unique IDs and the same effective day")

    def to_dict(self):
        result = _payload("BrokerSessionBalance", context=self.context.to_dict(), account_alias=self.account_alias,
            day=self.day, available_day=self.available_day, cash_total=self.cash_total,
            positions=[list(p) for p in self.positions], marks=[list(p) for p in self.marks],
            movements=[m.to_dict() for m in self.movements], terminalizations=[list(t) for t in self.terminalizations],
            observed_at=self.observed_at, source_ref=self.source_ref, source="broker_observation", broker_account_kind="simulated")
        if self.restriction_releases:
            result["restriction_releases"] = [r.to_dict() for r in self.restriction_releases]
        if self.share_credits:
            result["share_credits"] = [c.to_dict() for c in self.share_credits]
        return result

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "BrokerSessionBalance")
        if data["source"] != "broker_observation" or data["broker_account_kind"] != "simulated":
            raise ValueError("Only simulated broker session observations are supported")
        return cls(RunContext.from_dict(data["context"]), data["account_alias"], data["day"], data["available_day"], data["cash_total"],
            tuple(tuple(p) for p in data["positions"]), tuple(tuple(p) for p in data["marks"]),
            tuple(BrokerCashMovement.from_dict(m) for m in data["movements"]),
            tuple(tuple(t) for t in data["terminalizations"]), data["observed_at"], data["source_ref"],
            tuple(BrokerRestrictionRelease.from_dict(r) for r in data.get("restriction_releases", [])),
            tuple(BrokerShareCredit.from_dict(c) for c in data.get("share_credits", [])))
