import json


def test_promotion_review_surfaces_matching_family_holdout(tmp_path, monkeypatch):
    from trading.research import strategy_promotion_review as review

    monkeypatch.setattr(review.settings, "ROOT", tmp_path)
    holdout_root = tmp_path / "outputs" / "family_holdout" / "candidate_holdout"
    holdout_root.mkdir(parents=True)
    protocol_path = holdout_root / "protocol.json"
    protocol_path.write_text(json.dumps({
        "candidate_run": "candidate-run",
        "candidate_signal": "candidate_signal",
        "candidate_end": "2026-09-30",
    }), encoding="utf-8")
    (holdout_root / "run.json").write_text(json.dumps({
        "status": "waiting_for_new_complete_snapshot",
        "protocol": str(protocol_path),
        "source_cutoff": "2026-09-30",
        "holdout_sessions": 0,
        "fresh_holdout_claimed": False,
        "strategy_validated": False,
    }), encoding="utf-8")

    result = review._latest_family_holdout_by_signal()

    assert result["candidate_signal"]["status"] == "waiting_for_new_complete_snapshot"
    assert result["candidate_signal"]["candidate_end"] == "2026-09-30"
    assert result["candidate_signal"]["execution_admitted"] is False
