"""Annual cap20 continuation must reuse its causal scope and real ledger prefix."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.research.experiment import load_replay_checkpoint, replay, write_json
from trading.research.leader_historical_execution_policy import HistoricalExecutionPolicy
from trading.research.leader_momentum_capacity_continue import check_reference_append, restore_feed_prefix
from trading.research.leader_momentum_year import AnnualFeed
from trading.research.order_capacity_replay import PriorVolumeReplayPolicy
from trading.research.recent_alpha158_panel_replay import ExactSourceRawFeed
from trading.research.shortline_panel_replay import _config
from trading.research.stockdb_action_accounting import StockDBActionAccounting
from trading.target_exit_lifecycle import RISK_COMPAT_FULL_EXIT_LIFECYCLE


CONTEXT = RunContext("standalone_replay", TradingMode.REPLAY)
CAPACITY = "causal_first_seen_cap20"
DAYS = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
        "2025-01-08", "2025-01-09", "2025-01-10"]
SYMBOLS = [f"{code:06}.SSE" for code in range(600001, 600026)]


def _published_batches():
    # Monthly publications are compressed into synthetic consecutive decisions;
    # ranking construction and the annual schedule are outside this integration.
    return {day: {"day": day, "status": "complete", "reason": "monthly_ranking",
                  "candidates": [{"symbol": symbol, "score": float(100 + 10 * index - rank)}
                                 for rank, symbol in enumerate(SYMBOLS[index * 5:(index + 1) * 5])]}
            for index, day in enumerate(DAYS[:5])}


def _annual_feed(config, capital_policy, batches):
    source_days = ["2024-12-31"] + DAYS
    raw = pd.DataFrame([
        {"date": day, "code": "sh." + symbol[:6],
         "open": f"{10 + index / 10:.2f}", "high": f"{10 + index / 10:.2f}",
         "low": f"{10 + index / 10:.2f}", "close": f"{10 + index / 10:.2f}",
         "preclose": f"{10 + max(0, index - 1) / 10:.2f}",
         "volume": "1000000", "amount": str((10 + index / 10) * 1_000_000),
         "tradestatus": "1", "isST": "0", "adjustflag": "3"}
        for index, day in enumerate(source_days) for symbol in SYMBOLS])
    base = ExactSourceRawFeed(raw, source_days + ["2025-01-13"], SYMBOLS)
    base.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in SYMBOLS}
    universe = pd.DataFrame({"symbol": SYMBOLS, "code": ["sh." + symbol[:6] for symbol in SYMBOLS]})
    accounting = StockDBActionAccounting(
        [], [], [], {(code, 2025): True for code in universe.code}, universe, {"rename": None},
        SYMBOLS, source_days, DAYS,
        {"price_source": "independent_baostock_raw", "returns_reportable": False})
    execution = HistoricalExecutionPolicy(
        base, accounting, DAYS, prior_volume_policy=PriorVolumeReplayPolicy(CONTEXT))
    qualification = SimpleNamespace(assess=lambda *args, **kwargs: {"engine_block_reason": None})
    return AnnualFeed(execution, batches, config, DAYS[-2], DAYS[-1], qualification,
                      CAPACITY, capital_policy)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("capital_policy", ["original", "known_close_minimum_entry"])
def test_cap20_checkpoint_restores_scope_pending_T1_and_exact_continuous_account(tmp_path, capital_policy):
    config, batches = _config(100000, 5), _published_batches()
    original_batches = deepcopy(batches)
    full_feed = _annual_feed(config, capital_policy, batches)
    full_folder = tmp_path / "uninterrupted"
    replay(full_feed, config, DAYS, full_folder, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)

    # A fresh replay commits the first four publications. Its final close has
    # both tomorrow's fourth target and today's third cohort's unsettled lots.
    prefix_feed = _annual_feed(config, capital_policy, batches)
    prefix_folder = tmp_path / "prefix"
    replay(prefix_feed, config, DAYS[:4], prefix_folder,
           full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    write_json(prefix_folder / "batches.json", batches)
    write_json(prefix_folder / "decisions.json", prefix_feed.decisions)
    write_json(prefix_folder / "admissions.json", prefix_feed.admission)
    checkpoint = load_replay_checkpoint(
        prefix_folder, config, DAYS, full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    prefix_state = deepcopy(checkpoint["state"])
    assert prefix_state["day"] == DAYS[3]
    assert set(prefix_state["pending_targets"]) == set(SYMBOLS[15:20])
    assert prefix_feed.seen == SYMBOLS[:20]
    prefix_buys = [fill for fill in prefix_state["fills"].values()
                   if fill["side"] == "buy" and fill["day"] == DAYS[3]]
    assert {fill["symbol"] for fill in prefix_buys} == set(SYMBOLS[10:15])
    assert {fill["available_day"] for fill in prefix_buys} == {DAYS[4]}
    assert all(any(lot["qty"] > 0 and lot["available"] == DAYS[4]
                   for lot in prefix_state["positions"][symbol]) for symbol in SYMBOLS[10:15])
    assert prefix_state["prior_volume_execution_policy"]["context"] == CONTEXT.to_dict()

    saved_batches = _read(prefix_folder / "batches.json")
    saved_decisions = _read(prefix_folder / "decisions.json")
    saved_admissions = _read(prefix_folder / "admissions.json")
    saved_prefix = deepcopy((saved_batches, saved_decisions, saved_admissions))
    assert len(saved_decisions) == 4
    assert len(saved_admissions) == (4 if capital_policy == "known_close_minimum_entry" else 0)
    continued_feed = _annual_feed(config, capital_policy, saved_batches)
    assert continued_feed.seen == [] and continued_feed.decisions == [] and continued_feed.admission == []
    restore_feed_prefix(continued_feed, saved_batches, saved_decisions, saved_admissions, DAYS[3])
    assert continued_feed.seen == SYMBOLS[:20]
    assert continued_feed.decisions == saved_decisions and continued_feed.admission == saved_admissions

    continued_folder = tmp_path / "continued"
    result = replay(continued_feed, config, DAYS, continued_folder, checkpoint=checkpoint,
                    full_exit_lifecycle=RISK_COMPAT_FULL_EXIT_LIFECYCLE)
    full_state, continued_state = _read(full_folder / "account.json"), _read(continued_folder / "account.json")
    assert result["reused_sessions"] == 4 and result["newly_completed_sessions"] == 3
    assert _read(continued_folder / "replay.json")["status"] == "completed"
    # Includes cents, lots, pending targets, order/fill IDs, events and full exits.
    assert continued_state == full_state
    pd.testing.assert_frame_equal(pd.read_csv(continued_folder / "daily.csv"),
                                  pd.read_csv(full_folder / "daily.csv"), check_exact=True)
    assert continued_state["events"][:len(prefix_state["events"])] == prefix_state["events"]
    assert all(continued_state["orders"][key] == order for key, order in prefix_state["orders"].items())
    assert all(continued_state["fills"][key] == fill for key, fill in prefix_state["fills"].items())
    assert checkpoint["state"] == prefix_state

    assert continued_feed.seen == full_feed.seen == SYMBOLS[:20]
    assert continued_feed.decisions == full_feed.decisions
    assert continued_feed.admission == full_feed.admission
    assert continued_feed.decisions[:len(saved_decisions)] == saved_decisions
    assert continued_feed.admission[:len(saved_admissions)] == saved_admissions
    assert (saved_batches, saved_decisions, saved_admissions) == saved_prefix
    assert batches == original_batches
    fifth_decisions = [row for row in continued_feed.decisions if row["day"] == DAYS[4]]
    assert len(fifth_decisions) == 1
    assert fifth_decisions[0]["status"] == "NO_CHANGE"
    assert fifth_decisions[0]["reason"] == "capacity_filtered_all_NO_CHANGE"
    assert any(event["kind"] == "no_decision" and event["day"] == DAYS[4]
               and event["reason"] == "capacity_filtered_all_NO_CHANGE"
               for event in continued_state["events"])
    assert not any(event["kind"] == "targets_generated" and event["day"] == DAYS[4]
                   for event in continued_state["events"])
    assert not any(order["symbol"] in SYMBOLS[20:] for order in continued_state["orders"].values())
    fourth_buys = [fill for fill in continued_state["fills"].values()
                   if fill["side"] == "buy" and fill["day"] == DAYS[4]]
    assert {fill["symbol"] for fill in fourth_buys} == set(SYMBOLS[15:20])
    assert {fill["available_day"] for fill in fourth_buys} == {DAYS[5]}
    if capital_policy == "known_close_minimum_entry":
        fifth_admissions = [row for row in continued_feed.admission if row["day"] == DAYS[4]]
        assert len(fifth_admissions) == 1 and fifth_admissions[0]["status"] == "complete"
        assert [row["symbol"] for row in fifth_admissions[0]["candidates"]] == SYMBOLS[20:]

    assert any(event["kind"] == "targets_generated" and event["day"] == DAYS[-2]
               and event["targets"] == {} for event in continued_state["events"])
    assert {fill["symbol"] for fill in continued_state["fills"].values()
            if fill["side"] == "sell" and fill["day"] == DAYS[-1]} == set(SYMBOLS[15:20])
    assert not any(sum(lot["qty"] for lot in lots) for lots in continued_state["positions"].values())
    assert continued_state["pending_targets"] is None
    assert not continued_state["full_exit_lifecycle"]["remaining_full_exits"]
    for adapter in (full_feed, prefix_feed, continued_feed):
        assert not adapter.blocks and adapter.execution.accounting.checks and adapter.execution.checks
        assert all(adapter.execution.reference_ok(row) for row in adapter.execution.checks)


@pytest.mark.parametrize("mutation", ["raw_preclose_with_valid_hash", "event_on_checkpoint_day"])
def test_reference_append_rejects_changed_raw_or_nonfuture_event(tmp_path, mutation):
    old_view, new_view, event_view = (tmp_path / name for name in ("old", "new", "events"))
    for folder in (old_view, new_view, event_view):
        folder.mkdir()
    event = {"code": "sz.300100", "dividOperateDate": "2025-05-15"}
    old_rows = pd.DataFrame([
        {"code": "sz.300100", "date": day, "preclose": preclose,
         "previous_observed_close": previous,
         "reference_formula_status": "reference_mismatch" if day == "2025-05-15" else "reference_match",
         "reference_formula_basis": "reported_entitlement_terms_not_independent_reference",
         "expected_reference_rmb": expected, "reference_error_rmb": error}
        for day, preclose, previous, expected, error in (
            ("2025-05-15", 54.94, 76.92, "54.8", -0.14),
            ("2025-05-16", 54.92, 54.92, "54.92", 0.0))])
    new_rows = old_rows.copy(deep=True)
    new_rows.loc[0, ["reference_formula_status", "reference_formula_basis",
                     "expected_reference_rmb", "reference_error_rmb"]] = [
        "reference_match", "synthetic_reviewed_formula_fixture", "54.94", 0.0]
    metadata = {}
    for folder, rows, evidence in ((old_view, old_rows, []), (new_view, new_rows, [event])):
        rows.to_parquet(folder / "raw_reference_changes.parquet", index=False)
        write_json(folder / "distribution_reference_evidence.json", evidence)
        record = {"status": "research_reference_presence_diagnostic",
                  "event_view": str(event_view.resolve()), "execution_admitted": False,
                  "files": {name: sha256((folder / name).read_bytes()).hexdigest()
                            for name in ("raw_reference_changes.parquet", "distribution_reference_evidence.json")}}
        if folder == new_view:
            record["distribution_amendment"] = {
                "parent_reference_view": str(old_view.resolve()),
                "events": [[event["code"], event["dividOperateDate"]]],
                "entitlements_changed": False, "raw_prices_rescanned": False}
        metadata[folder] = record
        write_json(folder / "result.json", record)

    # First establish that the minimal fixture is a valid future-only append.
    accepted = check_reference_append(old_view, new_view, event_view, ["2025-05-14"])
    assert accepted["added_events"] == [("sz.300100", "2025-05-15")]
    assert accepted["reference_rows_refreshed"] == 1
    last_days = ["2025-05-14"]
    if mutation == "raw_preclose_with_valid_hash":
        new_rows.loc[0, "preclose"] = 54.95
        source = new_view / "raw_reference_changes.parquet"
        new_rows.to_parquet(source, index=False)
        metadata[new_view]["files"][source.name] = sha256(source.read_bytes()).hexdigest()
        write_json(new_view / "result.json", metadata[new_view])
        error = "changed raw/non-formula facts"
    else:
        last_days = ["2025-05-15"]  # The added event is on the saved close, not in its future.
        error = "strictly after every checkpoint close"
    with pytest.raises(ValueError, match=error):
        check_reference_append(old_view, new_view, event_view, last_days)
