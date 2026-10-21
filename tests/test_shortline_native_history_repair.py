"""Synthetic older-subset recovery; no market rescan, scores, Engine or fit."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from trading.research import shortline_native_event_panel as original
from trading.research import shortline_native_incremental as increment
from trading.research import shortline_native_history_repair as repair
from trading.research.tests.test_shortline_native_event_panel import bar
from test_shortline_native_incremental import NOW, Source, anchor, guards


SYMBOLS = ["300096.SZSE", "300211.SZSE", "300527.SZSE"]
KEEP = "000004.SZSE"


class SubsetSource(Source):
    def __init__(self, root, frames):
        super().__init__(root, frames)
        self.requests = []

    def frame(self, day, symbols=None):
        self.requests.append((day, None if symbols is None else list(symbols)))
        value = super().frame(day)
        return value if symbols is None else value.loc[value.symbol.isin(symbols)].copy()


@pytest.fixture
def evidence(tmp_path):
    days = pd.bdate_range("2026-06-01", periods=51).strftime("%Y-%m-%d").tolist()
    frames = {}
    for index, day in enumerate(days):
        rows = []
        for position, symbol in enumerate(SYMBOLS):
            close = round(10 + index / 100, 2) if index < 50 else 12.59
            previous = round(10 + (index - 1) / 100, 2) if index else 10.
            rows.append(bar(day, symbol, close=close, preclose=previous,
                status="1" if index <= 24 or index >= 41 + position else "0"))
        if index >= 49:
            rows.append(bar(day, KEEP))
        frames[day] = rows
    source = SubsetSource(tmp_path, frames)
    old = anchor(tmp_path, source, days[49])
    boot = increment.bootstrap_state(source, old, tmp_path / "bootstrap", max_context_sessions=12)
    assert boot["incomplete_buffer_symbols"] == SYMBOLS
    source.reads.clear()
    source.requests.clear()
    return source, days, old, boot


def bytes_in(*folders):
    return {str(file): file.read_bytes() for folder in folders for file in Path(folder).iterdir() if file.is_file()}


def test_three_incomplete_buffers_resume_only_older_subsets_and_exact_nine_returns_then_advance(evidence, tmp_path):
    source, days, old, boot = evidence
    previous = increment._read(boot["checkpoint_file"])
    before = bytes_in(old.parent, Path(boot["checkpoint_file"]).parent)
    assert [len(previous["states"][symbol]["closes"]) for symbol in SYMBOLS] == [9, 8, 7]
    got = repair.repair_anchor_state(source, boot["checkpoint_file"], tmp_path / "repair")
    assert got["schema"] == repair.RESULT_SCHEMA
    assert got["score_input_published"] is False and "latest_features_path" not in got
    assert got["metrics"] is None and got["engine_advanced"] is False
    assert got["incomplete_buffer_symbols"] == [] and got["complete_buffer_symbols"] == 4
    assert got["read_days"] == source.reads
    assert source.reads == list(reversed(days[11:38]))
    assert all(request and set(request) <= set(SYMBOLS) for _, request in source.requests)
    assert not set(source.reads) & set(boot["read_days"])
    assert len(got["anchor_return_compare"]) == 9
    assert all(row["status"] == "checked_original_anchor" and row["absolute_difference"] <= 1e-12
        for row in got["anchor_return_compare"])
    changed = increment._read(got["checkpoint_file"])
    assert changed["states"][KEEP] == previous["states"][KEEP]
    assert changed["source_prefix"] == previous["source_prefix"]
    assert changed["anchor_result"] == previous["anchor_result"] and changed["anchor_features"] == previous["anchor_features"]
    for symbol in SYMBOLS:
        old_state, new_state = previous["states"][symbol], changed["states"][symbol]
        assert new_state["closes"][-len(old_state["closes"]):] == old_state["closes"]
        assert len(new_state["closes"]) == 21 and new_state["history_complete"] is True
        assert {key: value for key, value in new_state.items() if key not in ("closes", "history_complete")} == {
            key: value for key, value in old_state.items() if key not in ("closes", "history_complete")}
    assert before == bytes_in(old.parent, Path(boot["checkpoint_file"]).parent)
    source.reads.clear()
    source.requests.clear()
    following = increment.advance_day(source, got["checkpoint_file"], days[50], tmp_path / "new_day")
    assert source.reads == [days[50]] and source.requests == [(days[50], None)]
    assert following["score_input_published"] is True
    assert following["operational_history_unknown_events"] == []
    assert before == bytes_in(old.parent, Path(boot["checkpoint_file"]).parent)


def test_remaining_short_history_stays_operational_unknown_without_fake_natural_null(evidence, tmp_path):
    source, _, _, boot = evidence
    got = repair.repair_anchor_state(source, boot["checkpoint_file"], tmp_path / "partial", max_context_sessions=3)
    assert got["incomplete_buffer_symbols"] == SYMBOLS and got["score_input_published"] is False
    assert got["status"] == "partial_bootstrap_history_recovery_buffers_still_unknown"
    assert all(row["status"] == "unavailable_operational_buffer_not_natural_null"
        for row in got["anchor_return_compare"] if row["feature"] == "ret_20_observed_rows")
    state = increment._read(got["checkpoint_file"])
    assert [len(state["states"][symbol]["closes"]) for symbol in SYMBOLS] == [9, 8, 7]
    assert all(state["states"][symbol]["history_complete"] is False for symbol in SYMBOLS)
    assert not list(Path(got["checkpoint_file"]).parent.glob("*.csv"))


def test_proven_young_history_null_remains_natural_after_older_buffer_recovery(tmp_path):
    days = pd.bdate_range("2026-07-01", periods=4).strftime("%Y-%m-%d").tolist()
    source = SubsetSource(tmp_path, {day: [bar(day, SYMBOLS[0])] for day in days})
    saved = anchor(tmp_path, source, days[2])
    boot = increment.bootstrap_state(source, saved, tmp_path / "bootstrap", max_context_sessions=1)
    source.reads.clear()
    source.requests.clear()
    got = repair.repair_anchor_state(source, boot["checkpoint_file"], tmp_path / "repaired")
    assert got["incomplete_buffer_symbols"] == [] and got["read_days"] == list(reversed(days[:2]))
    assert all(row["expected"] is None and row["actual"] is None
        and row["status"] == "checked_original_proven_short_history_null" for row in got["anchor_return_compare"])
    state = increment._read(got["checkpoint_file"])["states"][SYMBOLS[0]]
    assert state["history_complete"] is True and len(state["closes"]) == state["proven_history_count"] == 3
    source.reads.clear()
    following = increment.advance_day(source, got["checkpoint_file"], days[3], tmp_path / "next_day")
    assert source.reads == [days[3]] and following["operational_history_unknown_events"] == []


@pytest.mark.parametrize("fault", ["source_prefix", "anchor_csv", "read_days", "truncated_read_days", "advanced", "source_out", "anchor_out", "parent_out"])
def test_changed_inputs_advanced_state_or_source_package_output_reject_before_reads(evidence, tmp_path, fault):
    source, days, old, boot = evidence
    parent = Path(boot["checkpoint_file"]).parent
    checkpoint, out = boot["checkpoint_file"], tmp_path / "must_not_publish"
    if fault == "source_prefix":
        source.provenance["day_sources"][0]["raw_sha256"] = "b" * 64
    elif fault == "anchor_csv":
        anchor_path = Path(increment._read(checkpoint)["anchor_features"]["file"])
        anchor_path.write_bytes(anchor_path.read_bytes() + b"\n")
    elif fault in ("read_days", "truncated_read_days"):
        item = increment._read(parent / "result.json")
        if fault == "read_days":
            item["read_days"][1], item["read_days"][2] = item["read_days"][2], item["read_days"][1]
        else:
            item["read_days"] = item["read_days"][:-1]
            item["read_sessions"] = len(item["read_days"])
        item["source_proof"]["used_source_days"] = item["read_days"]
        (parent / "result.json").write_text(__import__("json").dumps(item), encoding="utf-8")
    elif fault == "advanced":
        changed = increment.advance_day(source, checkpoint, days[50], tmp_path / "already_advanced")
        checkpoint = changed["checkpoint_file"]
        source.reads.clear()
        source.requests.clear()
    elif fault == "source_out":
        out = tmp_path / "source" / "must_not_publish"
    elif fault == "anchor_out":
        out = old.parent / "must_not_publish"
    else:
        out = parent / "must_not_publish"
    with pytest.raises(ValueError):
        repair.repair_anchor_state(source, checkpoint, out)
    assert source.reads == [] and source.requests == [] and not out.exists()
