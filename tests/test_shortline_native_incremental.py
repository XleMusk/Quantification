"""Small synthetic exact comparisons; no old market panel, fit or provider."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from trading.research import shortline_native_event_panel as original
from trading.research import shortline_native_incremental as increment
from trading.research.shortline_native_scoring import load_score_input
from trading.research.tests.test_shortline_native_event_panel import bar


NOW = datetime.fromisoformat("2026-10-16T20:00:00+08:00")
A, B, C = "000001.SZSE", "000002.SZSE", "000003.SZSE"


class Source:
    def __init__(self, root, frames):
        self.calendar = sorted(frames)
        self.frames = {day: pd.DataFrame(rows) for day, rows in frames.items()}
        self.reads = []
        self.provenance = {"raw_file": str(root / "source" / "native.parquet"), "raw_sha256": "a" * 64,
            "source_view": str(root / "source"), "source_available_at": "2026-10-01T18:00:00+08:00"}
        self.provenance["day_sources"] = [{"day": day, "row_group": i,
            "source_view": self.provenance["source_view"], "raw_file": self.provenance["raw_file"],
            "raw_sha256": self.provenance["raw_sha256"], "source_available_at": self.provenance["source_available_at"],
            "component_id": "synthetic_native", "component_kind": "plain_native_view"}
            for i, day in enumerate(self.calendar)]

    def frame(self, day):
        self.reads.append(day)
        result = self.frames[day].copy(deep=True)
        result.attrs["provenance"] = deepcopy(next(row for row in self.provenance["day_sources"] if row["day"] == day))
        return result


@pytest.fixture(autouse=True)
def guards(monkeypatch):
    monkeypatch.setattr(original, "_now", lambda: NOW)
    monkeypatch.setattr(increment, "_now", lambda: NOW)
    def forbidden(*args, **kwargs):
        raise AssertionError("Increment tests must not call network, models or Engine")
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("trading.engine.advance", forbidden)
    monkeypatch.setattr("trading.research.shortline_frozen_model.load_generation", forbidden)


def make_frames(n=76):
    days = pd.bdate_range("2026-06-01", periods=n).strftime("%Y-%m-%d").tolist()
    frames = {}
    previous = 10.
    for i, day in enumerate(days):
        close = 10. if i == 0 else round(previous * 1.1 + 1e-9, 2)
        first = bar(day, A, close=close, preclose=previous, amount=100000.)
        previous = close
        # 46 suspended source sessions make 21 valid observed closes require
        # substantially more than 21 raw row groups during bootstrap.
        second = bar(day, B, close=11. if i == 74 else 10., preclose=11. if i == 75 else 10.,
            amount=200000., status="0" if 20 <= i <= 65 else "1", one_price=i == 74)
        current = [first, second]
        if i >= 72:
            current.append(bar(day, C, close=10., amount=300000.))
        frames[day] = current
    return days, frames


def anchor(root, source, day):
    folder = root / "old_anchor"
    original.build_from_source(source, folder, through=day)
    source.reads.clear()
    return folder / "result.json"


def test_bounded_bootstrap_restores_observed_context_long_stage_young_nulls_and_only_D_advance(tmp_path):
    days, frames = make_frames()
    source = Source(tmp_path, frames)
    saved = anchor(tmp_path, source, days[73])
    old_bytes = {path.name: path.read_bytes() for path in saved.parent.iterdir()}
    boot = increment.bootstrap_state(source, saved, tmp_path / "bootstrap", max_context_sessions=75)
    assert 21 < boot["read_sessions"] < 74
    assert source.reads == boot["read_days"] and days[74] not in source.reads
    assert boot["complete_buffer_symbols"] == 3 and boot["incomplete_buffer_symbols"] == []
    assert all(item["mismatches"] == 0 for item in boot["anchor_compare"].values())
    assert next(item for item in boot["symbol_restoration"] if item["symbol"] == C)["expected_closes"] == 2
    source.reads.clear()
    got = increment.advance_day(source, boot["checkpoint_file"], days[74], tmp_path / "new_D")
    assert source.reads == [days[74]] and got["score_input_published"] is True
    new = pd.read_csv(got["latest_features_path"], float_precision="round_trip").set_index("symbol")
    complete = original.produce(Source(tmp_path, frames), through=days[74])
    expected = pd.DataFrame(complete["latest_features"]).set_index("symbol")
    for symbol in (A, B, C):
        for column in ["stage", "stage_reference_known", "decision_eligible", "observed_history_close_count", *original.FEATURES]:
            first, second = new.loc[symbol, column], expected.loc[symbol, column]
            if pd.isna(second): assert pd.isna(first)
            elif isinstance(second, bool): assert first == second
            else: assert first == pytest.approx(second, abs=1e-12)
    assert new.loc[A, "stage"] > 21
    assert pd.isna(new.loc[B, "close_location"])  # original zero-range null is legal
    assert pd.isna(new.loc[C, "ret_20_observed_rows"])  # proven young-history null
    result, known, proof = load_score_input(tmp_path / "new_D" / "result.json")
    assert known.symbol.tolist() == [B] and pd.isna(known.iloc[0].close_location)
    assert proof["feature_semantics"] == original.FEATURE_SEMANTICS
    assert result["latest_scope"]["previously_observed_missing_symbols"] is None
    assert old_bytes == {path.name: path.read_bytes() for path in saved.parent.iterdir()}


def test_incomplete_operational_buffer_blocks_score_input_but_persists_checkpoint(tmp_path):
    days, frames = make_frames()
    source = Source(tmp_path, frames)
    saved = anchor(tmp_path, source, days[73])
    boot = increment.bootstrap_state(source, saved, tmp_path / "bootstrap", max_context_sessions=3)
    assert B in boot["incomplete_buffer_symbols"]
    source.reads.clear()
    got = increment.advance_day(source, boot["checkpoint_file"], days[74], tmp_path / "blocked_D")
    assert got["status"] == "blocked_operational_history_context_no_score_input"
    assert got["score_input_published"] is False and "latest_features_path" not in got
    assert got["operational_history_unknown_events"][0]["symbol"] == B
    assert Path(got["checkpoint_file"]).is_file() and source.reads == [days[74]]
    diagnostic = pd.read_csv(got["diagnostic_features_path"])
    assert diagnostic.loc[diagnostic.symbol.eq(B), "decision_eligible"].iloc[0]
    # The following own day can still append this real, blocked checkpoint.
    source.reads.clear()
    next_day = increment.advance_day(source, got["checkpoint_file"], days[75], tmp_path / "next_D")
    assert source.reads == [days[75]] and next_day["last_source_day"] == days[75]


def test_newly_observed_incomplete_history_does_not_acquire_artificial_natural_null_scores(tmp_path):
    days = pd.bdate_range("2026-07-01", periods=48).strftime("%Y-%m-%d").tolist()
    frames = {day: [bar(day, A)] for day in days}
    for index, day in enumerate(days[24:], 24):
        current, previous = (11., 10.) if index in (25, 45) else (10., 11.) if index in (26, 46) else (10., 10.)
        frames[day].append(bar(day, B, close=current, preclose=previous))
    source = Source(tmp_path, frames)
    saved = anchor(tmp_path, source, days[23])
    boot = increment.bootstrap_state(source, saved, tmp_path / "bootstrap", max_context_sessions=30)
    checkpoint = boot["checkpoint_file"]
    for index in range(24, 46):
        source.reads.clear()
        result = increment.advance_day(source, checkpoint, days[index], tmp_path / f"day_{index}")
        assert source.reads == [days[index]]
        if index == 25:
            assert result["score_input_published"] is False and result["operational_history_unknown_events"][0]["symbol"] == B
        checkpoint = result["checkpoint_file"]
    assert result["score_input_published"] is True
    _, known, _ = load_score_input(Path(checkpoint).parent / "result.json")
    assert known.symbol.tolist() == [B]


@pytest.mark.parametrize("change", ["skip", "source", "checkpoint", "clock", "output"])
def test_exact_prefix_state_clock_and_output_protections(tmp_path, change):
    days, frames = make_frames()
    source = Source(tmp_path, frames)
    saved = anchor(tmp_path, source, days[73])
    boot = increment.bootstrap_state(source, saved, tmp_path / "bootstrap", max_context_sessions=75)
    out = tmp_path / "new_D"
    day = days[74]
    if change == "skip": day = days[75]
    elif change == "source": source.provenance["day_sources"][0]["raw_sha256"] = "b" * 64
    elif change == "checkpoint":
        file = Path(boot["checkpoint_file"])
        file.write_bytes(file.read_bytes() + b"\n")
    elif change == "clock": source.provenance["source_available_at"] = "2027-01-01T20:00:00+08:00"
    else: out = saved.parent / "forbidden"
    source.reads.clear()
    with pytest.raises(ValueError):
        increment.advance_day(source, boot["checkpoint_file"], day, out)
    assert source.reads == [] and not out.exists()


def test_copied_anchor_descriptor_still_protects_actual_old_feature_package(tmp_path):
    days, frames = make_frames()
    source = Source(tmp_path, frames)
    saved = anchor(tmp_path, source, days[73])
    elsewhere = tmp_path / "copied_descriptor"
    elsewhere.mkdir()
    copied = elsewhere / "result.json"
    copied.write_bytes(saved.read_bytes())
    source.reads.clear()
    out = saved.parent / "must_not_write_inside_features"
    with pytest.raises(ValueError, match="old/source package"):
        increment.bootstrap_state(source, copied, out, max_context_sessions=75)
    assert source.reads == [] and not out.exists()
