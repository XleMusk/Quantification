"""Keep full feature history fixed while checking quotes only where Engine ran."""
import json

import pandas as pd
import pytest

from trading.research import stockdb_rolling_low_volatility as m


WARMUP, START, LAST, END = "2024-06-13", "2025-01-06", "2026-09-30", "2026-10-13"
VARIANT = "low_vol_half_momentum_20"


def source_frame():
    return pd.DataFrame([dict(code="002087", symbol="002087.SZSE", date=day,
        open=.17, high=.18, low=.16, close=.16, pre_close=.17,
        volume=20_061_549., amount=3_286_917.84, is_st=False,
        tradestatus="0" if day == WARMUP else "1", adjustflag="3")
        for day in [WARMUP, START, LAST, END]])


def check(before, after, **kwargs):
    return m._validate_resume_prefix(before, after, END, END, LAST, **kwargs)


def test_warmup_interpretation_difference_is_not_a_consumed_engine_quote():
    frame = source_frame()
    check(frame, frame.copy(), engine_quote_start=START)


@pytest.mark.parametrize("field,value,message", [
    ("close", .15, "raw history prefix"),
    ("tradestatus", "1", "native quote semantics prefix"),
    ("adjustflag", "1", "native quote semantics prefix"),
])
def test_warmup_raw_or_native_field_changes_are_still_rejected(field, value, message):
    before = source_frame()
    after = before.copy()
    after.loc[after.date.eq(WARMUP), field] = value
    with pytest.raises(ValueError, match=message):
        check(before, after, engine_quote_start=START)


def test_consumed_engine_quote_interpretation_change_is_still_rejected():
    frame = source_frame()
    frame.loc[frame.date.eq(START), "tradestatus"] = "0"
    with pytest.raises(ValueError, match="quote semantics prefix"):
        check(frame, frame.copy(), engine_quote_start=START)


def test_direct_prefix_helper_default_remains_strict_over_all_history():
    frame = source_frame()
    with pytest.raises(ValueError, match="quote semantics prefix"):
        check(frame, frame.copy())


@pytest.mark.parametrize("start", ["2025-1-6", "2026-10-01"])
def test_invalid_or_later_than_processed_quote_start_is_rejected(start):
    frame = source_frame()
    with pytest.raises(ValueError, match="fixed Engine quote prefix start"):
        check(frame, frame.copy(), engine_quote_start=start)


def test_resume_loader_uses_only_validated_original_protocol_start(tmp_path, monkeypatch):
    frame, config = source_frame(), m.portfolio()
    snapshot, artifacts = tmp_path / "snapshot", tmp_path / "artifacts"
    snapshot.mkdir()
    artifacts.mkdir()
    manifest = {"frozen_fixture": True}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    record = dict(schema=m.SCHEMA, status="blocked_research_only", research_only=True,
                  execution_admitted=False, config=config.to_dict(), variants=[VARIANT],
                  replay_start=START, replay_end=END, artifacts=str(artifacts),
                  results=[dict(signal=VARIANT, completed_sessions=2)])
    protocol = dict(config=config.to_dict(), variants=[VARIANT], replay_start=START,
                    replay_end=END, lookback=m.LOOKBACK, skip=m.SKIP,
                    rank_cadence="monthly_completed_session_end", snapshot=str(snapshot),
                    snapshot_manifest=manifest, supplemental_raw_view={"path": "fixture-old-raw"})
    run_file, protocol_file = tmp_path / "run.json", artifacts / "protocol.json"
    run_file.write_text(json.dumps(record), encoding="utf-8")
    protocol_file.write_text(json.dumps(protocol), encoding="utf-8")
    reads = []
    def read_old(path, supplemental, end):
        reads.append((path, supplemental, end))
        return path, manifest, frame.copy(), {}
    monkeypatch.setattr(m, "_read_snapshot", read_old)
    monkeypatch.setattr(m, "load_replay_checkpoint", lambda *args: dict(
        state={"day": LAST}, daily=[{"date": START}, {"date": LAST}]))
    result = m._resume_inputs(run_file, frame.copy(), START, END, config,
                              (VARIANT,), [START, LAST, END])
    assert result[3][VARIANT]["state"]["day"] == LAST
    assert reads == [(snapshot.resolve(), "fixture-old-raw", END)]
    # Changing the requested start cannot widen the quote-comparison exemption.
    with pytest.raises(ValueError, match="strategy, costs, variants or replay interval"):
        m._resume_inputs(run_file, frame.copy(), WARMUP, END, config,
                         (VARIANT,), [WARMUP, START, LAST, END])
    protocol["replay_start"] = WARMUP
    protocol_file.write_text(json.dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed signal protocol"):
        m._resume_inputs(run_file, frame.copy(), START, END, config,
                         (VARIANT,), [START, LAST, END])
    assert len(reads) == 1
