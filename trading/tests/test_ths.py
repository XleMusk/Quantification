"""No broker process or desktop writes in these tests."""
import ast
from datetime import timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from trading import settings
from trading.ths import journal
from trading.ths.adapter import PrefillAdapter, check_plan, create_plan, from_target, money, now
from trading.ths.broker import THSAdapter
from trading.ths.observations import csv_rows, normalize, observation, reconcile, retain_execution_evidence


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.sqlite3")
    journal.initialize()


@pytest.fixture
def snapshot():
    return {"source": "ths_controls", "account_alias": "test-only", "observed_at": now().isoformat(),
            "trading_date": now().astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            "cash_available": 1000000, "positions": {"600000.SSE": {"quantity": 200, "sellable": 100}}}


def plan_for(snapshot, **kwargs):
    values = {"symbol": "600000.SSE", "side": "buy", "price": "10.00", "qty": 100, "price_basis": "raw_rmb"}
    return create_plan(snapshot, **{**values, **kwargs})


@pytest.fixture
def profile():
    return {"verified_local": True, "account_alias": "test-only",
            "fields": {"symbol": {"control_id": 1}, "price": {"control_id": 2}, "qty": {"control_id": 3}},
            "guards": {"account": {"selector": {"control_id": 4}, "expected": "masked-test-account"},
                       "side": {"selector": {"control_id": 5}, "expected": {"buy": "Buy", "sell": "Sell"}},
                       "price_mode": {"selector": {"control_id": 6}, "expected": "Limit"}}}


class FakeWindow:
    def __init__(self):
        self.values = {1: "", 2: "", 3: "", 4: "masked-test-account", 5: "Buy", 6: "Limit"}
        self.writes = []
        self.bad_readback = False
        self.blocked = False

    def ready(self):
        if self.blocked:
            raise RuntimeError("Wrong window / popup / locked desktop")

    def check_fields(self, selectors):
        assert len({s["control_id"] for s in selectors}) == 3

    def text(self, selector):
        return self.values[selector["control_id"]]

    def write(self, selector, value):
        self.ready()
        key = selector["control_id"]
        self.writes.append((key, value))
        self.values[key] = "wrong" if self.bad_readback else value


@pytest.mark.parametrize("price", ["NaN", "Infinity", "1.001", "1e3", "1,00", "=10", "-1", ""])
def test_money_rejects_ambiguous_values(price):
    with pytest.raises(ValueError):
        money(price)


def test_money_is_exact():
    assert money("1,234.56") == 123456


def test_single_observation_replay_is_idempotent():
    from trading.store import connection

    value = {
        "kind": "account",
        "source": "ths_user_export",
        "account_alias": "test-only",
        "observed_at": "2026-10-12T09:30:00+08:00",
        "trading_date": "2026-10-12",
        "cash_available": 1000000,
        "cash_total": 1000000,
        "cash_frozen": 0,
        "positions": {},
    }

    journal.observe(value)
    journal.observe(value)

    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0] == 1


@pytest.mark.parametrize("change", [{"qty": 99}, {"qty": True}, {"qty": 2000},
                                   {"side": "sell", "qty": 200}, {"price_basis": "qfq"},
                                   {"symbol": "688001.SSE"}, {"price": "0"}])
def test_existing_ledger_and_price_boundaries(snapshot, change):
    with pytest.raises(ValueError):
        plan_for(snapshot, **change)


def test_stale_paper_or_future_account_never_planned(snapshot):
    for key, value in (("source", "paper"), ("observed_at", (now() - timedelta(minutes=6)).isoformat()),
                       ("observed_at", (now() + timedelta(minutes=1)).isoformat())):
        with pytest.raises(ValueError):
            plan_for({**snapshot, key: value})


def test_current_capture_timestamp_does_not_promote_historical_account(snapshot):
    with pytest.raises(ValueError, match="account trading date"):
        plan_for({**snapshot, "trading_date": "2020-01-01"})


