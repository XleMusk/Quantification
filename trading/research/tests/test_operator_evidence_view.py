import json

import pytest

from trading.research.operator_evidence_view import build_view


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_build_view_requires_three_core_artifacts(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_view({}, lambda _package, _name: None)


def test_build_view_keeps_saved_evidence_research_only(tmp_path):
    paths = {
        "strategy_evidence_card": _write(tmp_path / "evidence.json", {
            "status": "research_only", "opening_pack": {"candidates": []},
            "factor_replay": {}, "interpretation": {}, "limits": ["saved only"],
        }),
        "strategy_promotion_review": _write(tmp_path / "promotion.json", {
            "status": "manual_review", "candidate_count": 1,
            "eligible_candidate_count": 0, "rows": [],
        }),
        "strategy_board_json": _write(tmp_path / "board.json", {"rows": []}),
    }
    result = build_view({}, lambda _package, name: paths.get(name))
    assert result["schema"] == "offline_strategy_evidence_view_v1"
    assert result["execution_admitted"] is False
    assert result["automatic_promotion"] is False
    assert result["promotion"]["eligible_candidate_count"] == 0
