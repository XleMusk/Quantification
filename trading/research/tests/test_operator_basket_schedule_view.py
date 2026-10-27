from pathlib import Path

from trading import api
from trading.research.operator_basket_schedule_view import build_view


def _patch_schedule(monkeypatch, tmp_path: Path):
    schedule = tmp_path / "execution_schedule.csv"
    schedule.write_text(
        "phase,side,submission_mode,live_submission\n"
        "sell,sell,manual_ths_prefill_only,disabled\n"
        "buy,buy,manual_ths_prefill_only,disabled\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "operator_package", lambda: {})
    monkeypatch.setattr(api, "_package_artifact", lambda _package, name: schedule if name == "execution_schedule" else None)
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {"ledger_mutated": False})
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {"status": "not_observed"})
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_args: {"same_ledger_reconciliation_status": "not_observed"})


def test_basket_schedule_view_preserves_sell_before_buy_boundary(monkeypatch, tmp_path):
    _patch_schedule(monkeypatch, tmp_path)

    result = build_view(api.__dict__)

    assert result["schema"] == "offline_basket_schedule_view_v1"
    assert result["schedule"]["sell_before_buy"] is True
    assert result["schedule"]["manual_only"] is True
    assert result["schedule"]["live_submission_disabled_count"] == 2
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_basket_schedule_route_delegates_to_view(monkeypatch):
    expected = {"schema": "offline_basket_schedule_view_v1", "status": "delegated"}
    monkeypatch.setattr("trading.research.operator_basket_schedule_view.build_view", lambda namespace: expected)

    assert api.operator_package_basket_schedule() == expected