def test_target_uses_broker_holdings_not_paper(snapshot):
    target = {"mode": "current_preview", "strategy_id": "unit-test", "generated_at": now().isoformat(),
              "symbol": "600000.SSE", "target_qty": 300, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"}
    assert from_target(snapshot, target)["qty"] == 100
    for change in ({"mode": "replay"}, {"generated_at": "2020-01-01T00:00:00+00:00"}, {"target_qty": 200}):
        with pytest.raises(ValueError):
            from_target(snapshot, {**target, **change})


def test_plan_snapshot_immutable_and_expiry(snapshot):
    plan = plan_for(snapshot)
    snapshot["cash_available"] = 0
    assert plan["snapshot"]["cash_available"] == 1000000
    with pytest.raises(ValueError):
        check_plan(plan, current=now() + timedelta(minutes=6))


def test_prefill_writes_only_three_fields_and_reads_back(snapshot, profile):
    window = FakeWindow()
    result = PrefillAdapter(window, profile).prefill(plan_for(snapshot))
    assert window.writes == [(1, "600000"), (2, "10.00"), (3, "100")]
    assert result["status"] == "awaiting_manual_submission" and result["submitted"] is False


@pytest.mark.parametrize("control", [4, 5, 6])
def test_wrong_account_side_price_mode_stops_before_write(snapshot, profile, control):
    window = FakeWindow()
    window.values[control] = "wrong"
    with pytest.raises(ValueError):
        PrefillAdapter(window, profile).prefill(plan_for(snapshot))
    assert not window.writes


def test_unverified_mapping_blocked_desktop_mismatch_and_pause(snapshot, profile):
    window = FakeWindow()
    profile["verified_local"] = False
    with pytest.raises(ValueError):
        PrefillAdapter(window, profile).prefill(plan_for(snapshot))
    profile["verified_local"] = True
    window.blocked = True
    with pytest.raises(RuntimeError):
        PrefillAdapter(window, profile).prefill(plan_for(snapshot))
    window.blocked = False
    with pytest.raises(InterruptedError):
        PrefillAdapter(window, profile).prefill(plan_for(snapshot), stop_check=lambda: True)
    assert not window.writes
    with pytest.raises(InterruptedError):
        PrefillAdapter(window, profile).prefill(plan_for(snapshot), stop_check=lambda: len(window.writes) == 1)
    assert len(window.writes) == 1


def test_failed_readback_not_retried(snapshot, profile):
    plan = journal.save_plan(plan_for(snapshot))
    window = FakeWindow()
    window.bad_readback = True
    adapter = PrefillAdapter(window, profile)
    with pytest.raises(ValueError):
        journal.prefill(plan["id"], adapter)
    assert journal.get(plan["id"])["status"] == "prefill_unknown"
    with pytest.raises(ValueError):
        journal.prefill(plan["id"], adapter)
    with pytest.raises(ValueError):
        journal.save_plan(plan_for(snapshot))
    assert len(window.writes) == 1


def test_durable_claim_pause_and_concurrent_dedup(snapshot):
    plan = journal.save_plan(plan_for(snapshot))
    assert journal.pause(True)["kill_switch"] == "engaged"
    assert journal.status()["kill_switch"] == "engaged"
    with pytest.raises(ValueError, match="kill switch"):
        journal.save_plan(plan_for({**snapshot, "account_alias": "other"}))
    with pytest.raises(ValueError):
        journal.claim(plan["id"])
    assert journal.pause(False)["kill_switch"] == "disengaged"
    def attempt():
        try:
            return journal.claim(plan["id"])["status"]
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["blocked", "prefilling"]
    with pytest.raises(ValueError):
        journal.discard(plan["id"])


def mapping(kind):
    columns = {"account": ["symbol", "market", "quantity", "sellable"],
               "orders": ["symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"],
               "trades": ["symbol", "market", "side", "order_id", "execution_id", "qty", "price"]}[kind]
    return {"columns": {k: k for k in columns}, "markets": {"SH": "SSE", "SZ": "SZSE"},
            "sides": {"BUY": "buy", "SELL": "sell"},
            "statuses": {"accepted": "accepted", "filled": "filled", "cancelled": "cancelled"}}


def observed(kind, rows, **kwargs):
    return observation(kind, rows, mapping(kind), account_alias="test-only", observed_at=now().isoformat(),
                       trading_date=now().astimezone(timezone(timedelta(hours=8))).date().isoformat(), source="ths_user_export", **kwargs)


def test_export_code_and_contract_leading_zeroes(tmp_path):
    file = tmp_path / "broker.csv"
    file.write_text("symbol,market,side,order_id,qty,price,filled_qty,status\n000001,SZ,BUY,000002,100,10.00,0,accepted\n", encoding="utf-8")
    table = csv_rows(file, mapping("orders")["columns"])
    rows = normalize("orders", table, mapping("orders"))
    assert rows[0]["symbol"] == "000001.SZSE" and rows[0]["order_id"] == "000002"
    with pytest.raises(ValueError):
        normalize("orders", table * 2, mapping("orders"))
    with pytest.raises(ValueError):
        csv_rows(file, {"missing": "no_column"})


def test_observed_account_cash_sellable_stored_not_simulated():
    value = observed("account", [{"symbol": "600000", "market": "SH", "quantity": "200", "sellable": "100"}], cash="9,500.00")
    journal.observe(value)
    latest = journal.latest("test-only", "account")
    assert latest["cash_available"] == 950000
    assert latest["positions"]["600000.SSE"]["sellable"] == 100
    with pytest.raises(ValueError):
        observed("account", [], cash="NaN")


def test_historical_account_export_is_storable_but_cannot_fund_current_plan(monkeypatch):
    from datetime import datetime, timezone
    stamp = "2024-01-03T10:00:00+08:00"
    value = observation("account", [{"symbol": "600000", "market": "SH",
                                     "quantity": "200", "sellable": "100"}],
                        mapping("account"), account_alias="test-only", observed_at=stamp,
                        trading_date="2024-01-03", source="ths_user_export", cash="9,500.00")
    journal.observe(value)
    assert journal.latest("test-only", "account")["trading_date"] == "2024-01-03"
    with pytest.raises(ValueError, match="stale"):
        from trading.ths.adapter import create_plan
        create_plan(value, "600000.SSE", "buy", "10.00", 100, price_basis="raw_rmb",
                    current=datetime.now(timezone.utc))


def broker_rows():
    order = {"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "000002",
             "qty": "100", "price": "10.00", "filled_qty": "0", "status": "accepted"}
    fill = {k: v for k, v in order.items() if k not in {"filled_qty", "status"}}
    fill["execution_id"] = "000003"
    return order, fill


def cashflow_mapping():
    return {"basis": "cumulative_order_cashflow", "columns": {k: k for k in (
        "symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change")},
        "markets": {"SH": "SSE", "SZ": "SZSE"}, "sides": {"BUY": "buy", "SELL": "sell"}}


def cashflow_row(**changes):
    return {**{"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "000002",
               "gross_amount": "1000.00", "fees": "7.23", "cash_change": "-1007.23"}, **changes}


def observed_cashflow(rows):
    return observation("cashflows", rows, cashflow_mapping(), account_alias="test-only",
                       observed_at=now().isoformat(), trading_date=now().astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                       source="ths_user_export")


def funded_manual_plan(snapshot, profile, side="buy"):
    snapshot = {**snapshot, "cash_total": snapshot["cash_available"], "cash_frozen": 0,
                "cash_basis": "cash_balance_includes_frozen"}
    plan = journal.save_plan(plan_for(snapshot, side=side))
    journal.prefill(plan["id"], PrefillAdapter(FakeWindow(), profile if side == "buy" else {
        **profile, "guards": {**profile["guards"], "side": {**profile["guards"]["side"], "expected": {"sell": "Buy"}}}}))
    return journal.bind(plan["id"], "000002", snapshot["trading_date"])


def test_actual_cash_export_requires_exact_fees_signed_cash_and_balance():
    actual = normalize("cashflows", [cashflow_row()], cashflow_mapping())[0]
    assert actual["fees_cents"] == 723 and actual["cash_change_cents"] == -100723
    for bad in ({"cash_change": "-1007.22"}, {"fees": "NaN"}, {"cash_change": "1e3"}):
        with pytest.raises(ValueError):
            normalize("cashflows", [cashflow_row(**bad)], cashflow_mapping())
    with pytest.raises(ValueError):
        observed("account", [], cash="100.00", cash_total="100.00", cash_frozen="1.00")


def test_cash_and_holdings_readback_uses_broker_fees_not_simulated_estimate(snapshot, profile):
    plan = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    journal.observe(observed("orders", [{**order, "status": "filled", "filled_qty": "100"}]))
    journal.observe(observed("trades", [fill]))
    journal.observe(observed_cashflow([cashflow_row()]))
    account = observed("account", [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}],
                       cash="8992.77", cash_total="8992.77", cash_frozen="0.00")
    journal.observe(account)
    result = journal.refresh(plan["id"])["result"]
    assert result["account_cash_reconciled"] is True and result["account_positions_reconciled"] is True
    receipt = result["account_reconciliation"]
    assert receipt["fees_cents"] == 723 and receipt["cash_residual_cents"] == 0
    assert receipt["cash_basis"] == "cash_balance_includes_frozen"
    assert journal.refresh(plan["id"])["result"]["confirmed_account_reconciliation"] == receipt
    # A cashflow read can be later than the trade query; do not reuse an older balance.
    stale = {**account, "observed_at": result["trades_observed_at"]}
    with pytest.raises(ValueError, match="Refresh account after"):
        journal.save_plan(plan_for(stale))


