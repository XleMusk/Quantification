"""Synthetic daily wiring only; no provider, strategy replay or account advancement."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import native_lowvol_forward as forward
from trading.research import native_lowvol_publication as publication
from trading.research import system_daily_refresh as m
from trading.research.tests.test_recent_action_extension import fixture_extension, START, END


def fixture_lowvol(tmp_path, monkeypatch):
    event, reference, extension, bundle = fixture_extension(tmp_path / "evidence")
    calendar = Path(bundle["calendar"]["path"])
    saved_run = tmp_path / "saved_rank" / "run.json"
    m._atomic(saved_run, {"synthetic_saved_rank": True})
    old_publication = tmp_path / "old_plan" / "publication.json"
    m._atomic(old_publication, {"synthetic_saved_publication": True})
    current = {"source_day": START, "execution_day": END,
        "inception": "bootstrap_latest_saved_monthly_rank",
        "publication_ref": m._ref(old_publication), "calendar_ref": m._ref(calendar),
        "source_refs": [{**m._ref(saved_run), "role": "saved_run"}]}
    package = tmp_path / "complete_close"
    m._atomic(package / "receipt.json", {"day": END, "synthetic_complete_close": True})
    lowvol = {"run_directory": str(tmp_path / "account"),
        "source_directory": str(tmp_path / "opening_source"),
        "publications": [str(old_publication)], "raw_closes": [],
        "action_event_view": str(event), "action_reference_view": str(reference)}
    head = {"native_profile": {"ipo_evidence": "synthetic-ipo", "master_run": "synthetic-master"},
        "lowvol": lowvol}
    consumed, published = [], []

    def load(path):
        if Path(path).resolve() == old_publication.resolve():
            return deepcopy(current)
        return {**m._read(path), "publication_ref": m._ref(path)}

    def consume(*args, **kwargs):
        consumed.append((args, kwargs))
        return {"status": "completed", "last_execution_day": END, **m.FLAGS}

    def publish(*args, **kwargs):
        published.append((args, kwargs))
        path = Path(args[3]) / "publication.json"
        result = {"source_day": END, "execution_day": "2026-06-30", **m.FLAGS}
        m._atomic(path, result)
        return {**result, "publication_ref": m._ref(path)}

    monkeypatch.setattr(publication, "load_publication", load)
    monkeypatch.setattr(publication, "publish", publish)
    monkeypatch.setattr(forward, "run", consume)
    return SimpleNamespace(head=head, package=package, extension=extension, bundle=bundle,
        operation=tmp_path / "operation", consumed=consumed, published=published, current=current)


def invoke(saved, **kwargs):
    return m._lowvol(deepcopy(saved.head), END, saved.package, saved.operation,
        recent_action_extension=saved.extension, **kwargs)


def test_verified_extension_is_forwarded_and_all_pins_are_in_job_identity(tmp_path, monkeypatch):
    saved = fixture_lowvol(tmp_path, monkeypatch)
    result, updated = invoke(saved)
    assert result["status"] == "advanced_and_published_next_native_lowvol_plan"
    assert len(saved.consumed) == len(saved.published) == 1
    assert Path(saved.consumed[0][1]["recent_action_extension"]).resolve() == saved.extension.resolve()
    jobs = list((saved.operation / "lowvol").iterdir())
    assert len(jobs) == 1
    started = m._read(jobs[0] / "consumer_started.json")
    identity_payload = json.dumps(started["inputs"], sort_keys=True)
    assert m._ref(saved.extension)["sha256"] in identity_payload
    for query in saved.bundle["queries"]:
        assert query["payload"]["sha256"] in identity_payload
        assert query["receipt"]["sha256"] in identity_payload
    assert saved.bundle["actions"][0]["source_sha256"] in identity_payload
    before = (jobs[0] / "consumer.json").read_bytes()
    repeated, _ = invoke(saved)
    assert repeated == result and len(saved.consumed) == len(saved.published) == 1
    assert (jobs[0] / "consumer.json").read_bytes() == before
    assert len(updated["publications"]) == 2


def test_extension_missing_exact_action_day_never_calls_consumer_or_publisher(tmp_path, monkeypatch):
    saved = fixture_lowvol(tmp_path, monkeypatch)
    saved.current.update(source_day=END, execution_day="2026-06-30")
    result, _ = m._lowvol(deepcopy(saved.head), "2026-06-30", saved.package, saved.operation,
        recent_action_extension=saved.extension)
    assert "waiting" in result["status"]
    assert result["missing_action_sessions"] == ["2026-06-30"]
    assert not saved.consumed and not saved.published
    assert not (saved.operation / "lowvol").exists()


def test_changed_indirect_pin_cannot_reuse_saved_consumer_result(tmp_path, monkeypatch):
    saved = fixture_lowvol(tmp_path, monkeypatch)
    invoke(saved)
    prior = {p: p.read_bytes() for p in (saved.operation / "lowvol").rglob("consumer.json")}
    Path(saved.bundle["actions"][0]["source_file"]).write_bytes(b"%PDF-1.4 changed synthetic bytes")
    with pytest.raises(ValueError):
        invoke(saved)
    assert len(saved.consumed) == len(saved.published) == 1
    assert all(path.read_bytes() == value for path, value in prior.items())


def test_changed_identity_cannot_restart_an_unknown_consumer_in_same_operation(tmp_path, monkeypatch):
    saved = fixture_lowvol(tmp_path, monkeypatch)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("synthetic interruption after consumer entry")

    monkeypatch.setattr(forward, "run", interrupted)
    first, _ = invoke(saved)
    assert first["execution_progress_unknown"] is True and calls == [1]
    jobs = list((saved.operation / "lowvol").iterdir())
    assert len(jobs) == 1
    # Reproduce an actual interrupted write: only the durable start survives.
    (jobs[0] / "consumer.json").unlink()
    changed = deepcopy(saved.bundle)
    changed["created_at"] = "2026-06-30T10:01:01+08:00"
    m._atomic(saved.extension, changed)
    result, _ = invoke(saved)
    assert "unresolved" in result["status"] or "unknown" in result["status"]
    assert calls == [1] and not saved.published
    assert list((saved.operation / "lowvol").iterdir()) == jobs


def test_current_source_day_reuses_saved_plan_without_touching_extension_or_producers(tmp_path, monkeypatch):
    saved = fixture_lowvol(tmp_path, monkeypatch)
    saved.current["source_day"] = END
    saved.extension.unlink()  # Existing exact-D publication requires no new action consumption.
    result, updated = invoke(saved)
    assert result["status"] == "saved_exact_source_plan_reused"
    assert updated == saved.head["lowvol"]
    assert not saved.consumed and not saved.published and not saved.operation.exists()
