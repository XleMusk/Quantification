"""Engine-facing contract for a pinned research corporate-action view.

The Engine needs to bind the view to an account before it can ask the view to
validate a session.  Keeping this small contract in the core support layer
prevents the Engine and intraday consumer from importing a research module just
to perform the binding check; the research module still owns loading and
interpreting the evidence.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .contracts import RunContext, TradingMode


ACTION_VIEW_BINDING_ID = "research_corporate_action_component_binding_v1"
STATE_KEY = "research_corporate_action_binding"


def validate_research_action_binding(
    state: Mapping[str, Any], view: Any, context: RunContext, config: Any, day: str
) -> dict[str, Any] | None:
    """Bind a loaded action view once, without importing its research owner.

    ``ResearchCorporateActionView`` exposes ``binding_contract`` as the narrow
    runtime marker.  The marker plus the view's immutable context/binding
    properties preserve the previous admission checks while allowing the
    account engine to depend on this core contract instead of the loader.
    """
    saved = state.get(STATE_KEY)
    if STATE_KEY in state and not isinstance(saved, dict):
        raise ValueError("Saved corporate-action binding cannot be null or malformed")
    if view is None:
        if saved is not None:
            raise ValueError("A bound research corporate-action view cannot be disabled")
        return None
    binding_contract = getattr(view, "binding_contract", None)
    binding = getattr(view, "binding", None)
    view_context = getattr(view, "context", None)
    if (
        binding_contract != ACTION_VIEW_BINDING_ID
        or not isinstance(binding, dict)
        or not isinstance(context, RunContext)
        or context.mode != TradingMode.REPLAY
        or view_context != context
        or state.get("broker_binding")
    ):
        raise ValueError("Corporate-action components require their exact unbound REPLAY context")
    _canonical_day(day, "binding day")
    expected = {"context": context.to_dict(),
                "dividend_tax_reserve": str(Decimal(str(config.dividend_tax_reserve)))}
    if (binding.get("context") != expected["context"]
            or binding.get("dividend_tax_reserve") != expected["dividend_tax_reserve"]):
        raise ValueError("Corporate-action context or dividend tax reserve changed")
    if saved is not None:
        saved_base = {key: value for key, value in saved.items() if key != "replay_start_day"}
        start = saved.get("replay_start_day")
        if not isinstance(start, str):
            raise ValueError("Saved corporate-action binding has no replay start day")
        _canonical_day(start, "replay start day")
        if (saved_base != binding or state.get("day") is None
                or not start <= _canonical_day(state["day"], "saved day") < day):
            raise ValueError("Bound corporate-action source, mode, context or config changed")
        return {**binding, "replay_start_day": start}
    if (
        state.get("day") is not None
        or state.get("events")
        or state.get("orders")
        or state.get("fills")
        or state.get("positions")
        or state.get("entitlements")
        or state.get("receivables")
        or state.get("pending_targets") is not None
        or state.get("pending_published_close_intent") is not None
        or state.get("pending_risk_exits") is not None
    ):
        raise ValueError("Research corporate-action accounting must start in a fresh cash-only REPLAY context")
    return {**binding, "replay_start_day": day}


def _canonical_day(value: str, field: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Corporate-action {field} must be canonical") from error
    if parsed.isoformat() != value:
        raise ValueError(f"Corporate-action {field} must be canonical")
    return value


__all__ = ["ACTION_VIEW_BINDING_ID", "STATE_KEY", "validate_research_action_binding"]
