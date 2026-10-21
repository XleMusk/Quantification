"""Account share-credit exports through the sole Ledger; no native client calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from trading import store
from trading.contracts import BrokerSessionBalance, BrokerShareCredit, SecurityId, ShareQuantity
from trading.report import detail
from trading.ths import adapter, cli, journal, observations
from trading.ths.ledger_import import import_plan, reconcile_session, run_book
from trading.tests.test_broker_ledger_import import persisted, holding_row
from trading.tests.test_broker_sessions import next_session, observed_next, NEXT, NEXT_STAMP

FIELDS = ("credit_id", "symbol", "market", "credited_qty", "credit_day", "available_day", "kind")
MAPPING = {"columns": {k: k for k in FIELDS}, "basis": "account_bonus_share_credits",
           "quantity_unit": "share", "markets": {"SH": "SSE"},
           "kinds": {"BONUS": "bonus_shares", "TRANSFER": "capital_transfer"}}
ROW = {"credit_id": "000008", "symbol": "600000", "market": "SH", "credited_qty": "100",
       "credit_day": NEXT, "available_day": NEXT, "kind": "BONUS"}


def credit_view(rows=None, *, second=3):
    return observations.observation("share_credits", [ROW] if rows is None else rows, MAPPING,
        account_alias="test-only", observed_at=(datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=second)).isoformat(),
        trading_date=NEXT, source="ths_user_export")


def test_actual_share_csv_cli_plan_and_fill_consumer(next_session, tmp_path, capsys):
    run_id, _, _ = next_session
    profile = tmp_path / "share-profile.json"
    profile.write_text(json.dumps({"account_alias": "test-only", "tables": {"share_credits": MAPPING}}), encoding="utf-8")
    path = tmp_path / "account-share-fixture.csv"
    path.write_text(",".join(FIELDS) + "\n000008,600000,SH,75,2024-01-03,2024-01-03,BONUS\n"
                    "000009,600000,SH,25,2024-01-03,2024-01-03,TRANSFER\n", encoding="utf-8")
    assert cli.main(["import-export", "--kind", "share_credits", "--profile", str(profile), "--csv", str(path),
                     "--trading-date", NEXT, "--observed-at", "2024-01-03T10:00:03+08:00"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert [r["credit_id"] for r in exported["rows"]] == ["000008", "000009"]
    journal.observe(observed_next("account", [holding_row(400, 300)], second=5, cash="8992.77"))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises(ValueError, match="holdings"):
        reconcile_session(run_id)
    assert store.get(run_id, full=True)["state"] == before
    assert cli.main(["reconcile-session", "--run-id", run_id, "--include-share-credits"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["credited_shares"] == 100 and result["cash_total_cents"] == 899277
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeated = list(pool.map(lambda _: reconcile_session(run_id), range(2)))
    assert all(not r["changed"] and r["credited_shares"] == 0 for r in repeated)
    credited = store.get(run_id, full=True)["state"]
    assert credited["fills"] == before["fills"] and credited["cash"] == before["cash"]
    assert credited["positions"]["600000.SSE"][:len(before["positions"]["600000.SSE"])] == before["positions"]["600000.SSE"]
    assert detail(run_id)["holdings"][0]["quantity"] == 400
    assert detail(run_id)["holdings"][0]["sellable"] == 300
    plan = adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 300,
        price_basis="raw_rmb", current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=6))
    journal.save_plan(plan)
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET status='awaiting_manual_submission' WHERE id=?", (plan["id"],))
    journal.bind(plan["id"], "000002", NEXT)
    common = {"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002", "qty": "300", "price": "10.00"}
    journal.observe(observed_next("orders", [{**common, "filled_qty": "300", "status": "filled"}], second=7))
    journal.observe(observed_next("trades", [{**common, "execution_id": "000003"}], second=8))
    journal.observe(observed_next("cashflows", [{"symbol": "600000", "market": "SH", "side": "SELL", "order_id": "000002",
        "gross_amount": "3000.00", "fees": "7.23", "cash_change": "2992.77"}], second=9))
    journal.observe(observed_next("account", [holding_row(100, 0)], second=10, cash="11985.54"))
    sold = import_plan(run_id, plan["id"])
    assert sold["cash_total_cents"] == 1198554
    state = store.get(run_id, full=True)["state"]
    assert len(state["broker_share_credits"]) == 2 and len(state["fills"]) == 3
    assert state["positions"]["600000.SSE"] == [{"qty": 100, "available": "9999-12-31"}]
    assert not import_plan(run_id, plan["id"])["changed"]
    assert not reconcile_session(run_id, observed_at=result["observed_at"])["changed"]
    assert store.get(run_id, full=True)["state"] == state
    assert store.daily(run_id) == [] and store.get(run_id)["cursor"] == 0
    evidence = os.environ.get("QUANT_SHARE_TEST_EVIDENCE")
    if evidence:
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "consumer-receipt.json").write_text(json.dumps({
            "scope": "isolated_synthetic_account_share_exports_not_native_THS", "exported": exported,
            "reconciliation": result, "repeated": repeated, "same_run_id": run_id,
            "quantity_before": 300, "quantity_after_credit": 400, "sellable_after_credit": 300,
            "cash_after_credit_cents": credited["cash"], "original_lots_and_fills_unchanged": True,
            "sale_plan": plan, "sale_import": sold, "account_detail_after_sale": detail(run_id),
            "native_calls": 0, "submitted_orders": 0, "daily_rows": 0}, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("revised", [False, True])
def test_known_credit_availability_settles_without_unlocking_opening_lots(next_session, monkeypatch, revised):
    run_id, _, _ = next_session
    journal.observe(credit_view([{**ROW, "available_day": "2024-01-04"}]))
    journal.observe(observed_next("account", [holding_row(400, 200)], second=5, cash="8992.77"))
    first = reconcile_session(run_id, share_credits=True)
    before = store.get(run_id, full=True)["state"]
    day, stamp = "2024-01-04", "2024-01-04T10:00:00+08:00"
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: datetime.fromisoformat(stamp) + timedelta(seconds=30))
    for kind in ("orders", "trades", "share_credits"):
        view = journal.latest("test-only", kind, NEXT)
        view["observed_at"] = stamp
        if revised and kind == "share_credits":
            view["rows"][0]["credited_qty"] = 99
        journal.observe(view)
    for second, kind in enumerate(("orders", "trades", "movements"), 1):
        journal.observe(observed_next(kind, [], day=day, stamp=stamp, second=second))
    journal.observe(observed_next("account", [holding_row(400, 300)], day=day, stamp=stamp, second=4, cash="8992.77"))
    if revised:
        with pytest.raises(ValueError, match="Prior share credit evidence"):
            reconcile_session(run_id)
        assert store.get(run_id, full=True)["state"] == before
    else:
        assert reconcile_session(run_id)["changed"]
        assert detail(run_id)["holdings"][0]["sellable"] == 300
        state = store.get(run_id, full=True)["state"]
        assert sum(l["qty"] for l in state["positions"]["600000.SSE"] if l["available"] == "9999-12-31") == 100
        assert len(state["broker_share_credits"]) == 1
        assert not reconcile_session(run_id, observed_at=first["observed_at"])["changed"]
        assert store.get(run_id, full=True)["state"] == state


def test_credit_and_explicit_opening_release_keep_odd_lot_exit_rule(next_session):
    from trading.tests.test_broker_restriction_releases import release_view
    run_id, _, _ = next_session
    journal.observe(credit_view([{**ROW, "credited_qty": "75"}]))
    journal.observe(release_view())
    journal.observe(observed_next("account", [holding_row(375, 375)], second=5, cash="8992.77"))
    result = reconcile_session(run_id, share_credits=True, restriction_releases=True)
    assert result["credited_shares"] == 75 and result["released_opening_shares"] == 100
    current = datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=6)
    with pytest.raises(ValueError, match="odd_lot"):
        adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 275,
                            price_basis="raw_rmb", current=current)
    plan = adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 375,
                               price_basis="raw_rmb", current=current)
    assert journal.save_plan(plan)["qty"] == 375


@pytest.mark.parametrize("bad", ["quantity", "cash", "sellable", "unknown_release", "future_sellable", "off_calendar", "late", "missing", "unsupported_fund"])
def test_share_credit_cannot_hide_other_account_discrepancies(next_session, bad):
    run_id, _, _ = next_session
    rows = [dict(ROW)]
    total, sellable, cash = 400, 300, "8992.77"
    if bad == "quantity":
        total = 401
    elif bad == "cash":
        cash = "8992.78"
    elif bad == "sellable":
        sellable = 299
    elif bad == "unknown_release":
        sellable = 400
    elif bad == "future_sellable":
        rows[0]["available_day"] = "2024-01-04"
    elif bad == "off_calendar":
        rows[0]["available_day"] = "2024-01-06"
    elif bad == "unsupported_fund":
        rows[0]["symbol"] = "510300"
    if bad != "missing":
        journal.observe(credit_view(rows, second=6 if bad == "late" else 3))
    journal.observe(observed_next("account", [holding_row(total, sellable)], second=5, cash=cash))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises(ValueError):
        reconcile_session(run_id, share_credits=True)
    assert store.get(run_id, full=True)["state"] == before
    assert store.get(run_id)["error"].startswith("broker_session:")


def test_contract_export_and_cash_only_boundary(next_session):
    run_id, _, _ = next_session
    credit = BrokerShareCredit("000008", SecurityId("600000.SSE"), ShareQuantity(75), NEXT, NEXT, "bonus_shares")
    assert BrokerShareCredit.from_dict(json.loads(json.dumps(credit.to_dict()))) == credit
    for changes in ({"kind": "rights_subscription"}, {"quantity": 75}, {"available_day": "2024-01-02"}):
        with pytest.raises(ValueError):
            replace(credit, **changes)
    for changes in ({"credited_qty": "0"}, {"credited_qty": "-1"}, {"credited_qty": "1.0"},
                    {"kind": "unknown"}, {"available_day": ""}, {"market": "unknown"}):
        with pytest.raises(ValueError):
            observations.normalize("share_credits", [{**ROW, **changes}], MAPPING)
    with pytest.raises(ValueError):
        observations.normalize("share_credits", [ROW, ROW], MAPPING)
    with pytest.raises(ValueError):
        observations.normalize("share_credits", [], {**MAPPING, "quantity_unit": "lot"})
    with pytest.raises(ValueError):
        credit_view([{**ROW, "credit_day": "2024-01-04"}])
    with pytest.raises(SystemExit):
        cli.make_parser().parse_args(["capture", "--kind", "share_credits", "--profile", "unused", "--trading-date", NEXT])
    reconcile_session(run_id)
    state = store.get(run_id, full=True)["state"]
    with store.connection() as db:
        _, book = run_book(db, run_id)
    session = state["broker_sessions"][NEXT]
    receipt = BrokerSessionBalance.from_dict({**session["economic"], "observed_at": session["observed_at"],
                                             "source_ref": session["source_ref"]})
    assert "share_credits" not in receipt.to_dict()
    restored = BrokerSessionBalance.from_dict(replace(receipt, share_credits=(credit,)).to_dict())
    assert restored.share_credits == (credit,)
    with pytest.raises(ValueError, match="no order/share"):
        book.apply_broker_cash_statement(restored)
    assert book.state == state
