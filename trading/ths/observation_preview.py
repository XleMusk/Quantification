"""Validate a THS export bundle without saving observations or touching a client."""
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

from .observations import csv_rows, observation


ACCOUNT_FIELDS = {
    "symbol": "symbol",
    "market": "market",
    "quantity": "quantity",
    "sellable": "sellable",
}
ORDERS_FIELDS = {
    "symbol": "symbol",
    "market": "market",
    "side": "side",
    "order_id": "order_id",
    "qty": "qty",
    "price": "price",
    "filled_qty": "filled_qty",
    "status": "status",
}
TRADES_FIELDS = {
    "symbol": "symbol",
    "market": "market",
    "side": "side",
    "order_id": "order_id",
    "execution_id": "execution_id",
    "qty": "qty",
    "price": "price",
}
CASHFLOW_FIELDS = {
    "symbol": "symbol",
    "market": "market",
    "side": "side",
    "order_id": "order_id",
    "gross_amount": "gross_amount",
    "fees": "fees",
    "cash_change": "cash_change",
}
MARKETS = {"SSE": "SSE", "SZSE": "SZSE", "BSE": "BSE",
           "SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
SIDES = {"buy": "buy", "sell": "sell", "BUY": "buy", "SELL": "sell"}
STATUSES = {
    "accepted": "accepted", "partial": "partial", "filled": "filled",
    "cancelled": "cancelled", "rejected": "rejected",
    "ACCEPTED": "accepted", "PARTIAL": "partial", "FILLED": "filled",
    "CANCELLED": "cancelled", "REJECTED": "rejected",
}

CORE_PROFILE_KINDS = ("account", "orders", "trades", "cashflows")
STATEMENT_PROFILE_KINDS = ("movements", "restriction_releases", "share_credits")


def validate_observation_profile(profile, *, kinds=CORE_PROFILE_KINDS):
    """Validate an explicit export mapping without reading files or a client.

    A profile without ``tables`` is the built-in normalized contract plus an
    encoding/delimiter override.  Once ``tables`` is supplied, every selected
    view must be mapped explicitly so a typo cannot silently fall back to the
    normalized column names before a Ledger import.
    """
    profile = profile or {}
    if not isinstance(profile, dict):
        return {"status": "invalid", "valid": False,
                "errors": ["Observation profile must be a JSON object"],
                "kinds": list(kinds)}
    tables = profile.get("tables")
    if tables is None:
        return {
            "status": "default_mapping_contract",
            "valid": True,
            "explicit": False,
            "local_mapping_verified": profile.get("verified_local") is True,
            "kinds": list(kinds),
            "missing_kinds": [],
            "errors": [],
        }
    if not isinstance(tables, dict):
        return {"status": "invalid", "valid": False, "explicit": True,
                "kinds": list(kinds), "errors": ["Profile tables must be an object"]}

    required = {
        "account": {"symbol", "market", "quantity", "sellable"},
        "orders": {"symbol", "market", "side", "order_id", "qty", "price", "filled_qty", "status"},
        "trades": {"symbol", "market", "side", "order_id", "execution_id", "qty", "price"},
        "cashflows": {"symbol", "market", "side", "order_id", "gross_amount", "fees", "cash_change"},
        "movements": {"movement_id", "kind", "cash_change"},
        "restriction_releases": {"release_id", "symbol", "market", "released_qty", "release_day", "reason"},
        "share_credits": {"credit_id", "symbol", "market", "credited_qty", "credit_day", "available_day", "kind"},
    }
    errors = []
    missing = []
    for kind in kinds:
        mapping = tables.get(kind)
        if not isinstance(mapping, dict):
            missing.append(kind)
            continue
        columns = mapping.get("columns")
        if not isinstance(columns, dict):
            errors.append(f"{kind}: columns must be an object")
            continue
        allowed_columns = required[kind]
        if kind == "account" and "price" in columns:
            allowed_columns = allowed_columns | {"price"}
        if set(columns) != allowed_columns:
            errors.append(
                f"{kind}: columns must be exactly {sorted(allowed_columns)}"
            )
        elif any(not isinstance(value, str) or not value.strip() for value in columns.values()):
            errors.append(f"{kind}: mapped export headers must be non-empty strings")
        elif len(set(columns.values())) != len(columns):
            errors.append(f"{kind}: mapped export headers must be unique")
        if kind in {"account", "orders", "trades", "cashflows", "restriction_releases", "share_credits"}:
            if not isinstance(mapping.get("markets"), dict):
                errors.append(f"{kind}: markets mapping is required")
        if kind in {"orders", "trades", "cashflows"} and not isinstance(mapping.get("sides"), dict):
            errors.append(f"{kind}: sides mapping is required")
        if kind == "orders" and not isinstance(mapping.get("statuses"), dict):
            errors.append("orders: statuses mapping is required")
        if kind == "account" and "price" in columns and mapping.get("price_basis") != "raw_rmb":
            errors.append("account: priced holdings must declare price_basis=raw_rmb")
        if kind == "cashflows":
            if mapping.get("basis") != "cumulative_order_cashflow":
                errors.append("cashflows: basis must be cumulative_order_cashflow")
            if mapping.get("currency") != "CNY" or mapping.get("amount_unit") != "rmb":
                errors.append("cashflows: currency=CNY and amount_unit=rmb are required")
        if kind == "movements":
            if mapping.get("basis") != "nontrade_cash_movements" or mapping.get("currency") != "CNY" or mapping.get("amount_unit") != "rmb":
                errors.append("movements: nontrade cash movement semantics are required")
        if kind in {"restriction_releases", "share_credits"}:
            expected_basis = {
                "restriction_releases": "opening_restriction_releases",
                "share_credits": "account_bonus_share_credits",
            }[kind]
            if mapping.get("basis") != expected_basis or mapping.get("quantity_unit") != "share":
                errors.append(f"{kind}: explicit share-unit statement semantics are required")
    errors.extend(f"missing explicit table mapping: {kind}" for kind in missing)
    return {
        "status": "shape_valid" if not errors else "invalid",
        "valid": not errors,
        "explicit": True,
        "local_mapping_verified": profile.get("verified_local") is True,
        "kinds": list(kinds),
        "missing_kinds": missing,
        "errors": errors,
    }


def _mapping(kind, profile):
    declared = (profile or {}).get("tables", {}).get(kind)
    if declared:
        return declared
    fields = {
        "account": ACCOUNT_FIELDS,
        "orders": ORDERS_FIELDS,
        "trades": TRADES_FIELDS,
        "cashflows": CASHFLOW_FIELDS,
    }[kind]
    value = {
        "columns": fields,
        "markets": MARKETS,
    }
    if kind == "orders":
        value.update(sides=SIDES, statuses=STATUSES)
    elif kind == "trades":
        value["sides"] = SIDES
    elif kind == "cashflows":
        value.update(
            sides=SIDES,
            basis="cumulative_order_cashflow",
            currency="CNY",
            amount_unit="rmb",
        )
    return value


def _csv_encoding(profile, kind):
    """Resolve an explicit CSV encoding from the operator profile.

    THS exports on Windows are often GB18030/ANSI while the normalized bundle
    remains UTF-8.  Keep UTF-8-SIG as the default and never guess an encoding
    from file bytes; a profile may set one encoding for all CSVs or override a
    specific table.
    """
    profile = profile or {}
    encodings = profile.get("encodings") or {}
    if not isinstance(encodings, dict):
        raise ValueError("CSV encodings profile must be an object")
    encoding = encodings.get(kind, profile.get("encoding", "utf-8-sig"))
    if not isinstance(encoding, str) or not encoding.strip():
        raise ValueError(f"CSV encoding for {kind} must be a non-empty string")
    return encoding.strip()


def _csv_delimiter(profile, kind):
    """Resolve an explicit comma/tab delimiter from the operator profile.

    THS exports can be comma-separated or tab-separated depending on the
    export surface.  Delimiter detection is intentionally forbidden here:
    the profile must state the table format so a malformed export cannot be
    silently reinterpreted.
    """
    profile = profile or {}
    delimiters = profile.get("delimiters") or {}
    if not isinstance(delimiters, dict):
        raise ValueError("CSV delimiters profile must be an object")
    value = delimiters.get(kind, profile.get("delimiter", "comma"))
    if value in {",", "comma"}:
        return ","
    if value in {"\t", "tab"}:
        return "\t"
    raise ValueError(f"CSV delimiter for {kind} must be explicit comma or tab")


def _is_blank_text(path, *, encoding="utf-8-sig"):
    lines = Path(path).read_text(encoding=encoding).splitlines()
    # Header-only exports are valid for an account with no open orders or
    # trades.  Treat only a truly empty file as an unfilled template.
    return not any(line.strip() for line in lines)


def _is_blank_account(payload):
    placeholders = {None, "", "<fill-from-user-selected-account>",
                    "<fill-with-actual-observation-time+08:00>"}
    return (payload.get("account_alias") in placeholders
            or payload.get("observed_at") in placeholders
            or payload.get("cash_available") in placeholders
            or payload.get("cash_total") in placeholders
            or payload.get("cash_frozen") in placeholders
            # An empty position list is a valid observed cash-only account.
            # Template detection is already covered by the required cash/time
            # placeholders above; only an omitted positions field is invalid.
            or payload.get("positions") is None)


def _rmb_from_cents(value):
    if type(value) is not int or value < 0:
        raise ValueError("Account cash fields must be nonnegative integer cents")
    return f"{value / 100:.2f}"


def _account_observation(payload, mapping):
    if payload.get("schema") != "ths_account_observation_v1":
        raise ValueError("Account JSON must declare ths_account_observation_v1")
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise ValueError("Account JSON positions must be a list in the operator template")
    rows = []
    marks = payload.get("marks") or {}
    for item in positions:
        if not isinstance(item, dict):
            raise ValueError("Each account position must be an object")
        symbol = str(item.get("symbol", ""))
        if "." not in symbol:
            raise ValueError("Account positions require exchange-qualified symbols")
        code, market = symbol.split(".", 1)
        row = {
            "symbol": code,
            "market": market,
            "quantity": str(item.get("quantity", "")),
            "sellable": str(item.get("sellable", "")),
        }
        if "price" in mapping["columns"]:
            price = item.get("price")
            if price is None and symbol in marks:
                price = _rmb_from_cents(marks[symbol])
            if price is None:
                raise ValueError(f"Account position {symbol} is missing an explicit raw RMB price")
            row["price"] = str(price)
        rows.append(row)
    if payload.get("cash_available") is None:
        raise ValueError("Account JSON is missing cash_available")
    cash_total = payload.get("cash_total")
    cash_frozen = payload.get("cash_frozen")
    if cash_total is None or cash_frozen is None:
        raise ValueError("Account JSON needs cash_total and cash_frozen for reconciliation")
    return observation(
        "account",
        rows,
        mapping,
        account_alias=payload.get("account_alias"),
        observed_at=payload.get("observed_at"),
        trading_date=payload.get("trading_date"),
        source=payload.get("source"),
        cash=_rmb_from_cents(payload["cash_available"]),
        cash_total=_rmb_from_cents(cash_total),
        cash_frozen=_rmb_from_cents(cash_frozen),
    )


def _load_csv_observation(kind, path, mapping, account, observed_at, *, encoding="utf-8-sig",
                          delimiter=","):
    rows = csv_rows(path, mapping["columns"], encoding=encoding, delimiter=delimiter)
    return observation(
        kind,
        rows,
        mapping,
        account_alias=account["account_alias"],
        observed_at=observed_at,
        trading_date=account["trading_date"],
        source="ths_user_export",
    )


def _linkage(account, orders, trades, cashflows):
    order_rows = orders["rows"]
    trade_rows = trades["rows"]
    cash_rows = cashflows["rows"]
    order_ids = {row["order_id"] for row in order_rows}
    trade_ids = {row["order_id"] for row in trade_rows}
    cash_ids = {row["order_id"] for row in cash_rows}
    fills = defaultdict(int)
    trade_rows_by_order = defaultdict(list)
    cash_rows_by_order = defaultdict(list)
    for row in trade_rows:
        fills[row["order_id"]] += row["qty"]
        trade_rows_by_order[row["order_id"]].append(row)
    for row in cash_rows:
        cash_rows_by_order[row["order_id"]].append(row)
    mismatches = []
    trade_identity_mismatches = []
    cashflow_identity_mismatches = []
    status_fill_mismatches = []
    order_summaries = []
    for row in order_rows:
        observed_filled = fills[row["order_id"]]
        trade_rows_for_order = trade_rows_by_order[row["order_id"]]
        cash_rows_for_order = cash_rows_by_order[row["order_id"]]
        trade_gross = sum(item["qty"] * item["price_cents"] for item in trade_rows_for_order)
        cash = cash_rows_for_order[0] if len(cash_rows_for_order) == 1 else None
        status = "matched"
        reasons = []
        broker_status = row["status"]
        invalid_status_fill = (
            (broker_status == "filled" and row["filled_qty"] != row["qty"])
            or (broker_status == "partial" and not 0 < row["filled_qty"] < row["qty"])
            or (broker_status in {"accepted", "rejected"} and row["filled_qty"] != 0)
        )
        if invalid_status_fill:
            status_fill_mismatches.append({
                "order_id": row["order_id"],
                "status": broker_status,
                "order_qty": row["qty"],
                "filled_qty": row["filled_qty"],
            })
            status = "status_fill_mismatch"
            reasons.append("broker status is inconsistent with filled quantity")
        for trade in trade_rows_for_order:
            if trade["symbol"] != row["symbol"] or trade["side"] != row["side"]:
                mismatch = {
                    "order_id": row["order_id"],
                    "order_symbol": row["symbol"],
                    "order_side": row["side"],
                    "trade_symbol": trade["symbol"],
                    "trade_side": trade["side"],
                    "execution_id": trade["execution_id"],
                }
                trade_identity_mismatches.append(mismatch)
                reasons.append("trade symbol or side differs from order")
        for cashflow in cash_rows_for_order:
            if cashflow["symbol"] != row["symbol"] or cashflow["side"] != row["side"]:
                mismatch = {
                    "order_id": row["order_id"],
                    "order_symbol": row["symbol"],
                    "order_side": row["side"],
                    "cashflow_symbol": cashflow["symbol"],
                    "cashflow_side": cashflow["side"],
                }
                cashflow_identity_mismatches.append(mismatch)
                reasons.append("cashflow symbol or side differs from order")
        if observed_filled != row["filled_qty"]:
            status = "quantity_mismatch"
            reasons.append("trade quantity differs from order filled quantity")
        elif status == "status_fill_mismatch":
            pass
        elif reasons:
            status = "identity_mismatch"
        if not cash_rows_for_order:
            status = "cashflow_missing"
            reasons.append("cumulative cashflow is missing")
        elif len(cash_rows_for_order) > 1:
            status = "cashflow_ambiguous"
            reasons.append("more than one cumulative cashflow row")
        else:
            expected_cash = (trade_gross if row["side"] == "sell" else -trade_gross) - cash["fees_cents"]
            if cash["gross_cents"] != trade_gross or cash["cash_change_cents"] != expected_cash:
                status = "cashflow_mismatch"
                reasons.append("cashflow gross/signed change does not match observed trades")
        order_summaries.append({
            "order_id": row["order_id"],
            "symbol": row["symbol"],
            "side": row["side"],
            "broker_status": row["broker_status"],
            "order_qty": row["qty"],
            "order_filled_qty": row["filled_qty"],
            "trade_count": len(trade_rows_for_order),
            "trade_filled_qty": observed_filled,
            "trade_gross_cents": trade_gross,
            "cashflow_present": bool(cash_rows_for_order),
            "cashflow_gross_cents": cash["gross_cents"] if cash else None,
            "fees_cents": cash["fees_cents"] if cash else None,
            "cash_change_cents": cash["cash_change_cents"] if cash else None,
            "status": status,
            "reasons": reasons,
        })
        if observed_filled != row["filled_qty"]:
            mismatches.append({
                "order_id": row["order_id"],
                "order_filled_qty": row["filled_qty"],
                "trade_filled_qty": observed_filled,
            })
    orphan_trades = sorted(trade_ids - order_ids)
    orphan_cashflows = sorted(cash_ids - order_ids)
    missing_cashflows = sorted(order_ids - cash_ids)
    return {
        "order_count": len(order_rows),
        "trade_count": len(trade_rows),
        "cashflow_count": len(cash_rows),
        "order_status_counts": dict(sorted(Counter(row["status"] for row in order_rows).items())),
        "orphan_trade_order_ids": orphan_trades,
        "orphan_cashflow_order_ids": orphan_cashflows,
        "orders_missing_cashflow_order_ids": missing_cashflows,
        "filled_quantity_mismatches": mismatches,
        "trade_identity_mismatches": trade_identity_mismatches,
        "cashflow_identity_mismatches": cashflow_identity_mismatches,
        "status_fill_mismatches": status_fill_mismatches,
        "trade_qty_total": sum(row["qty"] for row in trade_rows),
        "gross_cashflow_cents": sum(row["cash_change_cents"] for row in cash_rows),
        "gross_trade_value_cents": sum(row["qty"] * row["price_cents"] for row in trade_rows),
        "fees_cents": sum(row["fees_cents"] for row in cash_rows),
        "position_count": len(account["positions"]),
        "order_summaries": order_summaries,
        "order_level_issues": [
            {"order_id": summary["order_id"], "status": summary["status"],
             "reasons": summary["reasons"]}
            for summary in order_summaries if summary["status"] != "matched"
        ],
    }


def preview_bundle(account_path, orders_path, trades_path, cashflows_path, *, profile=None,
                   observed_at=None):
    """Return a non-persistent, explicit readiness preview for four exports."""
    paths = [Path(account_path), Path(orders_path), Path(trades_path), Path(cashflows_path)]
    mapping_profile = profile or {}
    if any(not path.is_file() for path in paths):
        missing = [str(path) for path in paths if not path.is_file()]
        return {"schema": "ths_observation_bundle_preview_v1", "status": "missing_template", "missing": missing,
                "execution_admitted": False, "ledger_mutated": False,
                "next_action": "将同花顺当前账户、委托、成交和累计资金流水导出到指定目录后重新预览"}
    profile_validation = validate_observation_profile(mapping_profile)
    if not profile_validation["valid"]:
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "validation_error",
            "execution_admitted": False,
            "ledger_mutated": False,
            "profile_validation": profile_validation,
            "errors": profile_validation["errors"],
            "next_action": "修正 profile.json 的四张核心表字段映射、单位和语义声明后重新预览；未执行导入",
        }
    try:
        account_payload = json.loads(paths[0].read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "validation_error",
            "execution_admitted": False,
            "ledger_mutated": False,
            "errors": [f"Account JSON cannot be read: {error}"],
            "next_action": "replace the account export with valid UTF-8 JSON; no import was performed",
        }


    if not isinstance(account_payload, dict):
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "validation_error",
            "execution_admitted": False,
            "ledger_mutated": False,
            "errors": ["Account JSON must be an object"],
            "next_action": "replace the account export with a JSON object; no import was performed",
        }
    try:
        csv_encodings = {kind: _csv_encoding(mapping_profile, kind)
                         for kind in ("orders", "trades", "cashflows")}
        csv_delimiters = {kind: _csv_delimiter(mapping_profile, kind)
                          for kind in ("orders", "trades", "cashflows")}
        blank_csv = any(_is_blank_text(path, encoding=csv_encodings[kind])
                        for kind, path in zip(("orders", "trades", "cashflows"), paths[1:]))
    except (LookupError, UnicodeError, ValueError) as error:
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "validation_error",
            "execution_admitted": False,
            "ledger_mutated": False,
            "errors": [str(error)],
            "next_action": "correct the export encoding/profile; no import was performed",
        }
    # A real account snapshot may legitimately have header-only order, trade,
    # and cashflow exports on a quiet session.  Only an untouched account
    # payload keeps the bundle in the template state; the downstream parser
    # still validates the exact headers and account/date identity.
    if _is_blank_account(account_payload):
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "templates_blank",
            "execution_admitted": False,
            "ledger_mutated": False,
            "profile_validation": profile_validation,
            "inputs": {"account": str(paths[0]), "orders": str(paths[1]),
                       "trades": str(paths[2]), "cashflows": str(paths[3])},
            "next_action": "填写当前同花顺账户观察；委托/成交无记录时保留真实导出的表头空表，然后重新预览",
        }
    stamp = observed_at or account_payload.get("observed_at")
    try:
        account = _account_observation(account_payload, _mapping("account", mapping_profile))
        observations = {
            kind: _load_csv_observation(kind, path, _mapping(kind, mapping_profile), account, stamp,
                                        encoding=csv_encodings[kind], delimiter=csv_delimiters[kind])
            for kind, path in (("orders", paths[1]), ("trades", paths[2]), ("cashflows", paths[3]))
        }
        identity_errors = []
        for kind, value in observations.items():
            if value["account_alias"] != account["account_alias"]:
                identity_errors.append(f"{kind}: account alias mismatch")
            if value["trading_date"] != account["trading_date"]:
                identity_errors.append(f"{kind}: trading date mismatch")
        linkage = _linkage(account, observations["orders"], observations["trades"], observations["cashflows"])
        errors = identity_errors[:]
        if linkage["orphan_trade_order_ids"]:
            errors.append("trade rows reference unknown order IDs")
        if linkage["orphan_cashflow_order_ids"]:
            errors.append("cashflow rows reference unknown order IDs")
        if linkage["orders_missing_cashflow_order_ids"]:
            errors.append("orders are missing cumulative cashflow statements")
        if linkage["filled_quantity_mismatches"]:
            errors.append("order filled quantities do not match trade quantities")
        if linkage["trade_identity_mismatches"]:
            errors.append("trade rows do not match their order symbol/side")
        if linkage["cashflow_identity_mismatches"]:
            errors.append("cashflow rows do not match their order symbol/side")
        if linkage["status_fill_mismatches"]:
            errors.append("order statuses do not match filled quantities")
        if linkage["order_level_issues"]:
            errors.append("one or more orders fail per-order trade/cashflow reconciliation")
        status = "bundle_ready_for_reconciliation" if not errors else "validation_error"
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": status,
            "execution_admitted": False,
            "ledger_mutated": False,
            "profile_validation": profile_validation,
            "account": {"account_alias": account["account_alias"], "observed_at": account["observed_at"],
                         "trading_date": account["trading_date"]},
            "observations": {kind: {"observed_at": value["observed_at"], "row_count": len(value["rows"])}
                             for kind, value in observations.items()},
            "linkage": linkage,
            "errors": errors,
            "next_action": ("review the bundle, then use explicit import-export/reconcile commands; no import was performed"
                            if status == "bundle_ready_for_reconciliation" else "correct the listed export issues; no import was performed"),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "schema": "ths_observation_bundle_preview_v1",
            "status": "validation_error",
            "execution_admitted": False,
            "ledger_mutated": False,
            "profile_validation": profile_validation,
            "errors": [str(error)],
            "next_action": "correct the export templates; no import was performed",
        }


