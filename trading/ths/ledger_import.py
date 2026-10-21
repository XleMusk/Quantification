"""Explicit simulated-account receipts into the sole paused runs.state ledger.

This is an export consumer, not a client controller or an order sender. Every new
exchange session requires an observed reconciled baseline, not calendar guesses.
"""
import csv
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from ..config import StrategyConfig
from .. import settings, store
from ..contracts import (BrokerCashMovement, BrokerFill, BrokerFillBatch, BrokerRestrictionRelease, BrokerShareCredit, BrokerSessionBalance, OrderIntent, Price,
                         PriceBasis, RunContext, SecurityId, ShareQuantity, TradingMode)
from ..ledger import ACTIVE, Ledger
from ..portfolio_allocation import OBSERVED_LOT_ALLOCATION
from ..rules import cents, execution_rule, fees
from ..store import connection, now
from . import journal
from .adapter import fresh, validate_cash_balance, validate_snapshot


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_book(db, run_id):
    row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or row["mode"] != "paper" or row["status"] != "paused":
        raise PermissionError("Use an existing paused paper run, never a real/replay or worker-driven account")
    state = json.loads(row["state"])
    return row, Ledger(state, StrategyConfig(**json.loads(row["config"])), RunContext(run_id, TradingMode.PAPER))