def test_cash_discrepancy_stops_next_plan_without_overwriting_confirmed_receipt(snapshot, profile):
    plan = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    journal.observe(observed("orders", [{**order, "status": "filled", "filled_qty": "100"}]))
    journal.observe(observed("trades", [fill]))
    journal.observe(observed_cashflow([cashflow_row()]))
    rows = [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}]
    journal.observe(observed("account", rows, cash="8992.77", cash_total="8992.77", cash_frozen="0.00"))
    confirmed = journal.refresh(plan["id"])["result"]["confirmed_account_reconciliation"]
    journal.observe(observed("account", rows, cash="8992.78", cash_total="8992.78", cash_frozen="0.00"))
    changed = journal.refresh(plan["id"])
    assert changed["status"] == "reconciliation_required"
    assert changed["result"]["account_cash_reconciled"] is False
    assert changed["result"]["confirmed_account_reconciliation"] == confirmed
    with pytest.raises(ValueError, match="Unresolved plan"):
        journal.save_plan(plan_for(snapshot))


@pytest.mark.parametrize("side,qty,fees,change,total,sellable,frozen", [
    ("buy", 50, "7.23", "-507.23", "9492.77", "100", "500.00"),
    ("sell", 100, "8.19", "991.81", "10991.81", "0", "0.00")])
def test_partial_frozen_balance_and_sell_cash_are_not_gross_or_available_cash(
        snapshot, profile, side, qty, fees, change, total, sellable, frozen):
    plan = funded_manual_plan(snapshot, profile, side=side)
    order, fill = broker_rows()
    broker_side = "BUY" if side == "buy" else "SELL"
    journal.observe(observed("orders", [{**order, "side": broker_side, "status": "filled" if qty == 100 else "accepted", "filled_qty": str(qty)}]))
    journal.observe(observed("trades", [{**fill, "side": broker_side, "qty": str(qty)}]))
    flow = cashflow_row(side=broker_side, gross_amount="500.00" if qty == 50 else "1000.00", fees=fees, cash_change=change)
    journal.observe(observed_cashflow([flow]))
    available = f"{(money(total) - money(frozen)) / 100:.2f}"
    quantity = 200 + qty if side == "buy" else 200 - qty
    journal.observe(observed("account", [{"symbol": "600000", "market": "SH", "quantity": str(quantity), "sellable": sellable}],
                             cash=available, cash_total=total, cash_frozen=frozen))
    matched = journal.refresh(plan["id"])
    assert matched["status"] == ("observed_filled" if qty == 100 else "observed_partial")
    assert matched["result"]["account_cash_reconciled"] is True
    assert matched["result"]["account_reconciliation"]["after_cash_frozen"] == money(frozen)