def is_blank_bundle(account_path, orders_path, trades_path, cashflows_path, *, profile=None):
    """Return true only when every existing bundle member is still untouched.

    ``preview_bundle`` intentionally reports ``templates_blank`` when any one
    member is blank, which is useful for import readiness but unsafe for a
    refresh operation. Template refresh must use this stricter all-members
    predicate so a partially filled account export can never be overwritten.
    """
    paths = [Path(account_path), Path(orders_path), Path(trades_path), Path(cashflows_path)]
    if any(not path.is_file() for path in paths):
        return False
    try:
        account_payload = json.loads(paths[0].read_text(encoding="utf-8-sig"))
        if not isinstance(account_payload, dict) or not _is_blank_account(account_payload):
            return False
        encodings = {
            kind: _csv_encoding(profile or {}, kind)
            for kind in ("orders", "trades", "cashflows")
        }
        return all(
            len([line for line in path.read_text(encoding=encodings[kind]).splitlines() if line.strip()]) <= 1
            for kind, path in zip(("orders", "trades", "cashflows"), paths[1:])
        )
    except (OSError, UnicodeError, json.JSONDecodeError, LookupError, ValueError, TypeError):
        return False


def build_observation_bundle(account_path, orders_path, trades_path, cashflows_path, *, profile=None,
                             observed_at=None):
    """Build validated normalized observations for an explicit local consumer."""
    import json
    preview = preview_bundle(account_path, orders_path, trades_path, cashflows_path,
                             profile=profile, observed_at=observed_at)
    if preview.get("status") != "bundle_ready_for_reconciliation":
        raise ValueError(f"Observation bundle is not importable: {preview.get('status')}; {preview.get('errors', [])}")
    account_payload = json.loads(Path(account_path).read_text(encoding="utf-8-sig"))
    account = _account_observation(account_payload, _mapping("account", profile or {}))
    stamp = observed_at or account_payload.get("observed_at")
    observations = [account]
    for kind, path in (("orders", orders_path), ("trades", trades_path), ("cashflows", cashflows_path)):
        observations.append(_load_csv_observation(
            kind, path, _mapping(kind, profile or {}), account, stamp,
            encoding=_csv_encoding(profile, kind),
            delimiter=_csv_delimiter(profile, kind),
        ))
    return preview, observations


