"""Weight-to-plan consumers use isolated synthetic accounts, never native THS."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from trading.config import StrategyConfig
from trading.engine import shares_for_target
from trading.portfolio_allocation import OBSERVED_LOT_ALLOCATION, OPEN_LOT_ALLOCATION
from trading.rules import ETF_PROFILE, execution_rule
from trading.ths.adapter import check_plan, now
from trading.ths.broker import THSAdapter
from trading.ths.weights import from_weights


@pytest.fixture
def current():
    return datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


def account(stamp):
    return {"source": "ths_user_export", "account_alias": "synthetic-only", "kind": "account",
            "observed_at": stamp.isoformat(),
            "trading_date": stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            "cash_available": 0, "cash_total": 12000000, "cash_frozen": 12000000,
            "cash_basis": "cash_balance_includes_frozen",
            "positions": {"600000.SSE": {"quantity": 2000, "sellable": 2000}},
            "marks": {"600000.SSE": 1000}}


def weights(stamp):
    return {"mode": "current_preview", "strategy_id": "synthetic-not-admitted",
            "account_alias": "synthetic-only", "generated_at": stamp.isoformat(),
            "weight_unit": "equity_fraction", "targets": [
                {"symbol": "000001.SZSE", "target_weight": .2, "price_basis": "raw_rmb",
                 "limit_price_rmb": "12.00", "quote": {
                     "symbol": "000001.SZSE", "price_rmb": "10.00", "price_basis": "raw_rmb",
                     "observed_at": stamp.isoformat(),
                     "trading_date": stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat()}},
                {"symbol": "600000.SSE", "target_weight": .1, "price_basis": "raw_rmb",
                 "limit_price_rmb": "9.00"}]}


def test_weights_use_total_equity_and_marks_not_available_cash_or_limits(current):
    snapshot, value = account(current), weights(current)
    original = deepcopy(snapshot)
    result = THSAdapter({"account_alias": "synthetic-only"}).prepare_weights(snapshot, value, current=current)
    basis = result["weight_sizing"]
    assert basis["equity_cents"] == 14000000 and basis["held_value_cents"] == 2000000
    assert basis["cash_frozen_cents"] == 12000000
    indexed = {item["symbol"]: item for item in basis["targets"]}
    assert indexed["000001.SZSE"]["target_qty"] == 2800
    assert indexed["000001.SZSE"]["valuation_price_cents"] == 1000
    assert indexed["600000.SSE"]["target_qty"] == 1400
    assert indexed["600000.SSE"]["valuation_source"] == "account_mark"
    plan = result["next_plan"]
    assert (plan["symbol"], plan["side"], plan["qty"], plan["price_cents"]) == ("600000.SSE", "sell", 600, 900)
    assert result["adjustments"][1]["status"] == "waiting_for_sales"
    assert result["adjustments"][1]["validation_reason"] == "insufficient_cash"
    assert not result["saved"] and not result["client_interacted"] and not result["execution_admitted"]
    assert snapshot == original
    value["targets"][0]["target_weight"] = 0
    assert plan["weight_portfolio"]["targets"][0]["target_weight"] == .2
    check_plan(plan, current=current)


def test_broker_weight_entry_accepts_strategy_configuration(current):
    snapshot, value = account(current), weights(current)
    config = StrategyConfig(holdings=2, retain_rank=2, gross_limit=.4, single_limit=.2)
    result = THSAdapter({"account_alias": "synthetic-only"}).prepare_weights(
        snapshot, value, current=current, config=config
    )
    assert result["execution_config"]["holdings"] == 2
    assert result["weight_sizing"]["gross_limit"] == .4


def test_fresh_observation_recomputes_weight_residual_without_a_batch_cursor(current):
    snapshot, value = account(current), weights(current)
    snapshot["positions"]["600000.SSE"] = {"quantity": 1400, "sellable": 1400}
    snapshot.update(cash_total=12540000, cash_frozen=0, cash_available=12540000)
    result = from_weights(snapshot, value, current=current)
    assert result["weight_sizing"]["equity_cents"] == 13940000
    assert result["next_plan"]["symbol"] == "600000.SSE"
    assert result["next_plan"]["qty"] == 100


@pytest.mark.parametrize("policy", [None, OBSERVED_LOT_ALLOCATION])
def test_quote_expiry_is_not_renewed_by_later_plan_creation(current, policy):
    value = weights(current)
    value["lot_allocation"] = policy
    value["targets"][0]["quote"]["observed_at"] = (current - timedelta(seconds=290)).isoformat()
    plan = from_weights(account(current), value, current=current)["next_plan"]
    assert datetime.fromisoformat(plan["expires_at"]) == current + timedelta(seconds=10)
    with pytest.raises(ValueError, match="Expired"):
        check_plan(plan, current=current + timedelta(seconds=11))


@pytest.mark.parametrize("policy", [None, OBSERVED_LOT_ALLOCATION])
def test_bound_fees_govern_cash_reservation_and_stored_plan_recheck(current, policy):
    snapshot = {**account(current), "positions": {}, "marks": {},
                "cash_total": 100600, "cash_available": 100600, "cash_frozen": 0}
    value = weights(current)
    value["lot_allocation"] = policy
    value["targets"] = value["targets"][:1]
    value["targets"][0]["limit_price_rmb"] = "10.00"
    value["targets"][0]["quote"]["price_rmb"] = "1.50"
    assert from_weights(snapshot, value, current=current)["status"] == "awaiting_review"
    config = StrategyConfig(minimum_commission=50)
    blocked = from_weights(snapshot, value, current=current, config=config)
    assert blocked["status"] == "blocked" and blocked["reason"] == "insufficient_cash"
    assert blocked["next_plan"] is None
    snapshot.update(cash_total=106000, cash_available=106000)
    plan = from_weights(snapshot, value, current=current, config=config)["next_plan"]
    assert plan["execution_config"] == config.to_dict()
    check_plan(plan, current=current)
    plan["snapshot"].update(cash_total=100600, cash_available=100600)
    with pytest.raises(ValueError, match="insufficient_cash"):
        check_plan(plan, current=current)


def test_legacy_standalone_plan_keeps_its_default_config(current):
    plan = from_weights(account(current), weights(current), current=current)["next_plan"]
    plan.pop("execution_config")
    check_plan(plan, current=current)


@pytest.mark.parametrize("policy", [None, OBSERVED_LOT_ALLOCATION])
def test_t1_rejection_does_not_skip_to_a_buy(current, policy):
    snapshot = account(current)
    snapshot["positions"]["600000.SSE"]["sellable"] = 500
    value = {**weights(current), "lot_allocation": policy}
    result = from_weights(snapshot, value, current=current)
    assert result["status"] == "blocked" and result["reason"] == "insufficient_sellable_T1"
    assert result["next_plan"] is None and result["adjustments"][1]["status"] == "waiting_for_sales"


def test_observed_integer_targets_share_original_budget_and_preserve_explicit_exits(current):
    snapshot, value = account(current), weights(current)
    symbols = ["000003.SZSE", "000001.SZSE", "000002.SZSE"]
    template = value["targets"][0]
    value["targets"] = [{**template, "symbol": s, "target_weight": .1, "limit_price_rmb": "80.00",
                         "quote": {**template["quote"], "symbol": s, "price_rmb": "80.00"}} for s in symbols]
    value["targets"].append({"symbol": "600000.SSE", "target_weight": 0, "price_basis": "raw_rmb", "limit_price_rmb": "10.00"})
    legacy = from_weights(snapshot, value, current=current)
    assert [t["target_qty"] for t in legacy["portfolio"]["targets"]] == [100, 100, 100, 0]
    value["lot_allocation"] = OBSERVED_LOT_ALLOCATION
    result = from_weights(snapshot, value, current=current)
    assert [t["target_qty"] for t in result["portfolio"]["targets"]] == [200, 200, 100, 0]
    allocation = result["weight_sizing"]["allocation"]
    assert allocation["stock_budget_cny"] == 42000 and allocation["allocated_stock_cny"] == 40000
    assert allocation["squared_error_after"] < allocation["squared_error_before"]
    assert [e["symbol"] for e in allocation["estimates"]] == symbols
    assert result["next_plan"]["qty"] == 2000 and result["next_plan"]["side"] == "sell"
    assert all(a["status"] == "waiting_for_sales" and a["validation_reason"] == "insufficient_cash"
               for a in result["adjustments"] if a["side"] == "buy")
    assert result["next_plan"]["weight_portfolio"]["lot_allocation"] == OBSERVED_LOT_ALLOCATION
    assert result["next_plan"]["weight_sizing"]["allocation"] == allocation
    assert "lot_allocation" not in legacy["weight_sizing"]


def test_observed_integer_allocation_keeps_single_cap_and_rejects_replay_policy(current):
    value = weights(current)
    value["lot_allocation"] = OBSERVED_LOT_ALLOCATION
    value["targets"][0]["quote"]["price_rmb"] = "10.01"
    result = from_weights(account(current), value, current=current)
    assert result["portfolio"]["targets"][0]["target_qty"] == 2700  # Another lot would exceed .2.
    value["lot_allocation"] = OPEN_LOT_ALLOCATION
    with pytest.raises(ValueError, match="not a replay policy"):
        from_weights(account(current), value, current=current)


@pytest.mark.parametrize("change,match", [
    ({"mode": "replay"}, "current-preview"),
    ({"account_alias": "other"}, "alias mismatch"),
    ({"weight_unit": "percent"}, "equity_fraction"),
    ({"generated_at": "2020-01-01T00:00:00+00:00"}, "stale"),
    ({"targets": []}, "all held securities"),
])
def test_bad_portfolio_headers_not_promoted(current, change, match):
    with pytest.raises(ValueError, match=match):
        from_weights(account(current), {**weights(current), **change}, current=current)


@pytest.mark.parametrize("bad_weight", [True, "0.2", -.1, float("nan"), float("inf"), 20])
def test_weight_units_and_numbers_are_not_coerced(current, bad_weight):
    value = weights(current)
    value["targets"][0]["target_weight"] = bad_weight
    with pytest.raises(ValueError, match="weight"):
        from_weights(account(current), value, current=current)


def test_duplicate_and_over_cap_targets_reject_instead_of_silent_clipping(current):
    value = weights(current)
    value["targets"].append(deepcopy(value["targets"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        from_weights(account(current), value, current=current)
    value = weights(current)
    value["targets"][0]["target_weight"] = .21
    with pytest.raises(ValueError, match="position caps"):
        from_weights(account(current), value, current=current)
    value["targets"] = [{**value["targets"][0], "symbol": symbol, "target_weight": .2}
                        for symbol in ("600000.SSE", "600001.SSE", "600002.SSE", "600003.SSE", "600004.SSE")]
    with pytest.raises(ValueError, match="position caps"):
        from_weights(account(current), value, current=current)


@pytest.mark.parametrize("change,match", [
    ({"cash_total": 11999999}, "does not reconcile"),
    ({"marks": {}}, "Every holding"),
    ({"marks": {"600000.SSE": 0}}, "positive integer"),
    ({"source": "paper"}, "THS account"),
])
def test_bad_valuation_evidence_stops_preview(current, change, match):
    with pytest.raises(ValueError, match=match):
        from_weights({**account(current), **change}, weights(current), current=current)


def test_available_cash_alone_cannot_size_equity(current):
    snapshot = account(current)
    for key in ("cash_total", "cash_frozen", "cash_basis"):
        del snapshot[key]
    with pytest.raises(ValueError, match="total and frozen"):
        from_weights(snapshot, weights(current), current=current)


@pytest.mark.parametrize("change", [
    {"symbol": "600000.SSE"}, {"trading_date": "2020-01-01"},
    {"observed_at": "2020-01-01T00:00:00+00:00"},
    {"observed_at": "2026-10-02T08:01:00+00:00"},
    {"price_basis": "qfq"}, {"price_rmb": "0"}, {"price_rmb": "3.937"},
])
def test_new_security_requires_bound_fresh_raw_rmb_quote(current, change):
    value = weights(current)
    value["targets"][0]["quote"].update(change)
    with pytest.raises(ValueError):
        from_weights(account(current), value, current=current)


def test_limit_price_never_fills_in_a_missing_valuation(current):
    value = weights(current)
    del value["targets"][0]["quote"]
    with pytest.raises(ValueError, match="raw RMB quote"):
        from_weights(account(current), value, current=current)
    value = weights(current)
    value["targets"][1]["quote"] = deepcopy(value["targets"][0]["quote"])
    with pytest.raises(ValueError, match="account marks"):
        from_weights(account(current), value, current=current)


def test_scope_and_lot_rounding_do_not_grant_new_instrument_permissions(current):
    value = weights(current)
    value["targets"][0]["symbol"] = "510300.SSE"
    with pytest.raises(ValueError, match="main-board"):
        from_weights(account(current), value, current=current)
    value = weights(current)
    value["targets"][0]["quote"]["price_rmb"] = "10.01"
    result = from_weights(account(current), value, current=current)
    assert result["portfolio"]["targets"][0]["target_qty"] == 2700


@pytest.mark.parametrize("equity,weight,price", [(14000000, .2, 1000), (9999999, .17, 793), (1000000, 0, 0)])
def test_shared_sizing_preserves_existing_cent_and_mill_math(equity, weight, price):
    stock = execution_rule("600000.SSE", "2024-01-02")
    fund = execution_rule("510300.SSE", "2024-01-02", {"510300.SSE": ETF_PROFILE})
    assert shares_for_target(equity, weight, price, stock) == (int(equity * weight / price / 100) * 100 if price else 0)
    assert shares_for_target(equity, weight, price, fund) == (
        int(Decimal(equity) * Decimal(str(weight)) * 10 / price / 100) * 100 if price else 0)


def test_actual_weight_cli_preview_save_one_refuse_resend(tmp_path):
    stamp = now()
    snapshot, value = account(stamp), weights(stamp)
    account_file, weights_file = tmp_path / "synthetic-account.json", tmp_path / "synthetic-weights.json"
    account_file.write_text(json.dumps(snapshot), encoding="utf-8")
    weights_file.write_text(json.dumps(value), encoding="utf-8")
    home = tmp_path / "isolated-trading-home"
    env = {**os.environ, "QUANT_TRADING_HOME": str(home), "PYTHONUTF8": "1"}
    root = Path(__file__).resolve().parents[2]
    bootstrap = subprocess.run([sys.executable, "-Xutf8", "-c",
        "import json,sys; from trading.ths import journal; journal.initialize(); "
        "journal.observe(json.load(open(sys.argv[1], encoding='utf-8')))", str(account_file)],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert bootstrap.returncode == 0, bootstrap.stderr
    command = [sys.executable, "-Xutf8", str(root / "run_committee.py"), "ths", "plan-weights",
               "--account", "synthetic-only", "--weights", str(weights_file)]
    preview = subprocess.run([*command, "--out", str(tmp_path / "preview.json")],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert preview.returncode == 0, preview.stderr
    result = json.loads(preview.stdout)
    assert not result["saved"] and result["weight_sizing"]["equity_cents"] == 14000000
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0
    saved = subprocess.run([*command, "--save-next", "--out", str(tmp_path / "saved.json")],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert saved.returncode == 0, saved.stderr
    plan = json.loads(saved.stdout)["next_plan"]
    assert (plan["side"], plan["qty"]) == ("sell", 600)
    assert plan["weight_sizing"]["equity_cents"] == 14000000
    repeated = subprocess.run([*command, "--save-next"], cwd=root, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert repeated.returncode == 2 and "Unresolved plan exists" in repeated.stderr
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT payload FROM ths_observations").fetchone()[0]) == snapshot
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('runs','daily')").fetchall()
    (tmp_path / "consumer-receipt.json").write_text(json.dumps({
        "evidence_kind": "synthetic_cli_not_native_account", "equity_cents": 14000000,
        "target_shares": {item["symbol"]: item["target_qty"] for item in result["portfolio"]["targets"]},
        "preview_saved": False, "saved_plans": 1, "next_plan_side": "sell", "next_plan_qty": 600,
        "resend_exit_code": repeated.returncode, "native_client_accessed": False,
        "submitted": False, "account_unchanged": True,
    }, indent=2), encoding="utf-8")
