"""Publication I/O only: tiny saved native features, fake frozen-score math."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import shortline_native_scoring as m
from trading.research.baostock_daily_slice import digest


DAY = "2026-10-15"
COMPUTED_AT = "2026-10-15T22:00:00+08:00"
UNKNOWN = ["600003.SSE"]


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf8")


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    features = tmp_path / "latest_features.csv"
    rows = []
    for i, stage in enumerate((1, 3, None), 1):
        rows.append({"event_id": f"60000{i}.SSE|{DAY}|s{stage}", "date": DAY, "symbol": f"60000{i}.SSE",
            "stage": stage, "stage_reference_known": stage is not None, "decision_eligible": stage is not None,
            **{name: .1 * i for name in m.FEATURES}, "hit_5": 999, "label_witness_day": "2099-01-01"})
    pd.DataFrame(rows).to_csv(features, index=False)
    scope = {"stage_membership_complete": False, "unknown_stage_symbols": list(UNKNOWN),
        "source_rows": 3, "valid_scope_rows": 3, "full_market_stage_membership_complete": False}
    panel = tmp_path / "panel.json"
    _write(panel, {"feature_contract_id": "native_active_known_nonST_observed_nine_features_v1",
        "execution_admitted": False, "strategy_validated": False,
        "latest_features_path": str(features), "latest_features_sha256": digest(features),
        "last_source_day": DAY, "available_at": "2026-10-15T18:00:00+08:00",
        "feature_semantics": {"scope": "synthetic native own-day test"}, "latest_scope": scope})
    model = tmp_path / "model_manifest.json"
    _write(model, {"synthetic_test_double": True, "weights_not_loaded_or_fitted": True})
    calls = []
    monkeypatch.setattr(m, "load_generation", lambda _: SimpleNamespace(path=model))
    def fake_score(generation, frame, *, day, model_kind, feature_provenance):
        assert generation.path == model and day == DAY
        assert frame.symbol.tolist() == ["600001.SSE", "600002.SSE"]
        assert not {"hit_5", "label_witness_day"}.intersection(frame.columns)
        assert feature_provenance["unknown_stage_symbols"] == UNKNOWN
        scores = frame.copy()
        scores["model_hit5_probability"] = [.2, .4]
        scores["paper_candidate_only"] = False
        proof = {"schema": "shortline_frozen_single_date_score_v1", "day": day, "model_kind": model_kind,
            "status": "partial_known_stage_frozen_single_date_probabilities_only",
            "scored_at": COMPUTED_AT, "score_available_at": COMPUTED_AT,
            "model_available_at": "2026-10-15T21:00:00+08:00", "model_refit": False,
            "score_rows": len(scores), "stage_counts": {"1": 1, "2": 0, "3": 1},
            "stage_membership_complete": False, "known_stage_partial_research_only": True,
            "unknown_stage_symbols": list(UNKNOWN), "batch_complete_for_target": False,
            "score_only_not_candidate_batch": True, "target_generation_allowed": False,
            "feature_source": deepcopy(feature_provenance), **m.FLAGS}
        calls.append(model_kind)
        return {"scores": scores, "provenance": proof}
    monkeypatch.setattr(m, "score_day", fake_score)
    return SimpleNamespace(panel=panel, model=model, scope=scope, calls=calls)


def test_publication_is_last_closed_payload_receipt_and_preserves_partial_compute_boundary(tmp_path, monkeypatch, evidence):
    out = tmp_path / "scores"
    publication = out / "publication.json"
    payload_names = {"result.json", "20261015_lr_raw_scores.csv", "20261015_lr_raw_scores.json",
        "20261015_lgbm_raw_scores.csv", "20261015_lgbm_raw_scores.json"}
    observed = []
    original_open = Path.open
    def observed_open(path, mode="r", *args, **kwargs):
        if path == publication and mode == "x":
            # This runs before the receipt is opened. Every bound payload must
            # already be closed, fully parseable and hashable at this point.
            assert not publication.exists()
            assert {p.name for p in out.iterdir()} == payload_names
            result = json.loads((out / "result.json").read_text(encoding="utf8"))
            for batch in result["score_batches"]:
                csv, proof_file = Path(batch["score_file"]), Path(batch["provenance_file"])
                assert digest(csv) == batch["score_sha256"]
                assert digest(proof_file) == batch["provenance_sha256"]
                assert len(pd.read_csv(csv)) == 2
                assert json.loads(proof_file.read_text(encoding="utf8"))["score_available_at"] == COMPUTED_AT
            observed.append("all_payloads_closed_before_receipt")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observed_open)
    returned = m.score_saved_generation(evidence.model, evidence.panel, out)
    receipt = json.loads(publication.read_text(encoding="utf8"))
    saved_result = json.loads((out / "result.json").read_text(encoding="utf8"))
    assert observed == ["all_payloads_closed_before_receipt"]
    assert evidence.calls == ["lr", "lgbm"]
    assert returned["published_at"] == receipt["published_at"]
    assert "published_at" not in saved_result  # No post-hash result rewrite.
    assert receipt["schema"] == "native_shortline_raw_score_publication_v1"
    assert receipt["previous_payload_hashes_present"] is True
    assert receipt["receipt_written_after_all_bound_payloads"] is True
    assert receipt["payload_computation_time_is_publication"] is False
    assert receipt["source_scope"] == evidence.scope
    assert datetime.fromisoformat(receipt["published_at"]) >= datetime.fromisoformat(saved_result["completed_at"])
    assert datetime.fromisoformat(receipt["published_at"]) > datetime.fromisoformat(COMPUTED_AT)
    assert {binding["path"] for binding in receipt["files"]} == payload_names
    for binding in receipt["files"]:
        file = out / binding["path"]
        assert binding["sha256"] == digest(file) and binding["byte_count"] == file.stat().st_size
    for batch in receipt["score_batches"]:
        assert batch["score_status"] == "partial_known_stage_frozen_single_date_probabilities_only"
        assert batch["score_available_at"] == COMPUTED_AT
        assert batch["stage_membership_complete"] is False and batch["unknown_stage_symbols"] == UNKNOWN
        assert batch["target_generation_allowed"] is False
    assert receipt["target_generation_allowed"] is False and receipt["execution_admitted"] is False


@pytest.mark.parametrize('mismatch', ['day', 'identity', 'duplicate'])
def test_publication_rejects_cross_model_day_or_identity_mismatch(tmp_path, evidence, mismatch):
    out = tmp_path / 'mixed_batch'
    m.score_saved_generation(evidence.model, evidence.panel, out)
    result_file = out / 'result.json'
    result = json.loads(result_file.read_text(encoding='utf8'))
    row = next(item for item in result['score_batches'] if item['model_kind'] == 'lgbm')
    score_file, proof_file = Path(row['score_file']), Path(row['provenance_file'])
    scores = pd.read_csv(score_file)
    proof = json.loads(proof_file.read_text(encoding='utf8'))
    if mismatch == 'day':
        row['day'] = proof['day'] = '2026-10-16'
        scores['date'] = '2026-10-16'
    elif mismatch == 'identity':
        scores.loc[0, 'symbol'] = '600099.SSE'
    else:
        scores.loc[1, ['symbol', 'date', 'stage']] = scores.loc[0, ['symbol', 'date', 'stage']].values
    scores.to_csv(score_file, index=False, encoding='utf-8-sig')
    _write(proof_file, proof)
    row['score_sha256'], row['provenance_sha256'] = digest(score_file), digest(proof_file)
    _write(result_file, result)
    destination = tmp_path / 'rejected_publication.json'
    with pytest.raises(ValueError, match='same day and event identities|unique known-stage'):
        m.publish_saved_score_batch(result_file, destination)
    assert not destination.exists()


def test_payload_write_failure_has_no_publication_or_completed_batch(tmp_path, monkeypatch, evidence):
    out = tmp_path / "interrupted"
    original_csv = pd.DataFrame.to_csv
    def failed_csv(frame, path, *args, **kwargs):
        if Path(path).name == "20261015_lgbm_raw_scores.csv":
            raise OSError("synthetic second payload write failed")
        return original_csv(frame, path, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, "to_csv", failed_csv)
    with pytest.raises(OSError, match="second payload write failed"):
        m.score_saved_generation(evidence.model, evidence.panel, out)
    assert (out / "20261015_lr_raw_scores.csv").exists()
    assert not (out / "publication.json").exists()
    assert not (out / "result.json").exists()


def test_independent_legacy_publication_does_not_rewrite_or_rescore_and_declared_tamper_rejected(tmp_path, monkeypatch, evidence):
    out = tmp_path / "saved_batch"
    m.score_saved_generation(evidence.model, evidence.panel, out)
    result_file = out / "result.json"
    modern = json.loads(result_file.read_text(encoding="utf8"))
    # A separate saved legacy result has exactly the old unhashed format. The
    # original score files remain unchanged; absence of old hashes is explicit.
    legacy = deepcopy(modern)
    legacy.pop("publication_file", None)
    legacy.pop("publication_clock_requires_saved_receipt", None)
    for batch in legacy["score_batches"]:
        batch.pop("score_sha256")
        batch.pop("provenance_sha256")
    legacy_result = out / "legacy_result.json"
    _write(legacy_result, legacy)
    before = {str(p): p.read_bytes() for p in out.iterdir() if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("publishing saved payloads must not score or load models")
    monkeypatch.setattr(m, "score_day", forbidden)
    monkeypatch.setattr(m, "load_generation", forbidden)
    publication = tmp_path / "independent_legacy_publication.json"
    receipt = m.publish_saved_score_batch(legacy_result, publication)
    assert publication.exists() and receipt["previous_payload_hashes_present"] is False
    assert receipt["old_payloads_modified"] is False
    assert before == {str(p): p.read_bytes() for p in out.iterdir() if p.is_file()}
    assert evidence.calls == ["lr", "lgbm"]
    with pytest.raises(FileExistsError):
        m.publish_saved_score_batch(legacy_result, publication)
    changed = Path(modern["score_batches"][0]["score_file"])
    with changed.open("a", encoding="utf8") as handle:
        handle.write("changed after declared digest\n")
    rejected_publication = tmp_path / "should_not_publish.json"
    with pytest.raises(ValueError, match="digest changed"):
        m.publish_saved_score_batch(result_file, rejected_publication)
    assert not rejected_publication.exists()
