from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import (PortfolioIntent, RunContext, SignalUnavailable, TradingMode, decision_time)
from trading.engine import advance, reduce_replay_target
from trading.ledger import new_state
from trading.market_snapshot import INGESTION_SCOPE
from trading.research import volatility_timing as timing

DAY = "2024-01-31"
CONTEXT = RunContext("variance-test", TradingMode.REPLAY)
SYMBOL = "600000.SSE"
CONFIG = replace(StrategyConfig(), signal="momentum", rebalance_sessions=1, initial_cash=10000,
                 holdings=1, retain_rank=2, gross_limit=.8, single_limit=.8).validate()


def intent():
    return PortfolioIntent.full_target({SYMBOL: .8}, context=CONTEXT, decided_at=decision_time(DAY),
                                       execution_not_before="2024-02-01", source="rule:momentum")


def table():
    days = pd.bdate_range("2023-01-02", "2024-03-01").strftime("%Y-%m-%d").tolist()
    rows = []
    for index, day in enumerate(timing.month_ends(days, "2024-02-29")):
        rows.append({"date": day, "daily_observations": 20, "realized_variance": .001 * (1 + index % 4),
                     "monthly_return": .01 * (index % 5 - 2)})
    return pd.DataFrame(rows)


def fitted():
    return {"train_end": timing.TRAIN_END, "paired_months": 11, "constant": .001,
            "fixed_control_scale": .5}


def feed():
    days = [DAY, "2024-02-01", "2024-02-02", "2024-02-05"]
    row = {"open": 10., "close": 10., "preclose": 10., "isST": 0, "tradestatus": 1}
    return SimpleNamespace(calendar=days, symbols=[SYMBOL], rows={d: {SYMBOL: dict(row)} for d in days},
        instruments={SYMBOL: {"ipoDate": "2000-01-01", "outDate": ""}}, actions=[],
        volumes=pd.DataFrame(1000000., index=days, columns=[SYMBOL]),
        manifest={"start": days[0], "end": days[-1], "scope": INGESTION_SCOPE},
        next_day=lambda d: days[days.index(d) + 1] if d != days[-1] else "9999-12-31",
        scores=lambda day, config: [{"symbol": SYMBOL, "score": 1.}])


def test_target_policy_can_only_reduce_same_context_and_next_session():
    reduced = reduce_replay_target(intent(), lambda target: replace(target, weights=((SYMBOL, .4),)))
    assert reduced.to_legacy_pending() == {SYMBOL: .4}
    assert reduce_replay_target(intent(), lambda target: replace(target, weights=())).to_legacy_pending() == {}
    missing = reduce_replay_target(intent(), lambda target: PortfolioIntent.no_change(
        "missing", context=target.context, decided_at=target.decided_at))
    assert missing.reason == "missing" and missing.to_legacy_pending() is None


@pytest.mark.parametrize("change,reason", [
    ({"weights": ((SYMBOL, .9),)}, "only reduce"),
    ({"weights": (("000001.SZSE", .1),)}, "only reduce"),
    ({"execution_not_before": "2024-02-02"}, "next-session"),
    ({"context": RunContext("other", TradingMode.REPLAY)}, "account/mode"),
    ({"context": RunContext("variance-test", TradingMode.PAPER)}, "account/mode"),
    ({"decided_at": "2024-01-31T15:04:00+08:00"}, "decision time"),
])
def test_invalid_research_policy_not_admitted(change, reason):
    with pytest.raises(ValueError, match=reason):
        reduce_replay_target(intent(), lambda target: replace(target, **change))


def test_untyped_and_delta_policy_outputs_not_admitted():
    with pytest.raises(ValueError, match="typed"):
        reduce_replay_target(intent(), lambda target: {})
    with pytest.raises(ValueError, match="kind"):
        reduce_replay_target(intent(), lambda target: replace(PortfolioIntent.delta({SYMBOL: -.1}),
            context=target.context, decided_at=target.decided_at))


def test_paper_policy_rejected_before_mutation_or_callback():
    state, base = new_state(10000), feed()
    before = deepcopy(state)
    with patch("trading.engine.reduce_replay_target") as callback:
        with pytest.raises(ValueError, match="replay-only"):
            advance(state, base, DAY, CONFIG, 0, context=RunContext("paper", TradingMode.PAPER),
                    target_policy=lambda target: target)
        callback.assert_not_called()
    assert state == before


