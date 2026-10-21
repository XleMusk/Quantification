"""Bounded case details from one registered, already saved opening product.

The coordinator pins pack/result bytes. Legacy nested preview/CSV payloads were
not independently sealed: their fixed paths and source identities are checked,
and hashes observed on this read are labelled separately, never backdated.
"""
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import json
from pathlib import Path
import re

from .opening_daily_view import FLAGS, RUN_RELATIVE, opening_daily_view


SCHEMA = "saved_opening_daily_detail_v1"
_LIMIT = 10 * 1024 * 1024


def _read(path, refs, *, expected=None, kind="json"):
    path = Path(path).resolve()
    if path.stat().st_size > _LIMIT:
        raise ValueError("Saved opening detail exceeds its bounded file size")
    data = path.read_bytes()
    reference = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and reference != expected:
        raise ValueError("Registered opening artifact bytes changed")
    refs[str(path)] = reference
    if kind == "json":
        value = json.loads(data.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Saved opening JSON must be an object")
        return value
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("Saved opening CSV requires unambiguous headers")
    rows = []
    integers = {"current_qty", "target_qty", "delta_qty", "qty", "close_cents", "price_cents",
                "reserved_cents", "current_account_sellable_qty", "minimum_buy_quantity", "quantity_step"}
    fractions = {"target_weight", "effective_target_weight", "realized_weight", "target_budget_rmb", "minimum_buy_notional_rmb"}
    for raw in reader:
        if len(rows) >= 2000 or None in raw or any(value is None for value in raw.values()):
            raise ValueError("Saved opening CSV is malformed or exceeds its row bound")
        row = {key: value if value != "" else None for key, value in raw.items()}
        for key in integers & row.keys():
            if row[key] is not None:
                if not re.fullmatch(r"-?\d+", row[key]):
                    raise ValueError("Saved share quantity must be an integer or unknown")
                row[key] = int(row[key])
        for key in fractions & row.keys():
            if row[key] is not None:
                try:
                    value = Decimal(row[key])
                except InvalidOperation as error:
                    raise ValueError("Saved weight must be numeric or unknown") from error
                if not value.is_finite():
                    raise ValueError("Saved weight must be finite or unknown")
                row[key] = float(value)
        rows.append(row)
    return rows


def _exact_path(value, expected):
    if Path(value).resolve() != Path(expected).resolve():
        raise ValueError("Nested opening artifact escaped its registered fixed path")
    return Path(expected).resolve()


def _boundary(value):
    if (value.get("candidate_panel_only") is not True
            or any(value.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Saved opening detail changed its research boundary")


def _dates(value, route, *, source_key="source_cutoff"):
    if value.get(source_key) != route["source_day"] or value.get("execution_day") != route["execution_day"]:
        raise ValueError("Saved opening detail does not match its product D/E")


def _case_directory(case_id, *, family=None):
    # registered_research_saved_opening.build_opening always hashes its case
    # key, including otherwise filesystem-safe underscore-only identities.
    if family == "registered_research":
        return "case_" + sha256(case_id.encode()).hexdigest()[:24]
    return case_id if re.fullmatch(r"[A-Za-z0-9_-]+", case_id) else "case_" + sha256(case_id.encode()).hexdigest()[:24]


def _intent(value, route, strategy_id, case_id):
    _boundary(value)
    _dates(value, route, source_key="source_day")
    schemas = {"fixed": "saved_fixed_account_intent_v1", "eight_type": "saved_leader_eight_type_account_intent_v1",
               "leader20": "saved_leader_published_account_intent_v1", "alpha158": "saved_alpha158_account_intent_v1",
               "dated_catchup": "saved_leader_dated_catchup_account_intent_v1",
               "eight_type_residual": "saved_leader_eight_type_residual_account_intent_v1",
               "known_identity": "saved_leader_known_identity_account_intent_v1",
               "native_preopen": "saved_native_preopen_account_intent_v1",
               "native_lowvol": "saved_native_lowvol_preopen_intent_v1",
               "registered_research": "saved_registered_research_account_intent_v1"}
    if (value.get("schema") != schemas[route["family"]]
            or value.get("strategy_id") != strategy_id or value.get("case_id", case_id) != case_id
            or value.get("kind") not in {"FULL_TARGET", "NO_CHANGE"}
            or (value["kind"] == "NO_CHANGE" and value.get("weights") is not None)
            or (value["kind"] == "FULL_TARGET" and not isinstance(value.get("weights"), dict))):
        raise ValueError("Saved opening intent identity or null/full semantics changed")
    if route["family"] != "fixed" and value["refs"]["head"] != route["product_source_head"]:
        raise ValueError("Saved case intent belongs to another source head")
    projected = {key: deepcopy(value.get(key)) for key in ("kind", "weights", "original_weights", "remaining_exit_symbols",
        "queued_exit", "source_known_at", "target_version", "target_version_basis", "context", "reason",
        "stage_unknown_symbols", "policy_id")}
    if value.get("route_kind") in {"leader_type_router", "leader_type_router_runtime_repair"}:
        projected.update(active_type=value.get("active_type"), router_state=deepcopy(value.get("router_state")))
    if route["family"] == "native_preopen":
        decision = value["plan_decision"]
        if decision["model_kind"] != case_id or decision["decision"] != value["kind"] or decision["weights"] != value["weights"]:
            raise ValueError("Native displayed decision differs from its original model plan")
        projected["reason"] = decision.get("reason")
        projected["stage_unknown_symbols"] = deepcopy(decision.get("input_scope", {}).get("unknown_stage_symbols"))
    return projected


def _strategy_id(family, row):
    if family == "registered_research":
        from .registered_research_saved_opening import ROUTE_KINDS
        strategy_id = row.get("strategy_id")
        if (not isinstance(strategy_id, str)
                or not any(strategy_id == kind + ":" + row["case_id"] for kind in
                    ROUTE_KINDS)):
            raise ValueError("Registered research case needs its explicit independent route identity")
        return strategy_id
    if family == "native_lowvol":
        strategy_id = row.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id.startswith("native_lowvol:"):
            raise ValueError("Native low-volatility case needs its independent strategy identity")
        return strategy_id
    if family == "native_preopen":
        strategy_id = row.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id.startswith("native_preopen:") or not strategy_id.endswith(":" + row["case_id"]):
            raise ValueError("Native opening case has no explicit policy/model strategy identity")
        return strategy_id
    return ("alpha158:" if family == "alpha158" else "") + row["case_id"]


def _saved_holding_reviews(pack, route, pack_data, read_refs, strategies):
    reference = pack.get("holding_review_ref")
    if reference is None:
        return None
    if route["family"] not in {"eight_type", "dated_catchup", "eight_type_residual", "known_identity"}:
        raise ValueError("Saved holding review belongs to an unsupported source family")
    path = _exact_path(reference["path"], pack_data / "holding_review.json")
    _exact_path(pack["artifacts"]["holding_review"], path)
    record = _read(path, read_refs, expected=reference)
    _boundary(record)
    _dates(record, route, source_key="source_day")
    rows = record.get("case_reviews")
    if (record.get("schema") != "saved_leader_opening_holding_review_v1"
            or record.get("source_family") != route["family"]
            or record.get("current_risk_recomputed") is not False
            or not isinstance(rows, list) or [row["strategy_id"] for row in rows] != strategies):
        raise ValueError("Saved holding review identity or original case sequence changed")
    return {row["strategy_id"]: row for row in rows}


def _case_holding_review(value, saved_intent, route, case_id, positions):
    _boundary(value)
    _dates(value, route, source_key="source_day")
    row = value.get("saved_position_review")
    if (value.get("schema") != "saved_leader_holding_review_v1"
            or value.get("source_family") != route["family"]
            or value.get("strategy_id") != saved_intent["strategy_id"]
            or value.get("refs") != saved_intent["refs"]
            or value.get("source_known_at") != saved_intent["source_known_at"]
            or not isinstance(row, dict) or row.get("case_id") != case_id
            or row.get("context") != saved_intent["context"]
            or row.get("processed_day") != route["source_day"]
            or row.get("error") or row.get("blocking_evidence")
            or not isinstance(row.get("positions"), list)
            or not isinstance(row.get("latest_order_blocks"), list)):
        raise ValueError("Saved holding review differs from this exact source intent and account")
    held = row["positions"]
    quantities = {p["symbol"]: p["quantity_shares"] for p in held}
    observed = {p["symbol"]: p["quantity"] for p in positions}
    if (len(quantities) != len(held) or len(observed) != len(positions) or quantities != observed
            or any(type(p["quantity_shares"]) is not int or p["quantity_shares"] <= 0 for p in held)):
        raise ValueError("Saved holding review quantities differ from the product holdings")
    for position in positions:
        saved = next(p for p in held if p["symbol"] == position["symbol"])
        position["sellable_on_source_day"] = saved["sellable_on_book_day_shares"]
    return deepcopy(row)


def opening_daily_detail(data_root, route_id, *, now=None):
    current = now or datetime.now(timezone(timedelta(hours=8)))
    directory = (Path(data_root) / RUN_RELATIVE).resolve()
    base = {"schema": SCHEMA, "read_at": current.isoformat(), "cases": [], **FLAGS,
            "saved_artifacts_revalidated": True, "nested_payload_hashes_previously_pinned": False,
            "candidate_orders_are_fills": False, "cases_combined_into_account": False}
    if not directory.exists():
        return {**base, "status": "unavailable", "reason": "no_saved_opening_daily_coordinator", "route": None}
    head_path = directory / "head.json"
    before = head_path.read_bytes()
    head = json.loads(before.decode("utf-8-sig"))
    summary = opening_daily_view(data_root, now=current)
    route = next((row for row in summary["routes"] if row["id"] == route_id), None)
    if route is None:
        raise KeyError("Unknown registered opening route")
    committed = head["routes"][route_id]
    if committed is None:
        return {**base, "status": "unavailable", "reason": "no_committed_product", "route": route}
    product = committed["product"]
    if product["source_head"] != route["product_source_head"] or product["refs"] != route["refs"]:
        raise ValueError("Opening coordinator changed during case selection")
    read_refs = {}
    registration = _read(head["registration"]["path"], read_refs, expected=head["registration"])
    from .opening_daily import _load
    checked_head, registration = _load(directory)
    if checked_head != head:
        raise ValueError("Opening coordinator changed during registration selection")
    for reference in head.get("registration_addenda", []):
        _read(reference["path"], read_refs, expected=reference)
    configured = next(row for row in registration["routes"] if row["id"] == route_id)
    refs = product["refs"]
    family = route["family"]
    result = _read(refs["result"]["path"], read_refs, expected=refs["result"]) if "result" in refs and family != "fixed" else None
    if family == "fixed":
        sources = [{"case_id": configured["signal"], "status": "paper_preview_only", "preview": refs["preview"]["path"]}]
    else:
        _boundary(result)
        _dates(result, route)
        if (result.get("source_family", "eight_type") != family
                or result.get("source_head_sha256") != route["product_source_head"]["sha256"]
                or Path(result["source_run_directory"]).resolve() != Path(configured["source"]).resolve()):
            raise ValueError("Saved route result belongs to another source")
        sources = result["cases"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32 or len({row["case_id"] for row in sources}) != len(sources):
        raise ValueError("Saved route case list is missing, duplicated or outside its bound")
    pack = _read(refs["opening_pack"]["path"], read_refs, expected=refs["opening_pack"]) if "opening_pack" in refs else None
    ready = [row for row in sources if row["status"] == "paper_preview_only"]
    strategies = [_strategy_id(family, row) for row in ready]
    order_rows, checks, holding_reviews = [], [], None
    if pack is not None:
        if (pack.get("schema") != "a_share_opening_pack_v1" or pack.get("status") != "paper_candidates_only"
                or pack.get("execution_admitted") is not False or pack.get("automatic_strategy_selection") is not False
                or [row["strategy_id"] for row in pack["strategies"]] != strategies):
            raise ValueError("Saved opening pack case identity or boundary changed")
        _dates(pack, route)
        if result is not None:
            _exact_path(result["opening_pack"], refs["opening_pack"]["path"])
        pack_path = Path(refs["opening_pack"]["path"])
        pack_data = Path(data_root) / "research/opening_pack" / pack_path.parent.name
        for field, filename in (("candidate_orders", "candidate_orders.csv"),
                                ("ledger_preflight_by_strategy", "ledger_preflight_by_strategy.csv")):
            path = _exact_path(pack["artifacts"][field], pack_data / filename)
            rows = _read(path, read_refs, kind="csv")
            if any(row.get("strategy_id") not in strategies for row in rows):
                raise ValueError("Saved opening rows crossed research cases")
            if field == "candidate_orders":
                order_rows = rows
            else:
                checks = rows
        if pack.get("candidate_order_count") is not None and pack["candidate_order_count"] != len(order_rows):
            raise ValueError("Saved opening candidate order count differs from its rows")
        holding_reviews = _saved_holding_reviews(pack, route, pack_data, read_refs, strategies)
    elif ready:
        raise ValueError("Ready case previews have no saved opening pack")
    cases = []
    for source in sources:
        case_id = source["case_id"]
        strategy_id = _strategy_id(family, source)
        if source["status"] != "paper_preview_only":
            cases.append({"case_id": case_id, "strategy_id": strategy_id, "status": source["status"],
                "reason": source.get("reason"), "missing": deepcopy(source.get("missing")), "saved_source": deepcopy(source),
                "intent": None, "positions": None, "targets": None, "orders": None, "preflight": None,
                "fills": None, "returns": None, "holding_review": None,
                "holding_review_status": "not_saved"})
            continue
        packed = next(row for row in pack["strategies"] if row["strategy_id"] == strategy_id)
        expected_preview = (Path(refs["preview"]["path"]) if family == "fixed" else
                            Path(refs["result"]["path"]).parent / _case_directory(case_id, family=family) / "result.json")
        preview_path = _exact_path(source["preview"], expected_preview)
        _exact_path(packed["preview"], preview_path)
        preview = _read(preview_path, read_refs, expected=refs["preview"] if family == "fixed" else None)
        _boundary(preview)
        _dates(preview, route)
        if preview.get("strategy_id") != strategy_id or preview.get("status") != "paper_preview_only":
            raise ValueError("Saved case preview identity changed")
        if family == "fixed":
            saved_intent = _read(refs["intent"]["path"], read_refs, expected=refs["intent"])
            if Path(preview["source_run"]).resolve() != Path(saved_intent["refs"]["source_run"]["path"]).resolve():
                raise ValueError("Fixed preview refers to a different saved run")
            case_data = Path(data_root) / "research/paper_rebalance" / preview_path.parent.name
        else:
            if preview["source_refs"]["head"] != route["product_source_head"]:
                raise ValueError("Saved case preview belongs to another source head")
            case_data = preview_path.parent
            saved_intent = _read(_exact_path(preview["saved_intent"], case_data / "intent.json"), read_refs)
        intent = _intent(saved_intent, route, strategy_id, case_id)
        if preview.get("execution_config") != saved_intent.get("execution_config"):
            raise ValueError("Saved case preview configuration differs from its intent")
        filename = "provisional_target_holdings.csv" if family == "fixed" else "target_holdings.csv"
        target_path = _exact_path(preview["artifacts"]["provisional_target_holdings"], case_data / filename)
        targets = _read(target_path, read_refs, kind="csv")
        positions = [{"symbol": row["symbol"], "quantity": row.get("current_qty"),
            "mark_price_cents": row.get("close_cents"), "price_basis": row.get("price_basis"),
            "sellable_on_source_day": None} for row in targets if row.get("current_qty") is not None and row["current_qty"] > 0]
        if any(row.get("price_basis") != "research_raw_close_rmb" for row in targets):
            raise ValueError("Saved target prices lost their research-close unit")
        holding_review = (_case_holding_review(holding_reviews[strategy_id], saved_intent,
            route, case_id, positions) if holding_reviews is not None else None)
        orders = []
        for order in [row for row in order_rows if row["strategy_id"] == strategy_id]:
            matches = [row for row in checks if all(row.get(key) == order.get(key) for key in ("strategy_id", "symbol", "side", "qty"))]
            if len(matches) > 1:
                raise ValueError("Saved order has ambiguous preflight records")
            orders.append({**order, "status": order.get("source_status"), "reason": order.get("source_reason"),
                "price_basis": "research_raw_close_rmb", "preflight": matches[0] if matches else None})
        preflight = deepcopy(pack.get("ledger_preflight_by_strategy", {}).get(strategy_id))
        cases.append({"case_id": case_id, "strategy_id": strategy_id, "status": source["status"],
            "trade_status": preview.get("trade_status"), "intent": intent, "cash_rmb": preview.get("cash_rmb"),
            "equity_rmb": preview.get("terminal_equity_rmb"), "positions": positions, "targets": targets,
            "orders": orders, "preflight": preflight, "preflight_is_copied_ledger_only": True,
            "release_blockers": deepcopy(preview.get("release_blockers")),
            "missing": deepcopy(preview.get("provisional_missing_quote_symbols")),
            "price_basis": "research_raw_close_rmb", "fills": None, "returns": None,
            "holding_review": holding_review,
            "holding_review_status": "saved_product_bound" if holding_review is not None else "not_saved",
            "refs": {"preview": read_refs[str(preview_path)], "target_holdings": read_refs[str(target_path)],
                **({"holding_review": deepcopy(pack["holding_review_ref"])} if holding_review is not None else {})},
            "account_basis": "saved_research_preview_quantities_not_current_broker_holdings"})
    if head_path.read_bytes() != before:
        raise ValueError("Opening coordinator advanced during case detail read")
    return {**base, "status": "saved_case_details", "route": route, "refs": deepcopy(refs), "cases": cases,
        "read_references": list(read_refs.values()), "registered_artifact_hashes_verified": True,
        "nested_hash_basis": "fixed_parent_reference_and_source_identity_checked; nested_hash_observed_on_read_not_historical_seal"}
