"""Complete-portfolio consumers with synthetic accounts, never desktop access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from trading.ths.adapter import check_plan, from_portfolio, now
from trading.ths.broker import THSAdapter
from trading.ths.handoff import prepare_handoff


@pytest.fixture
def current():
    return datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def snapshot(current):
    return {"source": "ths_user_export", "account_alias": "synthetic-only", "kind": "account",
            "observed_at": current.isoformat(), "trading_date": "2026-10-02", "cash_available": 0,
            "positions": {"600000.SSE": {"quantity": 200, "sellable": 200}}}


def target(symbol, qty, price="10.00"):
    return {"symbol": symbol, "target_qty": qty, "limit_price_rmb": price, "price_basis": "raw_rmb"}


def portfolio(current, targets=None):
    return {"mode": "current_preview", "strategy_id": "synthetic-not-admitted",
            "account_alias": "synthetic-only", "generated_at": current.isoformat(),
            "targets": targets if targets is not None else [target("000001.SZSE", 100), target("600000.SSE", 0)]}


def test_complete_portfolio_sells_first_without_crediting_proceeds(snapshot, current):
    original = deepcopy(snapshot)
    value = portfolio(current)
    result = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_portfolio(snapshot, value, current=current)
    assert snapshot == original
    assert result["status"] == "awaiting_review" and result["saved"] is False
    assert result["cash_available_cents"] == 0
    assert result["client_interacted"] is False and result["execution_admitted"] is False
    plan = result["next_plan"]
    assert (plan["symbol"], plan["side"], plan["qty"]) == ("600000.SSE", "sell", 200)
    assert result["adjustments"][1]["status"] == "waiting_for_sales"
    assert result["adjustments"][1]["validation_reason"] == "insufficient_cash"
    check_plan(plan, current=current)
    value["targets"][1]["target_qty"] = 100
    assert plan["portfolio"]["targets"][1]["target_qty"] == 0


def test_remaining_shares_recomputed_from_fresh_observation(snapshot, current):
    value = portfolio(current)
    later = current + timedelta(seconds=2)
    snapshot.update(observed_at=later.isoformat(), cash_available=99900)
    snapshot["positions"]["600000.SSE"].update(quantity=100, sellable=100)
    result = from_portfolio(snapshot, value, current=later)
    assert result["next_plan"]["side"] == "sell" and result["next_plan"]["qty"] == 100
    assert result["adjustments"][1]["status"] == "waiting_for_sales"
    snapshot["positions"] = {}
    snapshot.update(observed_at=(later + timedelta(seconds=2)).isoformat(), cash_available=199800)
    result = from_portfolio(snapshot, value, current=later + timedelta(seconds=2))
    assert (result["next_plan"]["symbol"], result["next_plan"]["side"], result["next_plan"]["qty"]) == (
        "000001.SZSE", "buy", 100)


def test_t1_blocks_selling_and_does_not_release_buys(snapshot, current):
    snapshot["positions"]["600000.SSE"]["sellable"] = 100
    snapshot["cash_available"] = 1000000
    result = from_portfolio(snapshot, portfolio(current), current=current)
    assert result["status"] == "blocked" and result["next_plan"] is None
    assert result["reason"] == "insufficient_sellable_T1"
    assert result["adjustments"][1]["status"] == "waiting_for_sales"


def test_buy_order_is_deterministic_and_single_plan(snapshot, current):
    snapshot.update(positions={}, cash_available=100600)
    value = portfolio(current, [target("600000.SSE", 100), target("000001.SZSE", 100)])
    result = from_portfolio(snapshot, value, current=current)
    assert result["next_plan"]["symbol"] == "000001.SZSE"
    assert result["adjustments"][1]["status"] == "waiting_for_prior_step"
    assert "next_plan" not in result["adjustments"][1]
    snapshot["cash_available"] = 100000
    result = from_portfolio(snapshot, value, current=current)
    assert result["status"] == "blocked" and result["reason"] == "insufficient_cash"


def test_unchanged_unsupported_holdings_not_liquidated(snapshot, current):
    snapshot["positions"]["510300.SSE"] = {"quantity": 100, "sellable": 100}
    value = portfolio(current, [{"symbol": "510300.SSE", "target_qty": 100}, target("600000.SSE", 200)])
    result = from_portfolio(snapshot, value, current=current)
    assert result["status"] == "no_change" and result["next_plan"] is None
    value["targets"][0] = target("510300.SSE", 0)
    with pytest.raises(ValueError, match="supported exchange-qualified"):
        from_portfolio(snapshot, value, current=current)


def test_handoff_does_not_require_quote_for_unchanged_holding(snapshot, current):
    handoff = {
        "schema": "ths_manual_prefill_handoff_v1",
        "execution_admitted": False,
        "submission": "manual_only",
        "strategy_id": "synthetic-not-admitted",
        "target_template": [{"symbol": "600000.SSE", "target_qty": 200}],
    }
    quotes = {
        "schema": "ths_raw_quote_observation_v1",
        "source": "synthetic-only",
        "observed_at": current.isoformat(),
        "trading_date": snapshot["trading_date"],
        "price_basis": "raw_rmb",
        "prices": {},
    }
    result = prepare_handoff(handoff, snapshot, quotes, current=current)
    assert result["status"] == "no_change"
    assert result["next_plan"] is None
    assert result["source_handoff"]["target_template"] == [{"symbol": "600000.SSE", "target_qty": 200}]


def test_handoff_observed_account_sizing_reuses_engine_rank_retention(current):
    snapshot = {
        "source": "ths_user_export", "account_alias": "synthetic-only", "kind": "account",
        "observed_at": current.isoformat(), "trading_date": "2026-10-02",
        "cash_available": 0, "cash_total": 12000000, "cash_frozen": 12000000,
        "cash_basis": "cash_balance_includes_frozen",
        "positions": {"600000.SSE": {"quantity": 200, "sellable": 200}},
        "marks": {"600000.SSE": 1000},
    }
    handoff = {
        "schema": "ths_manual_prefill_handoff_v1", "execution_admitted": False,
        "submission": "manual_only", "strategy_id": "synthetic-not-admitted",
        "target_template": [{"symbol": "000001.SZSE", "target_qty": 2800},
                             {"symbol": "600000.SSE", "target_qty": 0}],
        "rank_template": [{"symbol": "000001.SZSE", "rank": 1, "score": 2.0},
                           {"symbol": "600000.SSE", "rank": 2, "score": 1.0}],
    }
    quotes = {"schema": "ths_raw_quote_observation_v1", "source": "synthetic-only",
              "observed_at": current.isoformat(), "trading_date": "2026-10-02",
              "price_basis": "raw_rmb", "prices": {
                  "000001.SZSE": {"price_rmb": "10.00"}}}
    config = {"holdings": 1, "retain_rank": 1, "gross_limit": .2, "single_limit": .2}
    result = prepare_handoff(handoff, snapshot, quotes, current=current,
                             execution_config=config, sizing_mode="observed_account")
    assert result["sizing_mode"] == "observed_account"
    assert result["selected_symbols"] == ["000001.SZSE"]
    assert result["next_plan"]["weight_portfolio"]["targets"] == [
        {"symbol": "000001.SZSE", "target_weight": .2, "price_basis": "raw_rmb",
         "limit_price_rmb": "10.00",
         "quote": {"symbol": "000001.SZSE", "price_rmb": "10.00", "price_basis": "raw_rmb",
                    "observed_at": current.isoformat(), "trading_date": "2026-10-02"}},
        {"symbol": "600000.SSE", "target_weight": 0.0, "price_basis": "raw_rmb",
         "limit_price_rmb": "10.00"},
    ]
    assert result["weight_sizing"]["targets"][0]["target_qty"] == 2400


def test_handoff_mainboard_scope_is_explicit_and_reports_exclusions(current):
    snapshot = {
        "source": "ths_user_export", "account_alias": "synthetic-only", "kind": "account",
        "observed_at": current.isoformat(), "trading_date": "2026-10-02",
        "cash_available": 10000000, "cash_total": 10000000, "cash_frozen": 0,
        "cash_basis": "cash_balance_includes_frozen", "positions": {}, "marks": {},
    }
    handoff = {
        "schema": "ths_manual_prefill_handoff_v1", "execution_admitted": False,
        "submission": "manual_only", "strategy_id": "synthetic-not-admitted",
        "target_template": [],
        "rank_template": [{"symbol": "300001.SZSE", "rank": 1, "score": 2.0},
                           {"symbol": "000001.SZSE", "rank": 2, "score": 1.0}],
    }
    quotes = {"schema": "ths_raw_quote_observation_v1", "source": "synthetic-only",
              "observed_at": current.isoformat(), "trading_date": "2026-10-02",
              "price_basis": "raw_rmb", "prices": {
                  "000001.SZSE": {"price_rmb": "10.00"}}}
    result = prepare_handoff(
        handoff, snapshot, quotes, current=current,
        execution_config={"holdings": 1, "retain_rank": 1, "gross_limit": .2, "single_limit": .2},
        sizing_mode="observed_account", execution_scope="mainboard_only",
    )
    assert result["execution_scope"] == "mainboard_only"
    assert result["scope_excluded_symbols"] == ["300001.SZSE"]
    assert result["selected_symbols"] == ["000001.SZSE"]


@pytest.mark.parametrize("change,match", [
    ({"mode": "replay"}, "current-preview"),
    ({"account_alias": "other"}, "account alias"),
    ({"generated_at": "2020-01-01T00:00:00+00:00"}, "stale"),
    ({"targets": []}, "all held securities"),
    ({"targets": [target("600000.SSE", 0), target("600000.SSE", 0)]}, "Duplicate"),
    ({"targets": [target("600000", 0)]}, "exchange-qualified"),
    ({"targets": [target("600000.SSE", True)]}, "absolute share"),
    ({"targets": [target("600000.SSE", -1)]}, "absolute share"),
    ({"targets": [target("600000.SSE", 0, "=10")]}, "plain RMB"),
    ({"targets": [{**target("600000.SSE", 0), "price_basis": "qfq"}]}, "research prices"),
])
def test_unsafe_portfolio_not_downgraded_to_partial_plan(snapshot, current, change, match):
    with pytest.raises(ValueError, match=match):
        from_portfolio(snapshot, {**portfolio(current), **change}, current=current)


def test_invalid_nonselected_target_still_stops_whole_preview(snapshot, current):
    value = portfolio(current)
    value["targets"][0]["limit_price_rmb"] = "0"
    with pytest.raises(ValueError, match="Invalid order"):
        from_portfolio(snapshot, value, current=current)


@pytest.mark.parametrize("change", [
    {"source": "paper"}, {"trading_date": "2020-01-01"},
    {"observed_at": "2020-01-01T00:00:00+00:00"},
])
def test_fresh_portfolio_cannot_promote_old_or_paper_account(snapshot, current, change):
    with pytest.raises(ValueError):
        from_portfolio({**snapshot, **change}, portfolio(current), current=current)


def test_actual_cli_preview_then_save_only_one_and_refuse_resend(tmp_path):
    stamp = now()
    value = {"source": "ths_user_export", "account_alias": "synthetic-only", "kind": "account",
             "observed_at": stamp.isoformat(),
             "trading_date": stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
             "cash_available": 0, "positions": {"600000.SSE": {"quantity": 200, "sellable": 200}}}
    account_file, portfolio_file = tmp_path / "synthetic-account.json", tmp_path / "synthetic-portfolio.json"
    account_file.write_text(json.dumps(value), encoding="utf-8")
    portfolio_file.write_text(json.dumps(portfolio(stamp)), encoding="utf-8")
    home = tmp_path / "isolated-trading-home"
    environment = {**os.environ, "QUANT_TRADING_HOME": str(home), "PYTHONUTF8": "1"}
    root = Path(__file__).resolve().parents[2]
    bootstrap = subprocess.run([sys.executable, "-Xutf8", "-c",
                               "import json,sys; from trading.ths import journal; journal.initialize(); "
                               "journal.observe(json.load(open(sys.argv[1], encoding='utf-8')))", str(account_file)],
                              cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert bootstrap.returncode == 0, bootstrap.stderr
    command = [sys.executable, "-Xutf8", str(root / "run_committee.py"), "ths", "plan-portfolio",
               "--account", "synthetic-only", "--portfolio", str(portfolio_file)]
    preview = subprocess.run([*command, "--out", str(tmp_path / "preview.json")], cwd=root, env=environment,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["saved"] is False
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0
    saved = subprocess.run([*command, "--save-next", "--out", str(tmp_path / "saved.json")], cwd=root, env=environment,
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert saved.returncode == 0, saved.stderr
    receipt = json.loads(saved.stdout)
    assert receipt["saved"] is True and receipt["next_plan"]["side"] == "sell"
    repeated = subprocess.run([*command, "--save-next"], cwd=root, env=environment, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
    assert repeated.returncode == 2 and "Unresolved plan exists" in repeated.stderr
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT payload FROM ths_observations").fetchone()[0]) == value
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('runs','daily')").fetchall()
    (tmp_path / "consumer-receipt.json").write_text(json.dumps({
        "evidence_kind": "synthetic_cli_not_native_account", "preview_saved": False,
        "saved_plans": 1, "resend_exit_code": repeated.returncode, "resend_reason": repeated.stderr.strip(),
        "native_client_accessed": False, "submitted": False, "account_unchanged": True,
    }, indent=2), encoding="utf-8")
