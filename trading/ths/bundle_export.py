"""Materialize structured THS journal observations as four-file exports."""
import csv
import json
from pathlib import Path
import shutil


KINDS = ("account", "orders", "trades", "cashflows")
HEADERS = {
    "orders": ("symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"),
    "trades": ("symbol", "market", "side", "order_id", "execution_id", "qty", "price"),
    "cashflows": ("symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"),
}


def _rmb(cents):
    if type(cents) is not int or cents < 0:
        raise ValueError("Journal money fields must be nonnegative integer cents")
    return f"{cents / 100:.2f}"


def _symbol_parts(symbol):
    if not isinstance(symbol, str) or symbol.count(".") != 1:
        raise ValueError("Journal symbols must be exchange-qualified")
    code, market = symbol.split(".", 1)
    if len(code) != 6 or market not in {"SSE", "SZSE", "BSE"}:
        raise ValueError("Journal symbol has an unsupported exchange or code")
    return code, market


def _target_is_replaceable(path, kind):
    if not path.exists():
        return True
    if kind == "account":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return payload.get("schema") == "ths_account_observation_v1" and (
            payload.get("account_alias", "").startswith("<fill-")
            or payload.get("observed_at", "").startswith("<fill-")
        )
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
    except (OSError, UnicodeError, csv.Error):
        return False
    return len(rows) <= 1


def _account_payload(value):
    positions = []
    marks = {}
    for row in value.get("rows", []):
        symbol = row["symbol"]
        _symbol_parts(symbol)
        position = {"symbol": symbol, "quantity": row["quantity"], "sellable": row["sellable"]}
        if "price_cents" in row:
            position["price"] = _rmb(row["price_cents"])
            marks[symbol] = row["price_cents"]
        positions.append(position)
    payload = {
        "schema": "ths_account_observation_v1",
        "source": value["source"],
        "account_alias": value["account_alias"],
        "observed_at": value["observed_at"],
        "trading_date": value["trading_date"],
        "cash_basis": value.get("cash_basis"),
        "cash_total": value.get("cash_total"),
        "cash_frozen": value.get("cash_frozen"),
        "cash_available": value.get("cash_available"),
        "positions": positions,
    }
    if marks:
        payload["marks"] = marks
    return payload


def _csv_rows(kind, value):
    result = []
    for row in value.get("rows", []):
        code, market = _symbol_parts(row["symbol"])
        base = {"symbol": code, "market": market, "side": row.get("side"), "order_id": row["order_id"]}
        if kind == "orders":
            base.update(qty=row["qty"], price=_rmb(row["price_cents"]),
                        filled_qty=row["filled_qty"], status=row.get("broker_status", row.get("status")))
        elif kind == "trades":
            base.update(execution_id=row["execution_id"], qty=row["qty"], price=_rmb(row["price_cents"]))
        else:
            base.update(gross_amount=_rmb(row["gross_cents"]), fees=_rmb(row["fees_cents"]),
                        cash_change=("-" if row["cash_change_cents"] < 0 else "")
                        + _rmb(abs(row["cash_change_cents"])))
        result.append(base)
    return result


def export_observations(values, root):
    """Write only selected journal views; never overwrite a non-template export."""
    values = list(values)
    if not values or any(value.get("kind") not in KINDS for value in values):
        raise ValueError("Export requires one or more supported observation kinds")
    aliases = {value.get("account_alias") for value in values}
    dates = {value.get("trading_date") for value in values}
    if len(aliases) != 1 or len(dates) != 1:
        raise ValueError("All exported observations must share account and trading date")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    paths = {"account": root / "account_observation.json",
             "orders": root / "orders.csv", "trades": root / "trades.csv",
             "cashflows": root / "cashflows.csv"}
    if len({value["kind"] for value in values}) != len(values):
        raise ValueError("Each observation kind may be exported once")
    # Preflight every destination before writing any member of the bundle.
    # This prevents a later non-template file from leaving an earlier member
    # partially refreshed.
    for value in values:
        kind = value["kind"]
        if not _target_is_replaceable(paths[kind], kind):
            raise ValueError(f"Refusing to overwrite non-template export: {paths[kind]}")
    for value in values:
        kind = value["kind"]
        if kind == "account":
            paths[kind].write_text(json.dumps(_account_payload(value), ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        else:
            with paths[kind].open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=HEADERS[kind])
                writer.writeheader()
                writer.writerows(_csv_rows(kind, value))
    kinds = sorted(value["kind"] for value in values)
    missing_kinds = [kind for kind in KINDS if kind not in kinds]
    return {
        "schema": "ths_observation_bundle_export_v1",
        "status": "materialized" if not missing_kinds else "materialized_partial",
        "account_alias": next(iter(aliases)),
        "trading_date": next(iter(dates)),
        "kinds": kinds,
        "missing_kinds": missing_kinds,
        "files": {kind: str(paths[kind]) for kind in sorted(value["kind"] for value in values)},
        "execution_admitted": False,
        "client_operation": False,
        "ledger_mutated": False,
        "next_action": (
            "export the missing cashflows view with cumulative_order_cashflow semantics before reconciliation"
            if missing_kinds else
            "review the four-file pack, then explicitly import or reconcile it"
        ),
    }


def attach_cashflow_export(source, root):
    """Attach one explicit user cashflow export without replacing evidence."""
    source = Path(source).expanduser()
    if not source.is_file():
        raise ValueError(f"Missing cashflow export: {source}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "cashflows.csv"
    if not _target_is_replaceable(target, "cashflows"):
        raise ValueError(f"Refusing to overwrite non-template export: {target}")
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target
