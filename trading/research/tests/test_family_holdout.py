import json

import pytest

from trading.research import family_holdout


def _candidate(tmp_path, *, holdout=False):
    root = tmp_path / "project"
    candidate = root / "outputs" / "family_pipeline" / "candidate"
    candidate.mkdir(parents=True)
    price = tmp_path / "price"
    price.mkdir()
    (price / "result.json").write_text(json.dumps({
        "status": "research_reconstruction_completed",
        "cutoff": "2026-09-30",
    }), encoding="utf-8")
    manifest = {
        "scope": "full_source_sh_sz_research_only",
        "replay_only": True,
        "start": "2024-01-01",
        "end": "2026-09-30",
        "price_view": str(price),
        "event_view": str(tmp_path / "events"),
        "reference_view": str(tmp_path / "references"),
        "raw_view": str(tmp_path / "raw"),
        "listing_master": str(tmp_path / "listing.csv"),
        "source_codes": 1,
        "canonical_codes": 1,
        "unsupported_instruments": {},
    }
    record = {
        "status": "completed_mature_month_pipeline_ledger",
        "execution_admitted": False,
        "strategy_validated": False,
        "protocol": {
            "fresh_holdout_claimed": False,
            "stock_manifest": manifest,
            "stock_inputs": {"price_view": str(price)},
            "rights_replay_policy": {"id": "rights_replay_waive_all_v1"},
        },
        "results": [{
            "signal": family_holdout.SIGNAL,
            "status": "completed",
            "net_return": 0.1,
            "max_drawdown": -0.2,
            "sharpe_zero_rf_252": 1.1,
        }],
    }
    if holdout:
        record["protocol"]["fresh_holdout_claimed"] = True
    (candidate / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return root, price


def test_register_then_check_same_cutoff_waits_without_replay(tmp_path, monkeypatch):
    project, price = _candidate(tmp_path)
    data_root = tmp_path / "data"
    monkeypatch.setattr(family_holdout.settings, "ROOT", project)
    monkeypatch.setattr(family_holdout.settings, "DATA_ROOT", data_root)

    root, registered = family_holdout.register("holdout", "candidate")
    checked_root, checked = family_holdout.check("holdout")

    assert checked_root == root
    assert registered["status"] == "waiting_for_new_complete_snapshot"
    assert checked["status"] == "waiting_for_new_complete_snapshot"
    assert checked["source_cutoff"] == "2026-09-30"
    assert checked["holdout_sessions"] == 0
    assert not (project / "outputs" / "family_holdout" / "holdout" / "holdout.json").exists()
    assert price.exists()


def test_candidate_that_claims_holdout_is_rejected(tmp_path, monkeypatch):
    project, _ = _candidate(tmp_path, holdout=True)
    monkeypatch.setattr(family_holdout.settings, "ROOT", project)
    monkeypatch.setattr(family_holdout.settings, "DATA_ROOT", tmp_path / "data")

    with pytest.raises(ValueError, match="must not already claim"):
        family_holdout.register("holdout", "candidate")


def test_replay_requires_an_immutable_ready_source(tmp_path, monkeypatch):
    project, _ = _candidate(tmp_path)
    data_root = tmp_path / "data"
    monkeypatch.setattr(family_holdout.settings, "ROOT", project)
    monkeypatch.setattr(family_holdout.settings, "DATA_ROOT", data_root)

    family_holdout.register("holdout", "candidate")
    with pytest.raises(ValueError, match="must pass the immutable prefix check"):
        family_holdout.replay("holdout")


def test_completed_holdout_cannot_be_reset_by_a_later_check(tmp_path, monkeypatch):
    project, _ = _candidate(tmp_path)
    data_root = tmp_path / "data"
    monkeypatch.setattr(family_holdout.settings, "ROOT", project)
    monkeypatch.setattr(family_holdout.settings, "DATA_ROOT", data_root)

    root, _ = family_holdout.register("holdout", "candidate")
    completed = json.loads((root / "head.json").read_text(encoding="utf-8"))
    completed.update(status="holdout_replay_completed", holdout_sessions=60,
                    fresh_holdout_claimed=True, strategy_validated=True,
                    checked_inputs={"price_view": "old"})
    (root / "head.json").write_text(json.dumps(completed), encoding="utf-8")

    with pytest.raises(ValueError, match="Completed holdout is immutable"):
        family_holdout.check("holdout")
    assert json.loads((root / "head.json").read_text(encoding="utf-8")) == completed