@pytest.mark.parametrize("missing", ["cashflow", "account", "cash_basis", "new_account_time"])
def test_missing_actual_evidence_never_invents_net_cash(snapshot, profile, missing):
    plan = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    journal.observe(observed("orders", [{**order, "status": "filled", "filled_qty": "100"}]))
    journal.observe(observed("trades", [fill]))
    if missing != "cashflow":
        journal.observe(observed_cashflow([cashflow_row()]))
    if missing != "account":
        kwargs = {} if missing == "cash_basis" else {"cash_total": "8992.77", "cash_frozen": "0.00"}
        value = observed("account", [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}],
                         cash="8992.77", **kwargs)
        if missing == "new_account_time":
            value["observed_at"] = plan["created_at"]
        journal.observe(value)
    result = journal.refresh(plan["id"])["result"]
    assert result["account_cash_reconciled"] is False
    assert result["account_reconciliation"]["status"] == "pending_evidence"
    assert result["confirmed_account_reconciliation"] is None


@pytest.mark.parametrize("change", ["fee", "missing_flow", "other_holding", "gross"])
def test_changed_statement_or_external_holdings_retain_prior_verified_receipt(snapshot, profile, change):
    plan = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    journal.observe(observed("orders", [{**order, "status": "filled", "filled_qty": "100"}]))
    journal.observe(observed("trades", [fill]))
    journal.observe(observed_cashflow([cashflow_row()]))
    rows = [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}]
    journal.observe(observed("account", rows, cash="8992.77", cash_total="8992.77", cash_frozen="0.00"))
    previous = journal.refresh(plan["id"])["result"]["confirmed_account_reconciliation"]
    cash = "8992.77"
    flow = cashflow_row()
    if change == "fee":
        flow.update(fees="7.24", cash_change="-1007.24")
        cash = "8992.76"
    elif change == "gross":
        flow.update(gross_amount="999.00", cash_change="-1006.23")
        cash = "8993.77"
    elif change == "other_holding":
        rows += [{"symbol": "000001", "market": "SZ", "quantity": "100", "sellable": "100"}]
    journal.observe(observed_cashflow([] if change == "missing_flow" else [flow]))
    journal.observe(observed("account", rows, cash=cash, cash_total=cash, cash_frozen="0.00"))
    changed = journal.refresh(plan["id"])
    assert changed["status"] == "reconciliation_required"
    assert changed["result"]["confirmed_account_reconciliation"] == previous


def test_cashflow_cli_is_export_only_and_imports_explicit_total_balance(tmp_path, capsys):
    import json
    from trading.ths.cli import main, make_parser
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"account_alias": "test-only", "tables": {
        "cashflows": cashflow_mapping(), "account": mapping("account")}}), encoding="utf-8")
    rows = tmp_path / "cashflows.csv"
    fields = list(cashflow_row())
    rows.write_text(",".join(fields) + "\n" + ",".join(cashflow_row()[key] for key in fields) + "\n", encoding="utf-8")
    stamp, day = now().isoformat(), now().astimezone(timezone(timedelta(hours=8))).date().isoformat()
    args = ["import-export", "--profile", str(profile), "--kind", "cashflows", "--csv", str(rows),
            "--observed-at", stamp, "--trading-date", day]
    assert main(args) == 0
    assert journal.latest("test-only", "cashflows")["rows"][0]["fees_cents"] == 723
    with pytest.raises(SystemExit):
        make_parser().parse_args(["capture", "--profile", str(profile), "--kind", "cashflows", "--trading-date", day])
    holdings = tmp_path / "holdings.csv"
    holdings.write_text("symbol,market,quantity,sellable\n600000,SH,300,100\n", encoding="utf-8")
    assert main(["import-export", "--profile", str(profile), "--kind", "account", "--csv", str(holdings),
                 "--observed-at", now().isoformat(), "--trading-date", day, "--cash-rmb", "8992.77",
                 "--cash-total-rmb", "8992.77", "--cash-frozen-rmb", "0.00"]) == 0
    assert journal.latest("test-only", "account")["cash_total"] == 899277
    assert "899277" in capsys.readouterr().out


def test_old_verified_plan_does_not_consume_next_plan_account_interval(snapshot, profile):
    first = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    first_order = {**order, "status": "filled", "filled_qty": "100"}
    journal.observe(observed("orders", [first_order]))
    journal.observe(observed("trades", [fill]))
    journal.observe(observed_cashflow([cashflow_row()]))
    account = observed("account", [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}],
                       cash="8992.77", cash_total="8992.77", cash_frozen="0.00")
    journal.observe(account)
    receipt = journal.refresh(first["id"])["result"]["confirmed_account_reconciliation"]
    second = journal.save_plan(plan_for(account))
    journal.prefill(second["id"], PrefillAdapter(FakeWindow(), profile))
    journal.bind(second["id"], "000005", account["trading_date"])
    journal.observe(observed("orders", [first_order, {**first_order, "order_id": "000005"}]))
    journal.observe(observed("trades", [fill, {**fill, "order_id": "000005", "execution_id": "000006"}]))
    journal.observe(observed_cashflow([cashflow_row(), cashflow_row(order_id="000005")]))
    journal.observe(observed("account", [{"symbol": "600000", "market": "SH", "quantity": "400", "sellable": "100"}],
                             cash="7985.54", cash_total="7985.54", cash_frozen="0.00"))
    assert journal.refresh(second["id"])["result"]["account_cash_reconciled"] is True
    old = journal.refresh(first["id"])
    assert old["status"] == "observed_filled"
    assert old["result"]["confirmed_account_reconciliation"] == receipt
    # Closing a balance interval must not hide a later correction to an old fill.
    journal.observe(observed("trades", [{**fill, "price": "9.99"},
                                       {**fill, "order_id": "000005", "execution_id": "000006"}]))
    conflict = journal.refresh(first["id"])
    assert conflict["status"] == "reconciliation_required"
    assert conflict["result"]["confirmed_account_reconciliation"] == receipt


