"""New quote-validation and restart contracts, with a tiny real Engine replay."""
from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from trading.contracts import RunContext, TradingMode
from trading.ledger import new_state
from trading.research import stockdb_controlled_signal_comparison as m
from trading.research.tests.test_stockdb_controlled_signal_comparison import inputs


A, B = "600000.SSE", "000001.SZSE"


@pytest.fixture
def tiny(monkeypatch):
    raw, closes, scores, instruments, calendar, config, days, _ = inputs()
    halt = raw.date.eq(days[1]) & raw.symbol.eq(A)
    raw.loc[halt, ["open", "high", "low", "volume", "amount", "tradestatus"]] = 0
    refs = {"raw_seed_scores_sha256": "synthetic_same_frozen_predictions",
        "native_gap_proof": {"opened_at": "2026-10-17T10:00:00+08:00",
                             "source_available_at": "2026-10-01T15:00:00+08:00"}}
    fixture = (raw, closes, scores, instruments, calendar, config, days, refs)
    monkeypatch.setattr(m, "_sources", lambda *args: deepcopy(fixture))
    def pool(self, day, configuration):
        if day not in self.pool_cache:
            # Explicit synthetic ranks isolate one failed RULE book from one
            # completed model book, while execution/accounting remains real.
            values = [{"symbol": B, "risk_adjusted_momentum": 1., "lgbm_day_local": .9,
                       "lgbm_legacy_global_diagnostic": .9}]
            if day != days[1]:
                values.append({"symbol": A, "risk_adjusted_momentum": 2., "lgbm_day_local": .1,
                               "lgbm_legacy_global_diagnostic": .1})
            self.pool_cache[day] = values
            self.audit.append({"date": day, "eligible": len(values), "excluded": {}})
        return self.pool_cache[day]
    monkeypatch.setattr(m.ControlledFeed, "pool", pool)
    return fixture


def seed_incomplete(tmp_path, monkeypatch, tiny):
    original_validation = m.ControlledFeed.validate_session
    def old_validation(self, state, day):
        required = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        required.update((state.get("pending_targets") or {}).keys())
        for symbol in required:
            row = self.rows.get(day, {}).get(symbol)
            if row is not None and row["open"] == 0:
                raise ValueError(f"Unknown required raw quote/status: {symbol} {day}; no future-path screening")
        return original_validation(self, state, day)
    with monkeypatch.context() as patch:
        patch.setattr(m.ControlledFeed, "validate_session", old_validation)
        source = tmp_path / "original-incomplete"
        record = m.run("synthetic", "synthetic", "synthetic", source, corrected_only=True)
    assert record["status"] == "incomplete"
    assert [(r["status"], r["sessions"]) for r in record["results"]] == [("failed", 1), ("completed", 3)]
    return source, record


def test_zero_open_known_status0_keeps_shares_no_fill_and_stale_while_unknown_stops(tiny):
    raw, closes, scores, instruments, calendar, config, days, _ = tiny
    feed = m.ControlledFeed(raw, closes, scores, instruments, calendar, config, arm=m.ARMS[0])
    state = new_state(config.initial_cash)
    state.update(day=days[0], positions={A: [{"qty": 100, "available": days[0]}]}, prices={A: 1000}, pending_targets={})
    before_cash = state["cash"]
    row = m.advance(state, feed, days[1], config, 1, context=RunContext(m.METHOD+":"+m.ARMS[0], TradingMode.REPLAY))
    assert sum(lot["qty"] for lot in state["positions"][A]) == 100
    assert state["cash"] == before_cash and len(state["fills"]) == 0 and len(state["orders"]) == 0
    assert row["stale_symbols"] == [A] and raw.loc[raw.date.eq(days[1]) & raw.symbol.eq(A), "open"].item() == 0
    assert state["prices"][A] == round(float(closes.at[days[1], A])*100)
    for field, value in (("tradestatus", np.nan), ("tradestatus", -1), ("close", 0.), ("close", np.nan), ("open", -1.)):
        bad = raw.copy()
        bad.loc[bad.date.eq(days[1]) & bad.symbol.eq(A), field] = value
        invalid = m.ControlledFeed(bad, closes, scores, instruments, calendar, config, arm=m.ARMS[0])
        account = new_state(config.initial_cash)
        account.update(day=days[0], positions={A: [{"qty": 100, "available": days[0]}]}, prices={A: 1000})
        before = json.dumps(account, sort_keys=True)
        with pytest.raises(ValueError, match="Unknown required raw quote/status"):
            m.advance(account, invalid, days[1], config, 1, context=RunContext(m.METHOD+":"+m.ARMS[0], TradingMode.REPLAY))
        assert json.dumps(account, sort_keys=True) == before


