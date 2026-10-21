"""Current-session export consumers in isolated synthetic accounts, not native THS."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from trading import store
from trading.contracts import BrokerSessionBalance
from trading.ledger import Ledger
from trading.report import detail
from trading.ths import adapter, cli, journal, observations
from trading.ths.ledger_import import import_movements, reconcile_session, run_book
from trading.tests.test_broker_ledger_import import persisted, holding_row
from trading.tests.test_broker_sessions import next_session, observed_next, NEXT, NEXT_STAMP

ROWS = [{"movement_id": "000001", "kind": "cash_dividend", "cash_change": "12.34"},
        {"movement_id": "000002", "kind": "tax", "cash_change": "-0.01"}]


def cash_views(rows=ROWS, *, cash="9005.10", second=6):
    journal.observe(observed_next("movements", rows, second=second))
    journal.observe(observed_next("account", [holding_row(300, 200)], second=second + 1, cash=cash))


def test_actual_cash_cli_cumulative_restart_and_next_plan(next_session, capsys):
    run_id, _, _ = next_session
    reconcile_session(run_id)
    opening = deepcopy(store.get(run_id, full=True)["state"]["broker_sessions"][NEXT])
    cash_views()
    assert cli.main(["import-movements", "--run-id", run_id]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["new_movements"] == 2 and first["cash_change_cents"] == 1233
    first_stamp = first["observed_at"]
    assert first["cash_total_cents"] == 900510 and not first["native_verified"] and not first["submit_enabled"]
    more = ROWS + [{"movement_id": "000003", "kind": "interest", "cash_change": "2.00"},
                   {"movement_id": "000004", "kind": "deposit", "cash_change": "100.00"},
                   {"movement_id": "000005", "kind": "withdrawal", "cash_change": "-50.00"},
                   {"movement_id": "000006", "kind": "fee", "cash_change": "-0.10"}]
    cash_views(more, cash="9057.00", second=8)
    assert cli.main(["import-movements", "--run-id", run_id]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["new_movements"] == 4 and second["cash_change_cents"] == 5190
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeats = list(pool.map(lambda _: import_movements(run_id), range(2)))
    assert all(not r["changed"] and r["new_movements"] == 0 for r in repeats)
    state = store.get(run_id, full=True)["state"]
    assert state["broker_sessions"][NEXT] == opening
    assert state["cash"] == 905700 and len(state["broker_cash_movements"]) == 6
    assert not import_movements(run_id, observed_at=first_stamp)["changed"]
    assert store.get(run_id, full=True)["state"] == state
    account_detail = detail(run_id)
    assert account_detail["available_cash"] == 9057 and account_detail["accounted_fees"] == 7.34
    assert account_detail["comparison"]["metrics"] is None and store.daily(run_id) == []
    assert store.get(run_id)["cursor"] == 0 and len(state["fills"]) == 2

    # The next plan consumes the updated observation and same cash/lot state.
    plan = adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 100,
                               price_basis="raw_rmb", current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=10))
    assert journal.save_plan(plan)["snapshot"]["cash_total"] == 905700
    with store.connection() as db:
        _, book = run_book(db, run_id)
    economic = state["broker_cash_statements"][f"ths:{NEXT}:{second['observed_at']}"]["economic"]
    saved_receipt = BrokerSessionBalance.from_dict({**economic, "observed_at": second["observed_at"],
                                                  "source_ref": second["source_ref"]})
    restarted = Ledger(json.loads(json.dumps(state)), book.config, book.context)
    assert not restarted.apply_broker_cash_statement(saved_receipt)
    evidence = os.environ.get("QUANT_CASH_TEST_EVIDENCE")
    if evidence:
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "consumer-receipt.json").write_text(json.dumps({
            "scope": "isolated_synthetic_exports_not_native_THS",
            "first": first, "second": second, "repeated": repeats,
            "opening_unchanged": state["broker_sessions"][NEXT] == opening,
            "sole_run_id": run_id, "cursor": 0, "daily_rows": 0, "fills_unchanged": 2,
            "next_plan": journal.get(plan["id"]), "native_calls": 0, "submitted_orders": 0,
            "account_detail": account_detail}, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("bad", ["cash", "shares", "unknown_release", "frozen", "missing", "revised", "late_view", "stale", "new_plan"])
def test_cash_conflicts_leave_sole_account_unchanged_and_block_plans(next_session, bad):
    run_id, _, _ = next_session
    reconcile_session(run_id)
    cash_views()
    import_movements(run_id)
    before = store.get(run_id, full=True)["state"]
    rows, cash = deepcopy(ROWS), "9005.10"
    if bad == "missing":
        rows = rows[:1]
    elif bad == "revised":
        rows[0]["cash_change"] = "12.35"
        cash = "9005.11"
    cash_views(rows, cash=cash, second=8)
    account = journal.latest("test-only", "account")
    if bad in {"cash", "shares", "unknown_release", "frozen", "stale"}:
        if bad == "cash":
            account["cash_total"] += 1
            account["cash_available"] += 1
        elif bad == "shares":
            account["positions"]["600000.SSE"]["quantity"] += 100
        elif bad == "unknown_release":
            account["positions"]["600000.SSE"]["sellable"] += 100
        elif bad == "frozen":
            account["cash_frozen"] = 1
            account["cash_available"] -= 1
        else:
            account["observed_at"] = "2024-01-03T09:00:00+08:00"
            with store.connection() as db:
                db.execute("DELETE FROM ths_observations WHERE kind='account' AND trading_date=?", (NEXT,))
        journal.observe(account)
    elif bad == "late_view":
        journal.observe(observed_next("orders", [], second=10))
    elif bad == "new_plan":
        plan = adapter.create_plan(account, "600000.SSE", "sell", "10.00", 100, price_basis="raw_rmb",
                                   current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=10))
        journal.save_plan(plan)
    with pytest.raises((ValueError, PermissionError)):
        import_movements(run_id)
    assert store.get(run_id, full=True)["state"] == before
    if bad == "new_plan":
        assert store.get(run_id)["error"] is None
        return
    assert store.get(run_id)["error"].startswith("broker_session: cash_statement:")
    with pytest.raises(ValueError, match="cash statement conflict"):
        reconcile_session(run_id)
    if bad not in {"stale", "new_plan", "frozen"}:
        plan = adapter.create_plan(account, "600000.SSE", "sell", "10.00", 100, price_basis="raw_rmb",
                                   current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=11))
        with pytest.raises(ValueError, match="session"):
            journal.save_plan(plan)


def test_cash_conflict_repair_cannot_clear_on_an_old_statement(next_session):
    run_id, _, _ = next_session
    reconcile_session(run_id)
    cash_views()
    first = import_movements(run_id)
    cash_views(ROWS + [{"movement_id": "000003", "kind": "interest", "cash_change": "2.00"}], second=8)
    with pytest.raises(ValueError, match="reconcile"):
        import_movements(run_id)
    assert not import_movements(run_id, observed_at=first["observed_at"])["changed"]
    assert store.get(run_id)["error"]
    journal.observe(observed_next("account", [holding_row(300, 200)], second=10, cash="9007.10"))
    assert import_movements(run_id)["cash_total_cents"] == 900710
    assert store.get(run_id)["error"] is None


def test_bind_day_cash_refusal_does_not_block_following_session(persisted):
    from trading.ths.ledger_import import bind_account
    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    before = store.get(run_id, full=True)["state"]
    with pytest.raises(ValueError, match="bind-day"):
        import_movements(run_id)
    assert store.get(run_id, full=True)["state"] == before and store.get(run_id)["error"] is None


@pytest.mark.parametrize("revised", [False, True])
def test_following_session_preserves_cash_and_rechecks_prior_statement(next_session, monkeypatch, revised):
    run_id, _, _ = next_session
    reconcile_session(run_id)
    cash_views()
    imported = import_movements(run_id)
    before = store.get(run_id, full=True)["state"]
    day, stamp = "2024-01-04", "2024-01-04T10:00:00+08:00"
    current = datetime.fromisoformat(stamp) + timedelta(seconds=30)
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: current)
    for kind in ("orders", "trades", "movements"):
        view = journal.latest("test-only", kind, NEXT)
        view["observed_at"] = stamp
        if revised and kind == "movements":
            view["rows"][0]["cash_change_cents"] += 1
        journal.observe(view)
    for second, kind in enumerate(("orders", "trades", "movements"), 1):
        journal.observe(observed_next(kind, [], day=day, stamp=stamp, second=second))
    journal.observe(observed_next("account", [holding_row(300, 200)], day=day, stamp=stamp, second=4, cash="9005.10"))
    if revised:
        with pytest.raises(ValueError, match="Prior cash statement"):
            reconcile_session(run_id)
        assert store.get(run_id, full=True)["state"] == before
    else:
        assert reconcile_session(run_id)["cash_total_cents"] == 900510
        state = store.get(run_id, full=True)["state"]
        assert not import_movements(run_id, observed_at=imported["observed_at"])["changed"]
        assert store.get(run_id, full=True)["state"] == state
        assert state["positions"] == before["positions"] and state["day"] == day
        assert len(state["broker_cash_movements"]) == 2 and store.daily(run_id) == []


def test_updated_cash_flows_into_existing_manual_order_receipt(next_session):
    from trading.ths.ledger_import import import_plan
    run_id, _, _ = next_session
    reconcile_session(run_id)
    cash_views()
    imported = import_movements(run_id)
    plan = adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 100,
                               price_basis="raw_rmb", current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=10))
    journal.save_plan(plan)
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET status='awaiting_manual_submission' WHERE id=?", (plan["id"],))
    journal.bind(plan["id"], "000002", NEXT)
    common = {"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002", "qty": "100", "price": "10.00"}
    journal.observe(observed_next("orders", [{**common, "filled_qty": "100", "status": "filled"}], second=11))
    journal.observe(observed_next("trades", [{**common, "execution_id": "000003"}], second=12))
    journal.observe(observed_next("cashflows", [{"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002",
        "gross_amount": "1000.00", "fees": "7.23", "cash_change": "992.77"}], second=13))
    journal.observe(observed_next("account", [holding_row(200, 100)], second=14, cash="9997.87"))
    assert import_plan(run_id, plan["id"])["cash_total_cents"] == 999787
    state = store.get(run_id, full=True)["state"]
    assert len(state["fills"]) == 3 and len(state["broker_cash_movements"]) == 2
    assert not import_movements(run_id, observed_at=imported["observed_at"])["changed"]
    with pytest.raises(PermissionError, match="first new order"):
        import_movements(run_id)
    assert store.get(run_id)["error"] is None and store.get(run_id, full=True)["state"] == state
    assert detail(run_id)["available_cash"] == 9997.87 and store.daily(run_id) == []
