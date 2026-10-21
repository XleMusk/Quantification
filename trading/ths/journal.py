"""THS observations and one-shot previews in the project's existing SQLite DB."""
import json
from datetime import datetime
import math
import os
import uuid

from ..store import connection
from .adapter import check_plan, now
from .observations import identifier, reconcile, reconcile_account, retain_execution_evidence

TERMINAL = ("discarded", "closed_unsubmitted", "observed_filled", "observed_cancelled", "observed_rejected")


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS ths_plans(
          id TEXT PRIMARY KEY, account_alias TEXT NOT NULL, status TEXT NOT NULL,
          payload TEXT NOT NULL, result TEXT, contract_id TEXT, trading_date TEXT,
          updated_at TEXT NOT NULL, UNIQUE(account_alias,trading_date,contract_id));
        CREATE TABLE IF NOT EXISTS ths_observations(
          id INTEGER PRIMARY KEY, account_alias TEXT NOT NULL, kind TEXT NOT NULL,
          observed_at TEXT NOT NULL, trading_date TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ths_control(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)


def decode(row):
    if not row:
        raise ValueError("Unknown plan")
    value = json.loads(row["payload"])
    value.update({key: row[key] for key in ("status", "contract_id", "trading_date", "updated_at")})
    value["result"] = json.loads(row["result"]) if row["result"] else None
    return value


def get(plan_id):
    with connection() as db:
        return decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())


def save_plan(plan):
    check_plan(plan)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        flag = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
        if flag and flag[0] == "true":
            raise ValueError("THS kill switch is active; resume before saving a new plan")
        if "strategy_signal" in plan:
            from .signals import check_signal_plan
            check_signal_plan(db, plan)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            bound = db.execute("SELECT state,error FROM runs WHERE json_extract(state,'$.broker_binding.account_alias')=?",
                               (plan["account_alias"],)).fetchall()
            for account in bound:
                if (json.loads(account["state"])["day"] != plan["snapshot"]["trading_date"]
                        or (account["error"] or "").startswith("broker_session:")):
                    raise ValueError("Reconcile the bound account's session before preparing a new plan")
        previous = db.execute("SELECT * FROM ths_plans WHERE account_alias=?", (plan["account_alias"],)).fetchall()
        if any(row["status"] not in TERMINAL for row in previous):
            raise ValueError("Unresolved plan exists for this account; query/reconcile, never resend")
        account_stamp = datetime.fromisoformat(plan["snapshot"]["observed_at"])
        for row in previous:
            if row["status"] not in {"observed_filled", "observed_cancelled", "observed_rejected"}:
                continue
            result = json.loads(row["result"]) if row["result"] else {}
            stamps = [result.get(key) for key in ("orders_observed_at", "trades_observed_at")]
            if not all(stamps):
                raise ValueError("Refresh account and reconcile the prior broker order's missing observation evidence")
            stamps.extend(result[key] for key in ("account_observed_at", "cashflows_observed_at") if result.get(key))
            receipt = result.get("confirmed_account_reconciliation")
            if receipt:
                stamps.extend(receipt[key] for key in ("account_observed_at", "cashflows_observed_at"))
            if any(account_stamp < datetime.fromisoformat(stamp) for stamp in stamps):
                raise ValueError("Refresh account after the prior broker order; old cash and holdings cannot be reused")
        db.execute("INSERT INTO ths_plans(id,account_alias,status,payload,updated_at) VALUES(?,?,?,?,?)",
                   (plan["id"], plan["account_alias"], "planned", json.dumps(plan), now().isoformat()))
    return get(plan["id"])


def paused():
    with connection() as db:
        row = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
        return bool(row and row[0] == "true")


