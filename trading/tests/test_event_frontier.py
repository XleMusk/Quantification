from dataclasses import replace

import pytest

from trading.contracts import RunContext, Signal, SignalFrame, SignalKind, SignalTime, SecurityId, TradingMode
from trading.event_frontier import summarize_frontiers, validate_signal_frontier


DAY = "2024-01-02"
NEXT = "2024-01-03"
CONTEXT = RunContext("frontier-test", TradingMode.REPLAY)
SECURITY = SecurityId("000001.SZSE")


def frame(available_at="2024-01-02T15:04:00+08:00"):
    stamp = SignalTime("2024-01-02T15:00:00+08:00", available_at)
    return SignalFrame(DAY, (Signal(SECURITY, .2, SignalKind.SCORE, stamp),), "test", CONTEXT)


def test_frontier_records_signal_availability_and_next_session_execution():
    result = validate_signal_frontier(frame(), decision_at="2024-01-02T15:05:00+08:00", execution_day=NEXT)
    assert result["status"] == "validated_with_signals"
    assert result["signal_available_max"] == "2024-01-02T15:04:00+08:00"
    assert result["execution_not_before"] == NEXT


def test_frontier_rejects_same_session_execution():
    with pytest.raises(ValueError, match="after"):
        validate_signal_frontier(frame(), decision_at="2024-01-02T15:05:00+08:00", execution_day=DAY)


def test_frontier_summary_retains_negative_invalid_state():
    summary = summarize_frontiers([{"status": "invalid", "signal_count": 1, "availability_evidence": []}])
    assert summary["status"] == "invalid"
    assert summary["invalid_sessions"] == 1
