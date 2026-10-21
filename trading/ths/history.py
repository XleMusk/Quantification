"""Read saved plan/receipt evidence, never refresh a client or import a ledger."""
import json

from ..store import connection
from .journal import decode


def history(run_id, *, limit=50, offset=0):
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Invalid broker history page")
    with connection() as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = db.execute("SELECT mode,state,error FROM runs WHERE id=?", (run_id,)).fetchone() if "runs" in tables else None
        if row is None:
            raise LookupError("Unknown run")
        binding = json.loads(row["state"]).get("broker_binding", {})
        if (row["mode"] != "paper" or binding.get("channel") != "ths"
                or binding.get("broker_account_kind") != "simulated" or not binding.get("account_alias")):
            raise PermissionError("Broker history requires a bound simulated THS run")
        alias = binding["account_alias"]
        pause = db.execute("SELECT value FROM ths_control WHERE key='paused'").fetchone() if "ths_control" in tables else None
        total, rows = 0, []
        if "ths_plans" in tables:
            total = db.execute("SELECT count(*) FROM ths_plans WHERE account_alias=?", (alias,)).fetchone()[0]
            rows = db.execute("SELECT * FROM ths_plans WHERE account_alias=? "
                              "ORDER BY julianday(updated_at) DESC,id DESC LIMIT ? OFFSET ?",
                              (alias, limit, offset)).fetchall()
        plans = []
        for saved in rows:
            plan = decode(saved)
            result = plan.get("result") or {}
            imported = result.get("ledger_import") or {}
            plans.append({**{key: plan.get(key) for key in (
                "id", "symbol", "side", "qty", "price_cents", "status", "contract_id",
                "trading_date", "created_at", "updated_at", "expires_at")},
                "account_observed_at": plan["snapshot"]["observed_at"],
                "confirmed_filled_qty": result.get("confirmed_filled_qty"),
                "confirmed_executions": result.get("confirmed_executions"),
                "orders_observed_at": result.get("orders_observed_at"),
                "trades_observed_at": result.get("trades_observed_at"),
                "account_reconciliation": result.get("account_reconciliation"),
                "confirmed_account_reconciliation": result.get("confirmed_account_reconciliation"),
                "ledger_imported": imported.get("ledger_imported") is True and imported.get("run_id") == run_id,
                "reason": result.get("reason"), "error": result.get("ledger_import_error") or result.get("error")})
    paused_state = pause[0] == "true" if pause else None
    return {"run_id": run_id, "account_alias": alias, "total": total, "limit": limit,
            "offset": offset, "plans": plans, "paused": paused_state,
            "kill_switch": ("engaged" if paused_state else "disengaged") if paused_state is not None else None,
            "run_error": row["error"], "source": "saved_broker_observations",
            "client_interacted": False, "submission_enabled": False}