def pause(value):
    with connection() as db:
        db.execute("INSERT INTO ths_control VALUES('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("true" if value else "false",))
    return {"paused": value, "kill_switch": "engaged" if value else "disengaged",
            "cancels_broker_orders": False}


def _new_prefill_owner():
    """Identify this exact process before its first possible desktop write."""
    import psutil
    pid = os.getpid()
    created = psutil.Process(pid).create_time()
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0:
        raise ValueError("Cannot establish the prefill process creation time")
    return {"attempt_id": uuid.uuid4().hex, "pid": pid,
            "process_create_time": created, "claimed_at": now().isoformat()}


def _known_prefill_owner(owner):
    return (isinstance(owner, dict) and isinstance(owner.get("attempt_id"), str)
            and bool(owner["attempt_id"]) and type(owner.get("pid")) is int and owner["pid"] > 0
            and not isinstance(owner.get("process_create_time"), bool)
            and isinstance(owner.get("process_create_time"), (int, float))
            and math.isfinite(owner["process_create_time"]) and owner["process_create_time"] > 0
            and isinstance(owner.get("claimed_at"), str) and bool(owner["claimed_at"]))


def _prefill_process_state(owner):
    """Read only the recorded PID; elapsed time never proves writer death."""
    import psutil
    if not _known_prefill_owner(owner):
        return {"status": "owner_state_unknown", "reason": "prefill_owner_identity_missing_or_invalid"}
    try:
        created = psutil.Process(owner["pid"]).create_time()
    except psutil.ZombieProcess:
        return {"status": "owner_state_unknown", "reason": "process_identity_unavailable"}
    except psutil.NoSuchProcess:
        return {"status": "owner_not_running", "pid": owner["pid"],
                "expected_process_create_time": owner["process_create_time"]}
    except (psutil.AccessDenied, OSError, ValueError):
        return {"status": "owner_state_unknown", "reason": "process_identity_unavailable"}
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0:
        return {"status": "owner_state_unknown", "reason": "process_creation_time_unavailable"}
    return {"status": "owner_alive" if created == owner["process_create_time"] else "pid_reused",
            "pid": owner["pid"], "expected_process_create_time": owner["process_create_time"],
            "observed_process_create_time": created}


def claim(plan_id):
    # Durable claim precedes ALL desktop writes. A crash remains unresolved.
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        flag = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
        if flag and flag[0] == "true":
            raise ValueError("Desktop writes are paused")
        plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        if plan["status"] != "planned":
            raise ValueError("Preview already attempted; do not repeat desktop writes")
        check_plan(plan)
        # Only one writer on this desktop, even across account aliases/processes.
        if db.execute("SELECT 1 FROM ths_plans WHERE status IN ('prefilling','prefill_unknown','awaiting_manual_submission','submission_unknown')").fetchone():
            raise ValueError("Another form is active or unresolved; manual review required")
        owner = _new_prefill_owner()
        result = {"status": "prefilling", "prefill_owner": owner}
        db.execute("UPDATE ths_plans SET status='prefilling',result=?,updated_at=? WHERE id=?",
                   (json.dumps(result), owner["claimed_at"], plan_id))
    return get(plan_id)


def _finish_prefill(plan_id, owner, result):
    """A late callback cannot overwrite recovery or another attempt's state."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        if (not _known_prefill_owner(owner) or plan["status"] != "prefilling"
                or (plan.get("result") or {}).get("prefill_owner") != owner):
            raise ValueError("Prefill attempt no longer owns this in-flight plan; callback not committed")
        value = {**result, "prefill_owner": dict(owner)}
        db.execute("UPDATE ths_plans SET status=?,result=?,updated_at=? WHERE id=?",
                   (value["status"], json.dumps(value), now().isoformat(), plan_id))
    return get(plan_id)


def prefill(plan_id, adapter):
    plan = claim(plan_id)
    owner = plan["result"]["prefill_owner"]
    try:
        result = adapter.prefill(plan, stop_check=paused)
    except BaseException as error:
        result = {"status": "prefill_unknown", "error": type(error).__name__,
                  "reason": "Partial form may remain; inspect it manually. No automatic retry."}
        _finish_prefill(plan_id, owner, result)
        raise
    return _finish_prefill(plan_id, owner, result)


def recover_prefill(plan_id, *, confirm_attempt):
    """Release a proven-dead writer into unknown state, never into a retry."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        flag = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
        if not flag or flag[0] != "true":
            raise ValueError("Pause desktop writes before recovering an interrupted prefill")
        plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        previous = plan.get("result") or {}
        owner = previous.get("prefill_owner")
        if not _known_prefill_owner(owner) or confirm_attempt != owner["attempt_id"]:
            raise ValueError("Exact recorded prefill owner and attempt confirmation required; legacy unknown ownership cannot be recovered")
        recovered = previous.get("prefill_recovery") or {}
        if (plan["status"] == "prefill_unknown" and recovered.get("attempt_id") == confirm_attempt
                and recovered.get("owner_state", {}).get("status") in {"owner_not_running", "pid_reused"}):
            return plan
        if plan["status"] != "prefilling":
            raise ValueError("Only this exact in-flight prefill attempt may be recovered")
        process = _prefill_process_state(owner)
        if process.get("status") not in {"owner_not_running", "pid_reused"}:
            raise ValueError("The recorded writer is alive or its process identity is unknown; recovery refused")
        stamp = now().isoformat()
        result = {**previous, "status": "prefill_unknown", "prefill_owner": dict(owner),
            "reason": "Interrupted writer has stopped; inspect the form and broker evidence before binding or closing this plan. Never retry prefill.",
            "prefill_recovery": {"attempt_id": confirm_attempt, "recovered_at": stamp, "owner_state": process},
            "submission_state": "unknown", "form_state_known": False,
            "broker_submission_performed": False, "client_interacted": False,
            "ledger_mutated": False, "execution_admitted": False}
        # A recovered process never proves whether a human submitted its form.
        result.pop("submitted", None)
        db.execute("UPDATE ths_plans SET status='prefill_unknown',result=?,updated_at=? WHERE id=?",
                   (json.dumps(result), stamp, plan_id))
    return get(plan_id)


def discard(plan_id):
    with connection() as db:
        changed = db.execute("UPDATE ths_plans SET status='discarded',updated_at=? WHERE id=? AND status='planned'",
                             (now().isoformat(), plan_id)).rowcount
        if changed != 1:
            raise ValueError("Only a never-attempted plan can be discarded; this does not cancel a broker order")
    return get(plan_id)


def close_unsubmitted(plan_id):
    """Explicit human acknowledgement, not an inference from missing query rows."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        flag = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone()
        plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        if not flag or flag[0] != "true":
            raise ValueError("Pause desktop writes before acknowledging an unsubmitted preview")
        if plan["contract_id"] or plan["status"] not in {"awaiting_manual_submission", "prefill_unknown"}:
            raise ValueError("Bound, active or interrupted-process plans require reconciliation/manual review")
        result = {"status": "closed_unsubmitted", "basis": "user_confirmed_form_cleared_and_never_submitted",
                  "cancels_broker_orders": False}
        db.execute("UPDATE ths_plans SET status=?,result=?,updated_at=? WHERE id=?",
                   (result["status"], json.dumps(result), now().isoformat(), plan_id))
    return get(plan_id)


def observe(value):
    """Append one observation, deduping exact replays but retaining revisions."""
    payload = json.dumps(value, sort_keys=True)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM ths_observations WHERE account_alias=? AND kind=? "
            "AND observed_at=? AND trading_date=? ORDER BY id DESC LIMIT 1",
            (value["account_alias"], value["kind"], value["observed_at"], value["trading_date"]),
        ).fetchone()
        if row and json.dumps(json.loads(row["payload"]), sort_keys=True) == payload:
            return value
        db.execute(
            "INSERT INTO ths_observations(account_alias,kind,observed_at,trading_date,payload) VALUES(?,?,?,?,?)",
            (value["account_alias"], value["kind"], value["observed_at"], value["trading_date"], payload),
        )
    return value


def observe_many(values, *, db=None):
    """Atomically append a validated observation bundle with exact-timestamp dedupe."""
    if db is None:
        with connection() as connection_db:
            connection_db.execute("BEGIN IMMEDIATE")
            return observe_many(values, db=connection_db)
    values = list(values)
    if not values or len({value["kind"] for value in values}) != len(values):
        raise ValueError("Observation bundle must contain one row for each view kind")
    payloads = [(value["account_alias"], value["kind"], value["observed_at"],
                 value["trading_date"], json.dumps(value, sort_keys=True)) for value in values]
    inserted, existing = [], []
    for value, payload in zip(values, payloads):
        row = db.execute(
            "SELECT payload FROM ths_observations WHERE account_alias=? AND kind=? "
            "AND observed_at=? AND trading_date=? ORDER BY id DESC LIMIT 1",
            payload[:4],
        ).fetchone()
        if row:
            if json.dumps(json.loads(row["payload"]), sort_keys=True) != payload[4]:
                raise ValueError(f"Conflicting observation already exists for {value['kind']} at {value['observed_at']}")
            existing.append(value["kind"])
            continue
        db.execute("INSERT INTO ths_observations(account_alias,kind,observed_at,trading_date,payload) VALUES(?,?,?,?,?)",
                   payload)
        inserted.append(value["kind"])
    return {"inserted_kinds": inserted, "already_present_kinds": existing,
            "observation_count": len(values),
            "idempotent_replay": not inserted and len(existing) == len(values)}


def latest(account_alias, kind, trading_date=None):
    with connection() as db:
        return _latest(db, account_alias, kind, trading_date)


def _latest(db, account_alias, kind, trading_date=None, *, required=True, observed_before=None):
    query = "SELECT id,payload FROM ths_observations WHERE account_alias=? AND kind=?"
    params = [account_alias, kind]
    if trading_date:
        query += " AND trading_date=?"
        params.append(trading_date)
    if observed_before is None:
        row = db.execute(query + " ORDER BY julianday(observed_at) DESC,id DESC LIMIT 1", params).fetchone()
    else:
        rows = db.execute(query + " AND julianday(observed_at)<=julianday(?)", [*params, observed_before]).fetchall()
        cutoff = datetime.fromisoformat(observed_before)
        eligible = [r for r in rows if datetime.fromisoformat(json.loads(r["payload"])["observed_at"]) <= cutoff]
        row = max(eligible, key=lambda r: (datetime.fromisoformat(json.loads(r["payload"])["observed_at"]), r["id"]), default=None)
    if not row:
        if not required:
            return None
        raise ValueError("No broker observation for this account/view/date")
    return json.loads(row["payload"])


def bind(plan_id, contract_id, trading_date):
    from datetime import date, datetime, timedelta, timezone
    date.fromisoformat(trading_date)
    contract_id = identifier(contract_id)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
        if plan["status"] not in {"awaiting_manual_submission", "prefill_unknown", "submission_unknown", "reconciliation_required"}:
            raise ValueError("Plan is not awaiting a manually submitted broker contract")
        planned_day = datetime.fromisoformat(plan["created_at"]).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        if trading_date != planned_day:
            raise ValueError("Broker date differs from the preview date")
        if plan["contract_id"] and (plan["contract_id"], plan["trading_date"]) != (contract_id, trading_date):
            raise ValueError("A broker binding cannot be silently reassigned")
        db.execute("UPDATE ths_plans SET contract_id=?,trading_date=?,updated_at=? WHERE id=?",
                   (contract_id, trading_date, now().isoformat(), plan_id))
    return get(plan_id)


def refresh(plan_id, *, db=None, interval_end=None):
    if db is None:
        with connection() as connection_db:
            connection_db.execute("BEGIN IMMEDIATE")
            return refresh(plan_id, db=connection_db, interval_end=interval_end)
    # The importer can share this serialized snapshot with its existing account write.
    plan = decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())
    if not plan["contract_id"]:
        return {"plan": plan, "observation": {"status": "submission_unknown", "reason": "Actual broker contract ID required"}}
    following = [json.loads(r[0])["created_at"] for r in db.execute(
        "SELECT payload FROM ths_plans WHERE account_alias=? AND id!=?",
        (plan["account_alias"], plan_id)).fetchall()]
    following = [stamp for stamp in following
                 if datetime.fromisoformat(stamp) > datetime.fromisoformat(plan["created_at"])]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        for account in db.execute("SELECT state FROM runs WHERE json_extract(state,'$.broker_binding.account_alias')=?",
                                  (plan["account_alias"],)):
            following.extend(s["observed_at"] for day, s in json.loads(account[0]).get("broker_sessions", {}).items()
                             if day > plan["trading_date"])
    if interval_end is not None:
        if datetime.fromisoformat(interval_end) <= datetime.fromisoformat(plan["created_at"]):
            raise ValueError("Session interval end must follow the original plan")
        following.append(interval_end)
    cutoff = min(following, key=datetime.fromisoformat) if following else None
    orders = _latest(db, plan["account_alias"], "orders", plan["trading_date"])
    trades = _latest(db, plan["account_alias"], "trades", plan["trading_date"])
    try:
        result = reconcile(plan, orders, trades)
    except ValueError as error:
        result = {"status": "reconciliation_required", "reason": "broker_observation_conflict",
                  "error": str(error), "orders_observed_at": orders["observed_at"],
                  "trades_observed_at": trades["observed_at"], "executions": [],
                  "account_cash_reconciled": False, "submitted_by_adapter": False}
    result = retain_execution_evidence(plan["result"], result)
    account = _latest(db, plan["account_alias"], "account", plan["trading_date"], required=False, observed_before=cutoff)
    cashflows = _latest(db, plan["account_alias"], "cashflows", plan["trading_date"],
                        required=False, observed_before=cutoff)
    result = reconcile_account(plan, result, account, cashflows, interval_closed=cutoff is not None)
    if cutoff:
        result["reconciliation_interval_end"] = cutoff
    if (plan.get("result") or {}).get("ledger_import"):
        result["ledger_import"] = plan["result"]["ledger_import"]
    db.execute("UPDATE ths_plans SET status=?,result=?,updated_at=? WHERE id=?",
               (result["status"], json.dumps(result), now().isoformat(), plan_id))
    return decode(db.execute("SELECT * FROM ths_plans WHERE id=?", (plan_id,)).fetchone())


def status():
    with connection() as db:
        rows = db.execute("SELECT * FROM ths_plans ORDER BY updated_at DESC LIMIT 20").fetchall()
    paused_state = paused()
    return {"paused": paused_state, "kill_switch": "engaged" if paused_state else "disengaged",
            "submission_enabled": False, "plans": [decode(row) for row in rows]}
