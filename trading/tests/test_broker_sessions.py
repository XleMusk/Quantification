"""Real consumers with isolated synthetic exports; no native client or account."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from trading import store
from trading.ths import adapter, cli, journal, observations
from trading.tests.test_broker_ledger_import import (DAY, STAMP, SYMBOL, persisted, export_mapping,
    holding_row, manual_plan, receipt_views)

NEXT = "2024-01-03"
NEXT_STAMP = "2024-01-03T10:00:00+08:00"


def observed_next(kind, rows, *, second=3, cash=None, day=NEXT, stamp=NEXT_STAMP):
    mapping = export_mapping(kind) if kind != "movements" else {
        "columns": {k: k for k in ("movement_id", "kind", "cash_change")},
        "basis": "nontrade_cash_movements", "currency": "CNY", "amount_unit": "rmb",
        "kinds": {k: k for k in ("cash_dividend", "interest", "tax", "fee", "deposit", "withdrawal")}}
    kwargs = {"cash": cash, "cash_total": cash, "cash_frozen": "0.00"} if kind == "account" else {}
    return observations.observation(kind, rows, mapping, account_alias="test-only", source="ths_user_export",
        observed_at=(datetime.fromisoformat(stamp) + timedelta(seconds=second)).isoformat(), trading_date=day, **kwargs)


@pytest.fixture
def next_session(persisted, monkeypatch):
    from trading.ths.ledger_import import bind_account, import_plan
    run_id, dataset = persisted
    (dataset / "calendar.csv").write_text("date\n2024-01-02\n2024-01-03\n2024-01-04\n2024-01-05\n", encoding="utf-8")
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")
    import_plan(run_id, plan["id"])
    current = datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=30)
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: current)
    for kind in ("orders", "trades", "cashflows"):
        value = journal.latest("test-only", kind, DAY)
        value["observed_at"] = NEXT_STAMP
        journal.observe(value)
    journal.observe(observed_next("orders", [], second=1))
    journal.observe(observed_next("trades", [], second=2))
    journal.observe(observed_next("movements", [], second=3))
    journal.observe(observed_next("account", [holding_row(300, 200)], second=4, cash="8992.77"))
    return run_id, plan, dataset


def test_next_session_settlement_same_account_cli_restart_and_old_receipt(next_session, capsys):
    from trading.ths.ledger_import import reconcile_session, import_plan
    from trading.report import detail
    run_id, plan, _ = next_session
    assert cli.main(["reconcile-session", "--run-id", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    state = store.get(run_id, full=True)["state"]
    assert state["day"] == NEXT and state["cash"] == 899277
    assert detail(run_id)["holdings"][0]["sellable"] == 200
    assert state["positions"][SYMBOL][1]["available"] == "9999-12-31"
    assert len(state["fills"]) == 2 and store.daily(run_id) == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile_session(run_id), range(2)))
    assert all(not r["changed"] for r in results)
    assert not import_plan(run_id, plan["id"])["changed"]
    assert journal.refresh(plan["id"])["result"]["account_cash_reconciled"]
    assert store.get(run_id, full=True)["state"] == state


def test_nontrade_cash_requires_actual_rows_and_is_not_invented_pnl(next_session):
    from trading.ths.ledger_import import reconcile_session
    from trading.report import detail
    run_id, _, _ = next_session
    journal.observe(observed_next("movements", [{"movement_id": "000001", "kind": "cash_dividend", "cash_change": "12.34"},
        {"movement_id": "000002", "kind": "tax", "cash_change": "-0.01"}], second=3))
    journal.observe(observed_next("account", [holding_row(300, 200)], second=5, cash="9005.10"))
    assert reconcile_session(run_id)["cash_total_cents"] == 900510
    state = store.get(run_id, full=True)["state"]
    assert len(state["broker_cash_movements"]) == 2
    assert not reconcile_session(run_id)["changed"]
    assert detail(run_id)["comparison"]["metrics"] is None
    assert detail(run_id)["accounted_fees"] == 7.24
    assert store.daily(run_id) == [] and store.get(run_id)["cursor"] == 0
    import os
    evidence = os.environ.get("QUANT_SESSION_TEST_EVIDENCE")
    if evidence:
        from pathlib import Path
        from trading.api import overview
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ui_fixture.json").write_text(json.dumps({"scope": "isolated_synthetic_multisession_export_not_native",
            "overview": overview(), "detail": detail(run_id)}, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("bad", ["cash", "shares", "unknown_release", "frozen", "calendar", "missing_prior_fill", "revised_fee", "active", "new_trade", "unknown_prior_execution", "late_current_view", "unimported_plan", "stale"])
def test_session_conflict_does_not_rewrite_account(next_session, bad):
    from trading.ths.ledger_import import reconcile_session
    run_id, plan, dataset = next_session
    account = journal.latest("test-only", "account")
    if bad in {"cash", "shares", "unknown_release", "frozen", "stale"}:
        if bad == "cash":
            account["cash_available"] += 1
            account["cash_total"] += 1
        elif bad == "shares":
            account["positions"][SYMBOL]["quantity"] += 100
        elif bad == "unknown_release":
            account["positions"][SYMBOL]["sellable"] = 300
        elif bad == "frozen":
            account["cash_frozen"] = 1
            account["cash_available"] -= 1
        else:
            account["observed_at"] = "2024-01-03T09:00:00+08:00"
        account["observed_at"] = account["observed_at"] if bad == "stale" else "2024-01-03T10:00:05+08:00"
        journal.observe(account)
        if bad == "stale":
            with store.connection() as db:
                db.execute("DELETE FROM ths_observations WHERE kind='account' AND observed_at=?", ("2024-01-03T10:00:04+08:00",))
    elif bad == "calendar":
        (dataset / "calendar.csv").write_text("date\n2024-01-02\n2024-01-03\n2024-01-05\n", encoding="utf-8")
    elif bad in {"missing_prior_fill", "revised_fee"}:
        kind = "trades" if bad == "missing_prior_fill" else "cashflows"
        value = journal.latest("test-only", kind, DAY)
        if kind == "trades":
            value["rows"] = []
        else:
            value["rows"][0]["fees_cents"] += 1
            value["rows"][0]["cash_change_cents"] -= 1
        journal.observe(value)
    elif bad == "late_current_view":
        journal.observe(observed_next("orders", [], second=5))
    elif bad == "unknown_prior_execution":
        view = journal.latest("test-only", "trades", DAY)
        view["rows"].append({**view["rows"][0], "order_id": "unowned", "execution_id": "unowned-fill"})
        journal.observe(view)
    elif bad in {"active", "new_trade"}:
        row = {"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "unowned", "qty": "100", "price": "10.00"}
        if bad == "active":
            row.update(filled_qty="0", status="accepted")
        else:
            row.update(execution_id="new-fill")
        journal.observe(observed_next("orders" if bad == "active" else "trades", [row]))
    else:
        with store.connection() as db:
            extra = {**plan, "id": "unimported", "created_at": "2024-01-02T10:01:00+08:00"}
            db.execute("INSERT INTO ths_plans(id,account_alias,status,payload,contract_id,trading_date,updated_at) VALUES(?,?,?,?,?,?,?)",
                (extra["id"], "test-only", "submission_unknown", json.dumps(extra), "000099", DAY, STAMP))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises((ValueError, PermissionError)):
        reconcile_session(run_id)
    assert store.get(run_id, full=True)["state"] == before


def test_next_day_sell_reuses_scoped_broker_ids_and_sole_cash(next_session):
    from trading.ths.ledger_import import reconcile_session, import_plan
    run_id, first, _ = next_session
    reconcile_session(run_id)
    account = journal.latest("test-only", "account")
    plan = adapter.create_plan(account, SYMBOL, "sell", "10.00", 100, price_basis="raw_rmb",
        current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=6))
    journal.save_plan(plan)
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET status='awaiting_manual_submission' WHERE id=?", (plan["id"],))
    journal.bind(plan["id"], "000002", NEXT)
    common = {"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002", "qty": "100", "price": "10.00"}
    journal.observe(observed_next("orders", [{**common, "filled_qty": "100", "status": "filled"}], second=7))
    journal.observe(observed_next("trades", [{**common, "execution_id": "000003"}], second=8))
    journal.observe(observed_next("cashflows", [{"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002",
        "gross_amount": "1000.00", "fees": "7.23", "cash_change": "992.77"}], second=9))
    journal.observe(observed_next("account", [holding_row(200, 100)], second=10, cash="9985.54"))
    assert import_plan(run_id, plan["id"])["cash_total_cents"] == 998554
    state = store.get(run_id, full=True)["state"]
    assert "ths:2024-01-03:000002:000003" in state["fills"] and len(state["fills"]) == 3
    assert not import_plan(run_id, first["id"])["changed"]
    assert store.get(run_id, full=True)["state"] == state


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_partial_reservations_release_only_after_observed_terminal(persisted, monkeypatch, side):
    from trading.ths.ledger_import import bind_account, import_plan, reconcile_session
    run_id, dataset = persisted
    (dataset / "calendar.csv").write_text("date\n2024-01-02\n2024-01-03\n2024-01-04\n", encoding="utf-8")
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan(side=side)
    receipt_views(plan)
    import_plan(run_id, plan["id"])
    current = datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=30)
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: current)
    for kind in ("orders", "trades", "cashflows"):
        view = journal.latest("test-only", kind, DAY)
        view["observed_at"] = NEXT_STAMP
        journal.observe(view)
    journal.observe(observed_next("orders", [], second=1))
    journal.observe(observed_next("trades", [], second=2))
    journal.observe(observed_next("movements", [], second=3))
    total, sellable, cash = (250, 150, "9492.77") if side == "buy" else (150, 50, "10492.77")
    journal.observe(observed_next("account", [holding_row(total, sellable)], second=4, cash=cash))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises(ValueError, match="unresolved"):
        reconcile_session(run_id)
    assert store.get(run_id, full=True)["state"] == before
    view = journal.latest("test-only", "orders", DAY)
    view["rows"][0]["status"] = "cancelled"
    view["rows"][0]["broker_status"] = "cancelled"
    journal.observe(view)
    assert reconcile_session(run_id)["changed"]
    state = store.get(run_id, full=True)["state"]
    assert state["orders"][plan["id"]]["status"] == "cancelled"
    assert state["broker_cash_frozen"] == 0
    assert state["cash"] == before["cash"] and len(state["fills"]) == 1
    assert state["broker_order_transitions"][plan["id"]]["day"] == NEXT
    assert not reconcile_session(run_id)["changed"]
    assert not import_plan(run_id, plan["id"])["changed"]
    assert store.get(run_id, full=True)["state"] == state


def test_third_session_and_historical_reimport_do_not_rebase_cash(next_session, monkeypatch):
    from trading.ths.ledger_import import reconcile_session
    run_id, _, _ = next_session
    reconcile_session(run_id)
    original_second_stamp = journal.latest("test-only", "account")["observed_at"]
    third = "2024-01-04"
    stamp = "2024-01-04T10:00:00+08:00"
    current = datetime.fromisoformat(stamp) + timedelta(seconds=30)
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: current)
    for kind in ("orders", "trades"):
        view = journal.latest("test-only", kind, NEXT)
        view["observed_at"] = stamp
        journal.observe(view)
    for second, kind in enumerate(("orders", "trades", "movements"), 1):
        journal.observe(observed_next(kind, [], second=second, day=third, stamp=stamp))
    journal.observe(observed_next("account", [holding_row(300, 200)], second=4, cash="8992.77", day=third, stamp=stamp))
    assert reconcile_session(run_id)["changed"]
    state = store.get(run_id, full=True)["state"]
    assert state["day"] == third and state["cash"] == 899277
    assert not reconcile_session(run_id, observed_at=original_second_stamp)["changed"]
    assert store.get(run_id, full=True)["state"] == state


def test_unverified_next_session_prevents_plan_before_any_prefill(next_session):
    from trading.ths.ledger_import import reconcile_session
    run_id, _, _ = next_session
    account = journal.latest("test-only", "account")
    plan = adapter.create_plan(account, SYMBOL, "sell", "10.00", 100, price_basis="raw_rmb",
        current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=6))
    with pytest.raises(ValueError, match="Reconcile the bound account"):
        journal.save_plan(plan)
    reconcile_session(run_id)
    assert journal.save_plan(plan)["status"] == "planned"


def test_cash_movement_csv_cli_is_export_only_and_signs_are_strict(next_session, tmp_path, capsys):
    from trading.ths.ledger_import import reconcile_session
    run_id, _, _ = next_session
    mapped = {"columns": {k: k for k in ("movement_id", "kind", "cash_change")}, "basis": "nontrade_cash_movements",
        "currency": "CNY", "amount_unit": "rmb",
        "kinds": {"credit": "deposit", "debit": "withdrawal"}}
    profile = tmp_path / "movement-profile.json"
    profile.write_text(json.dumps({"account_alias": "test-only", "tables": {"movements": mapped}}), encoding="utf-8")
    path = tmp_path / "actual-format-fixture.csv"
    path.write_text("movement_id,kind,cash_change\n000001,credit,100.00\n000002,debit,-40.00\n", encoding="utf-8")
    assert cli.main(["import-export", "--kind", "movements", "--profile", str(profile), "--csv", str(path),
        "--trading-date", NEXT, "--observed-at", "2024-01-03T10:00:03+08:00"]) == 0
    capsys.readouterr()
    journal.observe(observed_next("account", [holding_row(300, 200)], second=5, cash="9052.77"))
    assert reconcile_session(run_id)["cash_total_cents"] == 905277
    with pytest.raises(SystemExit):
        cli.make_parser().parse_args(["capture", "--kind", "movements", "--profile", str(profile), "--trading-date", NEXT])
    for row in ({"movement_id": "1", "kind": "debit", "cash_change": "10.00"},
                {"movement_id": "1", "kind": "credit", "cash_change": "1.001"},
                {"movement_id": "1", "kind": "unknown", "cash_change": "10.00"}):
        with pytest.raises(ValueError):
            observations.normalize("movements", [row], mapped)
    for change in ({"currency": "USD"}, {"amount_unit": "cent"}):
        with pytest.raises(ValueError):
            observations.normalize("movements", [], {**mapped, **change})


def test_core_session_roundtrip_atomicity_and_non_simulator_context():
    from dataclasses import replace
    from trading.contracts import BrokerCashMovement, BrokerSessionBalance, RunContext, TradingMode
    from trading.ledger import Ledger
    from trading.tests.test_broker_ledger_import import book_and_receipt
    book, fill_receipt = book_and_receipt()
    book.apply_broker_receipt(fill_receipt)
    receipt = BrokerSessionBalance(book.context, "test-only", NEXT, "2024-01-04", 950561, ((SYMBOL, 50, 50),), ((SYMBOL, 1001),),
        (BrokerCashMovement("001", "cash_dividend", 1234),), (("order1", "cancelled", "terminal-proof"),), NEXT_STAMP, "session-proof")
    before = deepcopy(book.state)
    with pytest.raises(ValueError, match="cash/holdings"):
        book.apply_broker_session(replace(receipt, cash_total=receipt.cash_total + 1))
    assert book.state == before
    assert book.apply_broker_session(receipt)
    restarted = Ledger(json.loads(json.dumps(book.state)), book.config, book.context)
    assert not restarted.apply_broker_session(BrokerSessionBalance.from_dict(json.loads(json.dumps(receipt.to_dict()))))
    assert restarted.state["cash"] == 950561
    with pytest.raises(ValueError):
        replace(receipt, context=RunContext("broker-test", TradingMode.REPLAY))
