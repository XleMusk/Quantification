"""Synthetic bound-run consumers; no native client, account or order access."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from trading import settings, store
from trading.contracts import (RunContext, SecurityId, Signal, SignalFrame, SignalKind,
                               SignalTime, TradingMode)
from trading.ths import adapter, journal, signals
from trading.ths.broker import THSAdapter
from trading.ths.ledger_import import bind_account
from trading.portfolio_allocation import OBSERVED_LOT_ALLOCATION, OPEN_LOT_ALLOCATION


def seed_case(root, current, previous=None, start=None, config=None, lot_allocation=None, bind=True):
    day = current.astimezone(signals.MARKET_TZ).date()
    previous = previous or (day - timedelta(days=1)).isoformat()
    first = (datetime.fromisoformat(previous).date() - timedelta(days=1)).isoformat()
    following = (day + timedelta(days=1)).isoformat()
    dataset = root / "synthetic-calendar-only-snapshot"
    dataset.mkdir(parents=True)
    (dataset / "manifest.json").write_text(json.dumps({"status": "ready", "folder": str(dataset),
        "evidence_kind": "synthetic_not_real_exchange_calendar"}), encoding="utf-8")
    (dataset / "calendar.csv").write_text("date\n" + "\n".join([first, previous, day.isoformat(), following]) + "\n", encoding="utf-8")
    store.initialize()
    journal.initialize()
    run = store.create("synthetic signal consumer", {"initial_cash": 140000, **(config or {})}, start or previous,
                       following, "synthetic-signal", mode="paper", dataset=dataset)
    store.control(run["id"], "pause")
    alias = "synthetic-signal-only"
    journal.observe({"kind": "orders", "source": "ths_user_export", "account_alias": alias,
        "trading_date": day.isoformat(), "observed_at": (current - timedelta(seconds=2)).isoformat(), "rows": []})
    snapshot = {"kind": "account", "source": "ths_user_export", "account_alias": alias,
        "trading_date": day.isoformat(), "observed_at": (current - timedelta(seconds=1)).isoformat(),
        "cash_available": 9000000, "cash_total": 9000000, "cash_frozen": 0,
        "cash_basis": "cash_balance_includes_frozen",
        "positions": {"600000.SSE": {"quantity": 2000, "sellable": 2000},
                      "600010.SSE": {"quantity": 1000, "sellable": 1000}},
        "marks": {"600000.SSE": 2000, "600010.SSE": 1000}}
    journal.observe(snapshot)
    if bind:
        bind_account(run["id"], alias, confirm_simulated_account=True, lot_allocation=lot_allocation)
    context = RunContext(run["id"], TradingMode.PAPER)
    event_time, available_at = previous + "T15:00:00+08:00", previous + "T15:01:00+08:00"
    ranked = ["000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE", "000005.SZSE", "600000.SSE"]
    frame = SignalFrame(previous, tuple(Signal(SecurityId(s), 6 - i, SignalKind.SCORE,
        SignalTime(event_time, available_at)) for i, s in enumerate(ranked)), "synthetic-rule-v1", context)
    prices = []
    for symbol in ranked[:4] + ["600000.SSE", "600010.SSE"]:
        price = {"symbol": symbol, "price_basis": "raw_rmb", "limit_price_rmb": "20.00" if symbol == "600000.SSE" else "10.00"}
        if symbol not in snapshot["positions"]:
            price["quote"] = {"symbol": symbol, "price_basis": "raw_rmb", "price_rmb": "10.00",
                "observed_at": current.isoformat(), "trading_date": day.isoformat()}
        prices.append(price)
    request = {"mode": "current_preview", "account_alias": alias, "strategy_id": frame.source,
               "generated_at": current.isoformat(), "signal_frame": frame.to_dict(), "prices": prices}
    return run["id"], request, snapshot


@pytest.fixture
def case(tmp_path, monkeypatch):
    current = datetime.fromisoformat("2026-10-08T09:35:00+08:00")
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "synthetic.sqlite3")
    monkeypatch.setattr(adapter, "now", lambda: current)
    monkeypatch.setattr(signals, "now", lambda: current)
    return (*seed_case(tmp_path, current, "2026-09-30"), current)


def test_scores_use_same_rank_buffer_and_existing_full_weight_consumer(case):
    run_id, request, snapshot, current = case
    before = store.get(run_id, full=True)["state"]
    result = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_signal(run_id, request, current=current)
    targets = result["portfolio_intent"]["weights"]
    assert targets == {s: .16 for s in ["600000.SSE", "000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE"]}
    assert "000005.SZSE" not in targets  # Rank6 existing holding is retained by rank8 buffer.
    quantities = {item["symbol"]: item["target_qty"] for item in result["portfolio"]["targets"]}
    assert quantities["600010.SSE"] == 0 and quantities["600000.SSE"] == 1100
    assert quantities["000001.SZSE"] == 2200
    assert (result["next_plan"]["symbol"], result["next_plan"]["side"], result["next_plan"]["qty"]) == ("600000.SSE", "sell", 900)
    assert result["weight_sizing"]["equity_cents"] == 14000000
    assert result["strategy_signal"]["execution_day"] == "2026-10-08"
    assert result["next_plan"]["strategy_signal"]["signal_frame"] == request["signal_frame"]
    assert not result["saved"] and not result["client_interacted"] and not result["execution_admitted"]
    assert store.get(run_id, full=True)["state"] == before
    assert journal.latest(snapshot["account_alias"], "account") == snapshot


def test_current_request_builder_serializes_only_fresh_paper_scores(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    built = signals.build_current_request(frame, request["prices"], snapshot["account_alias"], current=current)
    assert built["mode"] == "current_preview"
    assert built["strategy_id"] == frame.source
    assert built["producer"] == "ths_current_signal_builder_v1"
    assert built["signal_frame"] == request["signal_frame"]
    assert built["prices"] == request["prices"]


def test_score_rows_enter_the_same_current_request_contract(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    rows = frame.ranked_candidates()
    for row, signal in zip(rows, frame.signals):
        row["available_at"] = signal.time.available_at
    built = signals.build_current_request_from_scores(rows, frame.day, frame.source, run_id,
                                                       request["prices"], snapshot["account_alias"], current=current)
    assert built["signal_frame"] == request["signal_frame"]
    assert built["producer"] == "ths_current_signal_builder_v1"


def test_versioned_current_score_bundle_preserves_source_receipt(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    rows = frame.ranked_candidates()
    for row, signal in zip(rows, frame.signals):
        row["available_at"] = signal.time.available_at
    bundle = {"schema": signals.CURRENT_SCORE_BUNDLE_SCHEMA, "day": frame.day,
              "strategy": frame.source, "run_id": run_id, "scores": rows,
              "prices": request["prices"], "account": snapshot["account_alias"],
              "generated_at": current.isoformat(),
              "source": {"source_id": "synthetic-eod-source-v1", "kind": "current_eod",
                          "status": "validated_current_eod", "execution_admitted": False,
                          "decision_day": frame.day, "observed_at": (current - timedelta(seconds=30)).isoformat()}}
    built = signals.build_current_request_from_bundle(bundle, current=current)
    assert built["source_receipt"] == bundle["source"]
    assert built["signal_frame"] == request["signal_frame"]


def test_current_request_rejects_generation_before_source_observation(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    source = {"source_id": "synthetic-eod-source-v1", "kind": "current_eod",
              "status": "validated_current_eod", "execution_admitted": False,
              "decision_day": frame.day, "observed_at": (current - timedelta(seconds=30)).isoformat()}
    with pytest.raises(ValueError, match="generated before"):
        signals.build_current_request(frame, request["prices"], snapshot["account_alias"],
                                      source_receipt=source,
                                      generated_at=(current - timedelta(minutes=1)).isoformat(),
                                      current=current)


@pytest.mark.parametrize("change", ["schema", "status", "kind", "admission", "day", "future", "before_decision"])
def test_current_score_bundle_rejects_unverified_source_receipts(case, change):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    rows = frame.ranked_candidates()
    for row, signal in zip(rows, frame.signals):
        row["available_at"] = signal.time.available_at
    source = {"source_id": "synthetic-eod-source-v1", "kind": "current_eod",
              "status": "validated_current_eod", "execution_admitted": False,
              "decision_day": frame.day, "observed_at": (current - timedelta(seconds=30)).isoformat()}
    bundle = {"schema": signals.CURRENT_SCORE_BUNDLE_SCHEMA, "day": frame.day,
              "strategy": frame.source, "run_id": run_id, "scores": rows,
              "prices": request["prices"], "account": snapshot["account_alias"],
              "generated_at": current.isoformat(), "source": source}
    if change == "schema":
        bundle["schema"] = "historical_score_export_v1"
    elif change == "status":
        source["status"] = "research_only"
    elif change == "kind":
        source["kind"] = "historical_research"
    elif change == "admission":
        source["execution_admitted"] = True
    elif change == "day":
        source["decision_day"] = "2026-09-29"
    elif change == "before_decision":
        source["observed_at"] = "2026-09-29T15:01:00+08:00"
    else:
        source["observed_at"] = (current + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="Current score|source|future|decision day"):
        signals.build_current_request_from_bundle(bundle, current=current)


def test_score_rows_without_availability_are_not_promoted(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    with pytest.raises(ValueError, match="available_at"):
        signals.build_current_request_from_scores(frame.ranked_candidates(), frame.day, frame.source, run_id,
                                                   request["prices"], snapshot["account_alias"], current=current)


def test_score_rows_are_ranked_before_current_request_is_built(case):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    rows = frame.ranked_candidates()
    for row, signal in zip(rows, frame.signals):
        row["available_at"] = signal.time.available_at
    rows[0]["score"], rows[1]["score"] = rows[1]["score"], rows[0]["score"]
    with pytest.raises(ValueError, match="ranked in descending order"):
        signals.build_current_request_from_scores(rows, frame.day, frame.source, run_id,
                                                   request["prices"], snapshot["account_alias"], current=current)


@pytest.mark.parametrize("change,match", [
    ("replay", "end-of-day paper"), ("research", "Historical research"),
    ("stale", "stale"), ("assumption", "Historical EOD"), ("today", "prior session"),
])
def test_current_request_builder_rejects_non_current_inputs(case, change, match):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    if change == "replay":
        frame = replace(frame, context=RunContext(run_id, TradingMode.REPLAY))
    elif change == "research":
        frame = replace(frame, source="research:old")
    elif change == "stale":
        with pytest.raises(ValueError, match=match):
            signals.build_current_request(frame, request["prices"], snapshot["account_alias"],
                                          generated_at=(current - timedelta(minutes=6)).isoformat(), current=current)
        return
    elif change == "assumption":
        frame = replace(frame, signals=tuple(replace(s, time=replace(s.time,
            availability_evidence="historical_eod_assumption")) for s in frame.signals))
    elif change == "today":
        day = current.astimezone(signals.MARKET_TZ).date().isoformat()
        frame = replace(frame, day=day, signals=tuple(replace(s, time=replace(
            s.time, event_time=day + "T15:00:00+08:00", available_at=day + "T15:01:00+08:00")) for s in frame.signals))
    with pytest.raises(ValueError, match=match):
        signals.build_current_request(frame, request["prices"], snapshot["account_alias"], current=current)


def test_off_cycle_is_no_change_not_empty_liquidation(case):
    run_id, request, snapshot, current = case
    with store.connection() as db:
        db.execute("UPDATE runs SET start='2026-09-29' WHERE id=?", (run_id,))
    request["signal_frame"]["signals"] = []
    del request["prices"]
    result = signals.from_signal(run_id, request, current=current)
    assert result["status"] == "no_change" and result["next_plan"] is None
    assert result["portfolio_intent"]["kind"] == "NO_CHANGE"
    assert result["strategy_signal"]["signal_session_number"] == 1
    assert not store.daily(run_id)


def test_observed_policy_bound_at_opening_uses_selector_order_and_saves_one(tmp_path, monkeypatch):
    current = datetime.fromisoformat("2026-10-08T09:35:00+08:00")
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "synthetic.sqlite3")
    monkeypatch.setattr(adapter, "now", lambda: current)
    run_id, request, snapshot = seed_case(tmp_path, current, lot_allocation=OBSERVED_LOT_ALLOCATION)
    before = store.get(run_id, full=True)["state"]
    assert before["broker_binding"]["lot_allocation"] == OBSERVED_LOT_ALLOCATION
    for p in request["prices"]:
        if "quote" in p:
            p["quote"]["price_rmb"] = p["limit_price_rmb"] = "80.00"
    result = signals.from_signal(run_id, request, current=current)
    targets = result["portfolio"]["targets"]
    assert [p["symbol"] for p in targets] == ["600000.SSE", "000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE", "600010.SSE"]
    assert [p["target_qty"] for p in targets] == [1100, 300, 300, 300, 200, 0]
    assert result["weight_sizing"]["allocation"]["allocated_stock_cny"] == 110000
    assert result["strategy_signal"]["lot_allocation"] == OBSERVED_LOT_ALLOCATION
    saved = journal.save_plan(result["next_plan"])
    assert saved["weight_sizing"]["lot_allocation"] == OBSERVED_LOT_ALLOCATION
    assert store.get(run_id, full=True)["state"] == before and not store.daily(run_id)
    with pytest.raises(ValueError, match="immutable broker binding"):
        bind_account(run_id, snapshot["account_alias"], confirm_simulated_account=True)
    request["lot_allocation"] = None
    with pytest.raises(ValueError, match="immutable broker binding"):
        signals.from_signal(run_id, request, current=current)


def test_old_binding_cannot_opt_into_observed_policy_or_replay_policy(case):
    run_id, request, snapshot, current = case
    for policy in (OBSERVED_LOT_ALLOCATION, OPEN_LOT_ALLOCATION):
        request["lot_allocation"] = policy
        with pytest.raises(ValueError, match="immutable broker binding"):
            signals.from_signal(run_id, request, current=current)
    with pytest.raises(ValueError, match="not a replay policy"):
        bind_account(run_id, snapshot["account_alias"], confirm_simulated_account=True, lot_allocation=OPEN_LOT_ALLOCATION)


@pytest.mark.parametrize("field", ["strategy_signal", "weight_portfolio", "weight_sizing"])
def test_plan_save_cannot_relabel_the_bound_allocation(case, field):
    run_id, request, snapshot, current = case
    plan = signals.from_signal(run_id, request, current=current)["next_plan"]
    plan[field]["lot_allocation"] = OBSERVED_LOT_ALLOCATION
    with pytest.raises(ValueError, match="lot allocation changed since preview"):
        journal.save_plan(plan)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0


def test_explicit_empty_frame_on_due_day_is_full_cash_target(case):
    run_id, request, snapshot, current = case
    request["signal_frame"]["signals"] = []
    request["prices"] = [p for p in request["prices"] if p["symbol"] in snapshot["positions"]]
    result = signals.from_signal(run_id, request, current=current)
    assert result["portfolio_intent"]["kind"] == "FULL_TARGET" and result["portfolio_intent"]["weights"] == {}
    assert result["next_plan"]["qty"] == 2000 and result["next_plan"]["side"] == "sell"
    del request["signal_frame"]
    with pytest.raises(KeyError):
        signals.from_signal(run_id, request, current=current)


def test_consistent_t1_lock_blocks_next_sale_without_skipping_to_buys(case):
    run_id, request, snapshot, current = case
    value = store.get(run_id, full=True)["state"]
    value["positions"]["600000.SSE"] = [{"qty": 100, "available": "2026-10-08"},
                                            {"qty": 1900, "available": "9999-12-31"}]
    with store.connection() as db:
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(value), run_id))
    snapshot["positions"]["600000.SSE"]["sellable"] = 100
    snapshot["observed_at"] = current.isoformat()
    journal.observe(snapshot)
    result = signals.from_signal(run_id, request, current=current)
    assert result["status"] == "blocked" and result["next_plan"] is None
    assert result["reason"] == "insufficient_sellable_T1"
    assert result["adjustments"][0]["symbol"] == "600000.SSE"
    assert all(item["status"] == "waiting_for_sales" for item in result["adjustments"] if item["side"] == "buy")
    assert store.get(run_id, full=True)["state"] == value


@pytest.mark.parametrize("change,match", [
    ("replay", "context"), ("wrong_run", "context"), ("historical_assumption", "availability"),
    ("research_source", "historical"), ("wrong_alias", "context"),
    ("old_signal", "preceding"), ("same_day", "preceding"),
    ("unsorted", "descending"), ("expected_return", "explicit portfolio policy"),
    ("duplicate_prices", "exactly cover"), ("missing_exit_price", "exactly cover"),
])
def test_bad_signal_cannot_be_promoted_to_current_plan(case, change, match):
    run_id, request, snapshot, current = case
    frame = SignalFrame.from_dict(request["signal_frame"])
    if change == "replay":
        frame = replace(frame, context=RunContext(run_id, TradingMode.REPLAY))
    elif change == "wrong_run":
        frame = replace(frame, context=RunContext("other", TradingMode.PAPER))
    elif change == "historical_assumption":
        frame = replace(frame, signals=tuple(replace(s, time=replace(s.time, availability_evidence="historical_eod_assumption")) for s in frame.signals))
    elif change == "research_source":
        frame = replace(frame, source="research:old")
        request["strategy_id"] = frame.source
    elif change == "wrong_alias":
        request["account_alias"] = "other"
    elif change in {"old_signal", "same_day"}:
        day = "2026-09-29" if change == "old_signal" else "2026-10-08"
        frame = replace(frame, day=day, signals=tuple(replace(s, time=SignalTime(day + "T15:00:00+08:00", day + "T15:01:00+08:00")) for s in frame.signals))
    elif change == "unsorted":
        frame = replace(frame, signals=tuple(reversed(frame.signals)))
    elif change == "expected_return":
        frame = replace(frame, signals=tuple(replace(s, kind=SignalKind.EXPECTED_RETURN, horizon_sessions=5) for s in frame.signals))
    elif change == "duplicate_prices":
        request["prices"].append(deepcopy(request["prices"][0]))
    elif change == "missing_exit_price":
        request["prices"] = [p for p in request["prices"] if p["symbol"] != "600010.SSE"]
    request["signal_frame"] = frame.to_dict()
    with pytest.raises((ValueError, PermissionError), match=match):
        signals.from_signal(run_id, request, current=current)
    assert not store.daily(run_id)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["cash", "position", "sellable", "session", "unbound", "running", "calendar"])
def test_account_or_session_divergence_requires_reconciliation(case, change):
    run_id, request, snapshot, current = case
    if change in {"cash", "position", "sellable"}:
        if change == "cash":
            snapshot["cash_total"] += 1
            snapshot["cash_available"] += 1
        elif change == "position":
            snapshot["positions"]["600000.SSE"]["quantity"] += 100
        else:
            snapshot["positions"]["600000.SSE"]["sellable"] -= 100
        snapshot["observed_at"] = current.isoformat()
        journal.observe(snapshot)
    elif change == "calendar":
        with store.connection() as db:
            path = Path(db.execute("SELECT dataset FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        with (path / "calendar.csv").open("a") as stream:
            stream.write("2026-10-12\n")
    else:
        value = store.get(run_id, full=True)["state"]
        if change == "unbound":
            del value["broker_binding"]
        elif change == "session":
            value["day"] = "2026-09-30"
        with store.connection() as db:
            db.execute("UPDATE runs SET state=?,status=?,error=? WHERE id=?", (json.dumps(value),
                "running" if change == "running" else "paused", None, run_id))
    with pytest.raises((ValueError, PermissionError)):
        signals.from_signal(run_id, request, current=current)


@pytest.mark.parametrize("change", ["observation", "selector"])
def test_preview_cannot_save_after_account_or_selector_changes(case, change):
    run_id, request, snapshot, current = case
    plan = signals.from_signal(run_id, request, current=current)["next_plan"]
    if change == "observation":
        snapshot["observed_at"] = current.isoformat()
        journal.observe(snapshot)
    else:
        config = store.get(run_id)["config"]
        config["holdings"] = 4
        with store.connection() as db:
            db.execute("UPDATE runs SET config=? WHERE id=?", (json.dumps(config), run_id))
    with pytest.raises(ValueError, match="changed since"):
        journal.save_plan(plan)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["execution_only", "execution_and_selector", "legacy_nondefault"])
def test_signal_plan_must_keep_the_bound_execution_config(case, change):
    run_id, request, snapshot, current = case
    plan = signals.from_signal(run_id, request, current=current)["next_plan"]
    if change == "legacy_nondefault":
        plan.pop("execution_config")
    else:
        plan["execution_config"]["minimum_commission"] = 50
    if change != "execution_only":
        plan["strategy_signal"]["selector_config"]["minimum_commission"] = 50
    with pytest.raises(ValueError, match="execution config|changed since"):
        journal.save_plan(plan)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0


@pytest.mark.parametrize("custom", [False, True])
def test_actual_signal_cli_uses_one_existing_bound_ledger_and_saves_only_next(tmp_path, custom):
    home = tmp_path / "isolated-trading-home"
    env = {**os.environ, "QUANT_TRADING_HOME": str(home), "PYTHONUTF8": "1"}
    root = Path(__file__).resolve().parents[2]
    bootstrap = subprocess.run([sys.executable, "-Xutf8", "-c",
        "from pathlib import Path; import json,sys; from trading.tests.test_ths_signals import seed_case; "
        "from trading.ths.adapter import now; from trading import store; "
        "p=Path(sys.argv[1]); custom=sys.argv[2]=='1'; "
        "config={'holdings':4,'gross_limit':1.,'single_limit':.25,'minimum_commission':50} if custom else None; "
        "rid,request,account=seed_case(p,now(),config=config); "
        "request['prices']=[v for v in request['prices'] if not custom or v['symbol']!='000004.SZSE']; "
        "(p/'request.json').write_text(json.dumps(request),encoding='utf-8'); "
        "(p/'opening.json').write_text(json.dumps(store.get(rid,full=True)['state']),encoding='utf-8'); print(rid)",
        str(tmp_path), str(int(custom))], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert bootstrap.returncode == 0, bootstrap.stderr
    run_id = bootstrap.stdout.strip()
    command = [sys.executable, "-Xutf8", str(root / "run_committee.py"), "ths", "plan-signal",
               "--run-id", run_id, "--signal", str(tmp_path / "request.json")]
    preview = subprocess.run([*command, "--out", str(tmp_path / "preview.json")], cwd=root,
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert preview.returncode == 0, preview.stderr
    result = json.loads(preview.stdout)
    expected_qty = 300 if custom else 900
    assert result["next_plan"]["qty"] == expected_qty and not result["saved"]
    config = result["strategy_signal"]["selector_config"]
    assert result["execution_config"] == result["next_plan"]["execution_config"] == config
    assert result["weight_sizing"]["gross_limit"] == config["gross_limit"]
    assert result["weight_sizing"]["single_limit"] == config["single_limit"]
    assert result["portfolio_intent"]["weights"]["000001.SZSE"] == (.25 if custom else .16)
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 0
    saved = subprocess.run([*command, "--save-next", "--out", str(tmp_path / "saved.json")], cwd=root,
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert saved.returncode == 0, saved.stderr
    repeated = subprocess.run([*command, "--save-next"], cwd=root, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert repeated.returncode == 2 and "Unresolved plan exists" in repeated.stderr
    with sqlite3.connect(home / "trading.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM daily").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM ths_plans").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT config FROM runs").fetchone()[0]) == config
        assert json.loads(db.execute("SELECT state FROM runs").fetchone()[0]) == json.loads((tmp_path / "opening.json").read_text())
    (tmp_path / "consumer-receipt.json").write_text(json.dumps({
        "evidence_kind": "synthetic_bound_run_cli_not_native_account", "equity_cents": 14000000,
        "rank_buffer_retained": "600000.SSE", "selected_holdings": 4 if custom else 5,
        "execution_config": config, "bound_execution_config_preserved": True,
        "explicit_zero_exit": "600010.SSE", "next_plan_side": "sell", "next_plan_qty": expected_qty,
        "preview_saved": False, "saved_plans": 1, "resend_exit_code": repeated.returncode,
        "sole_ledger_unchanged": True, "daily_rows": 0, "native_client_accessed": False,
        "submitted": False, "execution_admitted": False,
    }, indent=2), encoding="utf-8")