def test_old_plan_does_not_consume_future_cashflow_evidence(snapshot, profile):
    first = funded_manual_plan(snapshot, profile)
    order, fill = broker_rows()
    journal.observe(observed("orders", [{**order, "status": "filled", "filled_qty": "100"}]))
    journal.observe(observed("trades", [fill]))
    account = observed("account", [{"symbol": "600000", "market": "SH", "quantity": "300", "sellable": "100"}],
                       cash="8992.77", cash_total="8992.77", cash_frozen="0.00")
    journal.observe(account)
    assert journal.refresh(first["id"])["status"] == "observed_filled"

    # The next plan creates the historical boundary. Its cashflow export is
    # intentionally newer than that boundary and must not prove the old plan.
    journal.save_plan(plan_for(account))
    journal.observe(observed_cashflow([cashflow_row()]))
    old = journal.refresh(first["id"])
    assert old["result"]["account_reconciliation"]["status"] == "pending_evidence"
    assert "cashflows_observed_at" not in old["result"]


def test_inspected_account_balance_selectors_are_optional_paired_and_read_only(snapshot, profile):
    profile["tables"] = {"account": mapping("account")}
    profile["views"] = {"account": {"guard": {"selector": {"control_id": 7}, "expected": "Holdings"},
                                    "grid": {"control_id": 8}, "cash_available": {"control_id": 9},
                                    "cash_total": {"control_id": 10}, "cash_frozen": {"control_id": 11}}}
    class QueryWindow(FakeWindow):
        def read_grid(self, selector):
            return [{"symbol": "600000", "market": "SH", "quantity": "200", "sellable": "100"}]
    window = QueryWindow()
    window.values.update({7: "Holdings", 9: "9,500.00", 10: "10,000.00", 11: "500.00"})
    result = THSAdapter(profile, window).read("account", trading_date=snapshot["trading_date"])
    assert result["cash_total"] == 1000000 and result["cash_frozen"] == 50000
    assert not window.writes
    profile["views"]["account"].pop("cash_frozen")
    with pytest.raises(ValueError, match="Both inspected"):
        THSAdapter(profile, window).read("account", trading_date=snapshot["trading_date"])


def test_reconcile_requires_contract_and_actual_fills(snapshot):
    plan = plan_for(snapshot)
    order, fill = broker_rows()
    orders, trades = observed("orders", [order]), observed("trades", [])
    assert reconcile(plan, orders, trades)["status"] == "submission_unknown"
    plan.update(contract_id="000002", trading_date=orders["trading_date"])
    assert reconcile(plan, orders, trades)["status"] == "observed_accepted"
    order.update(filled_qty="100", status="filled")
    orders = observed("orders", [order])
    assert reconcile(plan, orders, trades)["status"] == "reconciliation_required"
    trades = observed("trades", [fill])
    assert reconcile(plan, orders, trades)["status"] == "observed_filled"
    for change in ({"account_alias": "other"}, {"trading_date": "2000-01-01"}):
        with pytest.raises(ValueError):
            reconcile(plan, {**orders, **change}, trades)
    order["price"] = "9.99"
    with pytest.raises(ValueError):
        reconcile(plan, observed("orders", [order]), trades)


def test_journal_manual_binding_partial_then_full(snapshot, profile):
    plan = journal.save_plan(plan_for(snapshot))
    result = journal.prefill(plan["id"], PrefillAdapter(FakeWindow(), profile))
    assert result["status"] == "awaiting_manual_submission"
    journal.bind(plan["id"], "000002", now().astimezone(timezone(timedelta(hours=8))).date().isoformat())
    order, fill = broker_rows()
    order.update(filled_qty="50")
    fill.update(qty="50")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    assert journal.refresh(plan["id"])["status"] == "observed_partial"
    order.update(filled_qty="100", status="filled")
    fill2 = {**fill, "execution_id": "000004"}
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill, fill2]))
    assert journal.refresh(plan["id"])["status"] == "observed_filled"
    assert journal.refresh(plan["id"])["result"]["filled_qty"] == 100


def bound_test_plan(snapshot, profile):
    plan = journal.save_plan(plan_for(snapshot))
    journal.prefill(plan["id"], PrefillAdapter(FakeWindow(), profile))
    return journal.bind(plan["id"], "000002", snapshot["trading_date"])