def test_reduced_target_executes_in_same_ledger_only_on_following_session():
    state, base = new_state(10000), feed()
    policy = lambda target: replace(target, weights=((SYMBOL, .4),))
    advance(state, base, DAY, CONFIG, 0, target_policy=policy)
    assert not state["fills"] and state["pending_targets"] == {SYMBOL: .4}
    advance(state, base, "2024-02-01", CONFIG, 1, target_policy=policy)
    assert sum(lot["qty"] for lot in state["positions"][SYMBOL]) == 400
    assert all(row["day"] == "2024-02-01" for row in state["fills"].values())
    assert 590000 < state["cash"] < 600000


def test_no_change_preserves_target_and_unavailable_signal_does_not_call_overlay():
    state, base = new_state(10000), feed()
    advance(state, base, DAY, CONFIG, 0)
    previous = deepcopy(state["targets"])
    no_change = lambda target: PortfolioIntent.no_change("missing_variance", context=target.context,
                                                        decided_at=target.decided_at)
    advance(state, base, "2024-02-01", CONFIG, 1, target_policy=no_change)
    assert state["targets"] == previous and state["pending_targets"] is None
    assert state["events"][-1]["kind"] == "no_decision"
    base.scores = lambda day, config: SignalUnavailable("missing_score")
    with patch("trading.engine.reduce_replay_target") as callback:
        advance(state, base, "2024-02-02", CONFIG, 2, target_policy=no_change)
        callback.assert_not_called()
    assert any(state["positions"].values())


def test_existing_st_exit_and_limit_down_block_survive_overlay():
    state, base = new_state(10000), feed()
    policy = lambda target: replace(target, weights=((SYMBOL, .4),))
    advance(state, base, DAY, CONFIG, 0, target_policy=policy)
    advance(state, base, "2024-02-01", CONFIG, 1, target_policy=policy)
    base.scores = lambda day, config: SignalUnavailable("not_month_end")
    base.rows["2024-02-02"][SYMBOL].update(open=9., close=9., isST=1)
    advance(state, base, "2024-02-02", CONFIG, 2, target_policy=policy)
    assert any(state["positions"].values())
    assert any(e.get("reason") == "limit_down_queue_unknown" for e in state["events"])
    base.rows["2024-02-05"][SYMBOL].update(open=9.1, close=9.1, preclose=9., isST=1)
    advance(state, base, "2024-02-05", CONFIG, 3, target_policy=policy)
    assert not any(state["positions"].values())


def test_constant_uses_only_2023_lagged_pairs_not_forward_variance():
    values = table()
    actual = timing.calibration(values)
    train = values.loc[values.date.str.startswith("2023")]
    expected = train.monthly_return.iloc[1:].std() / (
        train.monthly_return.iloc[1:] / train.realized_variance.shift().iloc[1:]).std()
    assert actual["constant"] == pytest.approx(expected)
    assert actual["paired_months"] == 11
    assert 0 < actual["fixed_control_scale"] <= 1
    changed = values.copy()
    changed.loc[~changed.date.str.startswith("2023"), ["monthly_return", "realized_variance"]] = 1e9
    assert timing.calibration(changed) == actual


def test_calibration_missing_month_zero_variance_and_future_training_rejected():
    values = table()
    with pytest.raises(ValueError, match="twelve"):
        timing.calibration(values.iloc[1:])
    values.loc[0, "realized_variance"] = 0
    with pytest.raises(ValueError, match="variance"):
        timing.calibration(values)
    with pytest.raises(ValueError, match="calibration"):
        timing.VariancePolicy(table(), {**fitted(), "train_end": "2024-12-31"}, timing.VARIANTS[2], "2025-12-31")


def test_inverse_variance_not_inverse_volatility_or_leverage():
    values = table()
    values.loc[values.date.eq(DAY), "realized_variance"] = .004
    policy = timing.VariancePolicy(values, fitted(), timing.VARIANTS[2], "2025-12-31")
    assert policy(intent()).to_legacy_pending() == {SYMBOL: .2}
    values.loc[values.date.eq(DAY), "realized_variance"] = .0001
    policy = timing.VariancePolicy(values, fitted(), timing.VARIANTS[2], "2025-12-31")
    assert policy(intent()).to_legacy_pending() == {SYMBOL: .8}
    fixed = timing.VariancePolicy(values, fitted(), timing.VARIANTS[1], "2025-12-31")
    assert fixed(intent()).to_legacy_pending() == {SYMBOL: .4}


@pytest.mark.parametrize("value", [None, 0., float("nan")])
def test_missing_or_invalid_variance_is_no_change_not_full_cash(value):
    values = table()
    if value is None:
        values = values.loc[~values.date.eq(DAY)]
    else:
        values.loc[values.date.eq(DAY), "realized_variance"] = value
    policy = timing.VariancePolicy(values, fitted(), timing.VARIANTS[2], "2025-12-31")
    assert policy(intent()).to_legacy_pending() is None


