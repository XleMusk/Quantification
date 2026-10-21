from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.engine import advance
from trading.ledger import new_state
from trading.research import stockdb_rolling_low_volatility as rolling


VARIANT = "low_volatility_252_20"


def raw_frame():
    dates = pd.bdate_range("2023-01-02", periods=300).strftime("%Y-%m-%d")
    records = []
    for index in range(22):
        closes = 10 + index / 10 + np.arange(len(dates)) * .004 + np.sin(np.arange(len(dates)) / (4 + index)) * .12
        for position, day in enumerate(dates):
            close = round(float(closes[position]), 2)
            records.append({"code": f"600{index:03d}", "symbol": f"600{index:03d}.SSE", "date": day,
                            "open": close, "high": close + .05, "low": close - .05, "close": close,
                            "pre_close": round(float(closes[max(0, position - 1)]), 2),
                            "volume": 50_000_000., "amount": 500_000_000., "is_st": False})
    return pd.DataFrame(records)


def snapshot(path, frame):
    path.mkdir()
    parquet = path / "bars.parquet"
    frame[list(rolling.SOURCE_COLUMNS)].to_parquet(parquet, index=False)
    manifest = {"status": "complete_inventory_only", "execution_admitted": False, "chunks_expected": 1,
                "chunks": [{"parquet_file": parquet.name, "parquet_sha256": sha256(parquet.read_bytes()).hexdigest()}]}
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    frame = raw_frame()
    old_end, new_end = "2024-01-22", "2024-01-26"
    old = snapshot(tmp_path / "old", frame.loc[frame.date.le(old_end)])
    new = snapshot(tmp_path / "new", frame.loc[frame.date.le(new_end)])
    _, record = rolling.run(old, "2023-01-02", old_end, variants=(VARIANT,))
    previous = next((tmp_path / "project/outputs/stockdb_rolling_low_volatility").glob("*/run.json"))
    return frame, old, new, old_end, new_end, previous, record


def account(record):
    return json.loads((Path(record["artifacts"]) / VARIANT / "account.json").read_text(encoding="utf-8"))


def test_continuation_matches_cold_engine_ledger_and_keeps_rank_prefix(inputs):
    _, _, new, old_end, new_end, previous, saved = inputs
    old_account = account(saved)
    assert old_account["orders"] and old_account["fills"] and old_account["positions"]
    _, continued = rolling.run(new, "2023-01-02", new_end, variants=(VARIANT,), resume_run=previous)
    _, cold = rolling.run(new, "2023-01-02", new_end, variants=(VARIANT,))
    assert account(continued) == account(cold)
    assert account(saved) == old_account
    result = continued["results"][0]
    assert result["reused_sessions"] == saved["sessions"]
    assert result["newly_completed_sessions"] == 4
    assert continued["continuation_status"] == "continued_research_only"
    assert continued["account_reset"] is False and continued["execution_admitted"] is False
    before = pd.read_csv(Path(saved["artifacts"]) / f"{VARIANT}_ranks.csv")
    after = pd.read_csv(Path(continued["artifacts"]) / f"{VARIANT}_ranks.csv")
    pd.testing.assert_frame_equal(before, after.loc[after.date.le(old_end)].reset_index(drop=True))


def test_identical_resume_does_not_replay_or_reset_cash(inputs, monkeypatch):
    _, old, _, old_end, _, previous, saved = inputs
    original = account(saved)
    def unexpected(*args, **kwargs):
        pytest.fail("No new session must not build features or execute a replay")
    monkeypatch.setattr(rolling, "RollingStockDBFeed", unexpected)
    monkeypatch.setattr(rolling, "baseline_ledger_result", unexpected)
    _, result = rolling.run(old, "2023-01-02", old_end, variants=(VARIANT,), resume_run=previous)
    assert result["status"] == result["continuation_status"] == "no_new_session"
    assert result["artifacts"] == saved["artifacts"]
    assert result["newly_completed_sessions"] == 0
    assert account(result) == original


@pytest.mark.parametrize("change, message", [("raw", "raw history prefix"), ("calendar", "calendar prefix")])
def test_changed_processed_inputs_are_rejected(inputs, tmp_path, change, message):
    frame, _, _, old_end, new_end, previous, _ = inputs
    frame = frame.loc[frame.date.le(new_end)].copy()
    if change == "raw":
        frame.loc[frame.date.eq(old_end), "close"] += .01
    else:
        frame = frame.loc[frame.date.ne("2023-05-15")]
    changed = snapshot(tmp_path / "changed", frame)
    with pytest.raises(ValueError, match=message):
        rolling.run(changed, "2023-01-02", new_end, variants=(VARIANT,), resume_run=previous)


def test_completed_month_end_boundary_cannot_be_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    frame = raw_frame()
    old = snapshot(tmp_path / "old", frame.loc[frame.date.le("2024-01-31")])
    new = snapshot(tmp_path / "new", frame.loc[frame.date.le("2024-02-01")])
    out, _ = rolling.run(old, "2023-01-02", "2024-01-31", variants=(VARIANT,))
    with pytest.raises(ValueError, match="month-end signal prefix"):
        rolling.run(new, "2023-01-02", "2024-02-01", variants=(VARIANT,), resume_run=out / "run.json")


def test_config_change_rejected_and_raw_feed_stays_replay_only(inputs):
    frame, _, new, _, new_end, previous, _ = inputs
    with pytest.raises(ValueError, match="strategy, costs"):
        rolling.run(new, "2023-01-02", new_end, initial_cash=150000, variants=(VARIANT,), resume_run=previous)
    feed = rolling.RollingStockDBFeed(frame, new_end, variant=VARIANT)
    state = new_state(140000)
    with pytest.raises(ValueError, match="historical-replay-only"):
        advance(state, feed, "2023-01-02", rolling.portfolio(), 0,
                context=RunContext("paper-test", TradingMode.PAPER))
    assert state == new_state(140000)