def test_missing_trade_rows_cannot_erase_previously_confirmed_execution(snapshot, profile):
    plan = bound_test_plan(snapshot, profile)
    order, fill = broker_rows()
    order.update(filled_qty="50")
    fill.update(qty="50")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    assert journal.refresh(plan["id"])["status"] == "observed_partial"
    order.update(filled_qty="0")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", []))
    held = journal.refresh(plan["id"])
    assert held["status"] == "reconciliation_required"
    assert held["result"]["reason"] == "previously_confirmed_execution_missing"
    assert held["result"]["confirmed_executions"][0]["qty"] == 50
    # Repeated clipped views cannot remove evidence or release another plan.
    assert journal.refresh(plan["id"])["result"]["confirmed_executions"] == held["result"]["confirmed_executions"]
    with pytest.raises(ValueError, match="Unresolved"):
        journal.save_plan(plan_for({**snapshot, "observed_at": now().isoformat()}))
    order.update(filled_qty="100", status="filled")
    fill2 = {**fill, "execution_id": "000004"}
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill, fill2]))
    recovered = journal.refresh(plan["id"])
    assert recovered["status"] == "observed_filled"
    assert len(recovered["result"]["confirmed_executions"]) == 2


def test_same_execution_id_changed_economics_hold_instead_of_silent_overwrite(snapshot, profile):
    plan = bound_test_plan(snapshot, profile)
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    first = journal.refresh(plan["id"])
    assert first["status"] == "observed_filled"
    # Both prices obey the buy limit, but an execution ID must not change value.
    journal.observe(observed("trades", [{**fill, "price": "9.99"}]))
    changed = journal.refresh(plan["id"])
    assert changed["status"] == "reconciliation_required"
    assert changed["result"]["reason"] == "previously_confirmed_execution_changed"
    assert changed["result"]["confirmed_executions"] == first["result"]["confirmed_executions"]


def test_reconciliation_keeps_exact_gross_execution_receipt_not_account_cash(snapshot):
    plan = plan_for(snapshot)
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    first = {**fill, "qty": "40", "price": "9.99"}
    second = {**fill, "execution_id": "000004", "qty": "60", "price": "9.98"}
    orders, trades = observed("orders", [order]), observed("trades", [second, first])
    plan.update(contract_id="000002", trading_date=orders["trading_date"])
    result = reconcile(plan, orders, trades)
    assert result["status"] == "observed_filled"
    assert result["gross_executed_cents"] == 99840
    assert result["executions"][0]["execution_id"] == "000003"
    assert [r["notional_cents"] for r in result["executions"]] == [39960, 59880]
    assert result["account_cash_reconciled"] is False
    assert "cash_available" not in result


def test_legacy_id_only_receipt_cannot_be_downgraded_and_gains_economic_evidence():
    legacy = {"status": "observed_partial", "execution_ids": ["000003"], "filled_qty": 50}
    empty = {"status": "observed_accepted", "executions": [], "filled_qty": 0}
    held = retain_execution_evidence(legacy, empty)
    assert held["status"] == "reconciliation_required"
    assert held["confirmed_filled_qty"] == 50 and held["confirmed_execution_ids"] == ["000003"]
    assert held["confirmed_executions"] == []  # No invented prior price/amount.
    execution = {"execution_id": "000003", "qty": 50, "price_cents": 999, "notional_cents": 49950}
    restored = retain_execution_evidence(held, {"status": "observed_partial", "executions": [execution], "filled_qty": 50})
    assert restored["confirmed_executions"] == [execution]
    assert restored["status"] == "observed_partial"


def test_conflicting_order_view_holds_prior_execution_receipt(snapshot, profile):
    plan = bound_test_plan(snapshot, profile)
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    confirmed = journal.refresh(plan["id"])["result"]["confirmed_executions"]
    journal.observe(observed("orders", [{**order, "price": "9.99"}]))
    held = journal.refresh(plan["id"])
    assert held["status"] == "reconciliation_required"
    assert held["result"]["confirmed_executions"] == confirmed
    assert "prefilled plan" in held["result"]["error"]
    assert held["result"]["account_cash_reconciled"] is False


def test_concurrent_refreshes_persist_one_consistent_receipt(snapshot, profile):
    from trading.store import connection
    plan = bound_test_plan(snapshot, profile)
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: journal.refresh(plan["id"]), range(8)))
    assert all(reply["status"] == "observed_filled" for reply in replies)
    assert all(reply["result"]["confirmed_filled_qty"] == 100 for reply in replies)
    assert all(len(reply["result"]["confirmed_executions"]) == 1 for reply in replies)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM ths_plans").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0] == 2


def test_old_account_cannot_be_reused_after_a_completed_order(snapshot, profile):
    plan = journal.save_plan(plan_for(snapshot))
    journal.prefill(plan["id"], PrefillAdapter(FakeWindow(), profile))
    journal.bind(plan["id"], "000002", snapshot["trading_date"])
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    journal.observe(observed("orders", [order]))
    journal.observe(observed("trades", [fill]))
    assert journal.refresh(plan["id"])["status"] == "observed_filled"
    # Still under five minutes old, but predates known cash/position changes.
    with pytest.raises(ValueError, match="Refresh account"):
        journal.save_plan(plan_for(snapshot))
    fresh_account = {**snapshot, "observed_at": now().isoformat(), "cash_available": 899499,
                     "positions": {"600000.SSE": {"quantity": 300, "sellable": 100}}}
    assert journal.save_plan(plan_for(fresh_account))["status"] == "planned"


def test_pre_plan_observations_cannot_confirm_new_contract(snapshot):
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    orders, trades = observed("orders", [order]), observed("trades", [fill])
    orders["observed_at"] = trades["observed_at"] = (now() - timedelta(seconds=1)).isoformat()
    plan = plan_for(snapshot)
    plan.update(contract_id="000002", trading_date=orders["trading_date"])
    with pytest.raises(ValueError, match="predates"):
        reconcile(plan, orders, trades)


