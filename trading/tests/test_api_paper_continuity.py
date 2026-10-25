import json

from trading import api


def test_paper_account_continuity_reads_latest_saved_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)
    receipt = tmp_path / "outputs" / "paper_plans" / "20261024_lowvol_current_continuity_receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "schema": "quantification_paper_account_continuity_receipt_v1",
        "status": "VERIFIED_READONLY_CURRENT_CUTOFF",
        "run_dir": "D:/QuantificationData/trading/fixed_daily/current",
        "last_completed_day": "2026-10-23",
        "generation": 0,
        "daily_rows": 34,
        "replay_completed_sessions": 34,
        "order_count": 25,
        "fill_count": 44,
        "pending_targets": False,
        "cash_cny": 43413.07,
        "position_count": 19,
        "refresh_gate": "No new exchange session is available",
        "limitations": ["Saved research paper account only"],
        "execution_admitted": False,
    }), encoding="utf-8")

    result = api.paper_account_continuity()

    assert result["status"] == "VERIFIED_READONLY_CURRENT_CUTOFF"
    assert result["last_completed_day"] == "2026-10-23"
    assert result["daily_rows"] == result["replay_completed_sessions"] == 34
    assert result["order_count"] == 25
    assert result["fill_count"] == 44
    assert result["execution_admitted"] is False
    assert result["client_interacted"] is False


def test_paper_account_continuity_exposes_missing_receipt_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "ROOT", tmp_path)

    result = api.paper_account_continuity()

    assert result["status"] == "not_observed"
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
