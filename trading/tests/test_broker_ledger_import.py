"""Isolated broker-observation accounting; never touches a client or real account."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading import settings, store
from trading.config import StrategyConfig
from trading.contracts import BrokerFill, BrokerFillBatch, OrderIntent, Price, PriceBasis, RunContext, SecurityId, ShareQuantity, TradingMode
from trading.ledger import Ledger, new_state
from trading.ths import journal
from trading.ths.observations import reconcile_account

DAY = "2024-01-02"
STAMP = "2024-01-02T10:00:00+08:00"
SYMBOL = "600000.SSE"


def test_observation_bundle_profile_is_discovered_from_bundle_dir(tmp_path):
    from trading.ths.cli import _observation_bundle_profile

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    profile = {"schema": "ths_observation_bundle_profile_v1", "tables": {}}
    (bundle_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

    result = _observation_bundle_profile(SimpleNamespace(bundle_dir=str(bundle_dir), profile=None))

    assert result == profile


def book_and_receipt():
    context = RunContext("broker-test", TradingMode.PAPER)
    state = new_state(10000)
    state.update(day=DAY, broker_cash_frozen=0, broker_binding={
        "run_id": context.account_id, "account_alias": "test-only", "broker_account_kind": "simulated"})
    intent = OrderIntent("order1", context, DAY, SecurityId(SYMBOL), "buy", ShareQuantity(100), Price(1000, PriceBasis.RAW), "manual_plan")
    receipt = BrokerFillBatch(intent, "test-only", "000002", (BrokerFill("000003", ShareQuantity(50), Price(999, PriceBasis.RAW)),),
        723, "partial", "2024-01-03", 949327, 50000, ((SYMBOL, 50, 0),), ((SYMBOL, 1000),), STAMP, "actual-export-test")
    return Ledger(state, StrategyConfig(initial_cash=10000), context), receipt


def test_receipt_roundtrip_and_restart_idempotency():
    book, receipt = book_and_receipt()
    assert book.apply_broker_receipt(receipt)
    restarted = Ledger(json.loads(json.dumps(book.state)), book.config, book.context)
    assert not restarted.apply_broker_receipt(BrokerFillBatch.from_dict(json.loads(json.dumps(receipt.to_dict()))))
    assert restarted.state["cash"] == 949327
    assert restarted.state["orders"]["order1"]["fees"] == 723
    assert next(iter(restarted.state["fills"].values()))["fees"] is None


def test_actual_fees_partial_then_full_and_atomic_failure():
    book, receipt = book_and_receipt()
    book.apply_broker_receipt(receipt)
    full = replace(receipt, fills=receipt.fills + (BrokerFill("000004", ShareQuantity(50), Price(998, PriceBasis.RAW)),),
        fees_cents=819, status="filled", cash_total=899331, cash_frozen=0, positions=((SYMBOL, 100, 0),))
    before = deepcopy(book.state)
    with pytest.raises(ValueError, match="cash/holdings"):
        book.apply_broker_receipt(replace(full, cash_total=full.cash_total + 1))
    assert book.state == before
    assert book.apply_broker_receipt(full)
    assert book.state["cash"] == 899331 and len(book.state["fills"]) == 2
    assert book.state["orders"]["order1"]["fees"] == 819
    assert not book.apply_broker_receipt(full)
    with pytest.raises(ValueError, match="fees changed"):
        book.apply_broker_receipt(replace(full, fees_cents=820))


def test_partial_sell_readback_excludes_remaining_broker_reservation():
    before = {"source": "ths_user_export", "account_alias": "test-only", "observed_at": STAMP,
        "trading_date": DAY, "cash_available": 1000000, "cash_total": 1000000, "cash_frozen": 0,
        "cash_basis": "cash_balance_includes_frozen", "positions": {SYMBOL: {"quantity": 200, "sellable": 100}}}
    plan = {"snapshot": before, "account_alias": "test-only", "trading_date": DAY, "created_at": STAMP,
        "symbol": SYMBOL, "side": "sell", "qty": 100, "contract_id": "000002"}
    result = {"status": "observed_partial", "filled_qty": 50, "gross_executed_cents": 50000,
        "orders_observed_at": STAMP, "trades_observed_at": STAMP, "execution_ids": ["000003"]}
    account = {**before, "kind": "account", "cash_available": 1049181, "cash_total": 1049181,
        "positions": {SYMBOL: {"quantity": 150, "sellable": 0}}}
    flow = {"kind": "cashflows", "account_alias": "test-only", "trading_date": DAY, "observed_at": STAMP,
        "source": "ths_user_export", "basis": "cumulative_order_cashflow", "rows": [{"order_id": "000002",
        "symbol": SYMBOL, "side": "sell", "gross_cents": 50000, "fees_cents": 819, "cash_change_cents": 49181}]}
    assert reconcile_account(plan, result, account, flow)["account_positions_reconciled"]


def test_bound_account_rejects_simulator_and_wrong_context():
    book, receipt = book_and_receipt()
    from trading.engine import advance
    from trading.simulated_broker import SimulatedBrokerAdapter
    with pytest.raises(PermissionError):
        book.submit_intent(receipt.order)
    with pytest.raises(PermissionError):
        book.request_cancel("unknown", DAY)
    with pytest.raises(PermissionError):
        advance(book.state, None, DAY, book.config, 0)
    with pytest.raises(PermissionError):
        SimulatedBrokerAdapter(book)
    with pytest.raises(ValueError):
        replace(receipt, broker_account_kind="real")
    with pytest.raises(ValueError):
        replace(receipt, order=replace(receipt.order, context=RunContext("broker-test", TradingMode.REPLAY)))


@pytest.fixture
def persisted(tmp_path, monkeypatch):
    from trading.ths import adapter, cli, observations
    current = datetime.fromisoformat(STAMP)
    monkeypatch.setattr(adapter, "now", lambda: current + timedelta(seconds=20))
    monkeypatch.setattr(observations, "now", lambda: current + timedelta(seconds=20))
    monkeypatch.setattr(cli, "now", lambda: current + timedelta(seconds=20))
    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.sqlite3")
    store.initialize()
    journal.initialize()
    dataset = tmp_path / "snapshot"
    dataset.mkdir()
    monkeypatch.setattr(settings, "DATASET", dataset)
    (dataset / "manifest.json").write_text(json.dumps({"status": "ready", "folder": str(dataset),
        "start": DAY, "end": "2024-01-03", "symbols": [SYMBOL], "rows": 0}), encoding="utf-8")
    (dataset / "calendar.csv").write_text("date\n2024-01-02\n2024-01-03\n", encoding="utf-8")
    run = store.create("broker integration fixture", {"initial_cash": 12000}, DAY, "2024-01-03", "test-request", mode="paper", dataset=dataset)
    store.control(run["id"], "pause")
    journal.observe(export_observation("orders", [], 0))
    journal.observe(export_observation("account", [holding_row(200, 100)], 1, cash="10000.00"))
    return run["id"], dataset


def holding_row(total, sellable):
    return {"symbol": "600000", "market": "SH", "quantity": str(total), "sellable": str(sellable), "price": "10.00"}


def export_mapping(kind):
    fields = {"account": "symbol market quantity sellable price",
        "orders": "symbol market side order_id qty price filled_qty status",
        "trades": "symbol market side order_id execution_id qty price",
        "cashflows": "symbol market side order_id gross_amount fees cash_change"}[kind].split()
    return {"columns": {k: k for k in fields}, "markets": {"SH": "SSE"}, "sides": {"BUY": "buy", "SELL": "sell"},
        "statuses": {k: k for k in ("accepted", "partial", "filled", "cancelled", "rejected")},
        "price_basis": "raw_rmb", "basis": "cumulative_order_cashflow"}


def export_observation(kind, rows, second, *, cash=None, frozen="0.00"):
    from trading.ths.observations import observation
    from trading.ths.adapter import money
    kwargs = {}
    if kind == "account":
        kwargs = {"cash": f"{(money(cash) - money(frozen))/100:.2f}", "cash_total": cash, "cash_frozen": frozen}
    return observation(kind, rows, export_mapping(kind), account_alias="test-only", source="ths_user_export",
        trading_date=DAY, observed_at=(datetime.fromisoformat(STAMP) + timedelta(seconds=second)).isoformat(), **kwargs)


def manual_plan(side="buy", contract="000002", second=2):
    from trading.ths.adapter import create_plan
    account = journal.latest("test-only", "account")
    plan = create_plan(account, SYMBOL, side, "10.00", 100, price_basis="raw_rmb",
        current=datetime.fromisoformat(STAMP) + timedelta(seconds=second))
    # This fixture records a human submission; it performs zero native window writes.
    journal.save_plan(plan)
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET status='awaiting_manual_submission' WHERE id=?", (plan["id"],))
    return journal.bind(plan["id"], contract, DAY)


def receipt_views(plan, *, filled=50, status="partial", second=3):
    side = "BUY" if plan["side"] == "buy" else "SELL"
    order = {"symbol": "600000", "market": "SH", "side": side, "order_id": plan["contract_id"], "qty": "100",
        "price": "10.00", "filled_qty": str(filled), "status": status}
    fills = [{k: order[k] for k in ("symbol", "market", "side", "order_id", "price")} | {"qty": "50", "execution_id": "000003"}]
    if filled == 100:
        fills.append({**fills[0], "execution_id": "000004"})
    if filled == 0:
        fills = []
    before = plan["snapshot"]
    change = filled * 1000 * (1 if plan["side"] == "sell" else -1) - 723
    p = before["positions"][SYMBOL]
    total = p["quantity"] + filled * (1 if plan["side"] == "buy" else -1)
    sellable = p["sellable"] - filled if plan["side"] == "sell" else p["sellable"]
    if plan["side"] == "sell" and status == "partial":
        sellable -= 100 - filled
    frozen = "500.00" if plan["side"] == "buy" and status == "partial" else "0.00"
    journal.observe(export_observation("orders", [order], second))
    journal.observe(export_observation("trades", fills, second + 1))
    journal.observe(export_observation("cashflows", [{"symbol": "600000", "market": "SH", "side": side,
        "order_id": plan["contract_id"], "gross_amount": f"{filled * 10:.2f}", "fees": "7.23",
        "cash_change": f"{change/100:.2f}"}], second + 2))
    journal.observe(export_observation("account", [holding_row(total, sellable)] if total else [], second + 3,
        cash=f"{(before['cash_total'] + change)/100:.2f}", frozen=frozen))


def test_persistent_cli_binding_receipt_restart_and_concurrent_dedup(persisted, capsys):
    from trading.ths.cli import main
    from trading.ths.ledger_import import import_plan
    from trading.report import detail
    run_id, _ = persisted
    assert main(["bind-ledger", "--run-id", run_id, "--account", "test-only", "--confirm-simulated-account"]) == 0
    capsys.readouterr()
    opened = detail(run_id)
    assert opened["equity"] == 12000 and opened["cash"] == 10000 and opened["comparison"]["metrics"] is None
    assert opened["execution_source"] == "broker_observation"
    plan = manual_plan()
    receipt_views(plan)
    assert main(["import-ledger", "--run-id", run_id, "--id", plan["id"]]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["cash_total_cents"] == 949277 and imported["changed"]
    state = store.get(run_id, full=True)["state"]
    assert state["orders"][plan["id"]]["fees"] == 723
    assert len(state["fills"]) == 1 and next(iter(state["fills"].values()))["fees"] is None
    assert detail(run_id)["accounted_fees"] == 7.23
    import os
    evidence_dir = os.environ.get("QUANT_THS_TEST_EVIDENCE")
    if evidence_dir:
        from pathlib import Path
        from trading.api import overview
        folder = Path(evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ui_fixture.json").write_text(json.dumps({"scope": "isolated_synthetic_export_not_native",
            "overview": overview(), "detail": detail(run_id)}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert journal.get(plan["id"])["result"]["ledger_import"]["run_id"] == run_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: import_plan(run_id, plan["id"]), range(2)))
    assert all(not r["changed"] for r in results)
    assert store.get(run_id, full=True)["state"] == state
    receipt_views(plan, filled=100, status="filled", second=7)
    assert import_plan(run_id, plan["id"])["changed"]
    assert store.get(run_id, full=True)["state"]["cash"] == 899277
    assert detail(run_id)["available_cash"] == 8992.77
    assert store.daily(run_id) == [] and store.get(run_id)["cursor"] == 0
    assert store.get(run_id)["status"] == "paused"
    with pytest.raises(PermissionError):
        store.control(run_id, "resume")
    assert journal.refresh(plan["id"])["result"]["ledger_import"]["ledger_imported"] is True


def _preview_persistence_snapshot(run_id):
    with store.connection() as db:
        return {
            table: [tuple(row) for row in db.execute(
                f"SELECT * FROM {table} ORDER BY 1", ()
            ).fetchall()]
            for table in ("runs", "ths_plans", "ths_observations")
        }


def test_ledger_import_preview_reports_exact_deltas_without_mutation(persisted, tmp_path, capsys):
    from trading.ths.cli import main
    from trading.ths.ledger_import import bind_account, import_plan, preview_plan

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    before = _preview_persistence_snapshot(run_id)

    preview = preview_plan(run_id, plan["id"])
    assert preview["status"] == "ready_for_import"
    assert preview["would_change"] is True
    assert preview["changes"]["cash_total_cents"] == -50723
    assert preview["changes"]["cash_frozen_cents"] == 50000
    assert preview["changes"]["fees_cents"] == 723
    assert preview["changes"]["positions"][0]["quantity_delta"] == 50
    assert preview["ledger_mutated"] is False
    assert _preview_persistence_snapshot(run_id) == before

    out = tmp_path / "ledger-preview.json"
    assert main(["preview-ledger-import", "--run-id", run_id, "--id", plan["id"], "--out", str(out)]) == 0
    cli_preview = json.loads(out.read_text(encoding="utf-8"))
    assert cli_preview["status"] == "ready_for_import"
    assert _preview_persistence_snapshot(run_id) == before
    capsys.readouterr()

    imported = import_plan(run_id, plan["id"])
    assert imported["changed"] is True
    after_import = _preview_persistence_snapshot(run_id)
    replay = preview_plan(run_id, plan["id"])
    assert replay["status"] == "already_imported_no_change"
    assert replay["would_change"] is False
    assert _preview_persistence_snapshot(run_id) == after_import
    evidence_dir = __import__("os").environ.get("QUANT_THS_PREVIEW_EVIDENCE")
    if evidence_dir:
        evidence = Path(evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "ledger_import_preview_rehearsal.json").write_text(json.dumps({
            "scope": "isolated_synthetic_export_not_native",
            "preview": preview,
            "cli_preview": cli_preview,
            "import": imported,
            "repreview": replay,
            "persistence_unchanged_during_preview": True,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_ledger_import_preview_can_stage_a_valid_observation_bundle_and_roll_it_back(persisted, tmp_path):
    from trading.ths.bundle_export import export_observations
    from trading.ths.cli import main
    from trading.ths.ledger_import import bind_account

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")
    bundle_root = tmp_path / "bundle"
    values = [journal.latest("test-only", kind, DAY) for kind in ("account", "orders", "trades", "cashflows")]
    export_observations(values, bundle_root)
    profile_source = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    profile = bundle_root / "profile.json"
    profile.write_text(profile_source.read_text(encoding="utf-8"), encoding="utf-8")
    before = _preview_persistence_snapshot(run_id)
    out = tmp_path / "bundle-ledger-preview.json"

    assert main(["preview-ledger-import", "--run-id", run_id, "--id", plan["id"],
                 "--bundle-dir", str(bundle_root), "--profile", str(profile), "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "ready_for_import"
    assert result["bundle_preview"]["status"] == "bundle_ready_for_reconciliation"
    assert set(result["staged_observations"]["inserted_kinds"]) == {"orders", "trades", "cashflows"}
    assert result["staged_observations"]["already_present_kinds"] == ["account"]
    assert _preview_persistence_snapshot(run_id) == before


def test_ledger_import_preview_api_is_read_only(persisted):
    from trading.api import ThsSimulatedLedgerPreviewInput, preview_ths_simulated_ledger_import
    from trading.ths.ledger_import import bind_account

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    before = _preview_persistence_snapshot(run_id)
    result = preview_ths_simulated_ledger_import(
        ThsSimulatedLedgerPreviewInput(run_id=run_id, plan_id=plan["id"])
    )
    assert result["status"] == "ready_for_import"
    assert result["changes"]["fees_cents"] == 723
    assert result["ledger_mutated"] is False
    assert _preview_persistence_snapshot(run_id) == before


def test_ledger_import_preview_rejects_bound_account_mismatch(persisted):
    from trading.ths.ledger_import import bind_account, preview_plan

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    with store.connection() as db:
        state = json.loads(db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        state["broker_binding"]["account_alias"] = "other-account"
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))

    preview = preview_plan(run_id, plan["id"])
    assert preview["status"] == "blocked"
    assert "bound simulated broker account" in preview["error"]
    assert preview["ledger_mutated"] is False


def test_ledger_import_preview_rejects_cash_conflict_without_mutation(persisted):
    from trading.ths.ledger_import import bind_account, preview_plan

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    observations = [journal.latest("test-only", kind, DAY)
                    for kind in ("account", "orders", "trades", "cashflows")]
    account = next(value for value in observations if value["kind"] == "account")
    account = {**account, "cash_total": account["cash_total"] + 1}
    observations = [account if value["kind"] == "account" else value for value in observations]
    before = _preview_persistence_snapshot(run_id)

    preview = preview_plan(run_id, plan["id"], observations=observations)
    assert preview["status"] == "blocked"
    assert "Conflicting observation" in preview["error"]
    assert preview["ledger_mutated"] is False
    assert _preview_persistence_snapshot(run_id) == before


def test_persistent_sell_partial_cancel_releases_reservation(persisted):
    from trading.ths.ledger_import import bind_account, import_plan
    from trading.report import detail
    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan(side="sell")
    receipt_views(plan)
    import_plan(run_id, plan["id"])
    assert detail(run_id)["holdings"][0]["sellable"] == 0
    receipt_views(plan, status="cancelled", second=7)
    import_plan(run_id, plan["id"])
    assert detail(run_id)["holdings"][0]["sellable"] == 50
    assert store.get(run_id, full=True)["state"]["cash"] == 1049277
    assert len(store.get(run_id, full=True)["state"]["fills"]) == 1


@pytest.mark.parametrize("bad", ["attestation", "marks", "active", "capital", "calendar", "mode"])
def test_binding_rejects_unverified_or_incompatible_baseline(persisted, bad):
    from trading.ths.ledger_import import bind_account
    run_id, dataset = persisted
    if bad == "marks":
        account = journal.latest("test-only", "account")
        account.pop("marks")
        account["observed_at"] = (datetime.fromisoformat(STAMP) + timedelta(seconds=2)).isoformat()
        journal.observe(account)
    elif bad == "active":
        journal.observe(export_observation("orders", [{"symbol": "600000", "market": "SH", "side": "BUY",
            "order_id": "unowned", "qty": "100", "price": "10.00", "filled_qty": "0", "status": "accepted"}], 0))
    elif bad == "calendar":
        (dataset / "calendar.csv").write_text("date\n2024-01-02\n", encoding="utf-8")
    elif bad in {"capital", "mode"}:
        with store.connection() as db:
            if bad == "mode":
                db.execute("UPDATE runs SET mode='replay' WHERE id=?", (run_id,))
            else:
                config = store.get(run_id)["config"]
                config["initial_cash"] = 13000
                db.execute("UPDATE runs SET config=? WHERE id=?", (json.dumps(config), run_id))
    before = store.get(run_id, full=True)["state"]
    with pytest.raises((ValueError, PermissionError)):
        bind_account(run_id, "test-only", confirm_simulated_account=bad != "attestation")
    assert store.get(run_id, full=True)["state"] == before


@pytest.mark.parametrize("bad", ["cash", "marks", "fee_revision", "prefix", "calendar", "alias", "missing_execution"])
def test_import_conflict_keeps_existing_account_unchanged(persisted, bad):
    from trading.ths.ledger_import import bind_account, import_plan
    run_id, dataset = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    import_plan(run_id, plan["id"])
    if bad in {"cash", "marks"}:
        account = journal.latest("test-only", "account")
        if bad == "cash":
            account["cash_total"] += 1
            account["cash_available"] += 1
        else:
            account.pop("marks")
        account["observed_at"] = (datetime.fromisoformat(STAMP) + timedelta(seconds=9)).isoformat()
        journal.observe(account)
    elif bad == "fee_revision":
        flow = journal.latest("test-only", "cashflows")
        flow["rows"][0]["fees_cents"] += 1
        flow["rows"][0]["cash_change_cents"] -= 1
        flow["observed_at"] = (datetime.fromisoformat(STAMP) + timedelta(seconds=9)).isoformat()
        journal.observe(flow)
    elif bad == "missing_execution":
        trades = journal.latest("test-only", "trades")
        trades.update(rows=[], observed_at=(datetime.fromisoformat(STAMP) + timedelta(seconds=9)).isoformat())
        journal.observe(trades)
    elif bad == "calendar":
        (dataset / "calendar.csv").write_text("date\n2024-01-02\n2024-01-04\n", encoding="utf-8")
    elif bad in {"prefix", "alias"}:
        with store.connection() as db:
            state = store.get(run_id, full=True)["state"]
            if bad == "prefix":
                state["cash"] -= 1
            else:
                state["broker_binding"]["account_alias"] = "another-account"
            db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))
        if bad == "prefix":
            receipt_views(plan, filled=100, status="filled", second=7)
    before = store.get(run_id, full=True)["state"]
    with pytest.raises((ValueError, PermissionError)):
        import_plan(run_id, plan["id"])
    assert store.get(run_id, full=True)["state"] == before
    if bad != "alias":
        assert journal.get(plan["id"])["status"] == "reconciliation_required"
        assert journal.get(plan["id"])["result"]["ledger_import"]["ledger_imported"] is True


def test_old_duplicate_after_following_plan_does_not_replay_cash(persisted):
    from trading.ths.ledger_import import bind_account, import_plan
    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    first = manual_plan()
    receipt_views(first, filled=100, status="filled")
    import_plan(run_id, first["id"])
    first_views = {kind: journal.latest("test-only", kind) for kind in ("orders", "trades", "cashflows")}
    # A second buy would intentionally exceed the 25% single-position policy;
    # use a risk-reducing sell so this test remains about duplicate cash, not
    # bypassing the account-level order gate.
    second = manual_plan(side="sell", contract="000005", second=7)
    receipt_views(second, filled=100, status="filled", second=8)
    for kind, previous in first_views.items():
        cumulative = journal.latest("test-only", kind)
        cumulative["rows"] = previous["rows"] + cumulative["rows"]
        journal.observe(cumulative)
    import_plan(run_id, second["id"])
    state = store.get(run_id, full=True)["state"]
    assert state["cash"] == 998554
    assert not import_plan(run_id, first["id"])["changed"]
    assert store.get(run_id, full=True)["state"] == state
    assert len(state["fills"]) == 4


def test_original_cli_csv_exports_are_consumed_by_the_same_ledger(persisted, tmp_path, capsys):
    from trading.ths.cli import main
    from trading.ths.ledger_import import bind_account, import_plan
    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")
    profile = tmp_path / "explicit-export-profile.json"
    profile.write_text(json.dumps({"account_alias": "test-only", "tables": {k: export_mapping(k)
        for k in ("account", "orders", "trades", "cashflows")}}), encoding="utf-8")
    raw_rows = {
        "orders": [{"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "000002", "qty": "100", "price": "10.00", "filled_qty": "100", "status": "filled"}],
        "trades": [{"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "000002", "execution_id": "000003", "qty": "100", "price": "10.00"}],
        "cashflows": [{"symbol": "600000", "market": "SH", "side": "BUY", "order_id": "000002", "gross_amount": "1000.00", "fees": "7.23", "cash_change": "-1007.23"}],
        "account": [holding_row(300, 100)]}
    import csv
    for second, (kind, rows) in enumerate(raw_rows.items(), 7):
        path = tmp_path / f"{kind}.csv"
        fields = list(export_mapping(kind)["columns"])
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        args = ["import-export", "--profile", str(profile), "--kind", kind, "--csv", str(path), "--trading-date", DAY,
                "--observed-at", (datetime.fromisoformat(STAMP) + timedelta(seconds=second)).isoformat()]
        if kind == "account":
            args += ["--cash-rmb", "8992.77", "--cash-total-rmb", "8992.77", "--cash-frozen-rmb", "0.00"]
        assert main(args) == 0
        capsys.readouterr()
    assert import_plan(run_id, plan["id"])["cash_total_cents"] == 899277
    assert next(iter(store.get(run_id, full=True)["state"]["fills"])) == "ths:2024-01-02:000002:000003"


def test_observation_bundle_api_replays_into_same_ledger_idempotently(persisted):
    from trading.api import ThsObservationBundleImportInput, import_ths_observation_bundle
    from trading.ths.bundle_export import export_observations
    from trading.ths.ledger_import import bind_account

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")

    bundle_root = settings.DATA_ROOT / "ths" / "observation_bundle"
    values = [journal.latest("test-only", kind, DAY) for kind in ("account", "orders", "trades", "cashflows")]
    export_observations(values, bundle_root)
    profile_source = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    # The test runs in an isolated data root but uses the real project profile contract.
    (bundle_root / "profile.json").write_text(profile_source.read_text(encoding="utf-8"), encoding="utf-8")

    request = ThsObservationBundleImportInput(confirm_import=True, plan_id=plan["id"], run_id=run_id,
                                               confirm_ledger_import=True)
    first = import_ths_observation_bundle(request)
    assert first["status"] == "imported"
    assert first["ledger_imported"] is True
    assert first["ledger_mutated"] is True
    assert first["idempotent_replay"] is False
    assert first["ledger_preview"]["status"] == "ready_for_import"
    assert first["same_ledger_bundle_reconciliation"]["status"] == "ledger_reconciled"
    assert first["same_ledger_bundle_reconciliation"]["summary"]["matched_count"] == 1

    second = import_ths_observation_bundle(request)
    assert second["idempotent_replay"] is True
    assert second["ledger_imported"] is True
    assert second["ledger_mutated"] is False
    assert second["ledger_preview"]["status"] == "already_imported_no_change"
    assert second["same_ledger_bundle_reconciliation"]["status"] == "ledger_reconciled"


def test_observation_bundle_api_ingests_explicit_statement_views_to_same_journal(persisted, tmp_path):
    from trading.api import ThsObservationBundleImportInput, import_ths_observation_bundle
    from trading.ths.bundle_export import export_observations

    _run_id, _ = persisted
    plan = manual_plan()
    receipt_views(plan, filled=0, status="cancelled")
    bundle_root = settings.DATA_ROOT / "ths" / "observation_bundle"
    values = [journal.latest("test-only", kind, DAY)
              for kind in ("account", "orders", "trades", "cashflows")]
    export_observations(values, bundle_root)
    profile = json.loads((Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json").read_text())
    (bundle_root / "profile.json").write_text(json.dumps(profile), encoding="utf-8")

    import csv
    statement_path = bundle_root / "movements.csv"
    with statement_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["movement_id", "kind", "cash_change"])
        writer.writeheader()
        writer.writerow({"movement_id": "M-API-1", "kind": "interest", "cash_change": "1.25"})

    result = import_ths_observation_bundle(ThsObservationBundleImportInput(
        confirm_import=True,
        statement_kinds=["movements"],
    ))

    assert result["status"] == "imported"
    assert result["statement_observations"]["rows"] == {"movements": 1}
    assert result["statement_observations"]["ledger_mutated"] is False
    assert journal.latest("test-only", "movements", DAY)["rows"][0]["cash_change_cents"] == 125


def test_cli_bundle_import_ingests_selected_statement_views(persisted, tmp_path, capsys):
    from trading.ths.bundle_export import export_observations
    from trading.ths.cli import main

    plan = manual_plan()
    receipt_views(plan, filled=0, status="cancelled")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    values = [journal.latest("test-only", kind, DAY)
              for kind in ("account", "orders", "trades", "cashflows")]
    export_observations(values, bundle_dir)
    profile_path = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    statement_path = bundle_dir / "movements.csv"
    statement_path.write_text(
        "movement_id,kind,cash_change\nM-CLI-1,interest,2.00\n", encoding="utf-8"
    )
    output_path = tmp_path / "cli-result.json"

    assert main([
        "import-observation-bundle", "--bundle-dir", str(bundle_dir),
        "--profile", str(profile_path), "--confirm-import",
        "--statement-kinds", "movements", "--out", str(output_path),
    ]) == 0
    capsys.readouterr()

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["statement_observations"]["rows"] == {"movements": 1}
    assert journal.latest("test-only", "movements", DAY)["rows"][0]["cash_change_cents"] == 200


def test_atomic_bundle_import_rolls_back_staged_views_when_account_risk_blocks(persisted):
    from trading.ths.ledger_import import bind_account, import_observation_bundle

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")
    observations = [journal.latest("test-only", kind, DAY)
                    for kind in ("account", "orders", "trades", "cashflows")]

    # Inflate the saved order after the human-bound contract exists.  The risk
    # gate must reject before refresh/apply, while the surrounding transaction
    # must also remove any staged observation writes.
    with store.connection() as db:
        payload = json.loads(db.execute(
            "SELECT payload FROM ths_plans WHERE id=?", (plan["id"],)
        ).fetchone()[0])
        payload["qty"] = 1000
        db.execute("UPDATE ths_plans SET payload=? WHERE id=?", (json.dumps(payload), plan["id"]))
    before = _preview_persistence_snapshot(run_id)

    with pytest.raises(ValueError, match="Account order risk gate blocked"):
        import_observation_bundle(run_id, plan["id"], observations, confirm=True)

    assert _preview_persistence_snapshot(run_id) == before


def test_observation_bundle_api_blocks_before_journal_write_on_bound_account_mismatch(persisted):
    from fastapi import HTTPException
    from trading.api import ThsObservationBundleImportInput, import_ths_observation_bundle
    from trading.ths.bundle_export import export_observations
    from trading.ths.ledger_import import bind_account

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan, filled=100, status="filled")

    bundle_root = settings.DATA_ROOT / "ths" / "observation_bundle"
    values = [journal.latest("test-only", kind, DAY) for kind in ("account", "orders", "trades", "cashflows")]
    export_observations(values, bundle_root)
    profile_source = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    (bundle_root / "profile.json").write_text(profile_source.read_text(encoding="utf-8"), encoding="utf-8")

    with store.connection() as db:
        row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        state = json.loads(row[0])
        state["broker_binding"]["account_alias"] = "different-account"
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))
        before = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]

    request = ThsObservationBundleImportInput(confirm_import=True, plan_id=plan["id"], run_id=run_id,
                                               confirm_ledger_import=True)
    with pytest.raises(HTTPException) as error:
        import_ths_observation_bundle(request)
    assert error.value.status_code == 409
    assert error.value.detail["status"] == "ledger_preview_blocked"
    with store.connection() as db:
        after = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]
    assert after == before


def test_observation_bundle_api_checks_plan_binding_without_run_id(persisted):
    from fastapi import HTTPException
    from trading.api import ThsObservationBundleImportInput, import_ths_observation_bundle
    from trading.ths.bundle_export import export_observations

    run_id, _ = persisted
    from trading.ths.ledger_import import bind_account
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    values = [journal.latest("test-only", kind, DAY) for kind in ("account", "orders", "trades", "cashflows")]
    mismatched = [{**value, "account_alias": "different-account"} for value in values]

    bundle_root = settings.DATA_ROOT / "ths" / "observation_bundle"
    export_observations(mismatched, bundle_root)
    profile_source = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    (bundle_root / "profile.json").write_text(profile_source.read_text(encoding="utf-8"), encoding="utf-8")
    with store.connection() as db:
        before = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]

    with pytest.raises(HTTPException) as error:
        import_ths_observation_bundle(
            ThsObservationBundleImportInput(confirm_import=True, plan_id=plan["id"])
        )

    assert error.value.status_code == 409
    assert error.value.detail["status"] == "plan_observation_binding_mismatch"
    with store.connection() as db:
        after = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]
    assert after == before


def test_cli_reconcile_checks_plan_binding_before_journal_write(persisted, tmp_path):
    from trading.ths.bundle_export import export_observations
    from trading.ths.cli import main
    from trading.ths.ledger_import import bind_account

    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    values = [journal.latest("test-only", kind, DAY) for kind in ("account", "orders", "trades", "cashflows")]
    mismatched = [{**value, "account_alias": "different-account"} for value in values]
    bundle_dir = tmp_path / "bundle"
    export_observations(mismatched, bundle_dir)
    profile_source = Path(__file__).parents[1] / "ths" / "observation_bundle_profile.example.json"
    (bundle_dir / "profile.json").write_text(profile_source.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "result.json"
    with store.connection() as db:
        before = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]

    assert main([
        "reconcile-observation-bundle", "--id", plan["id"],
        "--bundle-dir", str(bundle_dir), "--confirm-import", "--out", str(out),
    ]) == 2

    with store.connection() as db:
        after = db.execute("SELECT COUNT(*) FROM ths_observations").fetchone()[0]
    assert after == before


def test_blank_observation_templates_can_refresh_but_partial_bundle_cannot(persisted):
    from fastapi import HTTPException
    from trading.api import ThsObservationBundleInitializeInput, initialize_ths_observation_bundle

    source_root = settings.DATA_ROOT / "research" / "opening_session_runs" / "2024-01-02"
    source_root.mkdir(parents=True)
    account_template = {
        "schema": "ths_account_observation_v1", "source": "ths_user_export",
        "account_alias": "<fill-from-user-selected-account>",
        "observed_at": "<fill-with-actual-observation-time+08:00>",
        "trading_date": DAY, "cash_basis": "cash_balance_includes_frozen",
        "cash_total": None, "cash_frozen": None, "cash_available": None, "positions": [],
    }
    (source_root / "account_observation.template.json").write_text(
        json.dumps(account_template), encoding="utf-8"
    )
    for name, header in (
        ("orders_observation.template.csv", "symbol,market,side,order_id,qty,price,filled_qty,status\n"),
        ("trades_observation.template.csv", "symbol,market,side,order_id,execution_id,qty,price\n"),
        ("cashflows_observation.template.csv", "symbol,market,side,order_id,gross_amount,fees,cash_change\n"),
    ):
        (source_root / name).write_text(header, encoding="utf-8")

    first = initialize_ths_observation_bundle(ThsObservationBundleInitializeInput(confirm_initialize=True))
    assert first["status"] == "blank_templates_initialized"
    assert first["template_refreshed"] is False

    account_template["trading_date"] = "2024-01-03"
    (source_root / "account_observation.template.json").write_text(
        json.dumps(account_template), encoding="utf-8"
    )
    refreshed = initialize_ths_observation_bundle(ThsObservationBundleInitializeInput(confirm_initialize=True))
    assert refreshed["status"] == "blank_templates_refreshed"
    assert refreshed["template_refreshed"] is True
    target = settings.DATA_ROOT / "ths" / "observation_bundle" / "account_observation.json"
    assert json.loads(target.read_text(encoding="utf-8"))["trading_date"] == "2024-01-03"

    filled = json.loads(target.read_text(encoding="utf-8"))
    filled["account_alias"] = "sim"
    filled["observed_at"] = "2024-01-03T09:30:00+08:00"
    filled["cash_total"] = 100000
    filled["cash_frozen"] = 0
    filled["cash_available"] = 100000
    target.write_text(json.dumps(filled), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        initialize_ths_observation_bundle(ThsObservationBundleInitializeInput(confirm_initialize=True))
    assert error.value.status_code == 409


def test_worker_cannot_complete_or_advance_a_bound_account(persisted):
    from trading.ths.ledger_import import bind_account
    from trading.worker import step
    from types import SimpleNamespace
    run_id, _ = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    before = store.get(run_id, full=True)["state"]
    assert not step(run_id, None)
    # Even an externally corrupted running flag cannot cause simulated completion.
    with store.connection() as db:
        db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    with pytest.raises(PermissionError):
        step(run_id, SimpleNamespace(calendar=[]), datetime(2024, 1, 4, tzinfo=timezone.utc))
    assert store.get(run_id, full=True)["state"] == before
    assert store.daily(run_id) == []


def test_following_session_and_second_writer_remain_blocked(persisted):
    from trading.ths.ledger_import import bind_account, import_plan
    run_id, dataset = persisted
    bind_account(run_id, "test-only", confirm_simulated_account=True)
    other = store.create("another fixture", {"initial_cash": 12000}, DAY, "2024-01-03", "another-key", mode="paper", dataset=dataset)
    store.control(other["id"], "pause")
    with pytest.raises(ValueError, match="account writer"):
        bind_account(other["id"], "test-only", confirm_simulated_account=True)
    plan = manual_plan()
    receipt_views(plan)
    import_plan(run_id, plan["id"])
    original = store.get(run_id, full=True)["state"]
    with store.connection() as db:
        db.execute("UPDATE ths_plans SET trading_date='2024-01-03' WHERE id=?", (plan["id"],))
    with pytest.raises(ValueError):
        import_plan(run_id, plan["id"])
    assert store.get(run_id, full=True)["state"] == original