def test_rejected_status_with_execution_is_not_terminal_success(snapshot):
    plan = plan_for(snapshot)
    order, fill = broker_rows()
    order.update(filled_qty="100", status="filled")
    orders, trades = observed("orders", [order]), observed("trades", [fill])
    orders["rows"][0]["status"] = "rejected"
    plan.update(contract_id="000002", trading_date=orders["trading_date"])
    assert reconcile(plan, orders, trades)["status"] == "reconciliation_required"


def test_human_close_requires_pause_and_unbound_completed_attempt(snapshot, profile):
    plan = journal.save_plan(plan_for(snapshot))
    journal.prefill(plan["id"], PrefillAdapter(FakeWindow(), profile))
    with pytest.raises(ValueError):
        journal.close_unsubmitted(plan["id"])
    journal.pause(True)
    assert journal.close_unsubmitted(plan["id"])["status"] == "closed_unsubmitted"
    journal.pause(False)
    second = journal.save_plan(plan_for(snapshot))
    journal.claim(second["id"])
    journal.pause(True)
    with pytest.raises(ValueError):
        journal.close_unsubmitted(second["id"])


def test_other_account_cannot_overwrite_unresolved_form(snapshot, profile):
    first = journal.save_plan(plan_for(snapshot))
    journal.prefill(first["id"], PrefillAdapter(FakeWindow(), profile))
    second = journal.save_plan(plan_for({**snapshot, "account_alias": "other"}))
    with pytest.raises(ValueError):
        journal.claim(second["id"])


def test_latest_observation_uses_time_not_import_order():
    current = observed("orders", [])
    older = {**current, "observed_at": (now() - timedelta(minutes=10)).isoformat()}
    journal.observe(current)
    journal.observe(older)
    assert journal.latest("test-only", "orders")["observed_at"] == current["observed_at"]


def test_project_launcher_explicit_arguments(capsys):
    from run_committee import main
    assert main(["ths", "status"]) == 0
    assert '"submission_enabled": false' in capsys.readouterr().out


