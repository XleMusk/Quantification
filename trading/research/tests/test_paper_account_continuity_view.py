import json

from trading.research import paper_account_continuity as view


def test_build_view_reads_saved_receipt_without_account_side_effects(tmp_path):
    path = tmp_path / "outputs" / "paper_plans" / "run_continuity_receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "status": "saved",
        "last_completed_day": "2026-10-23",
        "order_count": 4,
        "fill_count": 3,
        "pending_targets": True,
    }), encoding="utf-8")

    result = view.build_view(tmp_path)

    assert result["status"] == "saved"
    assert result["last_completed_day"] == "2026-10-23"
    assert result["order_count"] == 4
    assert result["pending_targets"] is True
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_build_view_exposes_invalid_saved_receipt(tmp_path):
    path = tmp_path / "outputs" / "paper_plans" / "run_continuity_receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    result = view.build_view(tmp_path)

    assert result["status"] == "invalid_receipt"
    assert result["source_file"] == str(path)
    assert result["execution_admitted"] is False