def test_resume_reuses_completed_files_and_metrics_and_continues_exact_global_index(tmp_path, monkeypatch, tiny):
    original, old = seed_incomplete(tmp_path, monkeypatch, tiny)
    files = {path: path.read_bytes() for path in original.rglob("*") if path.is_file()}
    prefix = m._json(original / m.ARMS[0] / "account.json")
    # A new native read clock is allowed; its original available_at is retained.
    changed = deepcopy(tiny)
    changed[-1]["native_gap_proof"]["opened_at"] = "2026-10-17T12:00:00+08:00"
    monkeypatch.setattr(m, "_sources", lambda *args: deepcopy(changed))
    real_advance, calls = m.advance, []
    def continuation(state, feed, day, config, index, *, context):
        assert feed.arm == m.ARMS[0]
        if not calls:
            assert state == prefix
        calls.append((day, index))
        return real_advance(state, feed, day, config, index, context=context)
    monkeypatch.setattr(m, "advance", continuation)
    out = tmp_path / "continued"
    result = m.run("synthetic", "synthetic", "synthetic", out, corrected_only=True, resume_incomplete=original)
    assert calls == [(tiny[6][1], 1), (tiny[6][2], 2)]
    assert result["status"] == "completed" and result["results"][1] == old["results"][1]
    assert result["resume"]["completed_cases_reused_without_engine"] == [m.ARMS[1]]
    assert result["resume"]["new_sessions_by_arm"] == {m.ARMS[0]: 2, m.ARMS[1]: 0}
    assert result["resume"]["reused_sessions_by_arm"] == {m.ARMS[0]: 1, m.ARMS[1]: 3}
    # This book had only a pending buy before the halt, so it held no stale
    # shares on that session. In particular the saved prefix '[]' stays false.
    assert result["results"][0]["metrics"]["stale_valuation_days"] == 0
    daily = m._saved_daily(out / m.ARMS[0] / "daily.csv", 3)
    assert daily[0]["stale_symbols"] == [] and isinstance(daily[0]["events"], list)
    assert isinstance(daily[0]["event_time_frontier"], dict)
    for path, data in files.items():
        assert path.read_bytes() == data
        if path.parent.name == m.ARMS[1]:
            assert (out / m.ARMS[1] / path.name).read_bytes() == data


def test_resume_changed_source_or_original_available_clock_rejects_before_engine(tmp_path, monkeypatch, tiny):
    original, _ = seed_incomplete(tmp_path, monkeypatch, tiny)
    original_files = {p: p.read_bytes() for p in original.rglob("*") if p.is_file()}
    monkeypatch.setattr(m, "advance", lambda *args, **kwargs: pytest.fail("Bad resume source must not advance either account"))
    for index, change in enumerate(("score", "availability")):
        altered = deepcopy(tiny)
        if change == "score":
            altered[-1]["raw_seed_scores_sha256"] = "changed"
        else:
            altered[-1]["native_gap_proof"]["source_available_at"] = "2026-10-02T15:00:00+08:00"
        monkeypatch.setattr(m, "_sources", lambda *args: deepcopy(altered))
        with pytest.raises(ValueError, match="source proofs"):
            m.run("synthetic", "synthetic", "synthetic", tmp_path / ("bad-"+str(index)),
                  corrected_only=True, resume_incomplete=original)
    with pytest.raises(ValueError, match="outside"):
        m.run("synthetic", "synthetic", "synthetic", original / "must-not-create",
              corrected_only=True, resume_incomplete=original)
    assert not (original / "must-not-create").exists()
    assert original_files == {p: p.read_bytes() for p in original.rglob("*") if p.is_file()}


def test_saved_daily_preserves_engine_none_frontier_on_nondecision_session(tmp_path):
    file = tmp_path / "daily.csv"
    rows = [{"date": "2026-01-05", "stale_symbols": [], "events": [],
             "event_time_frontier": {"status": "validated_with_signals"}},
            {"date": "2026-01-06", "stale_symbols": [], "events": [], "event_time_frontier": None}]
    pd.DataFrame(rows).to_csv(file, index=False)
    loaded = m._saved_daily(file, 2)
    assert loaded[0]["event_time_frontier"] == rows[0]["event_time_frontier"]
    assert loaded[1]["event_time_frontier"] is None
    assert all(row["events"] == [] and row["stale_symbols"] == [] for row in loaded)
    rows[1]["events"] = None
    pd.DataFrame(rows).to_csv(file, index=False)
    with pytest.raises(ValueError, match="containers are incomplete"):
        m._saved_daily(file, 2)
