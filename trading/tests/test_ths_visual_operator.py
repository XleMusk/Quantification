"""Operator cards reuse a synthetic existing preview without client or DB access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import pytest

from trading.ths.adapter import from_portfolio
from trading.ths.visual_operator import build_visual_operator_card


def _inputs(current):
    snapshot = {"source": "ths_user_export", "account_alias": "synthetic-only",
                "observed_at": current.isoformat(), "trading_date": current.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                "cash_available": 0, "positions": {"600000.SSE": {"quantity": 200, "sellable": 200}}}
    portfolio = {"mode": "current_preview", "strategy_id": "synthetic-unvalidated",
                 "account_alias": "synthetic-only", "generated_at": current.isoformat(),
                 "targets": [{"symbol": "600000.SSE", "target_qty": 0, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"},
                             {"symbol": "000001.SZSE", "target_qty": 100, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"}]}
    preview = from_portfolio(snapshot, portfolio, current=current)
    profile = {"verified_local": True, "account_alias": "synthetic-only",
               "fields": {"symbol": {"control_id": 1}, "price": {"control_id": 2}, "qty": {"control_id": 3}},
               "guards": {"account": {"selector": {"control_id": 4}, "expected": "synthetic-only"},
                          "side": {"selector": {"control_id": 5}, "expected": {"buy": "买入", "sell": "卖出"}},
                          "price_mode": {"selector": {"control_id": 6}, "expected": "限价"}}}
    return preview, profile


@pytest.fixture
def current():
    return datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)


def test_only_existing_sell_step_is_exposed_without_mutating_preview(current, monkeypatch):
    preview, profile = _inputs(current)
    originals = deepcopy((preview, profile))
    def forbidden(*args, **kwargs):
        raise AssertionError("No window, journal or persistent Ledger operation is allowed")
    monkeypatch.setattr("trading.store.connection", forbidden)
    monkeypatch.setattr("trading.ths.journal.save_plan", forbidden)
    monkeypatch.setattr("trading.ths.windows.ControlWindow", forbidden)
    card = build_visual_operator_card(preview, profile, current=current)
    assert card["next_plan_id"] == preview["next_plan"]["id"]
    assert card["expected_fields"] == {"symbol": "600000", "price": "10.00", "qty": "200"}
    assert card["remaining_adjustments"][0]["symbol"] == "000001.SZSE"
    assert card["remaining_adjustments"][0]["status"] == "waiting_for_sales"
    assert "expected_fields" not in card["remaining_adjustments"][0]
    assert (preview, profile) == originals
    assert card["write_allowed"] is card["submission_enabled"] is card["execution_admitted"] is False
    assert all(step["execution_enabled"] is False for step in card["operator_steps"])


def test_stale_preview_withholds_fields_and_steps(current):
    preview, profile = _inputs(current)
    card = build_visual_operator_card(preview, profile, current=current + timedelta(minutes=6))
    assert card["status"] == "blocked_plan_validation"
    assert card["expected_fields"] is None and card["operator_steps"] == []


@pytest.mark.parametrize("change,blocker", [
    ({"verified_local": False}, "gui_profile_unverified_local_mapping"),
    ({"account_alias": "another-account"}, "gui_profile_account_mismatch"),
    ({"guards": {}}, "gui_profile_missing_guard_account"),
])
def test_unverified_or_wrong_profile_never_presents_operation_steps(current, change, blocker):
    preview, profile = _inputs(current)
    profile.update(change)
    card = build_visual_operator_card(preview, profile, current=current)
    assert card["status"] == "blocked_gui_profile"
    assert blocker in card["blockers"] and card["operator_steps"] == []


def test_readback_mismatch_stops_and_matching_fields_do_not_advance_order(current):
    preview, profile = _inputs(current)
    readback = {"symbol": "600000", "price": "10.01", "qty": "200"}
    bad = build_visual_operator_card(preview, profile, current=current, readback=readback)
    assert bad["status"] == "readback_mismatch_stop"
    assert bad["readback_check"]["mismatches"][0]["category"] == "price_mismatch"
    readback["price"] = "10.00"
    good = build_visual_operator_card(preview, profile, current=current, readback=readback)
    assert good["status"] == "readback_verified_manual_checkpoint"
    assert good["next_plan_id"] == preview["next_plan"]["id"]
    assert good["automatic_next_step_allowed"] is False and good["submitted"] is False
    assert good["readback_check"]["source"] == "caller_supplied_json_not_live_client_capture"


def test_research_package_or_mismatched_next_adjustment_cannot_be_used(current):
    with pytest.raises(ValueError, match="not a research paper package"):
        build_visual_operator_card({"schema": "stockdb_paper_target_order_package_v1", "execution_admitted": False}, {}, current=current)
    preview, profile = _inputs(current)
    preview["adjustments"][0]["delta_qty"] = -100
    with pytest.raises(ValueError, match="exactly the existing next_plan"):
        build_visual_operator_card(preview, profile, current=current)


def test_cli_creates_only_a_readonly_card_artifact(tmp_path):
    preview, profile = _inputs(datetime.now(timezone.utc))
    preview_path, profile_path, output_path = (tmp_path / name for name in ("preview.json", "profile.json", "card.json"))
    preview_path.write_text(json.dumps(preview), encoding="utf-8")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "trading.ths.visual_operator", "--preview", str(preview_path),
                             "--profile", str(profile_path), "--out", str(output_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    card = json.loads(output_path.read_text(encoding="utf-8"))
    assert card["status"] == "offline_single_plan_reference_ready"
    assert card["source_artifacts"]["preview"] == str(preview_path.resolve())
    assert card["write_allowed"] is card["submission_enabled"] is card["ledger_mutated"] is False


def test_project_cli_reports_research_package_rejection_without_order_fields(tmp_path):
    preview_path, profile_path, output_path = (tmp_path / name for name in ("paper.json", "profile.json", "card.json"))
    preview_path.write_text(json.dumps({"schema": "stockdb_paper_target_order_package_v1",
                                        "execution_admitted": False}), encoding="utf-8")
    profile_path.write_text("{}", encoding="utf-8")
    from pathlib import Path
    launcher = Path(__file__).resolve().parents[2] / "run_committee.py"
    result = subprocess.run([sys.executable, str(launcher), "ths-visual-operator-card",
                             "--preview", str(preview_path), "--profile", str(profile_path),
                             "--out", str(output_path)], capture_output=True, text=True)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    card = json.loads(output_path.read_text(encoding="utf-8"))
    assert card["status"] == "blocked_input"
    assert card["expected_fields"] is card["next_order"] is None
    assert card["operator_steps"] == [] and card["write_allowed"] is False
