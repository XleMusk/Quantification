"""First-year source adapter checks; synthetic observations, no Engine/PnL."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import leader_partial_year_inputs as adapter
from trading.research import leader_eight_type_historical_native as native


def calendar():
    return {"schema": "cn_a_share_exchange_calendar_v1", "year": 2023, "open_sessions": [],
        "closed_ranges": ["2023-01-02/2023-01-02", "2023-01-23/2023-01-27",
            "2023-04-05/2023-04-05", "2023-05-01/2023-05-03", "2023-06-22/2023-06-23",
            "2023-09-29/2023-10-06"]}


def rows(day, symbols=("600001.SSE", "600002.SSE"), suspended=()):
    return pd.DataFrame([{"date": day, "symbol": symbol, "code": "sh." + symbol[:6],
        "open": "10", "high": "10", "low": "10", "close": "10", "preclose": "10",
        "volume": "0" if symbol in suspended else "10000000", "amount": "100000000",
        "adjustflag": "3", "turn": "2", "tradestatus": "0" if symbol in suspended else "1",
        "pctChg": "0", "peTTM": "", "pbMRQ": "", "psTTM": "", "pcfNcfTTM": "", "isST": "0"}
        for symbol in symbols])


def test_exact_first_source_year_has61_warmup_and175_plus6_not_full_year():
    payload = calendar()
    sessions, _, _ = native._calendar_view(payload, "2023-01-01", "2023-12-31", [])
    _, warmup, days = adapter._schedule(payload, sessions)
    assert len(warmup) == 61 and days[0] == "2023-04-06" and len(days) == 181
    assert len(adapter.CASE_IDS) == 32 and adapter.SPEC["full_2023_or_252_momentum_claimed"] is False
    with pytest.raises(ValueError, match="Exact 2023"):
        adapter._schedule(payload, [day for day in sessions if day != "2023-06-01"])


def test_anchor_filter_matches_annual_active_nonST_61_and_listing_without_later_membership():
    symbols = ["600001.SSE", "600002.SSE", "600003.SSE", "600004.SSE"]
    raw = rows(adapter.START, symbols, suspended=[symbols[2]])
    original = raw.copy(deep=True)
    counts = {symbol: 61 for symbol in symbols}
    counts[symbols[1]] = 60
    qualification = SimpleNamespace(assess=lambda symbol, *args, **kwargs:
        {"engine_block_reason": "listing_unknown" if symbol == symbols[3] else None})
    members, excluded = adapter._anchor_cohort(raw, counts, qualification)
    assert members == [symbols[0]]
    assert [row["symbol"] for row in excluded] == symbols[1:]
    pd.testing.assert_frame_equal(raw, original)
    assert counts[symbols[1]] == 60


def test_registered_adapter_reads_each_2023_day_once_and_keeps_environment_denominator(tmp_path, monkeypatch):
    payload = calendar()
    sessions, _, _ = native._calendar_view(payload, "2023-01-01", "2023-12-31", [])
    source = tmp_path / "native"
    source.mkdir()
    for name, value in (("plan.json", {"days": sessions}), ("receipts.json", {"status": "source_cache_complete"}),
            ("result.json", {"status": "source_cache_complete", "panel_file": "raw_daily.parquet", "panel_sha256": "fixture_raw"})):
        native._save(source / name, value)
    cal, master, units = (tmp_path / name for name in ("calendar.json", "master.json", "units.json"))
    native._save(cal, payload)
    native._save(master, {"fixture_master": True})
    native._save(units, {"turn_unit": "percent"})
    monkeypatch.setattr(adapter, "UNIT_EVIDENCE", units)
    events, references = tmp_path / "events", tmp_path / "references"
    events.mkdir(); references.mkdir()
    native._save(events / "result.json", {"status": "research_action_events_projected", "start": sessions[0],
        "end": sessions[-1], "execution_admitted": False})
    native._save(references / "result.json", {"status": "research_reference_presence_diagnostic",
        "event_view": str(events), "execution_admitted": False})
    (events / "source_input_calendar.csv").write_text("date\n" + "\n".join(sessions), encoding="utf-8")
    registration = tmp_path / "registration"
    record = adapter.register(source, cal, master, events, references, registration)
    assert record["cohort_generated"] is False and record["new_PnL_computed"] is False
    proof = {"posthoc_observed_at": native._now(), "calendar": str(cal), "fixture": True}
    records = {symbol: {"ipo_date": "2000-01-01", "reported_out_date": "", "available_at": native._now(),
        "board": "main", "source_basis": "synthetic_saved_listing_metadata"}
        for symbol in ("600001.SSE", "600002.SSE")}
    monkeypatch.setattr(native, "load_posthoc_listing", lambda *args, observed_at:
        native.PosthocListingQualification(deepcopy(records), tuple(sessions), {**proof, "posthoc_observed_at": observed_at}))
    calls, captured = [], {}
    class Source:
        calendar = sessions
        provenance = {"raw_sha256": "fixture_raw", "source_view": str(source)}
        def __init__(self, path):
            assert str(path) == str(source)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def _unchanged(self): pass
        def frame(self, day, symbols=None):
            calls.append(day)
            return rows(day, symbols or tuple(records), suspended=["600002.SSE"] if day in sessions[:2] else [])
    monkeypatch.setattr(adapter, "NativeHistorySource", Source)
    def derive(history, days, members, *args, **kwargs):
        captured.update(history_symbols=set(history.symbol), members=members, days=days)
        return {day: {"day": day, "types": [{"id": kind, "candidates": [], "unresolved_symbols": members}
            for kind in adapter.TYPE_IDS]} for day in days}
    monkeypatch.setattr(native, "derive_panels", derive)
    result = adapter.build_inputs(registration / "registration.json", tmp_path / "new_inputs")
    assert len(calls) == len(set(calls)) == 242
    assert captured["members"] == ["600001.SSE"] and captured["history_symbols"] == set(records)
    assert len(captured["days"]) == 181 and result["scope_members"] == ["600001.SSE"]
    assert result["raw_rows"] == 181 * 2
    assert result["native_source_groups_read"] == 242 and result["identity_or_historical_sector_membership_backfilled"] is False
