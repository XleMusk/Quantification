"""Convert fixed F01 fact results into the existing typed SignalFrame."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import math

from ..contracts import RunContext, SignalFrame, SignalUnavailable, decision_time
from .f01_financial_factor import F01Result, READY


SIGNAL_SOURCE = "financial_f01_asset_normalized_surprise_v1"


def signal_frame(results: Mapping[str, F01Result], day: str, context: RunContext,
                 *, source: str = SIGNAL_SOURCE):
    """Build a deterministic ranked frame, preserving non-ready reasons.

    A result is admitted only when it is a finite READY F01 value and its
    conservative availability timestamp fits the typed SignalFrame decision
    clock.  Missing/unknown values remain diagnostics; they do not become a
    zero score or an empty successful target.
    """

    if not isinstance(results, Mapping) or not isinstance(context, RunContext):
        raise TypeError("results and context have invalid types")
    rows, rejected = [], []
    decision_at = decision_time(day)
    for symbol, result in results.items():
        if not isinstance(symbol, str) or not isinstance(result, F01Result):
            raise TypeError("F01 signal inputs must map symbols to F01Result values")
        if result.status != READY or result.value is None:
            rejected.append({"symbol": symbol, "status": result.status, "reason": result.reason})
            continue
        value = float(result.value)
        if not math.isfinite(value) or result.available_at is None:
            rejected.append({"symbol": symbol, "status": result.status, "reason": "nonfinite_or_unavailable"})
            continue
        available = result.available_at.end or result.available_at.start
        if available.isoformat() > decision_at:
            rejected.append({"symbol": symbol, "status": result.status, "reason": "factor_available_after_decision"})
            continue
        rows.append({"symbol": symbol, "score": value, "factor_value": str(result.value),
                     "factor_definition_version": result.definition_version,
                     "factor_fact_ids": list(result.fact_ids),
                     "factor_available_at": available.isoformat(),
                     "factor_quality_state": result.status})
    rows.sort(key=lambda row: (-row["score"], row["symbol"]))
    if not rows:
        reason = "f01_no_ready_candidates" if rejected else "f01_no_inputs"
        return SignalUnavailable(reason)
    # Signal diagnostics are intentionally numeric in the shared contract.
    # Keep the full fact provenance in ``signal_provenance`` below rather than
    # weakening that contract with arbitrary strings or nested payloads.
    frame = SignalFrame.from_legacy(
        [{"symbol": row["symbol"], "score": row["score"]} for row in rows], day, source, context,
        # SignalTime's available_at describes the computation becoming usable
        # at this decision, while factor_available_at remains the dependency
        # provenance in diagnostics.  A dependency earlier than the decision
        # must not be passed as a signal timestamp before its market event.
        available_at=[decision_at for _ in rows],
    )
    return frame


def signal_provenance(results: Mapping[str, F01Result], *, accepted_symbols=None):
    """Return immutable, serializable F01 provenance beside a SignalFrame."""

    allowed = set(accepted_symbols) if accepted_symbols is not None else None
    return {
        symbol: result.to_dict()
        for symbol, result in sorted(results.items())
        if allowed is None or symbol in allowed
    }


__all__ = ["SIGNAL_SOURCE", "signal_frame", "signal_provenance"]
