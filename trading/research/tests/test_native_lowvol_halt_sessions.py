"""Known native halts through real Engine/Ledger, without filling raw blanks.

Reuse the existing small synthetic source-loader fixture. No historical market
replay, source request, model or live/paper worker is started by these checks.
"""
from pathlib import Path

import pandas as pd
import pytest

from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research.tests.test_native_lowvol_forward import (
    A, D, E, F, G, execute, first_commit, fixture, head,
)


def halt(e, day):
    raw = e.raw[day]["canonical"]
    raw.loc[:, "tradestatus"] = 0
    raw.loc[:, ["open", "high", "low", "close", "preclose", "preclose_for_limits"]] = 10.
    raw.loc[:, ["volume", "amount"]] = float("nan")


def run_through(e, day):
    return execute(e, days=tuple(value for value in (D, E, F, G) if value <= day), through=day)


def test_two_known_halt_sessions_commit_stale_source_valuations_and_keep_exit_pending(fixture):
    e = fixture
    first = first_commit(e)
    quantity = Ledger(first["state"], e.config, RunContext.from_dict(first["context"])).quantity(A)
    halt(e, F)
    halt(e, G)
    e.publish(E, F, {}, account=first)
    result = run_through(e, F)
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    paused = head(e)
    assert result["stale_symbols"] == [A]
    assert result["remaining_full_exits"][A]["remaining_quantity"] == quantity
    e.publish(F, G, account=paused)
    result = run_through(e, G)
    final = head(e)
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    assert final["state"]["day"] == G and [row["date"] for row in final["daily"]] == [E, F, G]
    assert final["state"]["orders"] == first["state"]["orders"]
    assert final["state"]["fills"] == first["state"]["fills"]
    assert final["state"]["cash"] == first["state"]["cash"]
    assert result["remaining_full_exits"][A]["remaining_quantity"] == quantity
    for row in final["daily"][-2:]:
        assert row["stale_symbols"] == [A]
        valuation = row["native_halt_valuation"]
        assert len(valuation) == 1 and valuation[0]["symbol"] == A
        assert valuation[0]["source_carried_close_rmb"] == 10.
        assert valuation[0]["source_volume_missing"] is valuation[0]["source_amount_missing"] is True
        assert valuation[0]["active_return_observation"] is False
        assert valuation[0]["current_traded_price_available"] is False
        assert any(event["kind"] == "order_blocked" and event.get("reason") == "suspended" for event in row["events"])
        assert pd.isna(e.raw[row["date"]]["canonical"].iloc[0]["volume"])
    assert result["native_halt_valuation"] == final["daily"][-1]["native_halt_valuation"]
    assert result["metrics"] is None and result["returns_reportable"] is False


def test_pure_NO_CHANGE_can_reopen_after_halt_without_requiring_unused_volume(fixture):
    e = fixture
    first = first_commit(e)
    halt(e, F)
    e.publish(E, F, account=first)
    paused_result = run_through(e, F)
    assert paused_result["status"] == "advanced_independent_native_lowvol_research_account", paused_result
    paused = head(e)
    e.publish(F, G, account=paused)
    result = run_through(e, G)
    final = head(e)
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    assert final["state"]["cash"] == first["state"]["cash"]
    assert final["state"]["positions"] == first["state"]["positions"]
    assert final["state"]["orders"] == first["state"]["orders"]
    assert final["state"]["fills"] == first["state"]["fills"]
    assert result["stale_symbols"] == result["native_halt_valuation"] == []
    assert final["daily"][-2]["native_halt_valuation"][0]["active_return_observation"] is False
    assert pd.isna(e.raw[F]["canonical"].iloc[0]["volume"])


@pytest.mark.parametrize("requested", ["full_exit", "forced_ST_NO_CHANGE", "residual_exit_NO_CHANGE"])
def test_reopened_possible_trade_still_requires_exact_prior_volume_and_does_not_commit(fixture, requested):
    e = fixture
    first = first_commit(e)
    halt(e, F)
    e.publish(E, F, {} if requested == "residual_exit_NO_CHANGE" else None, account=first)
    paused_result = run_through(e, F)
    assert paused_result["status"] == "advanced_independent_native_lowvol_research_account", paused_result
    paused = head(e)
    if requested == "forced_ST_NO_CHANGE":
        e.raw[G]["canonical"].loc[:, "isST"] = 1
        e.raw[G]["canonical"].loc[:, "is_st"] = True
    e.publish(F, G, {} if requested == "full_exit" else None, account=paused)
    before = (e.run_dir / "head.json").read_bytes()
    result = run_through(e, G)
    assert result["status"] == "failed_required_evidence_or_engine_session_no_commit", result
    assert "participation volume must be known" in result["error"]
    assert result["session_not_committed"] is True and result["newly_committed_sessions"] == 0
    assert result["last_execution_day"] == F
    assert (e.run_dir / "head.json").read_bytes() == before
    assert not (e.run_dir / "journal" / (G + ".json")).exists()


@pytest.mark.parametrize("unknown", ["status", "missing_row", "blank_close"])
def test_unknown_current_status_or_required_raw_row_still_stops_even_for_NO_CHANGE(fixture, unknown):
    e = fixture
    first = first_commit(e)
    if unknown == "status":
        e.raw[F]["canonical"].loc[:, "tradestatus"] = None
    elif unknown == "missing_row":
        e.raw[F]["canonical"] = e.raw[F]["canonical"].iloc[:0].copy()
    else:
        halt(e, F)
        e.raw[F]["canonical"].loc[:, "close"] = float("nan")
    e.publish(E, F, account=first)
    before = (e.run_dir / "head.json").read_bytes()
    result = run_through(e, F)
    assert result["status"] == "failed_required_evidence_or_engine_session_no_commit", result
    assert "explicit current raw prices and known status" in result["error"]
    assert result["last_execution_day"] == E and result["newly_committed_sessions"] == 0
    assert (e.run_dir / "head.json").read_bytes() == before
    assert not Path(e.run_dir / "journal" / (F + ".json")).exists()
