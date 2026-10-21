"""Read-only account risk calculations for a validated THS observation package.

The input is deliberately the same four-table package used by ``ledger_import``.
This module never writes the Ledger and never treats an isolated rehearsal as a
native account.  Money in THS account exports is integer cents; quote prices
are raw RMB values with two decimals.
"""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


SCHEMA = "ths_account_risk_view_v1"
ORDER_RISK_SCHEMA = "ths_account_order_risk_view_v1"


def _decimal(value, *, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _int(value, *, name, minimum=None):
    result = _decimal(value, name=name)
    if result != result.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    result = int(result)
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _symbol(row):
    symbol = str(row.get("symbol") or "").strip()
    market = str(row.get("market") or "").strip()
    if symbol and "." not in symbol and market:
        symbol = f"{symbol}.{market}"
    return symbol


def _quote_prices(quote_observation):
    if not isinstance(quote_observation, dict):
        return {}
    prices = quote_observation.get("prices") or {}
    if not isinstance(prices, dict):
        return {}
    result = {}
    for symbol, value in prices.items():
        raw = value.get("price_rmb") if isinstance(value, dict) else value
        if raw in (None, ""):
            continue
        try:
            result[str(symbol)] = _decimal(raw, name=f"quote[{symbol}]")
        except ValueError:
            continue
    return result


def _quote_record(quote_observation, symbol):
    if not isinstance(quote_observation, dict):
        return {}
    prices = quote_observation.get("prices") or {}
    if not isinstance(prices, dict):
        return {}
    value = prices.get(symbol)
    return value if isinstance(value, dict) else {}


def _optional_bool(value, *, name):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean")


def _price_limit_bounds(rule, preclose, trading_date, is_st):
    lower, upper = rule.limits(preclose, trading_date, is_st=is_st)
    unit = Decimal(1000 if rule.price_unit == "mill" else 100)
    return Decimal(lower) / unit, Decimal(upper) / unit


def _account_rows(package):
    holdings = package.get("holdings_candidate") or {}
    rows = holdings.get("rows") if isinstance(holdings, dict) else None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("holdings_candidate.rows must be a list")
    return [row for row in rows if isinstance(row, dict)]


def _merge_account_export_marks(package, rows):
    """Recover explicit marks from the validated account source when the
    summary package intentionally keeps holdings rows minimal."""
    source_path = (package.get("source_paths") or {}).get("account")
    if not isinstance(source_path, str) or not Path(source_path).is_file():
        return rows
    try:
        payload = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return rows
    source_rows = payload.get("positions") or payload.get("rows") or []
    if isinstance(source_rows, dict):
        source_rows = [dict(value, symbol=symbol) if isinstance(value, dict) else {"symbol": symbol}
                       for symbol, value in source_rows.items()]
    by_symbol = {_symbol(row): row for row in source_rows if isinstance(row, dict) and _symbol(row)}
    enriched = []
    for row in rows:
        extra = by_symbol.get(_symbol(row), {})
        merged = dict(extra)
        merged.update(row)
        for key in ("price_rmb", "price", "market_price"):
            if row.get(key) in (None, "") and extra.get(key) not in (None, ""):
                merged[key] = extra[key]
        enriched.append(merged)
    return enriched


def package_from_account_observation(account, *, source_scope="persistent_user_export"):
    """Adapt one validated account observation for the read-only risk engine.

    Ledger preview already validates the four observations and their account
    identity.  This small adapter lets that transaction reuse the same risk
    arithmetic without creating a second account model or writing a package
    to disk.
    """
    if not isinstance(account, dict):
        raise ValueError("account observation must be an object")
    if source_scope not in {"persistent_user_export", "isolated_rehearsal"}:
        raise ValueError("unsupported account observation source scope")
    # An explicit empty positions list is valid cash-only evidence.  A
    # missing positions/rows field is different and must remain unavailable
    # to the risk engine instead of being inferred as an empty account.
    if "positions" in account:
        positions = account.get("positions")
        holdings_evidence_present = positions is not None
    elif "rows" in account:
        positions = account.get("rows")
        holdings_evidence_present = positions is not None
    else:
        positions = []
        holdings_evidence_present = False
    positions = positions if positions is not None else []
    if isinstance(positions, dict):
        marks = account.get("marks") or {}
        normalized = []
        for symbol, value in positions.items():
            row = dict(value) if isinstance(value, dict) else {}
            row.setdefault("symbol", symbol)
            if row.get("price_rmb") in (None, "") and row.get("price") in (None, ""):
                mark_cents = marks.get(symbol)
                if mark_cents not in (None, ""):
                    row["price_rmb"] = str(Decimal(str(mark_cents)) / Decimal(100))
            if (row.get("price_rmb") in (None, "") and row.get("price") in (None, "")
                    and row.get("price_cents") not in (None, "")):
                mark_cents = _int(row["price_cents"], name=f"{symbol}.price_cents", minimum=1)
                row["price_rmb"] = str(Decimal(mark_cents) / Decimal(100))
            normalized.append(row)
        positions = normalized
    if not isinstance(positions, list):
        raise ValueError("account positions must be a list or object")
    return {
        "schema": "ths_structured_observation_package_v1",
        "source": "immutable_pre_submit_account_snapshot",
        "source_manifest_status": "not_applicable_pre_submit_snapshot",
        "source_manifest_issues": [],
        "source_scope": "pre_submit_account_snapshot",
        "account_source_scope": source_scope,
        "status": "complete_structured_observation_package",
        "account_alias": account.get("account_alias"),
        "account_observed": True,
        "orders_observed": True,
        "trades_observed": True,
        "cashflows_observed": True,
        "account_summary": {
            "metrics_raw": {
                "cash_total": account.get("cash_total"),
                "cash_frozen": account.get("cash_frozen"),
                "cash_available": account.get("cash_available"),
            },
            "observed_at": account.get("observed_at"),
            "full_account_observation": True,
        },
        "observation_window": {
            "trading_date": account.get("trading_date"),
        },
        "holdings_candidate_observed": holdings_evidence_present,
        "holdings_candidate_semantic_verified": holdings_evidence_present,
        "holdings_candidate": {
            "rows": positions if holdings_evidence_present else None,
            "semantic_verified": holdings_evidence_present,
        },
        "ready_for_ledger_reconciliation": True,
        "current_ledger_import_candidate": source_scope == "persistent_user_export",
        "native_verified": False,
        "broker_reconciled": False,
        "ledger_reconciled": False,
        "ledger_mutated": False,
    }


def build_account_risk_view(package, *, quote_observation=None,
                            max_single_weight=Decimal("0.25"),
                            max_gross_weight=Decimal("0.95"),
                            minimum_cash_buffer=Decimal("0.05"),
                            reconciliation=None):
    """Return a read-only account risk view from a complete structured package.

    ``account_ready`` requires a persistent user export.  An isolated rehearsal
    may still produce arithmetic and limit diagnostics, but is explicitly marked
    non-account evidence and cannot become execution input.
    """
    package = package if isinstance(package, dict) else {}
    source_scope = package.get("source_scope")
    if not source_scope:
        source_paths = package.get("source_paths") or {}
        source_text = " ".join(str(value).lower() for value in source_paths.values())
        alias_text = str(package.get("account_alias") or "").lower()
        source_scope = (
            "isolated_rehearsal"
            if "ths_bundle_rehearsal" in source_text or "synthetic" in alias_text
            else "persistent_user_export"
            if package.get("source") == "validated_ths_user_export_bundle"
            else "isolated_rehearsal"
            if package.get("source") == "isolated_ths_export_rehearsal"
            else "missing"
        )
    complete = package.get("status") == "complete_structured_observation_package"
    required = ("account_observed", "orders_observed", "trades_observed", "cashflows_observed")
    tables_complete = all(package.get(key) is True for key in required)
    summary = package.get("account_summary") or {}
    metrics = summary.get("metrics_raw") if isinstance(summary, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    blockers = []
    if not complete or not tables_complete:
        blockers.append("complete_four_table_structured_package_missing")
    source_manifest_status = package.get("source_manifest_status", "not_recorded")
    if source_manifest_status == "source_changed":
        blockers.append("structured_source_manifest_changed")
    if not package.get("holdings_candidate_semantic_verified"):
        blockers.append("holdings_semantics_not_verified")
    if source_scope not in {"persistent_user_export", "isolated_rehearsal", "pre_submit_account_snapshot"}:
        blockers.append("account_source_scope_not_supported")
    if source_scope == "persistent_user_export" and source_manifest_status != "verified":
        if source_manifest_status == "source_changed":
            # Preserve the more specific source-integrity diagnosis below.
            pass
        else:
            blockers.append("structured_source_manifest_unverified")

    cash_total = cash_frozen = cash_available = None
    try:
        cash_total = _int(metrics.get("cash_total"), name="cash_total", minimum=0)
        cash_frozen = _int(metrics.get("cash_frozen"), name="cash_frozen", minimum=0)
        cash_available = _int(metrics.get("cash_available"), name="cash_available", minimum=0)
    except ValueError as error:
        blockers.append(f"invalid_cash_fields:{error}")

    quote_prices = _quote_prices(quote_observation)
    holdings = package.get("holdings_candidate")
    holdings_rows = holdings.get("rows") if isinstance(holdings, dict) else None
    if not isinstance(holdings_rows, list):
        blockers.append("structured_holdings_evidence_missing")
        rows = []
    else:
        rows = _merge_account_export_marks(package, _account_rows(package))
    positions = []
    seen = set()
    mark_missing = []
    invalid_positions = []
    for row in rows:
        symbol = _symbol(row)
        if not symbol or symbol in seen:
            invalid_positions.append(symbol or "missing_symbol")
            continue
        seen.add(symbol)
        try:
            quantity = _int(row.get("quantity"), name=f"{symbol}.quantity", minimum=0)
            sellable = _int(row.get("sellable"), name=f"{symbol}.sellable", minimum=0)
            if sellable > quantity:
                raise ValueError("sellable exceeds quantity")
        except ValueError as error:
            invalid_positions.append(f"{symbol}:{error}")
            continue
        mark = quote_prices.get(symbol)
        mark_source = "raw_quote_observation"
        if mark is None:
            for key in ("price_rmb", "price", "market_price"):
                if row.get(key) not in (None, ""):
                    try:
                        mark = _decimal(row[key], name=f"{symbol}.{key}")
                    except ValueError:
                        mark = None
                    break
            mark_source = "account_export_mark"
        if mark is None and row.get("price_cents") not in (None, ""):
            try:
                cents = _int(row["price_cents"], name=f"{symbol}.price_cents", minimum=1)
                mark = Decimal(cents) / Decimal(100)
            except ValueError:
                mark = None
            mark_source = "account_export_mark"
        if mark is None or mark <= 0:
            mark_missing.append(symbol)
        value_cents = int((Decimal(quantity) * mark * 100).to_integral_value()) if mark else None
        positions.append({
            "symbol": symbol,
            "quantity": quantity,
            "sellable": sellable,
            "t1_blocked": quantity - sellable,
            "mark_price_rmb": f"{mark:.2f}" if mark is not None else None,
            "mark_source": mark_source if mark is not None else None,
            "market_value_cents": value_cents,
        })

    if invalid_positions:
        blockers.append("invalid_or_duplicate_positions")
    if mark_missing:
        blockers.append("missing_position_mark")
    if cash_total is not None and cash_frozen is not None and cash_frozen > cash_total:
        blockers.append("cash_frozen_exceeds_cash_total")
    if cash_total is not None and cash_available is not None and cash_available > cash_total:
        blockers.append("cash_available_exceeds_cash_total")
    if (cash_total is not None and cash_frozen is not None and cash_available is not None
            and cash_total - cash_frozen != cash_available):
        blockers.append("cash_total_minus_frozen_differs_from_available")

    # Account arithmetic is not enough to release another order.  A stale or
    # divergent broker bundle can leave the cash/position snapshot looking
    # valid while the sole Ledger still contains an unknown order or fee.
    # Keep this optional for the low-level calculator, but make API consumers
    # pass the authoritative same-Ledger receipt.
    reconciliation = reconciliation if isinstance(reconciliation, dict) else None
    reconciliation_status = reconciliation.get("status") if reconciliation else None
    reconciliation_issue_count = (
        (reconciliation.get("summary") or {}).get("issue_count", 0)
        if reconciliation else None
    )
    reconciliation_blockers = []
    if reconciliation_status in {
            "structured_package_not_ready", "isolated_rehearsal_only",
            "reconciliation_required",
    }:
        reconciliation_blockers.append(f"same_ledger_{reconciliation_status}")

    marked = [row for row in positions if row["market_value_cents"] is not None]
    holdings_value = sum(row["market_value_cents"] for row in marked)
    equity = cash_total + holdings_value if cash_total is not None else None
    weights = []
    if equity and equity > 0:
        for row in marked:
            weight = Decimal(row["market_value_cents"]) / Decimal(equity)
            row["weight"] = float(weight)
            weights.append((row["symbol"], weight))
    weights.sort(key=lambda item: item[1], reverse=True)
    max_weight = weights[0][1] if weights else Decimal(0)
    top5_weight = sum((item[1] for item in weights[:5]), Decimal(0))
    gross_weight = Decimal(holdings_value) / Decimal(equity) if equity else None
    available_cash_weight = Decimal(cash_available) / Decimal(equity) if equity and cash_available is not None else None
    frozen_cash_weight = Decimal(cash_frozen) / Decimal(equity) if equity and cash_frozen is not None else None
    t1_blocked = sum(row["t1_blocked"] for row in positions)
    total_quantity = sum(row["quantity"] for row in positions)
    sellable_quantity = sum(row["sellable"] for row in positions)
    sellable_ratio = Decimal(sellable_quantity) / Decimal(total_quantity) if total_quantity else Decimal(1)

    risk_flags = []
    if max_weight > _decimal(max_single_weight, name="max_single_weight"):
        risk_flags.append("single_position_concentration")
    if gross_weight is not None and gross_weight > _decimal(max_gross_weight, name="max_gross_weight"):
        risk_flags.append("gross_exposure_limit")
    if available_cash_weight is not None and available_cash_weight < _decimal(minimum_cash_buffer, name="minimum_cash_buffer"):
        risk_flags.append("cash_buffer_below_policy")
    if t1_blocked:
        risk_flags.append("t1_sell_blocked_quantity_present")

    account_ready = (
        not blockers
        and not reconciliation_blockers
        and source_scope == "persistent_user_export"
        and (
            reconciliation is None
            or reconciliation_status in {"ledger_reconciled", "explicit_empty_activity_confirmed"}
        )
    )
    arithmetic_ready = not blockers
    if not arithmetic_ready:
        status = "blocked_missing_or_invalid_account_evidence"
    elif reconciliation_blockers:
        status = "same_ledger_reconciliation_required"
    elif source_scope == "isolated_rehearsal":
        status = "isolated_rehearsal_only"
    elif source_scope == "pre_submit_account_snapshot":
        status = "pre_submit_snapshot_only"
    elif risk_flags:
        status = "account_risk_attention"
    else:
        status = "account_risk_ready_readonly"
    return {
        "schema": SCHEMA,
        "status": status,
        "source_scope": source_scope,
        "source_manifest_status": source_manifest_status,
        "source_manifest_issues": package.get("source_manifest_issues") or [],
        "account_alias": package.get("account_alias"),
        "account_ready": account_ready,
        "arithmetic_ready": arithmetic_ready,
        "native_verified": bool(package.get("native_verified")),
        "execution_admitted": False,
        "ledger_mutated": bool(package.get("ledger_mutated")),
        "ledger_reconciled": bool(package.get("ledger_reconciled")),
        "same_ledger_reconciliation": {
            "status": reconciliation_status,
            "issue_count": reconciliation_issue_count,
            "execution_gate": (
                "clear"
                if reconciliation_status in {"ledger_reconciled", "explicit_empty_activity_confirmed"}
                else "blocked" if reconciliation is not None else "not_supplied"
            ),
        },
        "ledger_binding": {
            "same_ledger_required": True,
            "current_ledger_import_candidate": bool(package.get("current_ledger_import_candidate")),
            "ready_for_ledger_reconciliation": bool(package.get("ready_for_ledger_reconciliation")),
            "ledger_reconciled": bool(package.get("ledger_reconciled")),
            "ledger_mutated": bool(package.get("ledger_mutated")),
            "next_action": (
                "run the existing explicit Ledger preview/import against this same four-table package"
                if package.get("ready_for_ledger_reconciliation") and not package.get("ledger_reconciled")
                else "retain the same Ledger binding and review the latest receipt"
            ),
        },
        "observation_window": package.get("observation_window"),
        "cash": {
            "total_cents": cash_total,
            "frozen_cents": cash_frozen,
            "available_cents": cash_available,
            "holdings_value_cents": holdings_value if arithmetic_ready else None,
            "equity_cents": equity if arithmetic_ready else None,
            "available_cash_weight": float(available_cash_weight) if available_cash_weight is not None else None,
            "frozen_cash_weight": float(frozen_cash_weight) if frozen_cash_weight is not None else None,
        },
        "exposure": {
            "gross_weight": float(gross_weight) if gross_weight is not None else None,
            "max_single_weight": float(max_weight),
            "top5_weight": float(top5_weight),
            "largest_positions": [{"symbol": symbol, "weight": float(weight)} for symbol, weight in weights[:5]],
        },
        "sellability": {
            "total_quantity": total_quantity,
            "sellable_quantity": sellable_quantity,
            "t1_blocked_quantity": t1_blocked,
            "sellable_ratio": float(sellable_ratio),
        },
        "positions": positions,
        "risk_flags": risk_flags,
        "blockers": blockers,
        "execution_blockers": list(dict.fromkeys(blockers + reconciliation_blockers)),
        "policy": {
            "max_single_weight": str(max_single_weight),
            "max_gross_weight": str(max_gross_weight),
            "minimum_cash_buffer": str(minimum_cash_buffer),
        },
        "valuation": {
            "quote_observation_supplied": bool(quote_prices),
            "quote_symbols_used": sorted(quote_prices),
            "fallback_account_marks_allowed": True,
            "basis": "raw_rmb_quotes_preferred_then_account_export_marks; read_only",
        },
    }


def evaluate_order_risk(package, orders, *, quote_observation=None,
                        max_single_weight=Decimal("0.25"),
                        max_gross_weight=Decimal("0.95"),
                        minimum_cash_buffer=Decimal("0.05"),
                        fee_buffer_cents=None,
                        reconciliation=None):
    """Evaluate a proposed order batch against the same account snapshot.

    This is deliberately a read-only pre-trade calculation.  Prices and
    quantities must be explicit in the proposed rows; the function never
    substitutes a quote for an order price and never treats missing fees as
    zero.  It complements ``build_account_risk_view`` by checking the
    projected account after the batch, including sellable quantity and cash
    buffer.  An explicit fee buffer can be supplied per batch or per order;
    it does not submit, journal, or mutate the Ledger.
    """
    account = build_account_risk_view(
        package,
        quote_observation=quote_observation,
        max_single_weight=max_single_weight,
        max_gross_weight=max_gross_weight,
        minimum_cash_buffer=minimum_cash_buffer,
        reconciliation=reconciliation,
    )
    result = {
        "schema": ORDER_RISK_SCHEMA,
        "status": "blocked",
        "source_scope": account.get("source_scope"),
        "account_alias": account.get("account_alias"),
        "account_risk": account,
        "orders": [],
        "projected": {},
        "risk_flags": [],
        "blockers": list(account.get("execution_blockers") or account.get("blockers") or []),
        "price_limit_checks": [],
        "execution_admitted": False,
        "ledger_mutated": False,
        "submission_enabled": False,
        "basis": "same_account_snapshot; explicit_order_price; explicit_fee_buffer; unfilled_sell_proceeds_not_spendable; slippage_not_inferred",
    }
    try:
        if fee_buffer_cents is not None:
            fee_buffer_cents = _int(fee_buffer_cents, name="fee_buffer_cents", minimum=0)
    except ValueError as error:
        result["blockers"].append(f"invalid_fee_buffer:{error}")
        return result
    if not isinstance(orders, list):
        result["blockers"].append("orders_must_be_a_list")
        return result
    if not account.get("arithmetic_ready"):
        result["blockers"].append("account_arithmetic_not_ready")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        return result
    if account.get("execution_blockers"):
        result["blockers"].append("account_release_blocked")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        return result

    cash = account["cash"]
    cash_available = cash.get("available_cents")
    equity = cash.get("equity_cents")
    if cash_available is None or equity is None or equity <= 0:
        result["blockers"].append("cash_or_equity_missing")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        return result
    positions = {
        row["symbol"]: {
            "quantity": int(row["quantity"]),
            "sellable": int(row["sellable"]),
            "mark_price_rmb": Decimal(str(row["mark_price_rmb"])) if row.get("mark_price_rmb") else None,
            "market_value_cents": int(row["market_value_cents"] or 0),
        }
        for row in account.get("positions") or []
    }
    projected_quantity = {symbol: row["quantity"] for symbol, row in positions.items()}
    projected_sellable = {symbol: row["sellable"] for symbol, row in positions.items()}
    projected_value = {symbol: row["market_value_cents"] for symbol, row in positions.items()}
    buy_notional = 0
    sell_notional = 0
    buy_fees = 0
    sell_fees = 0
    seen_order_ids = set()
    parsed = []
    trading_date = ((package.get("observation_window") or {}).get("trading_date")
                    or (package.get("account_summary") or {}).get("trading_date"))
    for index, raw in enumerate(orders):
        if not isinstance(raw, dict):
            result["blockers"].append(f"order_{index}:not_an_object")
            continue
        symbol = _symbol(raw)
        side = str(raw.get("side") or "").strip().lower()
        order_id = str(raw.get("order_id") or raw.get("id") or f"proposed-{index + 1}").strip()
        if order_id in seen_order_ids:
            result["blockers"].append(f"order_{order_id}:duplicate_order_id")
        seen_order_ids.add(order_id)
        try:
            quantity = _int(raw.get("qty", raw.get("quantity")), name=f"order[{order_id}].qty", minimum=1)
            price = _decimal(raw.get("price_rmb", raw.get("price")), name=f"order[{order_id}].price_rmb")
            if price <= 0:
                raise ValueError("price_rmb must be > 0")
        except ValueError as error:
            result["blockers"].append(f"order_{order_id}:{error}")
            continue
        if not symbol:
            result["blockers"].append(f"order_{order_id}:missing_symbol")
            continue
        if side not in {"buy", "sell"}:
            result["blockers"].append(f"order_{order_id}:side must be buy or sell")
            continue
        try:
            from trading.rules import execution_rule
            rule = execution_rule(symbol, trading_date) if trading_date else None
            held = int(projected_quantity.get(symbol, 0))
            if rule is None:
                result["blockers"].append(f"order_{order_id}:trading_date_missing_for_rule_check")
            else:
                rejection = rule.quantity.rejection(quantity, side, held=held)
                if rejection:
                    result["blockers"].append(f"order_{order_id}:{rejection}")
                if rule.price_unit == "cent" and (price * 100) != (price * 100).to_integral_value():
                    result["blockers"].append(f"order_{order_id}:price_tick_must_be_0.01_rmb")
                elif rule.price_unit == "mill" and (price * 1000) != (price * 1000).to_integral_value():
                    result["blockers"].append(f"order_{order_id}:price_tick_must_be_0.001_rmb")

                quote = _quote_record(quote_observation, symbol)
                preclose_raw = quote.get("preclose_rmb")
                limit_check = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "status": "not_available",
                    "basis": "explicit_quote_preclose_and_security_status",
                }
                if preclose_raw not in (None, ""):
                    try:
                        preclose = _decimal(
                            preclose_raw,
                            name=f"quote[{symbol}].preclose_rmb",
                        )
                        if preclose <= 0:
                            raise ValueError("preclose_rmb must be > 0")
                        if "is_st" not in quote:
                            limit_check["status"] = "blocked_status_unknown"
                            result["blockers"].append(
                                f"order_{order_id}:price_limit_st_status_unknown"
                            )
                        else:
                            is_st = _optional_bool(
                                quote["is_st"], name=f"quote[{symbol}].is_st"
                            )
                            lower, upper = _price_limit_bounds(
                                rule, preclose, trading_date, is_st
                            )
                            limit_check.update(
                                status="checked",
                                preclose_rmb=f"{preclose:.2f}",
                                is_st=is_st,
                                lower_rmb=f"{lower:.3f}".rstrip("0").rstrip("."),
                                upper_rmb=f"{upper:.3f}".rstrip("0").rstrip("."),
                            )
                            if price < lower:
                                result["blockers"].append(
                                    f"order_{order_id}:price_below_limit_down"
                                )
                            if price > upper:
                                result["blockers"].append(
                                    f"order_{order_id}:price_above_limit_up"
                                )
                    except ValueError as error:
                        limit_check["status"] = "invalid_evidence"
                        result["blockers"].append(
                            f"order_{order_id}:invalid_price_limit_evidence:{error}"
                        )
                if "tradable" in quote:
                    try:
                        tradable = _optional_bool(
                            quote["tradable"], name=f"quote[{symbol}].tradable"
                        )
                        limit_check["tradable"] = tradable
                        if not tradable:
                            result["blockers"].append(
                                f"order_{order_id}:security_not_tradable"
                            )
                    except ValueError as error:
                        limit_check["status"] = "invalid_evidence"
                        result["blockers"].append(
                            f"order_{order_id}:invalid_trade_status_evidence:{error}"
                        )
                result["price_limit_checks"].append(limit_check)
        except (KeyError, ValueError) as error:
            result["blockers"].append(f"order_{order_id}:instrument_rule_unavailable:{error}")
        notional_cents = int((Decimal(quantity) * price * 100).to_integral_value())
        if raw.get("fee_cents", fee_buffer_cents) is None:
            result["blockers"].append(f"order_{order_id}:fee_evidence_missing")
            continue
        try:
            order_fee_cents = _int(raw.get("fee_cents", fee_buffer_cents),
                                   name=f"order[{order_id}].fee_cents", minimum=0)
        except ValueError as error:
            result["blockers"].append(f"order_{order_id}:{error}")
            continue
        before = positions.get(symbol, {"quantity": 0, "sellable": 0, "market_value_cents": 0})
        if side == "sell" and quantity > int(projected_sellable.get(symbol, before["sellable"])):
            result["blockers"].append(f"order_{order_id}:sell_qty_exceeds_sellable")
        delta = quantity if side == "buy" else -quantity
        projected_quantity[symbol] = projected_quantity.get(symbol, 0) + delta
        if projected_quantity[symbol] < 0:
            result["blockers"].append(f"order_{order_id}:projected_quantity_negative")
        if side == "sell":
            projected_sellable[symbol] = projected_sellable.get(symbol, 0) - quantity
            sell_notional += notional_cents
            sell_fees += order_fee_cents
        else:
            projected_sellable.setdefault(symbol, before["sellable"])
            buy_notional += notional_cents
            buy_fees += order_fee_cents
        mark = before.get("mark_price_rmb")
        mark_source = "account_snapshot_mark"
        # A new buy has no prior account mark by definition.  Its explicit
        # limit price is the only admissible valuation for the projected
        # position; requiring a stale/absent mark here would block every new
        # position before the account risk gate can evaluate it.  Existing
        # holdings still require an observed mark and are never zero-filled.
        if mark is None and side == "buy" and before.get("quantity", 0) == 0:
            mark = price
            mark_source = "explicit_order_limit"
        if mark is None:
            result["blockers"].append(f"order_{order_id}:current_mark_missing")
        else:
            projected_value[symbol] = int((Decimal(projected_quantity[symbol]) * mark * 100).to_integral_value())
        parsed.append({
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price_rmb": f"{price:.2f}",
            "notional_cents": notional_cents,
            "fee_cents": order_fee_cents,
            "valuation_source": mark_source,
        })

    result["orders"] = parsed
    funding_cash = cash_available - buy_notional - buy_fees
    projected_cash = funding_cash + sell_notional - sell_fees
    projected_total_cash = cash["total_cents"] - buy_notional - buy_fees + sell_notional - sell_fees
    projected_holdings = sum(projected_value.values())
    projected_equity = projected_total_cash + projected_holdings
    # Fees reduce spendable cash and the cash-buffer check, but must not make
    # an otherwise exactly-at-limit position fail concentration solely because
    # the denominator lost a few cents.  Exposure policy is measured on the
    # fee-neutral projected equity; actual post-fee equity remains reported.
    exposure_equity = (cash["total_cents"] - buy_notional + sell_notional + projected_holdings)
    if funding_cash < 0:
        result["blockers"].append("buy_funding_exceeds_observed_available_cash")
    if projected_cash < 0:
        result["blockers"].append("projected_available_cash_negative")
    if projected_equity <= 0:
        result["blockers"].append("projected_equity_not_positive")
    projected_weights = []
    if exposure_equity > 0:
        for symbol, value in projected_value.items():
            quantity = projected_quantity.get(symbol, 0)
            if quantity <= 0:
                continue
            projected_weights.append((symbol, Decimal(value) / Decimal(exposure_equity)))
    projected_weights.sort(key=lambda item: item[1], reverse=True)
    projected_gross = (Decimal(projected_holdings) / Decimal(exposure_equity)
                       if exposure_equity > 0 else None)
    projected_cash_weight = (Decimal(funding_cash) / Decimal(projected_equity)
                             if projected_equity > 0 else None)
    if projected_weights and projected_weights[0][1] > _decimal(max_single_weight, name="max_single_weight"):
        result["risk_flags"].append("projected_single_position_concentration")
        result["blockers"].append("projected_single_position_limit_exceeded")
    if projected_gross is not None and projected_gross > _decimal(max_gross_weight, name="max_gross_weight"):
        result["risk_flags"].append("projected_gross_exposure_limit")
        result["blockers"].append("projected_gross_exposure_limit_exceeded")
    if projected_cash_weight is not None and projected_cash_weight < _decimal(minimum_cash_buffer, name="minimum_cash_buffer"):
        result["risk_flags"].append("projected_cash_buffer_below_policy")
        result["blockers"].append("projected_cash_buffer_below_policy")
    result["blockers"] = list(dict.fromkeys(result["blockers"]))
    result["risk_flags"] = list(dict.fromkeys(result["risk_flags"]))
    projected_positions = []
    for symbol in sorted(set(projected_quantity) | set(projected_sellable)):
        quantity = max(0, int(projected_quantity.get(symbol, 0)))
        if quantity <= 0:
            continue
        sellable = max(0, int(projected_sellable.get(symbol, 0)))
        projected_positions.append({
            "symbol": symbol,
            "quantity": quantity,
            "sellable": sellable,
            "t1_blocked": max(0, quantity - sellable),
        })
    result["projected"] = {
        "available_cash_cents": projected_cash,
        "total_cash_cents": projected_total_cash,
        "frozen_cash_cents": cash["frozen_cents"],
        "buy_funding_available_cents": funding_cash,
        "post_fill_cash_cents": projected_cash,
        "cash_after_buy_reservation_cents": funding_cash,
        "unfilled_sell_proceeds_spendable": False,
        "cash_buffer_basis": "observed_available_cash_after_buy_reservation; excludes_unfilled_sell_proceeds",
        "positions": projected_positions,
        "t1_blocked_quantity": sum(row["t1_blocked"] for row in projected_positions),
        "holdings_value_cents": projected_holdings,
        "equity_cents": projected_equity,
        "exposure_equity_cents": exposure_equity,
        "buy_notional_cents": buy_notional,
        "sell_notional_cents": sell_notional,
        "buy_fees_cents": buy_fees,
        "sell_fees_cents": sell_fees,
        "cash_weight": float(projected_cash_weight) if projected_cash_weight is not None else None,
        "gross_weight": float(projected_gross) if projected_gross is not None else None,
        "largest_positions": [
            {"symbol": symbol, "weight": float(weight)}
            for symbol, weight in projected_weights[:5]
        ],
        "fees_included": True,
        "slippage_included": False,
    }
    if result["blockers"]:
        result["status"] = "order_risk_blocked"
    elif result["risk_flags"]:
        result["status"] = "order_risk_attention"
    else:
        result["status"] = "order_risk_ready_readonly"
    return result
