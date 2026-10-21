"""Explicit, account-specific nontrade share conversion receipts for replay.

Neither an exchange ratio nor a delisting date determines credited shares. The
receipt states each consumed lot, credited lot, availability date and cash item.
The Engine opts a new account into one append-only receipt catalog.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re

from .contracts import (MARKET_TZ, RunContext, SecurityId, TradingMode, _day,
                        _payload, _read, _timestamp)


METHOD_ID = "explicit_nontrade_security_conversion_v1"
KNOWLEDGE_MODES = {"dated_asof", "posthoc_component_diagnostic"}
BINDING_KEY = "security_conversion_binding"
CATALOG_KEY = "security_conversion_catalog"
APPLIED_KEY = "security_conversion_applied"


def _now():
    return datetime.now(MARKET_TZ)


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Explicit {name} required")


@dataclass(frozen=True)
class ConversionCredit:
    source_lot_id: str
    source_quantity: int
    credited_quantity: int
    available_on: str

    def __post_init__(self):
        _identifier(self.source_lot_id, "source lot identity")
        if (type(self.source_quantity) is not int or self.source_quantity <= 0
                or type(self.credited_quantity) is not int or self.credited_quantity < 0):
            raise ValueError("Conversion lot quantities must be explicit nonnegative integers with a positive source")
        _day(self.available_on)

    def to_dict(self):
        return _payload("ConversionCredit", source_lot_id=self.source_lot_id,
            source_quantity=self.source_quantity, credited_quantity=self.credited_quantity,
            available_on=self.available_on)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "ConversionCredit")
        return cls(data["source_lot_id"], data["source_quantity"],
                   data["credited_quantity"], data["available_on"])


@dataclass(frozen=True)
class SecurityConversionReceipt:
    context: RunContext
    event_id: str
    effective_day: str
    source_symbol: str
    destination_symbol: str
    registered_quantity: int
    credits: tuple[ConversionCredit, ...]
    cash_cents: int
    source_file: str
    source_sha256: str
    source_available_at: str
    recorded_at: str

    def __post_init__(self):
        if not isinstance(self.context, RunContext) or self.context.mode != TradingMode.REPLAY:
            raise ValueError("Security conversion receipts require an explicit replay context")
        _identifier(self.event_id, "conversion event identity")
        _day(self.effective_day)
        SecurityId(self.source_symbol)
        SecurityId(self.destination_symbol)
        if self.source_symbol == self.destination_symbol:
            raise ValueError("Security conversion requires distinct source and destination securities")
        if type(self.registered_quantity) is not int or self.registered_quantity <= 0:
            raise ValueError("Explicit positive registered conversion quantity required")
        if (not isinstance(self.credits, tuple) or not self.credits
                or any(not isinstance(credit, ConversionCredit) for credit in self.credits)):
            raise ValueError("Conversion requires an immutable nonempty tuple of explicit lot credits")
        if len({credit.source_lot_id for credit in self.credits}) != len(self.credits):
            raise ValueError("Conversion source lot identities must be unique")
        if (sum(credit.source_quantity for credit in self.credits) != self.registered_quantity
                or sum(credit.credited_quantity for credit in self.credits) <= 0):
            raise ValueError("Conversion source quantities must reconcile and credited shares must be positive")
        if any(credit.available_on < self.effective_day for credit in self.credits):
            raise ValueError("Conversion share availability cannot precede the effective session")
        if type(self.cash_cents) is not int or self.cash_cents < 0:
            raise ValueError("Conversion cash component must be an explicit nonnegative integer, including zero")
        if not isinstance(self.source_file, str) or not Path(self.source_file).is_absolute():
            raise ValueError("Conversion needs an explicit absolute local source file")
        if not isinstance(self.source_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", self.source_sha256) is None:
            raise ValueError("Conversion source needs a complete SHA256")
        object.__setattr__(self, "source_sha256", self.source_sha256.lower())
        if _timestamp(self.source_available_at) > _timestamp(self.recorded_at):
            raise ValueError("Conversion receipt cannot precede its source availability")

    @property
    def credited_quantity(self):
        return sum(credit.credited_quantity for credit in self.credits)

    def validate_source(self):
        """Read the pinned local proof only; never discover or download a source."""
        source = Path(self.source_file)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != self.source_sha256:
            raise ValueError("Security conversion source file is missing or its SHA256 changed")

    def to_dict(self):
        return _payload("SecurityConversionReceipt", context=self.context.to_dict(),
            event_id=self.event_id, effective_day=self.effective_day,
            source_symbol=self.source_symbol, destination_symbol=self.destination_symbol,
            registered_quantity=self.registered_quantity,
            credits=[credit.to_dict() for credit in self.credits], cash_cents=self.cash_cents,
            source_file=self.source_file, source_sha256=self.source_sha256,
            source_available_at=self.source_available_at, recorded_at=self.recorded_at)

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "SecurityConversionReceipt")
        return cls(RunContext.from_dict(data["context"]), data["event_id"],
            data["effective_day"], data["source_symbol"], data["destination_symbol"],
            data["registered_quantity"], tuple(ConversionCredit.from_dict(row) for row in data["credits"]),
            data["cash_cents"], data["source_file"], data["source_sha256"],
            data["source_available_at"], data["recorded_at"])


@dataclass(frozen=True)
class SecurityConversionView:
    registry_id: str
    context: RunContext
    knowledge_mode: str
    receipts: tuple[SecurityConversionReceipt, ...]

    def __post_init__(self):
        _identifier(self.registry_id, "conversion registry identity")
        if (not isinstance(self.context, RunContext) or self.context.mode != TradingMode.REPLAY
                or self.knowledge_mode not in KNOWLEDGE_MODES):
            raise ValueError("Security conversion view requires an explicit replay context and knowledge mode")
        if (not isinstance(self.receipts, tuple)
                or any(not isinstance(receipt, SecurityConversionReceipt) for receipt in self.receipts)):
            raise ValueError("Conversion catalog must be an immutable tuple of typed receipts")
        if any(receipt.context != self.context for receipt in self.receipts):
            raise ValueError("Conversion receipt context differs from its catalog")
        if len({receipt.event_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("Duplicate conversion event identity")
        used = {}
        for receipt in self.receipts:
            same_day = used.setdefault(receipt.effective_day, set())
            symbols = {receipt.source_symbol, receipt.destination_symbol}
            if same_day.intersection(symbols):
                raise ValueError("Overlapping conversion source or destination on the same session")
            same_day.update(symbols)

    def to_dict(self):
        return _payload("SecurityConversionView", registry_id=self.registry_id,
            context=self.context.to_dict(), knowledge_mode=self.knowledge_mode,
            receipts=[receipt.to_dict() for receipt in self.receipts])

    @classmethod
    def from_dict(cls, payload):
        data = _read(payload, "SecurityConversionView")
        return cls(data["registry_id"], RunContext.from_dict(data["context"]), data["knowledge_mode"],
                   tuple(SecurityConversionReceipt.from_dict(row) for row in data["receipts"]))


def validate_view(state, view, day, config, context, feed_calendar):
    """Return immutable-method binding, full catalog and due receipts without writes.

    The full saved receipt prefix is authoritative. A new receipt may only be
    appended before its effective session has been processed. Unknown conversion
    economics therefore cannot be filled retrospectively into a resumed account.
    """
    saved = state.get(BINDING_KEY)
    if view is None:
        if (saved is not None or CATALOG_KEY in state or APPLIED_KEY in state
                or "security_conversion_records" in state):
            raise ValueError("A bound security conversion catalog cannot be disabled")
        return None, None, ()
    if (not isinstance(view, SecurityConversionView) or not isinstance(context, RunContext)
            or context.mode != TradingMode.REPLAY or view.context != context or state.get("broker_binding")):
        raise ValueError("Security conversion requires a matching unbound replay account")
    calendar = list(feed_calendar)
    if (not calendar or calendar != sorted(set(calendar)) or day not in calendar
            or any(_day(session) != session for session in calendar)):
        raise ValueError("Security conversion requires the pinned exact ordered session calendar")
    binding = {"schema_version": 1, "method_id": METHOD_ID, "registry_id": view.registry_id,
        "context": context.to_dict(), "knowledge_mode": view.knowledge_mode, "config": config.to_dict(),
        "calendar": calendar,
        "pit_verified": False, "returns_reportable": False, "execution_admitted": False}
    prefix = [receipt.to_dict() for receipt in view.receipts]
    previous_day = state.get("day")
    if saved is None:
        if (previous_day is not None or CATALOG_KEY in state or APPLIED_KEY in state
                or "security_conversion_records" in state or state.get("orders") or state.get("fills")
                or any(lots for lots in state.get("positions", {}).values())
                or state.get("pending_targets") is not None or state.get("targets")
                or state.get("pending_published_close_intent") is not None
                or state.get("pending_risk_exits") is not None):
            raise ValueError("Security conversion must be explicitly enabled on a new empty account's first session")
        saved_prefix = []
    else:
        if saved != binding or previous_day is None:
            raise ValueError("Security conversion binding, context, configuration or knowledge mode changed")
        _day(previous_day)
        saved_prefix = state.get(CATALOG_KEY)
        if not isinstance(saved_prefix, list):
            raise ValueError("Saved security conversion catalog is missing")
        if len(prefix) < len(saved_prefix) or prefix[:len(saved_prefix)] != saved_prefix:
            raise ValueError("Security conversion catalog changed or dropped its saved receipt prefix")
        for payload in saved_prefix:
            if SecurityConversionReceipt.from_dict(payload).to_dict() != payload:
                raise ValueError("Saved security conversion receipt is not canonical")
        if any(receipt.effective_day <= previous_day for receipt in view.receipts[len(saved_prefix):]):
            raise ValueError("A newly appended conversion cannot change an already settled session")
    applied = state.get(APPLIED_KEY, {})
    if not isinstance(applied, dict):
        raise ValueError("Invalid applied security conversion catalog")
    by_id = {receipt.event_id: receipt for receipt in view.receipts}
    for event_id, payload in applied.items():
        receipt = by_id.get(event_id)
        if (receipt is None or receipt.to_dict() != payload or previous_day is None
                or receipt.effective_day > previous_day):
            raise ValueError("Applied conversion differs from its saved receipt or settled session")
    now = _now()
    due = []
    for receipt in view.receipts:
        if (receipt.effective_day not in calendar
                or any(credit.available_on not in calendar for credit in receipt.credits)):
            raise ValueError("Conversion effective and share availability dates require exact calendar sessions")
        recorded = _timestamp(receipt.recorded_at)
        if recorded > now:
            raise ValueError("A future security conversion receipt cannot be used")
        if view.knowledge_mode == "dated_asof" and recorded > _timestamp(receipt.effective_day + "T09:30:00+08:00"):
            raise ValueError("Dated conversion evidence must be recorded by its effective session opening")
        receipt.validate_source()
        if receipt.event_id not in applied:
            if (receipt.effective_day < day
                    or previous_day is not None and receipt.effective_day <= previous_day):
                raise ValueError("Unconsumed security conversion skipped its exact effective session")
            if receipt.effective_day == day:
                due.append(receipt)
    return binding, prefix, tuple(due)
