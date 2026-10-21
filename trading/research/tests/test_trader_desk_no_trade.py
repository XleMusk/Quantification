"""Explicit saved NO_TRADE is usable; empty or damaged order plans are not."""
import csv
import json

import pytest

from trading import settings, research_diagnostics
from trading.research import trader_desk_snapshot as m


def write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_csv(path, fields, rows=()):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "ROOT", tmp_path / "project")
    monkeypatch.setattr(m, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(settings, "ROOT", tmp_path / "project")
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(research_diagnostics, "catalog", lambda: [])
    monkeypatch.setattr(m, "next_exchange_session", lambda day: ("2026-10-20", {"status": "fixture"}))
    paths = {key: tmp_path / (key + suffix) for key, suffix in [
        ("orders", ".csv"), ("preflight", ".csv"), ("targets", ".csv"),
        ("holdings", ".csv"), ("preview_orders", ".csv"), ("preview", ".json"), ("pack", ".json")]}
    write_csv(paths["orders"], ["strategy_id", "symbol", "side", "qty"])
    write_csv(paths["preflight"], ["strategy_id", "symbol", "side", "preflight_status"])
    write_csv(paths["targets"], ["strategy_id", "symbol", "target_qty", "active_target"], [
        {"strategy_id": "fixed", "symbol": "600001.SSE", "target_qty": 100, "active_target": "true"}])
    write_csv(paths["holdings"], ["symbol", "current_qty", "target_qty"], [
        {"symbol": "600001.SSE", "current_qty": 100, "target_qty": 100}])
    write_csv(paths["preview_orders"], ["symbol", "qty"], [{"symbol": "600001.SSE", "qty": 0}])
    preview = {"status": "paper_preview_only", "trade_status": "NO_TRADE", "strategy_id": "fixed",
        "execution_admitted": False, "source_cutoff": "2026-10-16", "execution_day": "2026-10-19",
        "provisional_order_count": 0, "position_count": 1,
        "artifacts": {"target_holdings": str(paths["holdings"]), "provisional_order_plan": str(paths["preview_orders"])}}
    strategy = {"strategy_id": "fixed", "preview": str(paths["preview"]),
        "candidate_order_count": 0, "buys": 0, "sells": 0, "estimated_notional_rmb": "0.00",
        "ledger_preflight_accepted": 0, "ledger_preflight_rejected": 0, "ledger_preflight_status": "completed",
        "execution_config": {"initial_cash": 140000, "minimum_commission": 5}, "position_count": 1}
    pack = {"schema": "a_share_opening_pack_v1", "status": "paper_candidates_only", "execution_admitted": False,
        "source_cutoff": "2026-10-16", "execution_day": "2026-10-19", "strategy_count": 1,
        "candidate_order_count": 0, "order_union_count": 0, "order_consensus_count": 0,
        "strategies": [strategy], "artifacts": {"candidate_orders": str(paths["orders"]),
            "ledger_preflight": str(paths["preflight"]), "candidate_targets": str(paths["targets"])}}
    def flush():
        write_json(paths["preview"], preview)
        write_json(paths["pack"], pack)
    flush()
    return paths, preview, pack, flush


@pytest.mark.parametrize("selected", [None, "fixed"])
def test_no_trade_full_desk_keeps_holdings_zero_operations_and_no_minimum_fee(saved, tmp_path, selected):
    paths, _, _, _ = saved
    before = {name: path.read_bytes() for name, path in paths.items()}
    out, result = m.build(paths["pack"], tmp_path / "desk", selected_strategy_id=selected)
    assert result["trade_status"] == "NO_TRADE"
    assert result["orders"]["total"] == result["orders"]["buy"] == result["orders"]["sell"] == 0
    assert result["pretrade_cost_estimate_rmb"] == "0.00"
    assert result["saved_research_holdings"][0]["quantity"] == 100
    assert result["paper_fill_rehearsal"]["synthetic_full_fill_count"] == 0
    assert result["paper_fill_rehearsal"]["cash_delta_rmb"] == "0.00"
    assert result["account_observed"] is result["submission_attempted"] is result["execution_admitted"] is False
    for name in ("blotter", "risk_checks", "execution_schedule", "paper_fill_projection"):
        assert m.read_csv(out / (name + ".csv")) == []
    assert {name: path.read_bytes() for name, path in paths.items()} == before


@pytest.mark.parametrize("corruption", ["not_no_trade", "claimed_order", "missing_targets", "target_changed", "preflight_only"])
def test_empty_damaged_or_non_no_trade_packs_are_rejected(saved, tmp_path, corruption):
    paths, preview, pack, flush = saved
    if corruption == "not_no_trade":
        preview["trade_status"] = "PAPER_TARGET_PENDING"
    elif corruption == "claimed_order":
        pack["strategies"][0]["candidate_order_count"] = 1
    elif corruption == "missing_targets":
        paths["targets"].unlink()
    elif corruption == "target_changed":
        write_csv(paths["targets"], ["strategy_id", "symbol", "target_qty"], [
            {"strategy_id": "fixed", "symbol": "600001.SSE", "target_qty": 0}])
    else:
        write_csv(paths["preflight"], ["strategy_id", "symbol", "side", "preflight_status"], [
            {"strategy_id": "fixed", "symbol": "600001.SSE", "side": "sell", "preflight_status": "accepted"}])
    flush()
    with pytest.raises((ValueError, FileNotFoundError)):
        m.build(paths["pack"], tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
