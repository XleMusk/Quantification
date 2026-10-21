"""Explicit opening-lock export consumers; synthetic accounts, no native calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from trading import store
from trading.contracts import BrokerRestrictionRelease, BrokerSessionBalance, SecurityId, ShareQuantity
from trading.report import detail
from trading.ths import adapter, cli, journal, observations
from trading.ths.ledger_import import reconcile_session, run_book
from trading.tests.test_broker_ledger_import import persisted, holding_row
from trading.tests.test_broker_sessions import next_session, observed_next, NEXT, NEXT_STAMP

MAPPING = {"columns": {k: k for k in ("release_id", "symbol", "market", "released_qty", "release_day", "reason")},
           "basis": "opening_restriction_releases", "quantity_unit": "share", "markets": {"SH": "SSE"},
           "reasons": {"OPENING_RELEASE": "opening_unknown_restriction_release"}}
ROW = {"release_id": "000007", "symbol": "600000", "market": "SH", "released_qty": "100",
       "release_day": NEXT, "reason": "OPENING_RELEASE"}


def release_view(rows=None, *, second=3, day=NEXT, stamp=NEXT_STAMP):
    return observations.observation("restriction_releases", [ROW] if rows is None else rows, MAPPING,
        account_alias="test-only", observed_at=(datetime.fromisoformat(stamp) + timedelta(seconds=second)).isoformat(),
        trading_date=day, source="ths_user_export")


def test_actual_release_csv_cli_and_sole_ledger_sell_plan(next_session, tmp_path, capsys):
    run_id, _, _ = next_session
    profile = tmp_path / "release-profile.json"
    profile.write_text(json.dumps({"account_alias": "test-only", "tables": {"restriction_releases": MAPPING}}), encoding="utf-8")
    path = tmp_path / "broker-format-fixture.csv"
    path.write_text("release_id,symbol,market,released_qty,release_day,reason\n000007,600000,SH,100,2024-01-03,OPENING_RELEASE\n", encoding="utf-8")
    assert cli.main(["import-export", "--kind", "restriction_releases", "--profile", str(profile), "--csv", str(path),
                     "--trading-date", NEXT, "--observed-at", "2024-01-03T10:00:03+08:00"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["rows"][0]["release_id"] == "000007"
    journal.observe(observed_next("account", [holding_row(300, 300)], second=5, cash="8992.77"))
    before = store.get(run_id, full=True)["state"]
    # A larger sellable count alone remains insufficient even if an export exists.
    with pytest.raises(ValueError, match="availability"):
        reconcile_session(run_id)
    assert store.get(run_id, full=True)["state"] == before
    assert cli.main(["reconcile-session", "--run-id", run_id, "--include-restriction-releases"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["released_opening_shares"] == 100 and result["cash_total_cents"] == 899277
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeats = list(pool.map(lambda _: reconcile_session(run_id), range(2)))
    assert all(not r["changed"] and r["released_opening_shares"] == 0 for r in repeats)
    state = store.get(run_id, full=True)["state"]
    assert len(state["broker_restriction_releases"]) == 1 and len(state["fills"]) == 2
    assert not any(lot["available"] == "9999-12-31" for lot in state["positions"]["600000.SSE"])
    account_detail = detail(run_id)
    assert account_detail["holdings"][0]["quantity"] == account_detail["holdings"][0]["sellable"] == 300
    assert account_detail["comparison"]["metrics"] is None and store.daily(run_id) == [] and store.get(run_id)["cursor"] == 0
    with store.connection() as db:
        _, book = run_book(db, run_id)
    session = state["broker_sessions"][NEXT]
    receipt = BrokerSessionBalance.from_dict({**session["economic"], "observed_at": session["observed_at"],
                                             "source_ref": session["source_ref"]})
    assert not book.apply_broker_session(receipt)
    assert receipt.to_dict()["restriction_releases"][0]["quantity"]["value"] == 100
    plan = adapter.create_plan(journal.latest("test-only", "account"), "600000.SSE", "sell", "10.00", 300,
        price_basis="raw_rmb", current=datetime.fromisoformat(NEXT_STAMP) + timedelta(seconds=6))
    saved = journal.save_plan(plan)
    assert saved["qty"] == 300 and saved["snapshot"]["positions"]["600000.SSE"]["sellable"] == 300
    evidence = os.environ.get("QUANT_RELEASE_TEST_EVIDENCE")
    if evidence:
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "consumer-receipt.json").write_text(json.dumps({
            "scope": "isolated_synthetic_release_exports_not_native_THS", "imported": imported,
            "reconciliation": result, "repeated": repeats, "next_plan": saved, "account_detail": account_detail,
            "same_run_id": run_id, "cash_unchanged": state["cash"] == before["cash"],
            "quantity_before": sum(lot["qty"] for lot in before["positions"]["600000.SSE"]),
            "quantity_after": 300, "prior_fills_unchanged": state["fills"] == before["fills"],
            "native_calls": 0, "submitted_orders": 0, "daily_rows": 0}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_partial_release_keeps_unknown_remainder_locked(next_session):
    run_id, _, _ = next_session
    journal.observe(release_view([{**ROW, "released_qty": "40"}]))
    journal.observe(observed_next("account", [holding_row(300, 240)], second=5, cash="8992.77"))
    assert reconcile_session(run_id, restriction_releases=True)["released_opening_shares"] == 40
    lots = store.get(run_id, full=True)["state"]["positions"]["600000.SSE"]
    assert sum(lot["qty"] for lot in lots if lot["available"] == "9999-12-31") == 60
    assert detail(run_id)["holdings"][0]["sellable"] == 240


def test_released_shares_use_existing_order_fill_accounting(next_session):
    from trading.ths.ledger_import import import_plan
    run_id, _, _ = next_session
    journal.observe(release_view())
    journal.observe(observed_next("account", [holding_row(300, 300)], second=5, cash="8992.77"))
    reconcile_session(run_id, restriction_releases=True)
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
    journal.observe(observed_next("account", [], second=10, cash="11985.54"))
    assert import_plan(run_id, plan["id"])["cash_total_cents"] == 1198554
    state = store.get(run_id, full=True)["state"]
    assert len(state["fills"]) == 3 and len(state["broker_restriction_releases"]) == 1
    assert detail(run_id)["holdings"] == [] and detail(run_id)["accounted_fees"] == 14.46
    assert not import_plan(run_id, plan["id"])["changed"]
    assert store.daily(run_id) == [] and store.get(run_id)["cursor"] == 0


@pytest.mark.parametrize("bad", ["exceeds", "wrong_symbol", "cash", "shares", "sellable", "late_view", "missing", "not_opening_lock"])
def test_release_evidence_cannot_rebase_or_create_shares(next_session, bad):
    run_id, _, _ = next_session
    rows = [dict(ROW)]
    if bad == "exceeds":
        rows[0]["released_qty"] = "101"
    elif bad == "wrong_symbol":
        rows[0]["symbol"] = "600010"
    if bad != "missing":
        journal.observe(release_view(rows, second=6 if bad == "late_view" else 3))
    total, available, cash = 300, 300, "8992.77"
    if bad == "cash":
        cash = "8992.78"
    elif bad == "shares":
        total = 400
    elif bad == "sellable":
        available = 299
    elif bad == "not_opening_lock":
        with store.connection() as db:
            state = json.loads(db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            for lot in state["positions"]["600000.SSE"]:
                if lot["available"] == "9999-12-31":
                    lot["available"] = "2024-01-05"
            db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))
    journal.observe(observed_next("account", [holding_row(total, available)], second=5, cash=cash))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises(ValueError):
        reconcile_session(run_id, restriction_releases=True)
    assert store.get(run_id, full=True)["state"] == before
    assert store.get(run_id)["error"].startswith("broker_session:")


@pytest.mark.parametrize("revised", [False, True])
def test_following_day_release_proof_and_historical_reread(next_session, monkeypatch, revised):
    run_id, _, _ = next_session
    journal.observe(release_view())
    journal.observe(observed_next("account", [holding_row(300, 300)], second=5, cash="8992.77"))
    first = reconcile_session(run_id, restriction_releases=True)
    before = store.get(run_id, full=True)["state"]
    day, stamp = "2024-01-04", "2024-01-04T10:00:00+08:00"
    current = datetime.fromisoformat(stamp) + timedelta(seconds=30)
    for module in (adapter, observations, cli):
        monkeypatch.setattr(module, "now", lambda: current)
    for kind in ("orders", "trades", "restriction_releases"):
        view = journal.latest("test-only", kind, NEXT)
        view["observed_at"] = stamp
        if revised and kind == "restriction_releases":
            view["rows"][0]["released_qty"] = 99
        journal.observe(view)
    for second, kind in enumerate(("orders", "trades", "movements"), 1):
        journal.observe(observed_next(kind, [], day=day, stamp=stamp, second=second))
    journal.observe(observed_next("account", [holding_row(300, 300)], day=day, stamp=stamp, second=4, cash="8992.77"))
    if revised:
        with pytest.raises(ValueError, match="Prior restriction release evidence"):
            reconcile_session(run_id)
        assert store.get(run_id, full=True)["state"] == before
    else:
        assert reconcile_session(run_id)["changed"]
        state = store.get(run_id, full=True)["state"]
        assert not reconcile_session(run_id, observed_at=first["observed_at"])["changed"]
        assert store.get(run_id, full=True)["state"] == state
        assert len(state["broker_restriction_releases"]) == 1


def test_release_contract_csv_guards_and_legacy_session_payload():
    release = BrokerRestrictionRelease("000007", SecurityId("600000.SSE"), ShareQuantity(100), NEXT)
    assert BrokerRestrictionRelease.from_dict(json.loads(json.dumps(release.to_dict()))) == release
    for changes in ({"reason": "t1_guess"}, {"quantity": 100}, {"day": "tomorrow"}):
        with pytest.raises(ValueError):
            replace(release, **changes)
    for changes in ({"released_qty": "0"}, {"released_qty": "1.0"}, {"reason": "unknown"}, {"market": "unknown"}):
        with pytest.raises(ValueError):
            observations.normalize("restriction_releases", [{**ROW, **changes}], MAPPING)
    with pytest.raises(ValueError):
        observations.normalize("restriction_releases", [ROW, ROW], MAPPING)
    with pytest.raises(ValueError):
        observations.normalize("restriction_releases", [], {**MAPPING, "quantity_unit": "lot"})
    with pytest.raises(ValueError):
        release_view([{**ROW, "release_day": "2024-01-04"}])
    with pytest.raises(SystemExit):
        cli.make_parser().parse_args(["capture", "--kind", "restriction_releases", "--profile", "unused", "--trading-date", NEXT])
    from trading.contracts import RunContext, TradingMode
    legacy = BrokerSessionBalance(RunContext("legacy", TradingMode.PAPER), "test-only", NEXT, "2024-01-04", 0,
                                  (), (), (), (), NEXT_STAMP, "legacy-proof")
    assert "restriction_releases" not in legacy.to_dict()
    assert BrokerSessionBalance.from_dict(legacy.to_dict()) == legacy
