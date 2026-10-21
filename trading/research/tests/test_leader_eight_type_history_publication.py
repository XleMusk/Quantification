"""Explicit projection publication; no source/Engine/API or existing checks."""
import json
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view
from trading.research.tests.test_leader_eight_type_history_view import fixture_history, SELECTED


def generations(tmp_path):
    inputs, old, data, _ = fixture_history(tmp_path)
    view.build(inputs, old)
    newer = old.parent / "synthetic_three_month_generation"
    view.build(inputs, newer)
    return inputs, old, newer, data


def test_absent_pointer_uses_original_generation_then_explicit_publish_selects_new(tmp_path, monkeypatch):
    _, old, newer, data = generations(tmp_path)
    before = view.read_history(data)
    assert before["status"] == "saved_history_projection" and before["projection_generation"] == old.name
    assert before["projection_published_at"] is None
    publication = view.publish(newer, data)
    assert publication["case_count"] == 64
    assert publication["generation_directory"] == newer.name
    reads, original = [], view._read
    def watch(path):
        reads.append(Path(path).resolve())
        assert old not in Path(path).resolve().parents
        return original(path)
    monkeypatch.setattr(view, "_read", watch)
    actual = view.read_history(data, "2026-02", SELECTED)
    assert actual["status"] == "saved_history_projection"
    assert actual["projection_generation"] == newer.name
    assert actual["projection_published_at"] == publication["actual_published_at"]
    assert len(reads) == 4  # pointer, manifest, small index and selected case only
    assert actual["source_originals_rechecked_at_read"] is False and actual["engine_advanced"] is False


def test_bad_or_escaping_current_pointer_does_not_fall_back_to_valid_original(tmp_path):
    _, old, newer, data = generations(tmp_path)
    view.publish(newer, data)
    pointer = old.parent / "current.json"
    saved = json.loads(pointer.read_text())
    for name in ("../outside", str(tmp_path.resolve()), "."):
        pointer.write_text(json.dumps({**saved, "generation_directory": name}), encoding="utf-8")
        actual = view.read_history(data)
        assert actual["status"] == "unavailable" and actual["detail"] is None
    pointer.write_text("malformed", encoding="utf-8")
    assert view.read_history(data)["status"] == "unavailable"
    assert (old / "manifest.json").exists()


def test_manifest_change_rejects_reader_and_failed_new_publication_keeps_old_pointer(tmp_path):
    inputs, old, newer, data = generations(tmp_path)
    view.publish(newer, data)
    pointer = old.parent / "current.json"
    original_pointer = pointer.read_bytes()
    broken = old.parent / "broken_generation"
    view.build(inputs, broken)
    (broken / "windows/2026-02" / (SELECTED + ".json")).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        view.publish(broken, data)
    assert pointer.read_bytes() == original_pointer
    manifest = newer / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    actual = view.read_history(data)
    assert actual["status"] == "unavailable" and actual["detail"] is None


def test_old_build_cli_stays_explicit_and_publish_requires_data_root(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(view, "build", lambda indexes, out: calls.append(("build", indexes, out)) or {"status": "built"})
    monkeypatch.setattr(view, "publish", lambda out, data: calls.append(("publish", out, data)) or {"status": "published"})
    argv = ["--comparison-index", str(tmp_path / "index.json"), "--out", str(tmp_path / "out")]
    assert view.main(argv) == 0 and len(calls) == 1
    with pytest.raises(SystemExit) as error:
        view.main(argv + ["--publish"])
    assert error.value.code == 2 and len(calls) == 1
    assert view.main(argv + ["--publish", "--data-root", str(tmp_path / "data")]) == 0
    assert [call[0] for call in calls] == ["build", "build", "publish"]
