"""Exact saved-system role support, without producer or parquet reads."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.research import recent_alpha158_view as m
from trading.research.tests.test_recent_alpha158_view import save


def test_system_roles_share_latest_ordering_with_legacy_and_reject_foreign_paths(tmp_path):
    def write(path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def ref(path):
        return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}

    def publication(path, source, day, computed, recorded):
        return write(path, {"schema": "recent_alpha158_dated_target_publication_v1",
            "status": "close_research_target_publication_only", "source_close_day": day,
            "proposed_execution_day": "2026-10-20", "recorded_at": recorded,
            "score_computed_at": computed, "publication_origin": "local_recording_now",
            "execution_binding_pending": False,
            "source": {"batch_run": str(source), "batch_sha256": ref(source)["sha256"]},
            "frozen_model_contract": {"frozen": "unchanged"},
            "cases": [{"case": k, "status": "unbound_research_full_target", "target_weights": None}
                for k in sorted(m.CASE_IDS)],
            **{k: False for k in ("publication_time_backfilled", "execution_day_rolled_forward",
                "strategy_validated", "selection_enabled", "execution_admitted", "model_refit",
                "broker_interacted", "provider_contacted")}})

    now = datetime.fromisoformat("2026-10-19T20:00:00+08:00")
    legacy_score, original, legacy_provider = save(tmp_path)
    project = tmp_path / "project"
    legacy_pub = publication(project / "outputs/recent_alpha158_target_publication/legacy/result.json",
        legacy_score, "2026-10-14", "2026-10-15T12:02:00+08:00", "2026-10-15T12:03:00+08:00")
    legacy = m.recent_alpha158_view(tmp_path, project_root=project, now=now)
    assert legacy["source_file"] == str(legacy_score)
    assert legacy["latest_publication"]["matches_displayed_score_batch"] is True
    artifacts = tmp_path / "research/system_daily/20261017_v1/operations/20261019/artifacts"
    provider = json.loads(legacy_provider.read_text())
    provider.update(created_at="2026-10-19T18:00:00+08:00", recent_source_days=["2026-10-19"])
    provider_path = write(artifacts / "alpha_provider/result.json", provider)
    score = deepcopy(original)
    score.update(started_at="2026-10-19T18:01:00+08:00", finished_at="2026-10-19T18:02:00+08:00",
        actual_computed_at="2026-10-19T18:02:00+08:00", source_available_at=provider["created_at"],
        provider_view=str(provider_path.parent), provider_result_sha256=ref(provider_path)["sha256"],
        requested_feature_dates=["2026-10-19"], score_dates=["2026-10-19"], score_rows=7,
        native_feature_rows=10, partitions=[{"symbols": [str(i) for i in range(10)], "rows": 10}],
        provider_calendar_end="2026-10-19", coverage=[{**original["coverage"][0], "day": "2026-10-19"}])
    score_path = write(artifacts / "alpha_score/result.json", score)
    (score_path.parent / "dated_scores.parquet").write_bytes(b"not-a-parquet-must-not-be-read")
    pub_path = publication(artifacts / "alpha_publication/result.json", score_path, "2026-10-19",
        score["finished_at"], "2026-10-19T18:03:00+08:00")
    cases = {k: {"context": {"account_id": "independent:" + k},
        "state": {"day": "2026-10-19", "cash": 100000, "positions": {}, "orders": {}, "fills": {}},
        "daily": [{"date": "2026-10-19", "equity": 1000}],
        "journal": [{"decision_day": "2026-10-19", "committed_at": "2026-10-19T18:04:00+08:00"}]}
        for k in sorted(m.CASE_IDS)}
    head = {"schema": "recent_alpha158_published_close_shared_ledger_checkpoint_v1",
        "registered_at": "2026-10-15T12:04:00+08:00", "first_decision_day": "2026-10-15",
        "binding": {"frozen_model_contract": {"frozen": "unchanged"}}, "cases": cases,
        "publications": [ref(legacy_pub), ref(pub_path)],
        **{k: False for k in ("strategy_validated", "selection_enabled", "execution_admitted",
            "model_refit", "broker_interacted", "provider_contacted", "user_account_mutated")}}
    head_path = write(tmp_path / "research/recent_alpha158_forward/original/head.json", head)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    view = m.recent_alpha158_view(tmp_path, project_root=project, now=now)
    assert view["source_file"] == str(score_path) and view["score_dates"] == ["2026-10-19"]
    assert view["provider_source_file"] == str(provider_path)
    assert view["latest_publication"]["source_file"] == str(pub_path)
    assert view["latest_publication"]["matches_displayed_score_batch"] is True
    assert view["saved_forward_trace"]["latest_publication_bound"] is True
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    foreign = write(tmp_path / "foreign/result.json", json.loads(pub_path.read_text()))
    head["publications"].append(ref(foreign))
    write(head_path, head)
    with pytest.raises(ValueError, match="exact registered artifact role"):
        m.recent_alpha158_view(tmp_path, project_root=project, now=now)
    head["publications"].pop()
    head["publications"][0]["sha256"] = "0" * 64
    write(head_path, head)
    with pytest.raises(ValueError, match="publication binding changed"):
        m.recent_alpha158_view(tmp_path, project_root=project, now=now)
    score_path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be ordered"):
        m.recent_alpha158_view(tmp_path, project_root=project, now=now)