def test_ths_shared_adapter_prepares_and_prefills_without_submit(snapshot, profile):
    window = FakeWindow()
    broker = THSAdapter(profile, window)
    target = {"mode": "current_preview", "strategy_id": "unit-test", "generated_at": now().isoformat(),
              "symbol": "600000.SSE", "target_qty": 300, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"}
    plan = journal.save_plan(broker.prepare(snapshot, target))
    assert plan["qty"] == 100
    result = journal.prefill(plan["id"], broker)
    assert result["result"]["submitted"] is False
    assert window.writes == [(1, "600000"), (2, "10.00"), (3, "100")]
    before = list(window.writes)
    with pytest.raises(PermissionError, match="disabled"):
        broker.submit(plan)
    with pytest.raises(PermissionError, match="disabled"):
        broker.cancel("000002", day=snapshot["trading_date"])
    assert window.writes == before
    with pytest.raises(ValueError, match="alias"):
        broker.prepare({**snapshot, "account_alias": "other"}, target)


def test_ths_shared_adapter_preserves_failed_attempt_hold(snapshot, profile):
    window = FakeWindow()
    window.bad_readback = True
    plan = journal.save_plan(plan_for(snapshot))
    adapter = THSAdapter(profile, window)
    with pytest.raises(ValueError, match="readback"):
        journal.prefill(plan["id"], adapter)
    assert journal.get(plan["id"])["status"] == "prefill_unknown"
    with pytest.raises(ValueError):
        journal.prefill(plan["id"], adapter)
    assert len(window.writes) == 1


@pytest.mark.parametrize("kind", ["account", "orders", "trades"])
def test_ths_shared_adapter_reads_normalized_actual_view(snapshot, profile, kind):
    profile["tables"] = {kind: mapping(kind)}
    profile["views"] = {kind: {"guard": {"selector": {"control_id": 7}, "expected": kind},
                              "grid": {"control_id": 8}, "cash_available": {"control_id": 9}}}
    order, fill = broker_rows()
    rows = {"account": [{"symbol": "000001", "market": "SZ", "quantity": "200", "sellable": "100"}],
            "orders": [order], "trades": [fill]}

    class QueryWindow(FakeWindow):
        def read_grid(self, selector):
            assert selector["control_id"] == 8
            return rows[kind]

    window = QueryWindow()
    window.values.update({7: kind, 9: "9,500.00"})
    adapter = THSAdapter(profile, window)
    value = adapter.read(kind, trading_date=snapshot["trading_date"])
    assert value["source"] == "ths_controls" and value["observed_at"]
    assert value["rows"]
    if kind == "account":
        assert value["cash_available"] == 950000
        assert value["positions"]["000001.SZSE"]["sellable"] == 100
    else:
        assert value["rows"][0]["price_cents"] == 1000
        assert value["rows"][0]["order_id"] == "000002"
    assert not window.writes
    with pytest.raises(ValueError, match="today"):
        adapter.read(kind, trading_date="2000-01-01")
    window.values[7] = "wrong"
    with pytest.raises(ValueError, match="page"):
        adapter.read(kind, trading_date=snapshot["trading_date"])


def test_ths_read_rejects_local_session_rollover(snapshot, profile, monkeypatch):
    profile["tables"] = {"account": mapping("account")}
    profile["views"] = {"account": {"guard": {"selector": {"control_id": 7}, "expected": "account"},
                                      "grid": {"control_id": 8}, "cash_available": {"control_id": 9}}}

    class QueryWindow(FakeWindow):
        def read_grid(self, selector):
            return [{"symbol": "600000", "market": "SH", "quantity": "200", "sellable": "100"}]

    window = QueryWindow()
    window.values.update({4: "masked-test-account", 7: "account", 9: "9,500.00"})
    start = now()
    later = start + timedelta(days=1)
    stamps = iter((start, later))
    monkeypatch.setattr("trading.ths.broker.now", lambda: next(stamps))
    with pytest.raises(ValueError, match="trading date boundary"):
        THSAdapter(profile, window).read("account", trading_date=start.astimezone(
            timezone(timedelta(hours=8))).date().isoformat())
    assert not window.writes


def test_ths_capabilities_are_not_account_or_ui_evidence(profile, capsys, monkeypatch):
    from trading.ths import cli
    monkeypatch.setattr(journal, "initialize", lambda: pytest.fail("Capability query should not open an account DB"))
    assert cli.main(["capabilities"]) == 0
    text = capsys.readouterr().out
    assert '"submit_enabled": false' in text and '"client_verified_this_call": false' in text
    capabilities = THSAdapter(profile).capabilities
    assert capabilities.prefill_implemented is True and capabilities.submit_enabled is False
    with pytest.raises(ValueError, match="window"):
        THSAdapter(profile).read("account", trading_date=now().astimezone(timezone(timedelta(hours=8))).date().isoformat())


def test_cli_target_uses_ths_shared_adapter(snapshot, tmp_path, capsys, monkeypatch):
    import json
    from trading.ths import cli
    journal.observe({**snapshot, "kind": "account"})
    target = {"mode": "current_preview", "strategy_id": "unit-test", "generated_at": now().isoformat(),
              "symbol": "600000.SSE", "target_qty": 300, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"}
    path = tmp_path / "target.json"
    path.write_text(json.dumps(target), encoding="utf-8")
    calls = []
    prepare = THSAdapter.prepare

    def record(self, observed_account, value, **kwargs):
        calls.append(value["strategy_id"])
        return prepare(self, observed_account, value, **kwargs)

    monkeypatch.setattr(THSAdapter, "prepare", record)
    assert cli.main(["plan-target", "--account", "test-only", "--target", str(path)]) == 0
    assert calls == ["unit-test"]
    assert '"qty": 100' in capsys.readouterr().out


def test_cli_capture_target_prefill_share_adapter_and_existing_journal(snapshot, profile, tmp_path, monkeypatch, capsys):
    import json
    from trading.ths import cli, windows
    profile["target"] = {"test_only": True}
    profile["tables"] = {"account": mapping("account")}
    profile["views"] = {"account": {"guard": {"selector": {"control_id": 7}, "expected": "Holdings"},
                                    "grid": {"control_id": 8}, "cash_available": {"control_id": 9}}}

    class QueryWindow(FakeWindow):
        def read_grid(self, selector):
            return [{"symbol": "600000", "market": "SH", "quantity": "200", "sellable": "100"}]

    window = QueryWindow()
    window.values.update({7: "Holdings", 9: "10,000.00"})
    monkeypatch.setattr(windows, "ControlWindow", lambda target: window)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert cli.main(["capture", "--profile", str(profile_path), "--kind", "account",
                     "--trading-date", snapshot["trading_date"], "--wait-seconds", "0"]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["positions"]["600000.SSE"]["quantity"] == 200 and not window.writes
    target = {"mode": "current_preview", "strategy_id": "unit-test", "generated_at": now().isoformat(),
              "symbol": "600000.SSE", "target_qty": 300, "limit_price_rmb": "10.00", "price_basis": "raw_rmb"}
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    assert cli.main(["plan-target", "--account", "test-only", "--target", str(target_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert cli.main(["prefill", "--id", plan["id"], "--confirm-plan", plan["id"],
                     "--profile", str(profile_path), "--wait-seconds", "0"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "awaiting_manual_submission"
    assert result["result"]["readback"] == {"symbol": "600000", "price": "10.00", "qty": "100"}
    assert window.writes == [(1, "600000"), (2, "10.00"), (3, "100")]
    assert result["result"]["submitted"] is False
    assert cli.main(["prefill", "--id", plan["id"], "--confirm-plan", plan["id"],
                     "--profile", str(profile_path), "--wait-seconds", "0"]) == 2
    assert len(window.writes) == 3


def test_cli_historical_capture_does_not_connect_to_window(profile, tmp_path, monkeypatch):
    import json
    from trading.ths import cli, windows
    profile["tables"] = {"account": mapping("account")}
    profile["target"] = {}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(windows, "ControlWindow", lambda target: pytest.fail("Bad date must stop before connecting"))
    assert cli.main(["capture", "--profile", str(path), "--kind", "account",
                     "--trading-date", "2000-01-01", "--wait-seconds", "0"]) == 2


def test_no_broker_action_or_keyboard_fallback_in_window_adapter():
    base = Path(__file__).parents[1] / "ths"
    prohibited = {"buy", "sell", "cancel", "cancel_all_entrusts", "click", "click_input", "type_keys", "send_keys",
                  "_submit_trade", "_handle_pop_dialogs", "_close_prompt_windows", "login"}
    for name in ("windows.py", "adapter.py", "broker.py", "cli.py"):
        tree = ast.parse((base / name).read_text(encoding="utf-8"))
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert not calls & prohibited