def build_statement_observations(account, statement_paths, *, profile=None, observed_at=None):
    """Parse explicit non-trade account statements for the same journal.

    These views are deliberately separate from the four-table order bundle:
    they are statement evidence for a later account/session reconciliation, not
    an inferred trade or an automatic Ledger mutation.
    """
    profile = profile or {}
    if not isinstance(statement_paths, dict) or not statement_paths:
        raise ValueError("Statement paths must contain at least one selected export")
    allowed = {"movements", "restriction_releases", "share_credits"}
    selected = set(statement_paths)
    if not selected <= allowed:
        raise ValueError("Unsupported statement export kind")
    profile_validation = validate_observation_profile(profile or {}, kinds=tuple(sorted(selected)))
    if not profile_validation["valid"]:
        raise ValueError("Invalid statement export profile: " + "; ".join(profile_validation["errors"]))
    tables = profile.get("tables") or {}
    if not isinstance(tables, dict) or any(not tables.get(kind) for kind in selected):
        raise ValueError("Each selected statement export requires an explicit profile mapping")
    stamp = observed_at or account.get("observed_at")
    result = []
    for kind in ("movements", "restriction_releases", "share_credits"):
        if kind not in selected:
            continue
        path = Path(statement_paths[kind])
        if not path.is_file():
            raise ValueError(f"Selected statement export is missing: {path}")
        result.append(_load_csv_observation(
            kind, path, _mapping(kind, profile), account, stamp,
            encoding=_csv_encoding(profile, kind),
            delimiter=_csv_delimiter(profile, kind),
        ))
    return result
