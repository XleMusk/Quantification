"""Materialize a structured, read-only THS evidence package.

This package is deliberately separate from the importable four-table export
bundle. It preserves static receipts and candidate rows without turning OCR or
an empty grid into broker observations, so downstream desks can consume one
explicit partial state and wait for native account/order/trade/cashflow rows.
"""

import json
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path

from .observations import observation


SCHEMA = "ths_structured_observation_package_v1"
SOURCE_MANIFEST_SCHEMA = "ths_observation_source_manifest_v1"
_SOURCE_KINDS = ("account", "orders", "trades", "cashflows")


_NATIVE_MARKETS = {"SSE", "SZSE", "BSE"}
_NATIVE_SIDES = {"buy": "buy", "sell": "sell"}
_NATIVE_STATUSES = {
    "accepted": "accepted", "partial": "partial", "filled": "filled",
    "cancelled": "cancelled", "rejected": "rejected", "unknown": "unknown",
}


def build_source_manifest(source_paths):
    """Record immutable file evidence for a validated four-table package.

    The manifest is a provenance check, not a trust upgrade: it only proves
    that the files consumed to build a package have not changed since that
    package was written.  Missing paths stay explicit instead of being
    replaced with an empty-table inference.
    """
    files = {}
    for kind in _SOURCE_KINDS:
        raw_path = (source_paths or {}).get(kind)
        path = Path(raw_path).expanduser().resolve() if isinstance(raw_path, str) else None
        if path is None or not path.is_file():
            files[kind] = {
                "path": str(path) if path else raw_path,
                "present": False,
            }
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files[kind] = {
            "path": str(path),
            "present": True,
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    return {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "status": "complete" if all(item["present"] for item in files.values()) else "incomplete",
        "files": files,
    }


def verify_source_manifest(manifest):
    """Recheck a package's source files without importing or mutating state."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SOURCE_MANIFEST_SCHEMA:
        return {"status": "not_recorded", "issues": [], "checked": False}
    issues = []
    files = manifest.get("files") or {}
    for kind in _SOURCE_KINDS:
        expected = files.get(kind) or {}
        raw_path = expected.get("path")
        path = Path(raw_path) if isinstance(raw_path, str) else None
        if not expected.get("present"):
            issues.append({"kind": kind, "code": "source_missing_at_capture"})
            continue
        if path is None or not path.is_file():
            issues.append({"kind": kind, "code": "source_file_missing"})
            continue
        actual = build_source_manifest({kind: str(path)})["files"][kind]
        if (actual.get("sha256") != expected.get("sha256")
                or actual.get("size_bytes") != expected.get("size_bytes")):
            issues.append({"kind": kind, "code": "source_file_changed",
                           "expected_sha256": expected.get("sha256"),
                           "actual_sha256": actual.get("sha256"),
                           "expected_size_bytes": expected.get("size_bytes"),
                           "actual_size_bytes": actual.get("size_bytes")})
    return {
        "status": "verified" if not issues else "source_changed",
        "issues": issues,
        "checked": True,
    }


def _native_mapping(kind):
    columns = {
        "account": {"symbol": "symbol", "market": "market", "quantity": "quantity",
                    "sellable": "sellable", "price": "price"},
        "orders": {"symbol": "symbol", "market": "market", "side": "side",
                   "order_id": "order_id", "qty": "qty", "price": "price",
                   "filled_qty": "filled_qty", "status": "status"},
        "trades": {"symbol": "symbol", "market": "market", "side": "side",
                   "order_id": "order_id", "execution_id": "execution_id",
                   "qty": "qty", "price": "price"},
        "cashflows": {"symbol": "symbol", "market": "market", "side": "side",
                       "order_id": "order_id", "gross_amount": "gross_amount",
                       "fees": "fees", "cash_change": "cash_change"},
    }[kind]
    mapping = {"columns": columns, "markets": {market: market for market in _NATIVE_MARKETS}}
    if kind != "account":
        mapping["sides"] = dict(_NATIVE_SIDES)
    if kind == "orders":
        mapping["statuses"] = dict(_NATIVE_STATUSES)
    if kind == "cashflows":
        mapping.update(basis="cumulative_order_cashflow", currency="CNY", amount_unit="rmb")
    else:
        if kind == "account":
            mapping["price_basis"] = "raw_rmb"
    return mapping


def _native_rmb(cents, field):
    if type(cents) is not int or cents < 0:
        raise ValueError(f"Native {field} must be a nonnegative integer cent amount")
    return f"{cents / 100:.2f}"


def _native_contract_payload(path, expected_kind, *, account=False):
    payload = _read(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Native {expected_kind} receipt must be a JSON object")
    if payload.get("kind") not in ({None, expected_kind} if not account else {None, "account"}):
        raise ValueError(f"Native {expected_kind} receipt kind mismatch")
    if not isinstance(payload.get("rows"), list):
        raise ValueError(f"Native {expected_kind} receipt must contain structured rows")
    if payload.get("row_count") is not None and payload["row_count"] != len(payload["rows"]):
        raise ValueError(f"Native {expected_kind} row_count does not match rows")
    if payload.get("structured_rows") is not True or payload.get("read_only") is not True:
        raise ValueError(f"Native {expected_kind} receipt is not an explicit read-only structured view")
    if any(payload.get(flag) is True for flag in ("submitted", "execution_admitted", "ledger_mutated")):
        raise ValueError(f"Native {expected_kind} receipt contains execution side effects")
    for key in ("account_alias", "observed_at", "trading_date"):
        if not payload.get(key):
            raise ValueError(f"Native {expected_kind} receipt is missing {key}")
    return payload


def _native_rows(kind, payload):
    """Convert an exact native receipt into the existing normalized-observation input.

    Native rows deliberately use exchange-qualified symbols and integer cents so
    this adapter performs no exchange, price, fee, or signed-cash inference.
    """
    converted = []
    for source in payload["rows"]:
        if not isinstance(source, dict):
            raise ValueError(f"Native {kind} rows must be objects")
        symbol = source.get("symbol")
        if not isinstance(symbol, str) or symbol.count(".") != 1:
            raise ValueError(f"Native {kind} rows require exchange-qualified symbols")
        code, market = symbol.split(".", 1)
        if len(code) != 6 or market not in _NATIVE_MARKETS or not code.isdigit():
            raise ValueError(f"Native {kind} row has an invalid exchange-qualified symbol")
        row = dict(source)
        row["symbol"], row["market"] = code, market
        if kind == "account":
            for field in ("quantity", "sellable", "price_cents"):
                if type(source.get(field)) is not int or source[field] < 0:
                    raise ValueError(f"Native account row requires nonnegative integer {field}")
            row["price"] = _native_rmb(source["price_cents"], "account price")
        elif kind in {"orders", "trades"}:
            for field in ("qty", "price_cents"):
                if type(source.get(field)) is not int or source[field] <= 0:
                    raise ValueError(f"Native {kind} row requires positive integer {field}")
            if source.get("side") not in _NATIVE_SIDES or not source.get("order_id"):
                raise ValueError(f"Native {kind} row requires explicit side and order_id")
            row["price"] = _native_rmb(source["price_cents"], f"{kind} price")
            if kind == "orders":
                if type(source.get("filled_qty")) is not int or not 0 <= source["filled_qty"] <= source["qty"]:
                    raise ValueError("Native order row has invalid filled_qty")
                if source.get("status") not in _NATIVE_STATUSES:
                    raise ValueError("Native order row has an unknown status")
            else:
                if not source.get("execution_id"):
                    raise ValueError("Native trade row requires execution_id")
        else:
            for field in ("gross_cents", "fees_cents"):
                if type(source.get(field)) is not int or source[field] < 0:
                    raise ValueError(f"Native cashflow row requires nonnegative integer {field}")
            if type(source.get("cash_change_cents")) is not int or source.get("side") not in _NATIVE_SIDES:
                raise ValueError("Native cashflow row requires signed cash change and side")
            if not source.get("order_id"):
                raise ValueError("Native cashflow row requires order_id")
            row["gross_amount"] = _native_rmb(source["gross_cents"], "gross")
            row["fees"] = _native_rmb(source["fees_cents"], "fees")
            row["cash_change"] = _native_rmb(abs(source["cash_change_cents"]), "cash change")
            if source["cash_change_cents"] < 0:
                row["cash_change"] = "-" + row["cash_change"]
        converted.append(row)
    return converted


def build_observations_from_native_index(index_path):
    """Normalize a complete native receipt index for the existing journal/Ledger.

    The native contract is intentionally stricter than the screenshot evidence
    index.  A partial index raises instead of fabricating an empty activity view.
    """
    index_path = Path(index_path)
    index = _read(index_path)
    if index.get("schema") != "ths_readonly_evidence_index_v1":
        raise ValueError("Expected ths_readonly_evidence_index_v1")
    summary_ref = (index.get("summary") or {}).get("path")
    if not isinstance(summary_ref, str) or not Path(summary_ref).is_file():
        raise ValueError("Native account summary receipt is missing")
    account = _native_contract_payload(summary_ref, "account", account=True)
    if account.get("schema") not in {
        "ths_native_account_observation_v1", "ths_native_account_summary_v1",
    }:
        raise ValueError("Native account receipt must declare a supported account receipt schema")
    cash_fields = ("cash_available_cents", "cash_total_cents", "cash_frozen_cents")
    if any(type(account.get(field)) is not int for field in cash_fields):
        raise ValueError("Native account receipt must expose total/frozen/available cash in integer cents")
    if account.get("source") not in {"ths_controls", "ths_user_export"}:
        raise ValueError("Native account receipt has an unsupported source")
    paths = {"account": summary_ref}
    payloads = {"account": account}
    for kind in ("orders", "trades", "cashflows"):
        ref = ((index.get(kind) or {}).get("native_receipt") or {})
        path = ref.get("path")
        if not isinstance(path, str) or not Path(path).is_file():
            raise ValueError(f"Native {kind} receipt is missing")
        payloads[kind] = _native_contract_payload(path, kind)
        paths[kind] = path
    if len({payload["account_alias"] for payload in payloads.values()}) != 1:
        raise ValueError("Native receipt account aliases do not match")
    if len({payload["trading_date"] for payload in payloads.values()}) != 1:
        raise ValueError("Native receipt trading dates do not match")
    if payloads["cashflows"].get("source") != "ths_user_export":
        raise ValueError("Native cashflow receipt must be an explicit user export")
    values = []
    account_rows = _native_rows("account", account)
    values.append(observation(
        "account", account_rows, _native_mapping("account"),
        account_alias=account["account_alias"], observed_at=account["observed_at"],
        trading_date=account["trading_date"], source=account["source"],
        cash=_native_rmb(account["cash_available_cents"], "available cash"),
        cash_total=_native_rmb(account["cash_total_cents"], "total cash"),
        cash_frozen=_native_rmb(account["cash_frozen_cents"], "frozen cash"),
    ))
    for kind in ("orders", "trades", "cashflows"):
        payload = payloads[kind]
        values.append(observation(
            kind, _native_rows(kind, payload), _native_mapping(kind),
            account_alias=payload["account_alias"], observed_at=payload["observed_at"],
            trading_date=payload["trading_date"], source=payload["source"],
        ))
    return values, paths


def build_structured_observation_package_from_native_index(index_path, out_dir,
                                                           *, source_scope="isolated_rehearsal"):
    """Build a ready package from native receipts without saving observations.

    ``isolated_rehearsal`` is the default so a native receipt can be checked
    before it is allowed into the persistent THS journal.  Persistent import is
    an explicit operator decision in the CLI and still never submits orders.
    """
    values, paths = build_observations_from_native_index(index_path)
    from .observation_preview import _linkage

    by_kind = {value["kind"]: value for value in values}
    linkage = _linkage(by_kind["account"], by_kind["orders"],
                       by_kind["trades"], by_kind["cashflows"])
    errors = []
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
    if errors:
        raise ValueError("Native receipt linkage failed: " + "; ".join(errors))
    preview = {
        "schema": "ths_observation_bundle_preview_v1",
        "status": "bundle_ready_for_reconciliation",
        "account": {"account_alias": by_kind["account"]["account_alias"],
                    "observed_at": by_kind["account"]["observed_at"],
                    "trading_date": by_kind["account"]["trading_date"]},
        "observations": {
            kind: {"observed_at": by_kind[kind]["observed_at"],
                   "row_count": len(by_kind[kind]["rows"])}
            for kind in ("account", "orders", "trades", "cashflows")
        },
        "linkage": linkage,
        "errors": [],
        "execution_admitted": False,
        "ledger_mutated": False,
    }
    return build_structured_observation_package_from_bundle(
        preview, values, out_dir, source_paths=paths, source_scope=source_scope)


def _empty_export_explicit(observed, row_count):
    """Distinguish an observed header-only export from missing evidence."""
    return bool(observed) and row_count == 0


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _source(ref):
    if not isinstance(ref, dict):
        return None
    path = ref.get("path")
    return {
        "path": path,
        "schema": ref.get("schema"),
        "status": ref.get("status"),
        "observed_at": ref.get("observed_at"),
        "structured_rows": ref.get("structured_rows"),
        "row_count": ref.get("row_count"),
        "read_only": ref.get("read_only"),
        "client_interacted": ref.get("client_interacted"),
        "submitted": ref.get("submitted"),
    }


def _account_alias_quality(value):
    """Classify the receipt text without treating it as identity proof."""
    if value is None or not str(value).strip():
        return "missing"
    text = str(value)
    if "\ufffd" in text:
        return "encoding_corrupted"
    return "receipt_text_unverified"


def _decimal(value):
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        return Decimal(text) if text else None
    except (InvalidOperation, ValueError):
        return None


def _summary_candidate_cross_check(summary, candidate_rows):
    """Check receipt arithmetic without certifying OCR row identity or completeness."""
    metrics = summary.get("metrics_raw") or {}
    cash = _decimal(metrics.get("cash_available"))
    market_value = _decimal(metrics.get("stock_market_value"))
    total_assets = _decimal(metrics.get("total_assets"))
    numeric_rows = [_decimal(row.get("market_value")) for row in candidate_rows]
    candidate_market_value = sum(numeric_rows, Decimal("0")) if all(value is not None for value in numeric_rows) else None
    residual = None
    if cash is not None and candidate_market_value is not None and total_assets is not None:
        residual = cash + candidate_market_value - total_assets
    arithmetic_pass = residual is not None and abs(residual) <= Decimal("0.01")
    return {
        "schema": "ths_receipt_summary_candidate_cross_check_v1",
        "status": "numeric_summary_matches_candidate_total" if arithmetic_pass else (
            "numeric_summary_candidate_total_mismatch" if residual is not None else "insufficient_numeric_evidence"
        ),
        "cash_available_rmb": float(cash) if cash is not None else None,
        "summary_stock_market_value_rmb": float(market_value) if market_value is not None else None,
        "candidate_market_value_rmb": float(candidate_market_value) if candidate_market_value is not None else None,
        "summary_total_assets_rmb": float(total_assets) if total_assets is not None else None,
        "residual_rmb": float(residual) if residual is not None else None,
        "candidate_row_count": len(candidate_rows),
        "numeric_candidate_row_count": sum(value is not None for value in numeric_rows),
        "arithmetic_pass": bool(arithmetic_pass),
        "semantic_verified": False,
        "broker_reconciled": False,
        "basis": "account_summary_plus_OCR_candidate_rows_only",
        "interpretation": (
            "An arithmetic pass does not certify security identity, row completeness, "
            "orders, trades, cashflows or broker reconciliation."
        ),
    }


def _native_structured_receipt_observed(index, kind):
    """Require an actual read-only structured receipt, not only an index flag."""
    if not index.get(f"{kind}_observed"):
        return False
    section = index.get(kind) or {}
    receipt = section.get("native_receipt") or {}
    path = receipt.get("path")
    if not isinstance(path, str) or not Path(path).is_file():
        return False
    try:
        payload = _read(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False
    rows = payload.get("rows")
    declared_count = payload.get("row_count")
    if not isinstance(rows, list) or (declared_count is not None and declared_count != len(rows)):
        return False
    if payload.get("kind") not in {None, kind}:
        return False
    return bool(
        receipt.get("structured_rows") is True
        and receipt.get("read_only") is True
        and receipt.get("submitted") is not True
        and receipt.get("execution_admitted") is not True
        and receipt.get("ledger_mutated") is not True
        and payload.get("structured_rows") is True
        and payload.get("read_only") is True
        and payload.get("submitted") is not True
        and payload.get("execution_admitted") is not True
        and payload.get("ledger_mutated") is not True
    )


def build_structured_observation_package(index_path, out_dir):
    index_path = Path(index_path)
    index = _read(index_path)
    if index.get("schema") != "ths_readonly_evidence_index_v1":
        raise ValueError("Expected ths_readonly_evidence_index_v1")

    summary = index.get("summary") or {}
    candidate = ((index.get("holdings") or {}).get("candidate") or {})
    holdings_receipt = ((index.get("holdings") or {}).get("native_receipt") or {})
    orders_receipt = ((index.get("orders") or {}).get("native_receipt") or {})
    trades_receipt = ((index.get("trades") or {}).get("native_receipt") or {})
    cashflows_receipt = ((index.get("cashflows") or {}).get("native_receipt") or {})
    order_count = orders_receipt.get("row_count")
    trade_count = trades_receipt.get("row_count")
    cashflow_count = cashflows_receipt.get("row_count")
    source_paths = {
        "account": summary.get("path"),
        "orders": orders_receipt.get("path"),
        "trades": trades_receipt.get("path"),
        "cashflows": cashflows_receipt.get("path"),
    }
    source_manifest = build_source_manifest(source_paths)
    source_paths_present = bool(
        all(isinstance(path, str) and Path(path).is_file() for path in source_paths.values())
    )
    candidate_rows = []
    candidate_path = candidate.get("path")
    if candidate_path and Path(candidate_path).is_file():
        candidate_payload = _read(candidate_path)
        candidate_rows = candidate_payload.get("rows") or []
    account_summary_observed = bool(
        summary.get("path") and summary.get("observed_at") and summary.get("metrics_raw")
    )
    account_structured_observed = bool(
        index.get("account_observed")
        and index.get("structured_account")
        and summary.get("full_account_observation")
        and isinstance(summary.get("path"), str)
        and Path(summary["path"]).is_file()
    )
    structured_receipts = {
        kind: _native_structured_receipt_observed(index, kind)
        for kind in ("orders", "trades", "cashflows")
    }
    all_observed = account_structured_observed and all(structured_receipts.values())
    # A present candidate file with zero rows is explicit cash-only evidence;
    # only a missing path/count is missing holdings evidence.
    holdings_candidate_observed = bool(
        candidate.get("path") and candidate.get("row_count") is not None
    )
    holdings_candidate_semantic_verified = bool(candidate.get("semantic_verified"))
    account_summary_cross_check = _summary_candidate_cross_check(summary, candidate_rows)
    account_alias = index.get("account_alias")
    account_alias_quality = _account_alias_quality(account_alias)
    unknowns = list(index.get("unknowns") or [])
    if not account_structured_observed and "native structured account rows are absent or unverifiable" not in unknowns:
        unknowns.append("native structured account rows are absent or unverifiable")
    for kind, observed in structured_receipts.items():
        if not observed:
            message = f"native structured {kind} rows are absent or unverifiable"
            if message not in unknowns:
                unknowns.append(message)
    if account_alias_quality == "encoding_corrupted":
        unknown = "account alias contains encoding replacement characters; do not use it as identity proof"
        if unknown not in unknowns:
            unknowns.append(unknown)

    package = {
        "schema": SCHEMA,
        "source": "native_readonly_receipt_index",
        "source_scope": "native_readonly_receipt",
        "current_ledger_import_candidate": False,
        "status": "complete_structured_observation_package" if all_observed else "partial_structured_observation_package",
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        # Keep the readiness contract flat for operator/API consumers.  The
        # nested tables remain the provenance-rich source of detail.
        "account_observed": bool(index.get("account_observed")),
        "account_summary_observed": account_summary_observed,
        "orders_observed": bool(index.get("orders_observed")),
        "trades_observed": bool(index.get("trades_observed")),
        "cashflows_observed": bool(index.get("cashflows_observed")),
        "account_structured_observed": account_structured_observed,
        "structured_receipts": structured_receipts,
        "holdings_candidate_observed": holdings_candidate_observed,
        "holdings_candidate_semantic_verified": holdings_candidate_semantic_verified,
        "account_summary": {
            "observed": account_summary_observed,
            "path": summary.get("path"),
            "schema": summary.get("schema"),
            "status": summary.get("status"),
            "observed_at": summary.get("observed_at"),
            "metrics_raw": summary.get("metrics_raw"),
            "positions_read": bool(summary.get("positions_read")),
            "full_account_observation": bool(summary.get("full_account_observation")),
        },
        "account_summary_cross_check": account_summary_cross_check,
        "holdings_candidate": {
            "observed": holdings_candidate_observed,
            "path": candidate.get("path"),
            "schema": candidate.get("schema"),
            "row_count": candidate.get("row_count"),
            "symbols": candidate.get("symbols") or [],
            "summary_cross_checks_pass": candidate.get("summary_cross_checks_pass"),
            "account_summary_cross_check": account_summary_cross_check,
            "semantic_verified": holdings_candidate_semantic_verified,
            "rows": candidate_rows,
        },
        "order_count": order_count,
        "trade_count": trade_count,
        "cashflow_count": cashflow_count,
        "empty_activity_exports": {
            "orders": _empty_export_explicit(index.get("orders_observed"), order_count),
            "trades": _empty_export_explicit(index.get("trades_observed"), trade_count),
            "cashflows": _empty_export_explicit(index.get("cashflows_observed"), cashflow_count),
        },
        "source_index": str(index_path),
        "source_paths": source_paths,
        "source_paths_present": source_paths_present,
        "source_manifest": source_manifest,
        "account_alias": account_alias,
        "account_alias_quality": account_alias_quality,
        "observation_window": index.get("observation_window"),
        "tables": {
            "account": {
                "status": "summary_only_no_structured_position_rows" if not index.get("account_observed") else "observed",
                "summary": {
                    "path": summary.get("path"),
                    "schema": summary.get("schema"),
                    "observed_at": summary.get("observed_at"),
                    "metrics_raw": summary.get("metrics_raw"),
                },
                "structured_rows": bool(index.get("structured_account")),
                "source": _source(summary),
            },
            "holdings": {
                "status": "candidate_only" if not candidate.get("semantic_verified") else "observed",
                "candidate": {
                    "path": candidate.get("path"),
                    "schema": candidate.get("schema"),
                    "row_count": candidate.get("row_count"),
                    "symbols": candidate.get("symbols") or [],
                    "summary_cross_checks_pass": candidate.get("summary_cross_checks_pass"),
                    "structured_rows": candidate.get("structured_rows"),
                    "semantic_verified": candidate.get("semantic_verified"),
                    "rows": candidate_rows,
                    "row_payload_status": "candidate_only_ocr_rows",
                },
                "native_receipt": _source(holdings_receipt),
            },
            "orders": {
                "status": "observed_rows_missing" if not index.get("orders_observed") else "observed",
                "native_receipt": _source(orders_receipt),
                "row_count": order_count,
                "empty_export_explicit": _empty_export_explicit(index.get("orders_observed"), order_count),
                "zero_orders_inferred": False,
            },
            "trades": {
                "status": "observed_rows_missing" if not index.get("trades_observed") else "observed",
                "native_receipt": _source(trades_receipt),
                "row_count": trade_count,
                "empty_export_explicit": _empty_export_explicit(index.get("trades_observed"), trade_count),
                "zero_trades_inferred": False,
            },
            "cashflows": {
                "status": "missing_native_export" if not index.get("cashflows_observed") else "observed",
                "native_receipt": _source(cashflows_receipt) if cashflows_receipt else None,
                "row_count": cashflow_count,
                "empty_export_explicit": _empty_export_explicit(index.get("cashflows_observed"), cashflow_count),
            },
        },
        "order_reconciliation": {
            "status": "ready_for_native_rows" if all_observed else "blocked_missing_native_structured_rows",
            "broker_reconciled": False,
            "ledger_reconciled": False,
            "orders_observed": bool(index.get("orders_observed")),
            "trades_observed": bool(index.get("trades_observed")),
            "cashflows_observed": bool(index.get("cashflows_observed")),
            "order_count": order_count,
            "trade_count": trade_count,
            "cashflow_count": cashflow_count,
            "empty_activity_exports": {
                "orders": _empty_export_explicit(index.get("orders_observed"), order_count),
                "trades": _empty_export_explicit(index.get("trades_observed"), trade_count),
                "cashflows": _empty_export_explicit(index.get("cashflows_observed"), cashflow_count),
            },
            "zero_orders_inferred": False,
            "zero_trades_inferred": False,
            "ledger_mutated": False,
            "execution_admitted": False,
            "next_action": (
                "review this package, then run Ledger preview before import"
                if all_observed else
                "replace receipt-only inputs with explicit native THS account/orders/trades/cashflows exports, then run preview before ledger import"
            ),
        },
        "ready_for_ledger_reconciliation": bool(all_observed),
        "next_action": (
            "review this package, then explicitly reconcile or import the same evidence into the bound paper run"
            if all_observed else
            "replace receipt-only inputs with explicit native THS account/orders/trades/cashflows exports, then run preview before ledger import"
        ),
        "unknowns": unknowns,
        "execution_admitted": False,
        "ledger_mutated": False,
        "source_paths_present": source_paths_present,
        "source_manifest_status": source_manifest["status"],
        "submitted": False,
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "structured_observation_package_v1.json"
    out_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": SCHEMA,
        "status": package["status"],
        "native_verified": False,
        "source_scope": package["source_scope"],
        "current_ledger_import_candidate": package["current_ledger_import_candidate"],
        "broker_reconciled": False,
        "ledger_reconciled": False,
        "path": str(out_path),
        "source_index": str(index_path),
        "order_reconciliation_status": package["order_reconciliation"]["status"],
        "ready_for_ledger_reconciliation": bool(package["ready_for_ledger_reconciliation"]),
        "next_action": package["next_action"],
        "execution_admitted": False,
        "ledger_mutated": False,
    }


def build_structured_observation_package_from_bundle(preview, observations, out_dir,
                                                     *, source_paths=None,
                                                     source_scope="persistent_user_export"):
    """Materialize a complete package from validated native/export rows.

    This is intentionally separate from the static receipt index above.  The
    bundle has already passed the four-table linkage checks, so the resulting
    package can expose row counts and reconciliation readiness without
    inferring zero orders or writing the Ledger.
    """
    if not isinstance(preview, dict) or preview.get("status") != "bundle_ready_for_reconciliation":
        raise ValueError("A bundle-ready preview is required")
    if source_scope not in {"persistent_user_export", "isolated_rehearsal"}:
        raise ValueError("source_scope must be persistent_user_export or isolated_rehearsal")
    values = {value.get("kind"): value for value in observations}
    required = {"account", "orders", "trades", "cashflows"}
    if set(values) != required:
        raise ValueError("A complete account/orders/trades/cashflows bundle is required")
    account = values["account"]
    account_alias_quality = _account_alias_quality(account.get("account_alias"))
    linkage = preview.get("linkage") or {}
    source_paths = source_paths or {}
    source_manifest = build_source_manifest(source_paths)
    # Preserve the distinction between an explicit empty positions table and
    # a missing holdings field.  The former is valid cash-only evidence; the
    # latter must remain visible as missing downstream.
    if "positions" in account:
        account_positions = account.get("positions")
        holdings_evidence_present = account_positions is not None
    elif "rows" in account:
        account_positions = account.get("rows")
        holdings_evidence_present = account_positions is not None
    else:
        account_positions = []
        holdings_evidence_present = False
    account_positions = account_positions if account_positions is not None else []
    if isinstance(account_positions, dict):
        account_position_rows = []
        for symbol, details in account_positions.items():
            row = dict(details) if isinstance(details, dict) else {"value": details}
            row.setdefault("symbol", symbol)
            account_position_rows.append(row)
    else:
        account_position_rows = list(account_positions)
    source_paths_present = bool(source_paths) and all(
        isinstance(source_paths.get(kind), str) and Path(source_paths[kind]).is_file()
        for kind in ("account", "orders", "trades", "cashflows")
    )
    holdings_ready = bool(holdings_evidence_present)
    package = {
        "schema": SCHEMA,
        "status": "complete_structured_observation_package" if holdings_ready else "partial_structured_observation_package",
        "source": (
            "validated_ths_user_export_bundle"
            if source_scope == "persistent_user_export"
            else "isolated_ths_export_rehearsal"
        ),
        "source_scope": source_scope,
        "current_ledger_import_candidate": source_scope == "persistent_user_export" and holdings_ready,
        "native_account": False,
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        # Mirror the receipt-index shape so downstream consumers do not need
        # separate parsing logic for native receipts versus validated exports.
        "account_observed": True,
        "account_summary_observed": True,
        "orders_observed": True,
        "trades_observed": True,
        "cashflows_observed": True,
        "holdings_candidate_observed": holdings_evidence_present,
        "holdings_candidate_semantic_verified": holdings_ready,
        "account_summary": {
            "observed": True,
            "path": source_paths.get("account"),
            "schema": account.get("schema"),
            "status": "validated_export",
            "observed_at": account.get("observed_at"),
            "metrics_raw": {
                "cash_total": account.get("cash_total"),
                "cash_frozen": account.get("cash_frozen"),
                "cash_available": account.get("cash_available"),
            },
            "positions_read": holdings_evidence_present,
            "full_account_observation": holdings_ready,
        },
        "holdings_candidate": {
            "observed": holdings_evidence_present,
            "path": source_paths.get("account"),
            "schema": account.get("schema"),
            "row_count": len(account_position_rows) if holdings_ready else None,
            "symbols": [str(row.get("symbol")) for row in account_position_rows if isinstance(row, dict) and row.get("symbol")],
            "summary_cross_checks_pass": True if holdings_ready else None,
            "semantic_verified": holdings_ready,
            "rows": account_position_rows if holdings_ready else None,
        },
        "order_count": linkage.get("order_count"),
        "trade_count": linkage.get("trade_count"),
        "cashflow_count": linkage.get("cashflow_count"),
        "empty_activity_exports": {
            "orders": linkage.get("order_count") == 0,
            "trades": linkage.get("trade_count") == 0,
            "cashflows": linkage.get("cashflow_count") == 0,
        },
        "account_alias": account.get("account_alias"),
        "account_alias_quality": account_alias_quality,
        "observation_window": {
            "observed_at": [values[k].get("observed_at") for k in ("account", "orders", "trades", "cashflows")],
            "trading_date": account.get("trading_date"),
            "atomic": False,
            "basis": "validated four-table export bundle; timestamps remain explicit",
        },
        "tables": {
            kind: {
                "status": "observed",
                "structured_rows": True,
                "row_count": len(values[kind].get("rows") or []),
                "empty_export_explicit": len(values[kind].get("rows") or []) == 0,
                "observed_at": values[kind].get("observed_at"),
                "source": values[kind].get("source"),
            }
            for kind in ("account", "orders", "trades", "cashflows")
        },
        "order_reconciliation": {
            "status": "ready_for_ledger_reconciliation" if holdings_ready else "blocked_missing_structured_holdings_rows",
            "broker_reconciled": False,
            "ledger_reconciled": False,
            "orders_observed": True,
            "trades_observed": True,
            "cashflows_observed": True,
            "order_count": linkage.get("order_count"),
            "trade_count": linkage.get("trade_count"),
            "cashflow_count": linkage.get("cashflow_count"),
            "zero_orders_inferred": False,
            "zero_trades_inferred": False,
            "linkage": linkage,
            "ledger_mutated": False,
            "execution_admitted": False,
            "next_action": (
                "Review this package, then explicitly reconcile or import the same evidence into the bound paper run"
                if holdings_ready else
                "Provide explicit structured holdings rows; do not infer an empty account from the missing field"
            ),
        },
        "ready_for_ledger_reconciliation": holdings_ready,
        "next_action": (
            "review this package, then explicitly reconcile or import the same evidence into the bound paper run"
            if holdings_ready else
            "provide explicit structured holdings rows; do not infer an empty account from the missing field"
        ),
        "unknowns": [] if holdings_ready else [
            "structured holdings rows are absent; empty holdings must not be inferred"
        ],
        "source_paths": source_paths,
        "source_paths_present": source_paths_present,
        "source_manifest": source_manifest,
        "source_manifest_status": source_manifest["status"],
        "execution_admitted": False,
        "ledger_mutated": False,
        "submitted": False,
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "structured_observation_package_v1.json"
    out_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "schema": SCHEMA,
        "status": package["status"],
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        "path": str(out_path),
        "order_reconciliation_status": package["order_reconciliation"]["status"],
        "ready_for_ledger_reconciliation": bool(package["ready_for_ledger_reconciliation"]),
        "next_action": package["next_action"],
        "source_paths_present": source_paths_present,
        "source_manifest_status": source_manifest["status"],
        "source_scope": source_scope,
        "current_ledger_import_candidate": source_scope == "persistent_user_export" and holdings_ready,
        "execution_admitted": False,
        "ledger_mutated": False,
    }
