import json

from trading.research import paper_preview_view as view


def test_missing_preview_is_explicit(tmp_path):
    result = view.build_view(tmp_path)
    assert result["status"] == "not_observed"
    assert result["execution_admitted"] is False


def test_invalid_preview_is_explicit(tmp_path):
    path = tmp_path / "research" / "paper_rebalance" / "run" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    result = view.build_view(tmp_path)
    assert result["status"] == "invalid_receipt"
    assert result["source_file"] == str(path)


def test_saved_preview_keeps_read_only_fields(tmp_path):
    path = tmp_path / "research" / "paper_rebalance" / "run" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "paper_preview_only", "strategy_id": "fixed",
                                "provisional_target_count": 20, "artifacts": {"target": "x"}}), encoding="utf-8")
    result = view.build_view(tmp_path)
    assert result["status"] == "paper_preview_only"
    assert result["provisional_target_count"] == 20
    assert result["ledger_mutated"] is False