def prepare_empty_paper_run(run_id, *, confirm=False):
    """Pause only an untouched paper run so it can receive an opening observation.

    This is a local ledger preparation step. It never touches a broker client and
    refuses runs with any state or daily history, so it cannot hide prior paper
    activity behind a broker opening balance.
    """
    if confirm is not True:
        raise PermissionError("Explicit simulated-account preparation confirmation required")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LookupError("运行不存在")
        if row["mode"] != "paper":
            raise PermissionError("仅允许 paper run，不能准备回放或实盘运行")
        state = json.loads(row["state"])
        config = json.loads(row["config"])
        if row["status"] not in {"running", "paused"}:
            raise ValueError("只有运行中或已暂停的 paper run 可以准备绑定")
        if (row["cursor"] or state.get("day") or state.get("cash") != cents(config["initial_cash"])
                or any(state.get(key) for key in ("orders", "fills", "events", "positions", "receivables", "entitlements", "prices", "targets", "candidates"))
                or state.get("pending_targets") is not None):
            raise ValueError("该 paper run 已有纸面历史，不能作为模拟账户开盘余额")
        if db.execute("SELECT 1 FROM daily WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError("该 paper run 已有 daily 历史，不能覆盖为模拟账户开盘余额")
        if state.get("broker_binding"):
            raise ValueError("该 paper run 已经绑定账户")
        if row["status"] == "running":
            db.execute("UPDATE runs SET status='paused',updated_at=? WHERE id=?", (now(), run_id))
        status = "already_paused" if row["status"] == "paused" else "paused_for_simulated_account_binding"
    return {"run_id": run_id, "status": status, "paper_ledger_mutation": False,
            "execution_admitted": False, "submission_enabled": False,
            "client_operation": False, "real_account_enabled": False}


def create_paper_run_from_observation(account_alias, *, confirm=False, name=None):
    """Create an empty paper run sized to one validated simulated-account snapshot."""
    if confirm is not True:
        raise PermissionError("Explicit simulated-account run creation confirmation required")
    with connection() as db:
        account = journal._latest(db, account_alias, "account")
        validate_snapshot(account)
        if not validate_cash_balance(account) or account["cash_frozen"]:
            raise ValueError("账户观察必须包含明确总现金/冻结现金，且冻结现金为零")
        day = account["trading_date"]
        orders = journal._latest(db, account_alias, "orders", day)
        if any(o["status"] not in {"filled", "cancelled", "rejected"} for o in orders["rows"]):
            raise ValueError("仍有未决委托，不能建立开盘账本")
        if not journal._latest(db, account_alias, "trades", day, required=False):
            raise ValueError("缺少成交观察（无成交也要导出空表）")
        if not journal._latest(db, account_alias, "cashflows", day, required=False):
            raise ValueError("缺少资金流水观察")
        marks = marks_of(account)
        opening_equity = account["cash_total"] + sum(marks[s] * p["quantity"] for s, p in positions_of(account).items())
        if opening_equity < cents(1000) or opening_equity > cents(10_000_000):
            raise ValueError("观察到账户总资产超出 paper 运行允许范围")
        from .calendar_snapshot import dataset_for_day
        try:
            calendar = calendar_receipt(settings.DATASET, day)
            dataset = settings.DATASET
        except ValueError:
            dataset = dataset_for_day(day)
            calendar = calendar_receipt(dataset, day)
        account_ref = digest(account)
    config = StrategyConfig(initial_cash=opening_equity / 100).validate().to_dict()
    request_key = f"ths-observed-opening-v1:{account_alias}:{account_ref[:24]}"
    run = store.create(name or f"THS simulated opening · {account_alias}", config, day, day,
                       request_key, mode="paper", dataset=dataset)
    return {"run": run, "account_alias": account_alias, "opening_equity_cents": opening_equity,
            "calendar": calendar, "paper_ledger_mutation": False, "execution_admitted": False,
            "submission_enabled": False, "client_operation": False, "real_account_enabled": False}


def calendar_receipt(dataset, day):
    folder = Path(dataset)
    manifest_path = folder / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("status") != "ready":
        raise ValueError("Binding requires a pinned ready dataset calendar")
    calendar_path = Path(manifest.get("folder", folder)) / "calendar.csv"
    calendar_bytes = calendar_path.read_bytes()
    with calendar_path.open(encoding="utf-8-sig", newline="") as stream:
        days = [row["date"] for row in csv.DictReader(stream)]
    if not days or days != sorted(set(days)) or any(date.fromisoformat(d).isoformat() != d for d in days):
        raise ValueError("Invalid pinned exchange calendar")
    if day not in days or days.index(day) + 1 >= len(days):
        raise ValueError("Pinned calendar lacks the actual order day/following exchange session")
    return {"manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "calendar_sha256": sha256(calendar_bytes).hexdigest(), "day": day,
            "available_day": days[days.index(day) + 1]}


def marks_of(account):
    held = {s for s, p in account["positions"].items() if p["quantity"]}
    marks = account.get("marks", {})
    if set(marks) != held:
        raise ValueError("Every holding requires its observed raw RMB valuation, not an inferred execution price")
    for symbol, price in marks.items():
        SecurityId(symbol)
        Price(price, PriceBasis.RAW)
    return marks


def positions_of(account):
    return {s: dict(p) for s, p in account["positions"].items() if p["quantity"]}


def sessions_of(state):
    binding = state["broker_binding"]
    return state.get("broker_sessions", {binding["calendar"]["day"]: {
        "calendar": binding["calendar"], "observed_at": binding["opening_observed_at"],
        "account_source_ref": binding["opening_source_ref"], "opening_order_ids": binding["opening_order_ids"]}})


def pinned_calendar(row, binding, day):
    calendar = calendar_receipt(row["dataset"], day)
    if any(calendar[k] != binding["calendar"][k] for k in ("manifest_sha256", "calendar_sha256")):
        raise ValueError("Bound exchange calendar/snapshot changed; do not silently alter settlement")
    return calendar


def closing_stamp(state, day):
    following = [s["observed_at"] for d, s in sessions_of(state).items() if d > day]
    return min(following, key=datetime.fromisoformat) if following else None


def bind_account(run_id, account_alias, *, confirm_simulated_account=False, lot_allocation=None):
    if confirm_simulated_account is not True:
        raise PermissionError("Explicit simulated-account attestation required; real accounts remain disabled")
    if lot_allocation not in (None, OBSERVED_LOT_ALLOCATION):
        raise ValueError("Broker binding requires the observed-mark lot allocation policy, not a replay policy")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row, book = run_book(db, run_id)
        state = book.state
        if state.get("broker_binding"):
            raise ValueError("Run already has an immutable broker binding")
        if (row["cursor"] or state["day"] or state["cash"] != cents(book.config.initial_cash)
                or any(state[k] for k in ("orders", "fills", "events", "positions", "receivables", "entitlements", "prices", "targets", "candidates"))
                or state["pending_targets"] is not None):
            raise ValueError("Cannot bind an evolved simulator account or overwrite its history")
        if db.execute("SELECT 1 FROM daily WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError("Existing daily simulation history is not a broker opening balance")
        for other in db.execute("SELECT state FROM runs WHERE id!=?", (run_id,)):
            if json.loads(other[0]).get("broker_binding", {}).get("account_alias") == account_alias:
                raise ValueError("This broker alias already has an account writer")
        account = journal._latest(db, account_alias, "account")
        validate_snapshot(account)
        if not validate_cash_balance(account) or account["cash_frozen"]:
            raise ValueError("Opening balance must have explicit total/frozen cash and no outstanding frozen funds")
        day = account["trading_date"]
        orders = journal._latest(db, account_alias, "orders", day)
        if (datetime.fromisoformat(orders["observed_at"]) > datetime.fromisoformat(account["observed_at"])
                or any(o["status"] not in {"filled", "cancelled", "rejected"} for o in orders["rows"])):
            raise ValueError("Refresh the opening account after an order view with no unresolved broker orders")
        for prior in db.execute("SELECT status FROM ths_plans WHERE account_alias=?", (account_alias,)):
            if prior[0] not in journal.TERMINAL:
                raise ValueError("Unresolved THS plan cannot become an opening balance")
        trades = journal._latest(db, account_alias, "trades", day, required=False)
        if trades and datetime.fromisoformat(trades["observed_at"]) > datetime.fromisoformat(account["observed_at"]):
            raise ValueError("Opening account predates the trade readback")
        marks = marks_of(account)
        opening_equity = account["cash_total"] + sum(marks[s] * p["quantity"] for s, p in positions_of(account).items())
        if opening_equity != cents(book.config.initial_cash):
            raise ValueError("Observed opening equity differs from configured initial capital; do not invent starting PnL")
        calendar = calendar_receipt(row["dataset"], day)
        state.update(cash=account["cash_total"], broker_cash_frozen=0, day=day,
            pending_targets=None, targets={}, candidates=[], prices=dict(marks),
            broker_valuation_observed_at=account["observed_at"],
            broker_binding={"run_id": run_id, "account_alias": account_alias, "channel": "ths",
                "broker_account_kind": "simulated", "attestation": "human_confirmed_simulated_account",
                "opening_equity_cents": opening_equity, "opening_observed_at": account["observed_at"],
                "opening_source_ref": digest(account), "opening_order_ids": [o["order_id"] for o in orders["rows"]],
                "calendar": calendar})
        if lot_allocation is not None:
            state["broker_binding"]["lot_allocation"] = lot_allocation
        for symbol, p in positions_of(account).items():
            state["positions"][symbol] = [lot for lot in (
                {"qty": p["sellable"], "available": day},
                {"qty": p["quantity"] - p["sellable"], "available": "9999-12-31"}) if lot["qty"]]
        state["broker_sessions"] = {day: {"calendar": calendar, "observed_at": account["observed_at"],
            "account_source_ref": digest(account), "opening_order_ids": [o["order_id"] for o in orders["rows"]]}}
        book.emit(day, "broker_opening_balance", source_ref=digest(account), opening_equity=opening_equity,
                  source="broker_observation", broker_account_kind="simulated")
        db.execute("UPDATE runs SET state=?,updated_at=? WHERE id=?", (json.dumps(state), now(), run_id))
    result = {"run_id": run_id, "account_alias": account_alias, "bound": True,
              "opening_equity_cents": opening_equity, "submit_enabled": False, "native_verified": False}
    if lot_allocation is not None:
        result["lot_allocation"] = lot_allocation
    return result


def make_receipt(db, row, book, plan):
    binding = book.state.get("broker_binding", {})
    result = plan.get("result") or {}
    if (binding.get("channel") != "ths" or binding.get("broker_account_kind") != "simulated"
            or binding.get("account_alias") != plan.get("account_alias")):
        raise PermissionError("Plan does not belong to this explicitly bound simulated broker account")
    if (result.get("status") not in {"observed_partial", "observed_filled", "observed_cancelled", "observed_rejected"}
            or not result.get("account_cash_reconciled") or not result.get("account_positions_reconciled")):
        raise ValueError("Complete reconciled actual execution, fee, cash and holdings proof required")
    day = plan["trading_date"]
    calendar = pinned_calendar(row, binding, day)
    session = sessions_of(book.state).get(day)
    if not session or session["calendar"] != calendar:
        raise ValueError("Next-day account baseline requires reconciliation before importing orders")
    if datetime.fromisoformat(plan["created_at"]) < datetime.fromisoformat(session["observed_at"]):
        raise ValueError("Plan predates this account's opening baseline")
    proof = result["confirmed_account_reconciliation"]
    account_rows = db.execute("SELECT payload FROM ths_observations WHERE account_alias=? AND kind='account' AND trading_date=? AND observed_at=?",
        (plan["account_alias"], day, proof["account_observed_at"])).fetchall()
    accounts = [json.loads(r[0]) for r in account_rows]
    if not accounts or len({digest(a) for a in accounts}) != 1:
        raise ValueError("Unique exact historical account observation for this receipt is required")
    account = accounts[0]
    validate_snapshot(account, current=datetime.fromisoformat(account["observed_at"]))
    marks = marks_of(account)
    if (account["cash_total"] != proof["after_cash_total"] or positions_of(account) != proof["observed_positions"]
            or account["cash_frozen"] != proof["after_cash_frozen"]):
        raise ValueError("Account observation changed since cash reconciliation")
    if result.get("confirmed_executions") != result.get("executions"):
        raise ValueError("Current executions do not equal the retained confirmed evidence")
    orders = journal._latest(db, plan["account_alias"], "orders", day)
    known = {r[0] for r in db.execute("SELECT contract_id FROM ths_plans WHERE account_alias=? AND trading_date=?",
        (plan["account_alias"], day))}
    known.update(session.get("opening_order_ids", []))
    trades = journal._latest(db, plan["account_alias"], "trades", day)
    if any(o["order_id"] not in known for o in orders["rows"] + trades["rows"]):
        raise ValueError("Unowned broker order/execution after the opening baseline; reconcile before accounting")
    if not (plan["symbol"].startswith("60") and plan["symbol"].endswith(".SSE")
            or plan["symbol"].startswith("00") and plan["symbol"].endswith(".SZSE")):
        raise ValueError("Native receipt path remains main-board stock only")
    order = OrderIntent(plan["id"], book.context, day, SecurityId(plan["symbol"]), plan["side"],
        ShareQuantity(plan["qty"]), Price(plan["price_cents"], PriceBasis.RAW), "ths_manual_plan")
    fills = tuple(BrokerFill(f["execution_id"], ShareQuantity(f["qty"]), Price(f["price_cents"], PriceBasis.RAW))
        for f in result["executions"])
    source_ref = digest({"plan": {k: plan[k] for k in ("id", "contract_id", "trading_date")},
                         "executions": result["executions"], "proof": proof, "account": account})
    status = result["status"].removeprefix("observed_")
    transition = book.state.get("broker_order_transitions", {}).get(plan["id"])
    if transition and transition["day"] > day:
        if transition["status"] != status:
            raise ValueError("Confirmed later-session terminal status changed")
        # The old same-day receipt remains historical. Its later observed terminal
        # transition is a separate event, not retroactively a same-day cash proof.
        status = book.state["broker_receipts"][plan["id"]]["economic"]["status"]
    return BrokerFillBatch(order, plan["account_alias"], plan["contract_id"], fills, proof["fees_cents"],
        status, calendar["available_day"], account["cash_total"], account["cash_frozen"],
        tuple(sorted((s, p["quantity"], p["sellable"]) for s, p in positions_of(account).items())),
        tuple(sorted(marks.items())), account["observed_at"], source_ref)


def check_prefix(book, plan):
    before = plan["snapshot"]
    if not validate_cash_balance(before):
        raise ValueError("Plan has no explicit pre-submit cash baseline")
    expected = deepcopy(positions_of(before))
    cash = before["cash_total"]
    prior = book.state["orders"].get(plan["id"])
    if prior:
        if prior.get("source") != "broker_observation":
            raise ValueError("Cannot replace a simulated order with a broker receipt")
        cash += prior["notional"] * (1 if plan["side"] == "sell" else -1) - prior["fees"]
        p = expected.setdefault(plan["symbol"], {"quantity": 0, "sellable": 0})
        p["quantity"] += prior["filled"] * (1 if plan["side"] == "buy" else -1)
        if plan["side"] == "sell":
            p["sellable"] -= prior["filled"]
            if prior["status"] == "partial":
                p["sellable"] -= prior["qty"] - prior["filled"]
    elif book.state["broker_cash_frozen"] != before["cash_frozen"]:
        raise ValueError("Pre-submit frozen cash differs from the existing ledger")
    actual = {s: {"quantity": book.quantity(s), "sellable": book.available(s, plan["trading_date"])}
              for s in book.state["positions"] if book.quantity(s)}
    expected = {s: p for s, p in expected.items() if p["quantity"]}
    if book.state["cash"] != cash or actual != expected:
        raise ValueError("Pre-submit cash/holdings prefix does not match the sole existing ledger")


def _apply_reconciled_plan(db, row, book, plan):
    receipt = make_receipt(db, row, book, plan)
    previous_import = (plan["result"] or {}).get("ledger_import")
    if previous_import and previous_import["run_id"] != row["id"]:
        raise ValueError("This broker receipt already belongs to another ledger")
    prior = book.state.get("broker_receipts", {}).get(plan["id"])
    economic = {k: v for k, v in receipt.to_dict().items() if k not in {"observed_at", "source_ref"}}
    if not prior or prior["economic"] != economic:
        check_prefix(book, plan)
    return receipt, book.apply_broker_receipt(receipt)


def _order_risk_for_plan(plan):
    """Evaluate the proposed order against its immutable pre-submit snapshot."""
    from .account_risk import evaluate_order_risk, package_from_account_observation

    snapshot = plan.get("snapshot") or {}
    source_scope = "isolated_rehearsal" if snapshot.get("rehearsal_only") else "persistent_user_export"
    package = package_from_account_observation(snapshot, source_scope=source_scope)
    config = StrategyConfig(**(plan.get("execution_config") or {})).validate()
    fee_buffer = fees(
        plan["price_cents"] * plan["qty"],
        plan["side"],
        config,
        equity_etf=execution_rule(plan["symbol"], plan["trading_date"]).is_fund,
        day=plan["trading_date"],
    )
    return evaluate_order_risk(package, [{
        "order_id": plan["id"],
        "symbol": plan["symbol"],
        "side": plan["side"],
        "qty": plan["qty"],
        "price_rmb": Decimal(plan["price_cents"]) / Decimal(100),
        "fee_cents": fee_buffer,
    }])


def _preview_balance(book, day):
    return {
        "cash_total_cents": book.state["cash"],
        "cash_available_cents": book.cash_available(),
        "cash_frozen_cents": book.state.get("broker_cash_frozen", 0),
        "order_count": len(book.state["orders"]),
        "fill_count": len(book.state["fills"]),
        "fees_cents": sum(order.get("fees", 0) for order in book.state["orders"].values()),
        "positions": {
            symbol: {"quantity": book.quantity(symbol), "sellable": book.available(symbol, day)}
            for symbol in sorted(book.state["positions"]) if book.quantity(symbol)
        },
    }


def _stage_plan_observations(db, plan, observations):
    values = list(observations)
    day = plan["trading_date"]
    if (len(values) != 4 or {value["kind"] for value in values} != {"account", "orders", "trades", "cashflows"}
            or any(value["account_alias"] != plan["account_alias"] or value["trading_date"] != day
                   for value in values)):
        raise ValueError("Export bundle must match this plan's account and trading date in all four views")
    staged = journal.observe_many(values, db=db)
    if any(journal._latest(db, value["account_alias"], value["kind"], day) != value for value in values):
        raise ValueError("Export bundle is older than the current journal evidence; preview the latest bundle")
    return staged


def _persist_plan_receipt(db, run_id, book, plan, receipt, changed):
    if changed:
        db.execute("UPDATE runs SET state=?,updated_at=? WHERE id=?", (json.dumps(book.state), now(), run_id))
    result = plan["result"]
    result["ledger_import"] = {"run_id": run_id, "source_ref": receipt.source_ref,
                               "broker_account_kind": "simulated", "ledger_imported": True}
    db.execute("UPDATE ths_plans SET result=? WHERE id=?", (json.dumps(result), plan["id"]))


def import_observation_bundle(run_id, plan_id, observations, *, confirm=False):
    """Commit the four views, refreshed plan and same Ledger receipt together."""
    if confirm is not True:
        raise PermissionError("Explicit Ledger import confirmation is required")
    if not settings.DB_PATH.is_file():
        raise ValueError("Existing paper Ledger database is required")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row, book = run_book(db, run_id)
        original = journal.decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        binding = book.state.get("broker_binding", {})
        if (binding.get("channel") != "ths"
                or binding.get("broker_account_kind") != "simulated"
                or binding.get("account_alias") != original.get("account_alias")):
            raise PermissionError("Plan does not belong to this bound simulated broker account")
        day = original["trading_date"]
        if not original.get("contract_id") or not day:
            raise ValueError("Actual broker contract ID and trading date are required before Ledger import")
        # Hold the same write lock from evidence staging through final receipt:
        # a stale bundle or any accounting failure rolls back all three tables.
        journal_receipt = _stage_plan_observations(db, original, observations)
        order_risk = _order_risk_for_plan(original)
        if order_risk["status"] == "order_risk_blocked":
            raise ValueError("Account order risk gate blocked Ledger import: "
                             + "; ".join(order_risk.get("blockers") or ["unknown blocker"]))
        plan = journal.refresh(plan_id, db=db, interval_end=closing_stamp(book.state, day))
        receipt, changed = _apply_reconciled_plan(db, row, book, plan)
        _persist_plan_receipt(db, run_id, book, plan, receipt, changed)
        result = {
            "schema": "ths_atomic_ledger_bundle_import_v1", "atomic": True,
            "journal": journal_receipt, "plan": plan,
            "account_order_risk": order_risk,
            "idempotent_replay": bool(journal_receipt["idempotent_replay"] and not changed),
            "ledger": {"run_id": run_id, "plan_id": plan_id, "ledger_imported": True,
                       "changed": changed, "cash_total_cents": book.state["cash"],
                       "source_ref": receipt.source_ref, "native_verified": False, "submit_enabled": False},
            "ledger_imported": True, "ledger_mutated": changed,
            "execution_admitted": False, "submission_enabled": False, "client_operation": False,
        }
    return result


def preview_plan(run_id, plan_id, *, observations=None):
    """Compute a receipt's exact Ledger changes and roll back all staged evidence."""
    preview = {
        "schema": "ths_ledger_import_preview_v1", "run_id": run_id, "plan_id": plan_id,
        "status": "blocked", "would_change": False, "ledger_imported": False,
        "ledger_mutated": False, "journal_mutated": False, "plan_mutated": False,
        "native_verified": False, "execution_admitted": False, "submission_enabled": False,
        "client_operation": False,
        "basis": "same paused paper Ledger import calculation; all staged observations and plan refresh are rolled back",
    }
    if not settings.DB_PATH.is_file():
        return {**preview, "error": "Existing paper Ledger database is required"}
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row, book = run_book(db, run_id)
            original = journal.decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
            binding = book.state.get("broker_binding", {})
            if (binding.get("channel") != "ths"
                    or binding.get("broker_account_kind") != "simulated"
                    or binding.get("account_alias") != original.get("account_alias")):
                raise PermissionError("Plan does not belong to this bound simulated broker account")
            day = original["trading_date"]
            if not original.get("contract_id") or not day:
                raise ValueError("Actual broker contract ID and trading date are required before Ledger preview")
            preview.update(account_alias=original["account_alias"], trading_date=day,
                           before=_preview_balance(book, day))
            if observations is not None:
                preview["staged_observations"] = _stage_plan_observations(db, original, observations)
            # These observations are post-submit broker evidence.  Do not
            # apply the proposed order a second time to that post-fill
            # account; pre-submit risk is bound to the immutable plan snapshot.
            order_risk = _order_risk_for_plan(original)
            preview["account_order_risk"] = order_risk
            if order_risk["status"] == "order_risk_blocked":
                preview.update(status="blocked_by_account_order_risk",
                               error="Account order risk gate blocked the Ledger import")
                return preview
            plan = journal.refresh(plan_id, db=db, interval_end=closing_stamp(book.state, day))
            preview["plan_status"] = plan["status"]
            preview["account_reconciliation"] = (plan.get("result") or {}).get("account_reconciliation")
            receipt, changed = _apply_reconciled_plan(db, row, book, plan)
            before, after = preview["before"], _preview_balance(book, day)
            scalar_keys = ("cash_total_cents", "cash_available_cents", "cash_frozen_cents", "order_count", "fill_count", "fees_cents")
            position_changes = []
            for symbol in sorted(set(before["positions"]) | set(after["positions"])):
                old = before["positions"].get(symbol, {"quantity": 0, "sellable": 0})
                new = after["positions"].get(symbol, {"quantity": 0, "sellable": 0})
                if old != new:
                    position_changes.append({"symbol": symbol, "before": old, "after": new,
                                             "quantity_delta": new["quantity"] - old["quantity"],
                                             "sellable_delta": new["sellable"] - old["sellable"]})
            preview.update(status="ready_for_import" if changed else "already_imported_no_change",
                           would_change=changed, after=after, source_ref=receipt.source_ref,
                           changes={**{key: after[key] - before[key] for key in scalar_keys},
                                    "positions": position_changes})
        except (PermissionError, ValueError, LookupError) as error:
            preview.update(error=str(error), error_type=type(error).__name__)
        finally:
            db.rollback()
    return preview


def import_plan(run_id, plan_id):
    failure = None
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row, book = run_book(db, run_id)
        original_plan = journal.decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        binding = book.state.get("broker_binding", {})
        if (binding.get("channel") != "ths" or binding.get("broker_account_kind") != "simulated"
                or binding.get("account_alias") != original_plan["account_alias"]):
            raise PermissionError("Plan does not belong to this bound simulated broker account")
        order_risk = _order_risk_for_plan(original_plan)
        if order_risk["status"] == "order_risk_blocked":
            raise ValueError("Account order risk gate blocked Ledger import: "
                             + "; ".join(order_risk.get("blockers") or ["unknown blocker"]))
        plan = original_plan
        try:
            plan = journal.refresh(plan_id, db=db, interval_end=closing_stamp(book.state, original_plan["trading_date"]))
            receipt, changed = _apply_reconciled_plan(db, row, book, plan)
        except ValueError as error:
            # Keep the account untouched, but commit the readback conflict so the
            # next plan cannot proceed on a previously terminal, now invalid proof.
            failure = error
            if "result" in plan and plan["result"]:
                result = {**plan["result"], "status": "reconciliation_required", "ledger_import_error": str(error)}
                db.execute("UPDATE ths_plans SET status=?,result=?,updated_at=? WHERE id=?",
                           (result["status"], json.dumps(result), now(), plan_id))
        else:
            _persist_plan_receipt(db, run_id, book, plan, receipt, changed)
    if failure:
        raise failure
    return {"run_id": run_id, "plan_id": plan_id, "ledger_imported": True, "changed": changed,
            "cash_total_cents": book.state["cash"], "source_ref": receipt.source_ref,
            "native_verified": False, "submit_enabled": False}


def reconcile_session(run_id, *, observed_at=None, restriction_releases=False, share_credits=False):
    if type(restriction_releases) is not bool or type(share_credits) is not bool:
        raise ValueError("Restriction release/share credit consumption requires an explicit boolean")
    failure = None
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row, book = run_book(db, run_id)
        book._execution_source(True)
        binding = book.state["broker_binding"]
        if binding.get("channel") != "ths":
            raise PermissionError("Only the explicitly bound simulated THS account is supported")
        alias = binding["account_alias"]
        if (row["error"] or "").startswith("broker_session: cash_statement:"):
            raise ValueError("Resolve the cash statement conflict with import-movements before session reconciliation")
        if observed_at is None:
            account = journal._latest(db, alias, "account")
        else:
            rows = db.execute("SELECT payload FROM ths_observations WHERE account_alias=? AND kind='account' AND observed_at=?",
                              (alias, observed_at)).fetchall()
            values = [json.loads(r[0]) for r in rows]
            if not values or len({digest(a) for a in values}) != 1:
                raise ValueError("Exact unique account observation required")
            account = values[0]
        day = account["trading_date"]
        try:
            calendar = pinned_calendar(row, binding, day)
            known_session = sessions_of(book.state).get(day)
            if known_session and "economic" not in known_session:
                if restriction_releases or share_credits:
                    raise ValueError("Restriction releases/share credits are supported at a following-session baseline only")
                if known_session["account_source_ref"] != digest(account):
                    raise ValueError("Changed opening baseline cannot replace bind-account history")
                return {"run_id": run_id, "day": day, "changed": False, "cash_total_cents": book.state["cash"],
                        "native_verified": False, "submit_enabled": False}
            if not known_session:
                validate_snapshot(account)
                previous_day = book.state["day"]
                if day != pinned_calendar(row, binding, previous_day)["available_day"]:
                    raise ValueError("Reconcile the exact following exchange session; do not skip dates or weekends")
            else:
                validate_snapshot(account, current=datetime.fromisoformat(account["observed_at"]))
                previous_day = known_session["previous_day"]
                if known_session["account_source_ref"] != digest(account):
                    raise ValueError("Changed same-session account is not a new opening baseline; import the underlying events")
            if not validate_cash_balance(account) or account["cash_frozen"]:
                raise ValueError("New-session baseline requires explicit total/frozen cash and no current frozen funds")
            marks = marks_of(account)
            stamp = account["observed_at"]
            current_views = [journal._latest(db, alias, kind, day, observed_before=stamp if known_session else None)
                             for kind in ("orders", "trades", "movements")]
            for view in current_views:
                if not known_session:
                    fresh(view["observed_at"])
                if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                    raise ValueError("Refresh account after all new-session source views")
            orders, trades, movements_view = current_views
            release_view = None
            if restriction_releases or known_session and known_session["economic"].get("restriction_releases"):
                release_view = journal._latest(db, alias, "restriction_releases", day,
                                               observed_before=stamp if known_session else None)
                if (release_view["source"] != "ths_user_export"
                        or release_view.get("basis") != "opening_restriction_releases"
                        or release_view.get("quantity_unit") != "share"):
                    raise ValueError("Explicit actual opening restriction release export required")
                if not known_session:
                    fresh(release_view["observed_at"])
                if datetime.fromisoformat(release_view["observed_at"]) > datetime.fromisoformat(stamp):
                    raise ValueError("Refresh account after the restriction release export")
                latest_releases = journal._latest(db, alias, "restriction_releases", day)
                if ({r["release_id"]: r for r in latest_releases["rows"]}
                        != {r["release_id"]: r for r in release_view["rows"]}):
                    raise ValueError("Confirmed restriction release statement changed or has new unaccounted rows")
            credit_view = None
            if share_credits or known_session and known_session["economic"].get("share_credits"):
                credit_view = journal._latest(db, alias, "share_credits", day,
                                              observed_before=stamp if known_session else None)
                if (credit_view["source"] != "ths_user_export"
                        or credit_view.get("basis") != "account_bonus_share_credits"
                        or credit_view.get("quantity_unit") != "share"):
                    raise ValueError("Explicit actual account bonus/capital-transfer share export required")
                if not known_session:
                    fresh(credit_view["observed_at"])
                if datetime.fromisoformat(credit_view["observed_at"]) > datetime.fromisoformat(stamp):
                    raise ValueError("Refresh account after the share credit export")
                latest_credits = journal._latest(db, alias, "share_credits", day)
                if ({c["credit_id"]: c for c in latest_credits["rows"]}
                        != {c["credit_id"]: c for c in credit_view["rows"]}):
                    raise ValueError("Confirmed share credit statement changed or has new unaccounted rows")
            if orders["rows"] or trades["rows"]:
                raise ValueError("New-session opening must precede new orders/trades, not rebase over them")
            if (movements_view["source"] != "ths_user_export" or movements_view.get("basis") != "nontrade_cash_movements"
                    or movements_view.get("currency") != "CNY" or movements_view.get("cash_unit") != "cent"):
                raise ValueError("Explicit actual nontrade cash statement, including an empty one, required")
            latest_movements = journal._latest(db, alias, "movements", day)
            latest_by_id = {m["movement_id"]: m for m in latest_movements["rows"]}
            if any(latest_by_id.get(m["movement_id"]) != m for m in movements_view["rows"]):
                raise ValueError("Previously confirmed cash movement changed or disappeared")
            previous_views = [journal._latest(db, alias, kind, previous_day) for kind in ("orders", "trades")]
            previous_known_ids = set(sessions_of(book.state)[previous_day].get("opening_order_ids", []))
            previous_known_ids.update(r[0] for r in db.execute("SELECT contract_id FROM ths_plans WHERE account_alias=? AND trading_date=?",
                                                              (alias, previous_day)))
            for view in previous_views:
                if any(r["order_id"] not in previous_known_ids for r in view["rows"]):
                    raise ValueError("Unowned prior-session order/execution must be reconciled, not hidden by a matching net balance")
                if not known_session:
                    fresh(view["observed_at"])
                    if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                        raise ValueError("Refresh account after prior-session order/trade views")
            statement_days = {s["economic"]["day"] for s in book.state.get("broker_cash_statements", {}).values()
                              if s["economic"]["day"] <= previous_day}
            for statement_day in statement_days:
                view = journal._latest(db, alias, "movements", statement_day)
                supplied = {m["movement_id"]: (m["kind"], m["cash_change_cents"]) for m in view["rows"]}
                retained = {m["id"]: (m["kind"], m["cash_change_cents"])
                            for m in book.state["broker_cash_movements"].values() if m["day"] == statement_day}
                if supplied != retained:
                    raise ValueError("Prior cash statement changed or has unaccounted movements; import-movements first")
                if not known_session and statement_day == previous_day:
                    fresh(view["observed_at"])
                    if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                        raise ValueError("Refresh account after the prior cash statement")
            release_days = {r["day"] for r in book.state.get("broker_restriction_releases", {}).values() if r["day"] <= previous_day}
            for release_day in release_days:
                view = journal._latest(db, alias, "restriction_releases", release_day)
                supplied = {r["release_id"]: (r["symbol"], r["released_qty"], r["release_day"], r["reason"]) for r in view["rows"]}
                retained = {}
                for record in book.state["broker_restriction_releases"].values():
                    if record["day"] == release_day:
                        release = BrokerRestrictionRelease.from_dict(record)
                        retained[release.id] = (release.security.symbol, release.quantity.shares, release.day, release.reason)
                if supplied != retained:
                    raise ValueError("Prior restriction release evidence changed, disappeared or has unaccounted rows")
                if not known_session and release_day == previous_day:
                    fresh(view["observed_at"])
                    if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                        raise ValueError("Refresh account after the prior restriction release statement")
            credit_days = {c["day"] for c in book.state.get("broker_share_credits", {}).values() if c["day"] <= previous_day}
            for credit_day in credit_days:
                view = journal._latest(db, alias, "share_credits", credit_day)
                supplied = {c["credit_id"]: (c["symbol"], c["credited_qty"], c["credit_day"], c["available_day"], c["kind"])
                            for c in view["rows"]}
                retained = {}
                for record in book.state["broker_share_credits"].values():
                    if record["day"] == credit_day:
                        credit = BrokerShareCredit.from_dict(record)
                        retained[credit.id] = (credit.security.symbol, credit.quantity.shares, credit.day,
                                               credit.available_day, credit.kind)
                if supplied != retained:
                    raise ValueError("Prior share credit evidence changed, disappeared or has unaccounted rows")
                if not known_session and credit_day == previous_day:
                    fresh(view["observed_at"])
                    if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                        raise ValueError("Refresh account after the prior share credit statement")
            terminalizations, plan_proofs = [], []
            plans = db.execute("SELECT * FROM ths_plans WHERE account_alias=?", (alias,)).fetchall()
            for raw in plans:
                plan = journal.decode(raw)
                if datetime.fromisoformat(plan["created_at"]) < datetime.fromisoformat(binding["opening_observed_at"]):
                    continue
                if plan["status"] in {"discarded", "closed_unsubmitted"}:
                    continue
                if not plan["trading_date"] or plan["trading_date"] > previous_day:
                    if datetime.fromisoformat(plan["created_at"]) <= datetime.fromisoformat(stamp):
                        raise ValueError("Unresolved/unimported plan or new-session plan predates the baseline")
                    continue
                order = book.state["orders"].get(plan["id"])
                if not order or order.get("source") != "broker_observation":
                    raise ValueError("Import the previous plan's actual fills before opening a new session")
                end = closing_stamp(book.state, plan["trading_date"]) or stamp
                plan = journal.refresh(plan["id"], db=db, interval_end=end)
                result = plan["result"] or {}
                if (result.get("status") not in {"observed_filled", "observed_cancelled", "observed_rejected"}
                        or not result.get("account_cash_reconciled") or not result.get("account_positions_reconciled")):
                    raise ValueError("Prior broker order is unresolved or its execution/fee proof changed")
                proof = result["confirmed_account_reconciliation"]
                if (result["filled_qty"] != order["filled"] or result["gross_executed_cents"] != order["notional"]
                        or proof["fees_cents"] != order["fees"] or result["executions"] != result["confirmed_executions"]):
                    raise ValueError("New/changed prior-day fill or fee needs receipt reconciliation, not an account rebase")
                supplied = {f"ths:{plan['trading_date']}:{plan['contract_id']}:{f['execution_id']}": f for f in result["executions"]}
                recorded = {key: f for key, f in book.state["fills"].items() if f["order_id"] == plan["id"]}
                if (supplied.keys() != recorded.keys() or any(
                        (f["qty"], f["price_cents"]) != (recorded[key]["qty"], recorded[key]["price"]) for key, f in supplied.items())):
                    raise ValueError("Previous executions differ from the same ledger's actual receipt")
                if plan["trading_date"] == previous_day and not known_session:
                    for kind in ("orders", "trades", "cashflows"):
                        view = journal._latest(db, alias, kind, previous_day)
                        fresh(view["observed_at"])
                        if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                            raise ValueError("Refresh account after prior-session query readbacks")
                status = result["status"].removeprefix("observed_")
                proof_ref = digest({"plan_id": plan["id"], "contract_id": plan["contract_id"], "status": status,
                    "executions": result["executions"], "fees_cents": proof["fees_cents"]})
                transition = book.state.get("broker_order_transitions", {}).get(plan["id"])
                if order["status"] in ACTIVE or transition and transition["day"] == day:
                    terminalizations.append((plan["id"], status, proof_ref))
                elif status != order["status"]:
                    raise ValueError("Previous terminal broker status changed")
                plan_proofs.append(proof_ref)
            cash_movements = tuple(BrokerCashMovement(m["movement_id"], m["kind"], m["cash_change_cents"]) for m in movements_view["rows"])
            releases = tuple(BrokerRestrictionRelease(r["release_id"], SecurityId(r["symbol"]), ShareQuantity(r["released_qty"]),
                             r["release_day"], r["reason"]) for r in release_view["rows"]) if release_view else ()
            credits = tuple(BrokerShareCredit(c["credit_id"], SecurityId(c["symbol"]), ShareQuantity(c["credited_qty"]),
                            c["credit_day"], c["available_day"], c["kind"]) for c in credit_view["rows"]) if credit_view else ()
            for credit in credits:
                if credit.available_day != day:
                    pinned_calendar(row, binding, credit.available_day)
            source = {"account": account, "views": current_views, "prior_proofs": sorted(plan_proofs), "calendar": calendar}
            if release_view is not None:
                source["restriction_releases"] = release_view
            if credit_view is not None:
                source["share_credits"] = credit_view
            receipt = BrokerSessionBalance(book.context, alias, day, calendar["available_day"], account["cash_total"],
                tuple(sorted((s, p["quantity"], p["sellable"]) for s, p in positions_of(account).items())), tuple(sorted(marks.items())),
                cash_movements, tuple(sorted(terminalizations)), stamp,
                digest(source), releases, credits)
            changed = book.apply_broker_session(receipt)
            if changed:
                book.state["broker_sessions"][day].update(calendar=calendar, previous_day=previous_day,
                    account_source_ref=digest(account), opening_order_ids=[])
                db.execute("UPDATE runs SET state=?,error=NULL,updated_at=? WHERE id=?", (json.dumps(book.state), now(), run_id))
            else:
                db.execute("UPDATE runs SET error=NULL WHERE id=?", (run_id,))
        except ValueError as error:
            failure = error
            db.execute("UPDATE runs SET error=?,updated_at=? WHERE id=?", (f"broker_session: {error}", now(), run_id))
    if failure:
        raise failure
    return {"run_id": run_id, "day": day, "changed": changed, "cash_total_cents": book.state["cash"],
            "released_opening_shares": sum(r.quantity.shares for r in releases) if changed else 0,
            "credited_shares": sum(c.quantity.shares for c in credits) if changed else 0,
            "observed_at": stamp, "source_ref": receipt.source_ref, "native_verified": False, "submit_enabled": False}


def import_movements(run_id, *, observed_at=None):
    """Consume cumulative current-session exports before its first new plan."""
    failure = None
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row, book = run_book(db, run_id)
        book._execution_source(True)
        binding = book.state["broker_binding"]
        if binding.get("channel") != "ths":
            raise PermissionError("Only the explicitly bound simulated THS account is supported")
        alias = binding["account_alias"]
        account = journal._latest(db, alias, "account")
        if observed_at is not None:
            rows = db.execute("SELECT payload FROM ths_observations WHERE account_alias=? AND kind='account' AND observed_at=?",
                              (alias, observed_at)).fetchall()
            values = [json.loads(r[0]) for r in rows]
            if not values or len({digest(a) for a in values}) != 1:
                raise ValueError("Exact unique account observation required")
            account = values[0]
        day, stamp = account["trading_date"], account["observed_at"]
        key = f"ths:{day}:{stamp}"
        retained = book.state.get("broker_cash_statements", {}).get(key)
        session = sessions_of(book.state).get(day)
        if observed_at is not None and not retained:
            raise ValueError("--observed-at only rereads an already imported cash statement")
        if not retained and session and "economic" in session:
            if day != book.state["day"]:
                raise ValueError("New cash statement must belong to the current accounted session")
            if any(o["status"] in ACTIVE or o["day"] == day for o in book.state["orders"].values()):
                raise PermissionError("Cash import is supported before this session's first new order only")
            for raw in db.execute("SELECT * FROM ths_plans WHERE account_alias=?", (alias,)):
                plan = journal.decode(raw)
                if (plan["status"] not in {"discarded", "closed_unsubmitted"}
                        and datetime.fromisoformat(plan["created_at"]) >= datetime.fromisoformat(session["observed_at"])):
                    raise PermissionError("Cash import must precede this session's first new plan")
        try:
            if row["error"] and not row["error"].startswith("broker_session: cash_statement:"):
                raise ValueError("Resolve the existing account reconciliation error before cash import")
            calendar = pinned_calendar(row, binding, day)
            if not session or "economic" not in session:
                raise ValueError("Reconcile a following-session baseline first; bind-day cash history is not yet scoped")
            validate_snapshot(account, current=datetime.fromisoformat(stamp) if retained else None)
            if not validate_cash_balance(account) or account["cash_frozen"]:
                raise ValueError("Cash import requires explicit total/frozen cash with no frozen funds")
            marks = marks_of(account)
            views = [journal._latest(db, alias, kind, day, observed_before=stamp if retained else None)
                     for kind in ("orders", "trades", "movements")]
            for view in views:
                if datetime.fromisoformat(view["observed_at"]) > datetime.fromisoformat(stamp):
                    raise ValueError("Refresh account after all cash-import source views")
                if not retained:
                    fresh(view["observed_at"])
            orders, trades, movements = views
            if orders["rows"] or trades["rows"]:
                raise ValueError("Cash import is supported before new orders/trades only")
            if (movements["source"] != "ths_user_export" or movements.get("basis") != "nontrade_cash_movements"
                    or movements.get("currency") != "CNY" or movements.get("cash_unit") != "cent"):
                raise ValueError("Explicit cumulative actual nontrade cash statement required")
            latest = journal._latest(db, alias, "movements", day)
            latest_by_id = {m["movement_id"]: m for m in latest["rows"]}
            if any(latest_by_id.get(m["movement_id"]) != m for m in movements["rows"]):
                raise ValueError("Previously confirmed cash movement changed or disappeared")
            if not retained:
                if journal._latest(db, alias, "account") != account:
                    raise ValueError("New cash import must use the latest account, not a historical balance")
                # Recheck the opening's already-accounted IDs even if this is the first intraday statement.
                opening = journal._latest(db, alias, "movements", day, observed_before=session["observed_at"])
                if any(latest_by_id.get(m["movement_id"]) != m for m in opening["rows"]):
                    raise ValueError("Opening cash movement evidence changed or disappeared")
            receipt = BrokerSessionBalance(book.context, alias, day, calendar["available_day"], account["cash_total"],
                tuple(sorted((s, p["quantity"], p["sellable"]) for s, p in positions_of(account).items())),
                tuple(sorted(marks.items())),
                tuple(BrokerCashMovement(m["movement_id"], m["kind"], m["cash_change_cents"]) for m in movements["rows"]),
                (), stamp, digest({"account": account, "views": views, "calendar": calendar}))
            before_cash = book.state["cash"]
            before_movements = len(book.state.get("broker_cash_movements", {}))
            changed = book.apply_broker_cash_statement(receipt)
            if changed:
                db.execute("UPDATE runs SET state=?,error=NULL,updated_at=? WHERE id=?", (json.dumps(book.state), now(), run_id))
            elif (journal._latest(db, alias, "account") == account
                  and all(journal._latest(db, alias, view["kind"], day) == view for view in views)):
                db.execute("UPDATE runs SET error=NULL WHERE id=?", (run_id,))
        except ValueError as error:
            failure = error
            if (session and "economic" in session and day == book.state["day"]
                    and (not row["error"] or row["error"].startswith("broker_session: cash_statement:"))):
                db.execute("UPDATE runs SET error=?,updated_at=? WHERE id=?",
                           (f"broker_session: cash_statement: {error}", now(), run_id))
    if failure:
        raise failure
    return {"run_id": run_id, "day": day, "changed": changed, "cash_total_cents": book.state["cash"],
            "new_movements": len(book.state.get("broker_cash_movements", {})) - before_movements,
            "cash_change_cents": book.state["cash"] - before_cash,
            "observed_at": stamp, "source_ref": receipt.source_ref, "native_verified": False,
            "submit_enabled": False, "scope": "current_session_before_first_new_plan"}
