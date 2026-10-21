"""Explicit saved-close routing input, separate from legacy target reductions."""
from dataclasses import dataclass
from datetime import date, datetime
import math

from .contracts import IntentKind, MARKET_TZ, PortfolioIntent, SecurityId, TradingMode, decision_time


POLICY_ID = "single_active_type_cyclic_environment_switch_shared_engine_v1:published_close"
SCHEMA = "published_close_router_envelope_v1"
RECEIPT_SCHEMA = "published_close_router_postopen_receipt_v1"
BINDING_SCHEMA = "published_close_router_policy_binding_v1"
OWNER_STATE_KEY = "leader_type_router"


@dataclass(frozen=True)
class PublishedCloseRouterEnvelope:
    original_intent: PortfolioIntent
    source_day: str
    execution_day: str
    published_at: str
    policy_id: str
    owner_before: str | None
    owner_symbols: tuple[str, ...]

    def __post_init__(self):
        intent = self.original_intent
        if (not isinstance(intent, PortfolioIntent) or intent.kind not in {IntentKind.FULL_TARGET, IntentKind.NO_CHANGE}
                or intent.context is None or intent.context.mode != TradingMode.REPLAY
                or not intent.source or intent.execution_not_before != self.execution_day
                or intent.decided_at != decision_time(self.source_day) or self.policy_id != POLICY_ID):
            raise ValueError("Explicit registered router input kind, context, policy, D event clock and E are required")
        if (date.fromisoformat(self.source_day).isoformat() != self.source_day
                or date.fromisoformat(self.execution_day).isoformat() != self.execution_day
                or self.execution_day <= self.source_day):
            raise ValueError("Router source/execution dates must be canonical and ordered")
        clock = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        if clock.utcoffset() is None:
            raise ValueError("Actual router publication needs an explicit timezone")
        clock = clock.astimezone(MARKET_TZ)
        if not (datetime.fromisoformat(self.source_day + "T15:00:00+08:00") <= clock
                < datetime.fromisoformat(self.execution_day + "T09:30:00+08:00")):
            raise ValueError("Router publication must be after D close and strictly before exact E opening")
        if (len(intent.weights) > 5 or any(not math.isclose(weight, .16, rel_tol=0, abs_tol=1e-12)
                for _, weight in intent.weights) or sum(weight for _, weight in intent.weights) > .8 + 1e-12):
            raise ValueError("Published router input uses at most five original fixed .16 slots")
        if self.owner_before is not None and (not isinstance(self.owner_before, str) or not self.owner_before):
            raise ValueError("Router owner must be explicit or null")
        if (not isinstance(self.owner_symbols, tuple) or self.owner_symbols != tuple(sorted(set(self.owner_symbols)))):
            raise ValueError("Owner exposure symbols must be immutable, distinct and sorted")
        for symbol in self.owner_symbols:
            SecurityId(symbol)

    def to_dict(self):
        return {"schema": SCHEMA, "original_intent": self.original_intent.to_dict(),
            "source_day": self.source_day, "execution_day": self.execution_day,
            "published_at": self.published_at, "policy_id": self.policy_id,
            "owner_before": self.owner_before, "owner_symbols": list(self.owner_symbols)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ValueError("Explicit published-close router envelope schema required")
        return cls(PortfolioIntent.from_dict(value["original_intent"]), value["source_day"],
            value["execution_day"], value["published_at"], value["policy_id"],
            value["owner_before"], tuple(value["owner_symbols"]))


def checked_receipt(envelope, result, *, held_symbols=()):
    """Validate a post-open result without calling its policy on restoration."""
    if not isinstance(envelope, PublishedCloseRouterEnvelope) or not isinstance(result, PortfolioIntent):
        raise ValueError("Router callback must return a typed PortfolioIntent")
    original = envelope.original_intent
    if (result.context != original.context or result.decided_at != original.decided_at
            or result.execution_not_before != envelope.execution_day or result.source != original.source
            or result.kind not in {IntentKind.FULL_TARGET, IntentKind.NO_CHANGE}):
        raise ValueError("Router callback changed account, D/E or the original source frontier")
    allowed = dict(original.weights)
    if any(symbol not in allowed or weight != allowed[symbol] for symbol, weight in result.weights):
        raise ValueError("Router callback may only retain original fixed candidate slots")
    if original.kind == IntentKind.NO_CHANGE and result.kind == IntentKind.FULL_TARGET:
        if result.weights or not set(held_symbols).issubset(envelope.owner_symbols):
            raise ValueError("NO_CHANGE may only become an empty owner-exposure exit, never new targets")
    return {"schema": RECEIPT_SCHEMA, "decision_day": envelope.source_day,
        "execution_not_before": envelope.execution_day, "published_at": envelope.published_at,
        "policy_id": envelope.policy_id, "envelope": envelope.to_dict(), "intent": result.to_dict(),
        "source_availability_verified_by_engine": False, "source_verified": False,
        "pit_verified": False, "execution_admitted": False}