def test_monthly_variance_is_sum_of_daily_deviations_and_has_no_future_input():
    days = pd.bdate_range("2023-01-02", "2023-03-02").strftime("%Y-%m-%d").tolist()
    returns = np.array([.01 if index % 2 else -.005 for index in range(len(days))])
    closes = 100 * (1 + returns).cumprod()
    frame = pd.DataFrame({"date": days, "code": "sh.000300", "close": closes,
        "preclose": np.r_[100, closes[:-1]], "open": closes, "high": closes, "low": closes,
        "volume": 100, "amount": 1000, "pctChg": returns * 100})
    actual = timing.variance_table(frame.loc[frame.date.le("2023-02-28")], days, "2023-02-28")
    jan = returns[np.array([d.startswith("2023-01") for d in days])]
    assert actual.iloc[0].realized_variance == pytest.approx(((jan - jan.mean()) ** 2).sum())
    earlier = timing.variance_table(frame.loc[frame.date.le("2023-01-31")], days, "2023-01-31")
    pd.testing.assert_frame_equal(earlier, actual.iloc[:1])
    bad = frame.copy()
    bad.loc[1, "preclose"] += 1
    with pytest.raises(ValueError, match="continuity"):
        timing.variance_table(bad.loc[bad.date.le("2023-02-28")], days, "2023-02-28")


def test_2026_boundary_rejected_before_loading_prices():
    with patch.object(timing, "HistoricalFeed") as loader:
        with pytest.raises(ValueError, match="2024-2025"):
            timing.run("unused", "unused", end="2026-09-24")
        loader.assert_not_called()


def test_protocol_precedes_all_three_same_basis_replays_and_keeps_failures(tmp_path):
    base = feed()
    base.folder = tmp_path / "snapshot"
    base.folder.mkdir()
    for name in ("manifest.json", "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"):
        (base.folder / name).write_text("fixed", encoding="utf-8")
    calls = []
    def consume(wrapper, config, dates, destination, *, target_policy):
        saved = json.loads(next((tmp_path / "outputs/market_replay").glob("*/protocol.json")).read_text())
        assert saved["signals"] == list(timing.VARIANTS) and saved["calibration"]["train_end"] == timing.TRAIN_END
        assert not saved["tuning"] and not saved["execution_admitted"]
        calls.append(config.to_dict())
        target_policy(intent())
        if len(calls) == 2:
            return {"status": "failed", "error": "unresolved corporate action", "completed_sessions": 1}
        return {"status": "completed", "completed_sessions": len(dates), "net_return": -.1,
                "max_drawdown": -.2, "sharpe_zero_rf_252": -1., "fees": 5., "worst_day": -.02}
    with patch.object(timing, "ROOT", tmp_path), patch.object(timing, "DATA_ROOT", tmp_path / "data"), \
            patch.object(timing, "HistoricalFeed", return_value=base), \
            patch.object(timing, "index_inputs", return_value=(table(), {"price_cutoff": "2024-02-02"})), \
            patch.object(timing, "source_receipts", return_value=[]), \
            patch.object(timing, "implementation_hashes", return_value={}), \
            patch.object(timing, "__file__", str(tmp_path / "candidate.py")), \
            patch.object(timing, "baseline_ledger_result", side_effect=consume):
        (tmp_path / "candidate.py").write_text("fixture", encoding="utf-8")
        out = timing.run(base.folder, tmp_path / "indices", DAY, "2024-02-02")
    result = json.loads((out / "run.json").read_text())
    assert len(calls) == 3 and calls[0] == calls[1] == calls[2]
    assert result["status"] == "failed_volatility_adaptation_diagnostic"
    assert result["purpose"] == "fixed_variance_overlay_2024_2025_diagnostic"
    assert [r.get("net_return") for r in result["results"]] == [-.1, None, -.1]
    assert not result["strategy_validated"] and not result["live_ready"]


