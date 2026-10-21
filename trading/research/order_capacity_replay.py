"""Explicit research-only per-order capacity handling and saved-snapshot seed.

This does not migrate an account. A seed is a new diagnostic fork of pinned
REPLAY bytes; holdings, cash, lots and outstanding decisions remain unchanged.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from numbers import Real
from pathlib import Path
import math

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, PortfolioIntent, RunContext, TradingMode, decision_time
from ..target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE


METHOD_ID = "replay_per_order_exact_prior_volume_unknown_blocks_order_v1"
BINDING_SCHEMA = "replay_per_order_prior_volume_binding_v1"
SEED_SCHEMA = "independent_research_snapshot_seed_v1"


@dataclass(frozen=True)
class PriorVolumeReplayPolicy:
    context: RunContext

    def __post_init__(self):
        if not isinstance(self.context, RunContext) or self.context.mode != TradingMode.REPLAY:
            raise ValueError("Per-order prior-volume policy is explicit REPLAY only")

    @property
    def method_id(self):
        return METHOD_ID


def _expected(policy, context, config):
    if (not isinstance(policy, PriorVolumeReplayPolicy) or policy.context != context
            or context.mode != TradingMode.REPLAY or not isinstance(config, StrategyConfig)):
        raise ValueError("Per-order capacity policy context/configuration mismatch")
    config.validate()
    return {"schema": BINDING_SCHEMA, "method_id": METHOD_ID,
        "context": context.to_dict(), "config": config.to_dict(),
        "full_exit_lifecycle": RISK_COMPAT_FULL_EXIT_LIFECYCLE,
        "execution_admitted": False, "selection_enabled": False,
        "strategy_validated": False, "returns_reportable": False}


def validate_binding(state, policy, context, config, lifecycle):
    """Pure validation. Engine stores the returned binding only after guards."""
    saved = state.get("prior_volume_execution_policy")
    if policy is None:
        if saved is not None:
            raise ValueError("Per-order prior-volume policy cannot be disabled")
        if lifecycle == RISK_COMPAT_FULL_EXIT_LIFECYCLE:
            raise ValueError("Risk-compatible lifecycle requires its per-order capacity policy")
        return None
    expected = _expected(policy, context, config)
    if lifecycle != RISK_COMPAT_FULL_EXIT_LIFECYCLE:
        raise ValueError("Per-order capacity policy requires its fixed risk-compatible lifecycle")
    if saved != expected and (saved is not None or state.get("day") is not None):
        raise ValueError("Per-order capacity policy cannot attach to or change an existing account; use an explicit research snapshot seed")
    return expected


def unknown_volume(value):
    return (not isinstance(value, Real) or isinstance(value, bool)
        or not math.isfinite(value) or value < 0)


def validate_seed_session(state, calendar, day):
    """A diagnostic seed must consume its pinned sessions without a gap."""
    seed = state.get("research_snapshot_seed")
    if seed is None:
        return
    days = list(calendar)
    digest = hashlib.sha256(json.dumps(days, separators=(",", ":")).encode()).hexdigest()
    saved_day = state.get("day")
    if (not isinstance(seed, dict) or seed.get("schema") != SEED_SCHEMA
            or seed.get("method_id") != METHOD_ID
            or days != sorted(set(days)) or digest != seed.get("calendar_sha256")
            or seed.get("source_day") not in days or saved_day not in days
            or saved_day < seed["source_day"]):
        raise ValueError("Research snapshot seed session/calendar binding changed")
    index = days.index(saved_day)
    if index + 1 >= len(days) or day != days[index + 1]:
        raise ValueError("Research snapshot seed must process the exact next session")
    source_index = days.index(seed["source_day"])
    if (source_index + 1 >= len(days)
            or seed.get("first_execution_day") != days[source_index + 1]
            or saved_day == seed["source_day"] and day != seed["first_execution_day"]):
        raise ValueError("Research snapshot seed first execution session changed")


def _clock(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    if parsed is None or parsed.utcoffset() is None:
        raise ValueError("Snapshot seed requires offset-aware actual source clocks")
    return parsed


def _map_context(value, original, replacement):
    if isinstance(value, dict):
        if value.get("contract_type") == "RunContext":
            if value != original:
                raise ValueError("Saved snapshot contains another typed account context")
            return deepcopy(replacement)
        return {key: _map_context(item, original, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_context(item, original, replacement) for item in value]
    return deepcopy(value)


def seed_research_snapshot(source_file, source_sha256, *, context, config, calendar,
                           source_available_at, source_observed_at):
    """Create a new, explicitly bound REPLAY fork without touching the source.

    The caller supplies the original experiment's config and pinned calendar.
    These are recorded as explicit diagnostic inputs, never inferred from cash.
    """
    path = Path(source_file).resolve()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(source_sha256, str) or digest != source_sha256:
        raise ValueError("Research snapshot seed source SHA256 changed")
    available, observed = _clock(source_available_at), _clock(source_observed_at)
    if available > observed or observed > datetime.now(MARKET_TZ):
        raise ValueError("Research snapshot seed source clocks are future or reversed")
    original = json.loads(raw)
    if (not isinstance(original, dict) or original.get("broker_binding")
            or original.get("research_snapshot_seed") is not None
            or original.get("prior_volume_execution_policy") is not None
            or original.get("full_exit_lifecycle") is not None
            or original.get("preopen_execution_binding") is not None):
        raise ValueError("Seed requires an unbound saved historical research account")
    days = list(calendar)
    if days != sorted(set(days)) or any(date.fromisoformat(day).isoformat() != day for day in days):
        raise ValueError("Snapshot seed requires a pinned exact ordered session calendar")
    day = original.get("day")
    if day not in days or days.index(day) + 1 >= len(days) or available.date().isoformat() < day:
        raise ValueError("Snapshot seed needs the saved day and its exact successor")
    next_day = days[days.index(day) + 1]
    old_binding = original.get("replay_risk_exit_policy")
    if (not isinstance(old_binding, dict) or old_binding.get("schema") != "replay_risk_exit_policy_v1"
            or not isinstance(old_binding.get("policy_id"), str) or not old_binding["policy_id"]):
        raise ValueError("Saved research snapshot must identify its original risk policy and context")
    old_context = RunContext.from_dict(old_binding["context"])
    policy = PriorVolumeReplayPolicy(context)
    binding = _expected(policy, context, config)
    if old_context.mode != TradingMode.REPLAY or old_context == context:
        raise ValueError("Snapshot seed must fork into a new independent REPLAY context")
    required = {"cash", "positions", "orders", "fills", "events", "pending_targets", "targets",
        "candidates", "prices", "entitlements", "receivables", "day", "sequence"}
    if not required.issubset(original) or type(original["cash"]) is not int or original["cash"] < 0:
        raise ValueError("Saved snapshot is not a complete original Ledger state")
    state = _map_context(original, old_context.to_dict(), context.to_dict())
    lifecycle = {"schema_version": 1, "method_id": RISK_COMPAT_FULL_EXIT_LIFECYCLE,
        "target_version": 0, "latest_target": None, "remaining_full_exits": {}}
    pending = state["pending_targets"]
    if pending is not None:
        # No reconstructed ranking or date inference: use the original full
        # target event that actually queued these weights at the saved close.
        events = [item for item in state["events"] if item.get("kind") == "targets_generated"]
        event = events[-1] if events else None
        if (not isinstance(event, dict) or event.get("day") != day or event.get("signal_date") != day
                or event.get("targets") != pending or state["targets"] != pending
                or event.get("execution_not_before") != next_day
                or not isinstance(event.get("signal_source"), str) or not event["signal_source"]):
            raise ValueError("Snapshot pending target lacks its exact original close/version provenance")
        intent = PortfolioIntent.full_target(pending, context=context, decided_at=decision_time(day),
            execution_not_before=next_day, source=event["signal_source"])
        if any(weight > config.single_limit + 1e-12 for _, weight in intent.weights) or sum(
                weight for _, weight in intent.weights) > config.gross_limit + 1e-12:
            raise ValueError("Saved pending target exceeds the explicit diagnostic config")
        lifecycle.update(target_version=1, latest_target={"version": 1, "decided_on": day,
            "decided_at": intent.decided_at, "execution_not_before": next_day,
            "source": intent.source, "weights": intent.to_legacy_pending(),
            "version_basis": "independent_seed_version_not_recovered_historical_lifecycle"})
    risk = state.get("pending_risk_exits")
    if risk is not None:
        from ..engine import ReplayRiskExitIntent
        checked = ReplayRiskExitIntent.from_dict(risk)
        if checked.context != context or checked.decided_on != day or checked.execution_on != next_day:
            raise ValueError("Seed risk exit must preserve its exact next-session decision")
    state["prior_volume_execution_policy"] = binding
    state["full_exit_lifecycle"] = lifecycle
    state["research_snapshot_seed"] = {"schema": SEED_SCHEMA,
        "method_id": METHOD_ID, "source_file": str(path), "source_sha256": digest,
        "source_available_at": source_available_at, "source_observed_at": source_observed_at,
        "source_context": old_context.to_dict(), "context": context.to_dict(),
        "source_day": day, "first_execution_day": next_day,
        "calendar_sha256": hashlib.sha256(json.dumps(days, separators=(",", ":")).encode()).hexdigest(),
        "config": config.to_dict(), "config_basis": "explicit_original_diagnostic_config_not_inferred_from_cash",
        "pending_target_version_basis": "new_seed_version_1_if_original_pending_full_target_else_0",
        "original_historical_lifecycle_version_known": False,
        "source_account_modified": False, "independent_technical_diagnostic": True,
        "research_only": True, "pit_verified": False, "returns_reportable": False,
        "selection_enabled": False, "strategy_validated": False, "execution_admitted": False}
    return state
