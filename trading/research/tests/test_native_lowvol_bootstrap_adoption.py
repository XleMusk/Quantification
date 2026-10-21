"""One metadata-only generation adoption contract with synthetic small files."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import native_lowvol_bootstrap_adoption as m


def test_new_bootstrap_replaces_only_metadata_prefix_without_mutating_old_generation(tmp_path, monkeypatch):
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-19T20:00:00+08:00").astimezone(tz)

    monkeypatch.setattr(m, "datetime", Clock)
    monkeypatch.setattr("trading.engine.advance", lambda *a, **k: pytest.fail("Metadata adoption cannot run Engine"))
    config = {"fixed_synthetic_lowvol_config": True}
    identity = {"fixed_synthetic_signal_source": True}
    old_publication = save(tmp_path / "old_publication/publication.json", {
        "source_day": "2026-10-16", "config": config, "signal_source_identity": identity})
    new_publication = save(tmp_path / "new_publication/publication.json", {"synthetic_new_publication": True})
    extension = save(tmp_path / "extension/extension.json", {"synthetic_prospective_extension": True})
    old_run, old_source = tmp_path / "old_account", tmp_path / "old_source"
    old_source.mkdir()
    old = {"route_id": "native_lowvol", "publications": [str(old_publication)],
        "raw_closes": [str(tmp_path / "old_raw/2026-10-16")],
        "run_directory": str(old_run), "source_directory": str(old_source),
        "action_event_view": str(tmp_path / "action_events/result.json"),
        "action_reference_view": str(tmp_path / "action_references/result.json"),
        "last_processed_day": "2026-10-16", "fixed_source_metadata": {"retain": True}}
    head = {"profile": {"last_source_day": "2026-10-19"}, "last_source_day": "2026-10-19", "lowvol": old}
    old_head_file = save(tmp_path / "system/head.json", head)
    original_head = deepcopy(head)
    loaded = {"source_day": "2026-10-19", "execution_day": "2026-10-20",
        "inception": "bootstrap_latest_saved_monthly_rank", "bootstrap_applied": True,
        "account_ref": None, "previous_publication_ref": None, "account_snapshot": None,
        "publication_ref": {"file": str(new_publication), "sha256": "synthetic_new_publication_hash"},
        "config": config, "signal_source_identity": identity,
        "source_proof": {"source_folder": str(tmp_path / "new_raw/2026-10-19")},
        "calendar_ref": {"file": str(tmp_path / "calendar/calendar.json")},
        "published_at": "2026-10-19T19:00:00+08:00"}
    def load_publication(path):
        assert Path(path) == new_publication
        return deepcopy(loaded)
    monkeypatch.setattr(m, "load_publication", load_publication)
    extension_calls = []
    def readiness(path, events, references, calendar):
        extension_calls.append((path, events, references, calendar))
        return {"window_start": "2026-10-19", "window_end": "2026-10-20", "ready_for_day": "2026-10-19"}
    monkeypatch.setattr(m, "extension_readiness", readiness)
    new_run, new_source = tmp_path / "new_account", tmp_path / "new_source"
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = m.prepare(head, new_publication, new_run, new_source, extension, "native_lowvol_bootstrap_20261019")
    lowvol, generation = result["lowvol"], result["generation"]
    assert lowvol["publications"] == [str(new_publication)]
    assert lowvol["raw_closes"] == [loaded["source_proof"]["source_folder"]]
    assert lowvol["run_directory"] == str(new_run.resolve())
    assert lowvol["source_directory"] == str(new_source.resolve())
    assert lowvol["route_id"] == "native_lowvol_bootstrap_20261019" and "last_processed_day" not in lowvol
    assert lowvol["fixed_source_metadata"] == old["fixed_source_metadata"]
    assert generation["prior_lowvol_snapshot"] == original_head["lowvol"]
    assert generation["source_day"] == "2026-10-19" and generation["first_execution_day"] == "2026-10-20"
    assert generation["extension_observation"]["ready_for_day"] == "2026-10-19"
    assert all(generation[key] is False for key in ("original_account_migrated", "original_failure_rewritten",
        "research_account_created", "engine_advanced", "orders_generated", "execution_admitted"))
    assert extension_calls == [(extension, old["action_event_view"], old["action_reference_view"], loaded["calendar_ref"]["file"])]
    assert head == original_head and not new_run.exists() and not new_source.exists()
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    # The returned copies cannot retroactively rewrite the original head.
    lowvol["fixed_source_metadata"]["retain"] = False
    generation["prior_lowvol_snapshot"]["raw_closes"].clear()
    assert head == original_head and old_head_file.read_bytes() == before[old_head_file]

    loaded["source_day"] = "2026-10-16"
    with pytest.raises(ValueError, match="independent source-D bootstrap"):
        m.prepare(head, new_publication, new_run, new_source, extension)
    loaded["source_day"] = "2026-10-19"
    loaded["publication_ref"]["file"] = str(old_publication)
    with pytest.raises(ValueError, match="independent source-D bootstrap"):
        m.prepare(head, new_publication, new_run, new_source, extension)
    loaded["publication_ref"]["file"] = str(new_publication)
    with pytest.raises(ValueError, match="separate from prior accounts"):
        m.prepare(head, new_publication, old_run, new_source, extension)
    saved_account = save(old_run / "head.json", {"existing_account_must_not_migrate": True})
    existing_bytes = saved_account.read_bytes()
    with pytest.raises(ValueError, match="cannot migrate an existing"):
        m.prepare(head, new_publication, new_run, new_source, extension)
    assert head == original_head and saved_account.read_bytes() == existing_bytes
    assert not new_run.exists() and not new_source.exists()