@pytest.mark.parametrize("change", ["later_tail", "prefix", "next_session"])
def test_index_calendar_checks_used_prefix_and_next_session_only(tmp_path, change):
    days = pd.bdate_range("2023-01-02", "2024-03-05").strftime("%Y-%m-%d").tolist()
    end = "2024-02-29"
    returns = np.array([.01 if i % 2 else -.005 for i in range(len(days))])
    closes = 100 * (1 + returns).cumprod()
    frame = pd.DataFrame({"date": days, "code": "sh.000300", "close": closes,
        "preclose": np.r_[100, closes[:-1]], "open": closes, "high": closes, "low": closes,
        "volume": 100, "amount": 1000, "pctChg": returns * 100})
    frame.to_parquet(tmp_path / "prices.parquet", index=False)
    (tmp_path / "raw.csv").write_text("fixture", encoding="utf-8")
    receipt = {"file": "prices.parquet", "raw_file": "raw.csv"}
    for name, digest in (("file", "sha256"), ("raw_file", "raw_sha256")):
        receipt[digest] = sha256((tmp_path / receipt[name]).read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    index_calendar = days[:]
    if change == "later_tail":
        index_calendar = days[:-1]
    else:
        index_calendar.remove("2024-02-01" if change == "prefix" else "2024-03-01")
    with patch.object(timing, "load_indices", return_value=({"receipts": {"sh.000300": receipt}}, index_calendar)):
        if change == "later_tail":
            values, observed = timing.index_inputs(tmp_path, days, end)
            assert values.date.iloc[-1] == end
            assert observed["price_rows_consumed"] == sum(day <= end for day in days)
        else:
            with pytest.raises(ValueError, match="cutoff and its next"):
                timing.index_inputs(tmp_path, days, end)


def test_bulk_selected_cases_preserve_rules_and_sole_consumer(tmp_path):
    base = feed()
    base.folder = tmp_path / "price_view"
    base.folder.mkdir()
    (base.folder / "result.json").write_text("{}", encoding="utf-8")
    base.unsupported_instruments = {"689009.SSE": "unsupported CDR"}
    base.symbols.append("689009.SSE")
    inputs = {key: str(tmp_path / key) for key in (
        "price_view", "event_view", "reference_view", "raw_view", "listing_master")}
    base.manifest.update(scope=timing.BULK_SCOPE, **inputs, source_codes=2, canonical_codes=2,
                         unsupported_instruments=base.unsupported_instruments, limitations=["synthetic fixture"])
    variants, calls = timing.VARIANTS[1:], []
    def consume(wrapper, config, dates, destination, *, target_policy):
        saved = json.loads(next((tmp_path / "outputs/market_replay").glob("*/protocol.json")).read_text())
        assert saved["signals"] == list(variants) and saved["predefined_variants"] == list(timing.VARIANTS)
        assert saved["stock_input_sha256"] == {} and saved["bulk_inputs"]["canonical_codes"] == 2
        assert wrapper.base is base and wrapper.variant == "monthly_momentum"
        calls.append((config.to_dict(), dates))
        target_policy(intent())
        return {"status": "completed", "completed_sessions": len(dates), "net_return": -.1,
                "max_drawdown": -.2, "sharpe_zero_rf_252": -1., "fees": 5., "worst_day": -.02}
    with patch.object(timing, "ROOT", tmp_path), patch.object(timing, "DATA_ROOT", tmp_path / "data"), \
            patch.object(timing, "BulkResearchFeed", return_value=base) as loader, \
            patch.object(timing, "HistoricalFeed") as old_loader, \
            patch.object(timing, "index_inputs", return_value=(table(), {"price_cutoff": "2024-02-02"})), \
            patch.object(timing, "source_receipts", return_value=[]), \
            patch.object(timing, "implementation_hashes", return_value={}), \
            patch.object(timing, "__file__", str(tmp_path / "candidate.py")), \
            patch.object(timing, "baseline_ledger_result", side_effect=consume):
        (tmp_path / "candidate.py").write_text("fixture", encoding="utf-8")
        out = timing.run(None, tmp_path / "indices", DAY, "2024-02-02", price_view=base.folder,
            event_view=tmp_path / "events", reference_view=tmp_path / "refs",
            listing_master=tmp_path / "listing.csv", variants=variants)
        loader.assert_called_once_with(base.folder, tmp_path / "events", tmp_path / "refs", tmp_path / "listing.csv")
        old_loader.assert_not_called()
    record = json.loads((out / "run.json").read_text())
    assert len(calls) == 2 and calls[0] == calls[1]
    assert [row["signal"] for row in record["results"]] == list(variants)
    assert record["board_counts"] == {"main": 1, "unsupported": 1}
    assert not record["strategy_validated"] and not record["live_ready"]


def test_partial_bulk_inputs_and_duplicate_variants_stop_before_loading():
    with patch.object(timing, "BulkResearchFeed") as loader, patch.object(timing, "HistoricalFeed") as old_loader:
        with pytest.raises(ValueError, match="aligned"):
            timing.run(None, "unused", price_view="price", event_view="events")
        with pytest.raises(ValueError, match="unique predefined"):
            timing.run("unused", "unused", variants=[timing.VARIANTS[1]] * 2)
        loader.assert_not_called()
        old_loader.assert_not_called()
