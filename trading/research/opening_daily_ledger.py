"""Read one product-bound research Ledger at its last completed session.

Only existing Ledger getters are used. The next opening plan, copied preflight
and last completed session remain separate; unavailable snapshots are not
reconstructed from a newer head or from an unsealed preview copy.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import RunContext
from ..ledger import Ledger
from .opening_daily_detail import _case_directory, _dates, _exact_path, _intent, _read, _strategy_id
from .opening_daily_view import FLAGS, RUN_RELATIVE, opening_daily_view
from .saved_target_execution import project_saved_target_execution


SCHEMA = "saved_opening_daily_ledger_v1"


def _ref(value):
    return {"path": value.get("path", value.get("file")), "sha256": value["sha256"]}


def _unknown(base, reason):
    return {**base, "status": "unavailable", "reason": reason, "ledger_day": None,
        "context": None, "cash": None, "positions": None, "orders": None, "fills": None,
        "order_count": None, "fill_count": None, "order_blocked": None, "order_blocks": None, "blocked_count": None,
        "target_execution_diagnostics": None, "target_execution_diagnostic_count": None,
        "daily_summary": None, "executed_decision": None, "executed_decision_reason": reason}


def _project(state, config, day, context):
    if any(key not in state for key in ("orders", "fills", "positions", "cash", "prices", "receivables")):
        return None, "saved_ledger_collections_incomplete"
    if (not isinstance(state["orders"], dict) or not isinstance(state["fills"], dict)
            or not isinstance(state["positions"], dict) or state.get("broker_binding")):
        raise ValueError("Saved source is not an unbound research Ledger")
    if len(state["orders"]) > 20000 or len(state["fills"]) > 40000:
        raise ValueError("Saved selected Ledger exceeds its read bound")
    if any(not isinstance(row.get("day"), str) for row in list(state["orders"].values()) + list(state["fills"].values())):
        return None, "order_or_fill_day_unavailable"
    if any(row["day"] > day for row in list(state["orders"].values()) + list(state["fills"].values())):
        raise ValueError("Saved completed Ledger contains future orders or fills")
    fills, all_fills = [], state["fills"]
    for fill_id, row in all_fills.items():
        order = state["orders"].get(row.get("order_id"))
        if (order is None or row.get("symbol") != order.get("symbol") or row.get("side") != order.get("side")
                or type(row.get("qty")) is not int or row["qty"] <= 0):
            raise ValueError("Saved fill cannot be joined to its original order/security")
        if row["day"] == day:
            fills.append({**deepcopy(row), "fill_id": fill_id, "price_unit": row.get("price_unit", "cent"),
                          "fees": row.get("fees"), "fees_unit": "cent"})
    parent_ids = {row["order_id"] for row in fills}
    orders = []
    for order_id, row in state["orders"].items():
        if row.get("id") != order_id:
            raise ValueError("Saved order identity differs from its Ledger key")
        matched = [fill for fill in all_fills.values() if fill["order_id"] == order_id]
        if row.get("filled") != sum(fill["qty"] for fill in matched):
            raise ValueError("Saved order filled quantity differs from its actual fill rows")
        if row.get("fees") is not None and all(fill.get("fees") is not None for fill in matched):
            if row["fees"] != sum(fill["fees"] for fill in matched):
                raise ValueError("Saved cumulative order fees differ from incremental fill fees")
        if row["day"] == day or order_id in parent_ids:
            orders.append({**deepcopy(row), "order_id": order_id, "price_unit": row.get("price_unit", "cent"),
                           "fees": row.get("fees"), "fees_unit": "cent",
                           "fill_ids_on_ledger_day": [fill["fill_id"] for fill in fills if fill["order_id"] == order_id]})
    book = Ledger(state, config, RunContext.from_dict(context) if context is not None else None)
    positions, marks_known = [], True
    for symbol in state["positions"]:
        quantity = book.quantity(symbol)
        if quantity <= 0:
            continue
        mark = state["prices"].get(symbol)
        marks_known = marks_known and type(mark) is int and mark > 0
        sellable = book.available(symbol, day)
        if not 0 <= sellable <= quantity:
            raise ValueError("Saved dated available quantity contradicts settled lots/reservations")
        positions.append({"symbol": symbol, "quantity": quantity, "sellable_on_ledger_day": sellable,
            "unavailable_or_reserved_qty": quantity - sellable, "mark_price": mark,
            "price_unit": state.get("price_units", {}).get(symbol, "cent"),
            "lots": deepcopy(state["positions"][symbol]), "mark_is_broker_quote": False})
    cash = {"total_cents": state["cash"], "available_cents": book.cash_available(),
            "reserved_cents": state["cash"] - book.cash_available(), "frozen_cents": None,
            "equity_cents": book.equity() if marks_known else None, "unit": "cent",
            "basis": "saved_completed_ledger_balance_not_reconstructed_from_today_fills"}
    events = state.get("events")
    blocked = ([deepcopy(row) for row in events if row.get("day") == day and row.get("kind") == "order_blocked"]
               if isinstance(events, list) else None)
    diagnostics = project_saved_target_execution(events, day)
    saved_owner = ({"active_type": (state.get("leader_type_router") or {}).get("active_type"),
        "router_state": deepcopy(state["leader_type_router"])} if isinstance(state.get("leader_type_router"), dict) else {})
    return {"cash": cash, "positions": positions, "orders": orders, "fills": fills, **saved_owner,
        "order_count": len(orders), "fill_count": len(fills), "order_blocked": blocked, "order_blocks": blocked,
        "blocked_count": len(blocked) if blocked is not None else None,
        "target_execution_diagnostics": diagnostics,
        "target_execution_diagnostic_count": len(diagnostics) if diagnostics is not None else None,
        "query_basis": "orders_created_on_D_or_parent_of_D_fill; fills_exact_D; getter_positions_and_cash_at_D"}, None


def _executed(case, day, context, family, refs):
    candidates = [entry for entry in case["journal"] if entry["execution_day"] == day]
    if len(candidates) != 1:
        return None, "no_exact_prior_decision_journal_for_this_execution_session"
    entry = candidates[0]
    reference = _ref(entry["publication"])
    publication = _read(reference["path"], refs, expected=reference)
    if publication["source_close_day"] != entry["decision_day"] or publication["proposed_execution_day"] != day:
        raise ValueError("Executed decision publication does not belong to its exact execution day")
    bound = entry["bound_intent" if family == "alpha158" else "bound_original_intent"]
    if bound.get("context") != context or bound.get("execution_not_before") != day:
        raise ValueError("Executed source decision belongs to another research book/session")
    latest = case["daily"][-1]
    consumed = latest.get("consumed_published_close_intent")
    original = (consumed.get("original_intent", consumed.get("intent")) if isinstance(consumed, dict) else None)
    if original != bound:
        return None, "latest_session_does_not_record_consumption_of_that_exact_prior_intent"
    return {"source_day": entry["decision_day"], "execution_day": day,
        "kind": bound["kind"], "weights": deepcopy(bound["weights"]),
        "published_at": entry["publication"]["recorded_at"], "ref": reference,
        "context": deepcopy(context), "consumed_receipt": deepcopy(consumed)}, None


def _registered_unbound_ledger(base, selected, configured, refs, coordinator_path, before, current):
    """A missing next target cannot erase an independently sealed old Ledger."""
    from .registered_research_saved_opening import load_saved_case
    reference = base["product_source_head"]
    path = _exact_path(reference["path"], Path(configured["source"]) / "head.json")
    if sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        return _unknown(base, "product_source_head_replaced_original_snapshot_unavailable")
    try:
        saved = load_saved_case(path, selected["case_id"], current=current)
    except ValueError as error:
        if str(error) == "Original registered account has no committed Ledger":
            return _unknown(base, "original_registered_account_has_no_committed_ledger")
        raise
    for proof in saved["refs"].values():
        _read(proof["path"], refs, expected=proof)
    state, daily, context, day = (saved[k] for k in ("state", "daily", "context", "ledger_day"))
    config = StrategyConfig(**saved["config"]).validate()
    if (saved["refs"]["head"] != reference or state["day"] != day or daily["date"] != day
            or not isinstance(day, str)):
        raise ValueError("Registered saved Ledger differs from its exact original session")
    projected, reason = _project(state, config, day, context)
    if reason:
        return _unknown(base, reason)
    if (round(daily["cash"] * 100) != state["cash"] or (projected["cash"]["equity_cents"] is not None
            and round(daily["equity"] * 100) != projected["cash"]["equity_cents"])):
        raise ValueError("Saved daily cash/equity differs from its exact Ledger snapshot")
    if coordinator_path.read_bytes() != before:
        raise ValueError("Coordinator changed during selected Ledger read")
    return {**base, **projected, "strategy_id": _strategy_id("registered_research", selected),
        "status": "saved_completed_research_ledger", "ledger_day": day, "context": context,
        "context_basis": "original_selected_registered_replay_book_context",
        "source_binding_status": saved["basis"], "daily_summary": deepcopy(daily),
        "daily_summary_currency_unit": "CNY", "executed_decision": saved["executed_decision"],
        "executed_decision_reason": saved.get("executed_decision_reason"),
        "next_target_status": selected["status"], "next_target_reason": selected.get("reason"),
        "read_references": list(refs.values()), "independent_case_not_personal_account": True}


def opening_daily_ledger(data_root, route_id, case_id, *, now=None):
    current = now or datetime.now(timezone(timedelta(hours=8)))
    directory = (Path(data_root) / RUN_RELATIVE).resolve()
    base = {"schema": SCHEMA, "read_at": current.isoformat(), "route_id": route_id, "case_id": case_id,
        "source_day": None, "planned_execution_day": None, "source_head": None,
        "product_source_head": None, "stale": None, "refs": {}, **FLAGS,
        "ledger_getters_only": True, "next_plan_is_processed_fill_evidence": False,
        "plan_execution_reconciliation": None, "latest_plan_reconciliation": None,
        "last_executed_plan_reconciliation": None, "plan_reconciliation_history": None}
    if not directory.exists():
        return _unknown(base, "no_saved_opening_daily_coordinator")
    coordinator_path = directory / "head.json"
    before = coordinator_path.read_bytes()
    head = json.loads(before.decode("utf-8-sig"))
    summary = opening_daily_view(data_root, now=current)
    route = next((row for row in summary["routes"] if row["id"] == route_id), None)
    if route is None:
        raise KeyError("Unknown registered opening route")
    committed = head["routes"][route_id]
    if committed is None:
        return _unknown(base, "no_committed_opening_product")
    product, refs = committed["product"], {}
    if product["source_head"] != route["product_source_head"]:
        raise ValueError("Coordinator changed during Ledger selection")
    registration = _read(head["registration"]["path"], refs, expected=head["registration"])
    from .opening_daily import _load
    checked_head, registration = _load(directory)
    if checked_head != head:
        raise ValueError("Opening coordinator changed during Ledger registration selection")
    for reference in head.get("registration_addenda", []):
        _read(reference["path"], refs, expected=reference)
    configured = next(row for row in registration["routes"] if row["id"] == route_id)
    family = route["family"]
    base.update(source_day=route["source_day"], planned_execution_day=route["execution_day"],
        source_head=route["source_head"], product_source_head=route["product_source_head"],
        stale=route["stale"], source_changed=route["source_changed"], route=route, family=family,
        strategy_id=("alpha158:" if family == "alpha158" else "") + case_id)
    product_refs = product["refs"]
    if family == "fixed":
        if case_id != configured["signal"]:
            raise KeyError("Unknown registered opening case")
        pack = _read(product_refs["opening_pack"]["path"], refs, expected=product_refs["opening_pack"])
        _dates(pack, route)
        if (pack.get("schema") != "a_share_opening_pack_v1"
                or [row["strategy_id"] for row in pack["strategies"]] != [case_id]):
            raise ValueError("Fixed case is not the registered opening pack strategy")
        intent = _read(product_refs["intent"]["path"], refs, expected=product_refs["intent"])
    else:
        result = _read(product_refs["result"]["path"], refs, expected=product_refs["result"])
        if result["source_head_sha256"] != route["product_source_head"]["sha256"]:
            raise ValueError("Saved case index belongs to another source head")
        selected = next((row for row in result["cases"] if row["case_id"] == case_id), None)
        if selected is None:
            raise KeyError("Unknown registered opening case")
        if selected["status"] != "paper_preview_only":
            from .opening_plan_execution_reconciliation import reconcile_saved_history
            history = reconcile_saved_history(directory, route_id, case_id, now=current)
            base.update(last_executed_plan_reconciliation=history["last_executed_plan_reconciliation"],
                plan_reconciliation_history=history)
            if family == "registered_research":
                return _registered_unbound_ledger(base, selected, configured, refs, coordinator_path, before, current)
            return _unknown(base, "saved_opening_case_not_ready")
        base["strategy_id"] = _strategy_id(family, selected)
        preview_path = _exact_path(selected["preview"], Path(product_refs["result"]["path"]).parent / _case_directory(case_id) / "result.json")
        preview = _read(preview_path, refs)
        _dates(preview, route)
        intent = _read(_exact_path(preview["saved_intent"], preview_path.parent / "intent.json"), refs)
        from .opening_plan_execution_reconciliation import reconcile, reconcile_saved_history
        # The original plan stays fixed while its own source account may
        # advance. Reconciliation independently checks that exact append-only
        # context/version/publication; it never substitutes a newer plan.
        base["plan_execution_reconciliation"] = reconcile(preview_path, now=current)
        history = reconcile_saved_history(directory, route_id, case_id, latest_preview=preview_path,
            latest=base["plan_execution_reconciliation"], now=current)
        base.update(latest_plan_reconciliation=base["plan_execution_reconciliation"],
            last_executed_plan_reconciliation=history["last_executed_plan_reconciliation"],
            plan_reconciliation_history=history)
    _intent(intent, route, base["strategy_id"], case_id)
    config = StrategyConfig(**intent["execution_config"]).validate()
    day, context, daily, case, native_snapshot = route["source_day"], None, None, None, None
    if family == "fixed":
        evidence = intent["refs"]
        run = _read(evidence["source_run"]["path"], refs, expected=evidence["source_run"])
        state = _read(evidence["account"]["path"], refs, expected=evidence["account"])
        receipt = _read(evidence["replay"]["path"], refs, expected=evidence["replay"])
        if (run["replay_end"] != day or state["day"] != day or receipt.get("status") != "completed"
                or receipt.get("last_completed_session") != day or receipt.get("account_basis") != "last_successfully_completed_session"
                or receipt.get("config") != config.to_dict() or run.get("config") != config.to_dict()
                or receipt.get("completed_sessions") != run.get("sessions") or run.get("variants") != [case_id]
                or run.get("execution_admitted") is not False):
            raise ValueError("Fixed completed account/run/receipt binding changed")
        context_basis = "fixed_original_state_has_no_persisted_context; account_isolated_by_pinned_run_signal_and_account"
        source_binding = "exact_immutable_fixed_account_and_run_refs"
    elif family in {"native_preopen", "native_lowvol", "registered_research"}:
        if family == "native_preopen":
            from .native_saved_opening import load_saved_case
        elif family == "registered_research":
            from .registered_research_saved_opening import load_saved_case
        else:
            from .native_lowvol_saved_opening import load_saved_case
        source_path = _exact_path(route["product_source_head"]["path"], Path(configured["source"]) / "head.json")
        if sha256(source_path.read_bytes()).hexdigest() != route["product_source_head"]["sha256"]:
            return _unknown(base, "product_source_head_replaced_original_snapshot_unavailable")
        native_snapshot = load_saved_case(source_path, case_id, current=current)
        for reference in native_snapshot["refs"].values():
            _read(reference["path"], refs, expected=reference)
        state, daily, context = (native_snapshot[key] for key in ("state", "daily", "context"))
        if (native_snapshot["ledger_day"] != day or state["day"] != day or daily["date"] != day
                or context != intent["context"] or native_snapshot["config"] != config.to_dict()
                or native_snapshot["refs"]["head"] != route["product_source_head"]):
            raise ValueError("Native selected Ledger differs from its exact opening account, plan or date")
        context_basis = "original_selected_native_replay_book_context"
        source_binding = native_snapshot.get("basis", "exact_native_lowvol_account_and_publication")
    else:
        source_path = _exact_path(route["product_source_head"]["path"], Path(configured["source"]) / "head.json")
        if sha256(source_path.read_bytes()).hexdigest() != route["product_source_head"]["sha256"]:
            return _unknown(base, "product_source_head_replaced_original_snapshot_unavailable")
        source = _read(source_path, refs, expected=route["product_source_head"])
        case = source["cases"][case_id]
        state, daily = case["state"], case["daily"][-1] if case["daily"] else None
        prefix = {"eight_type": "leader-eight-type-forward:", "leader20": "leader-published-forward:",
                  "eight_type_residual": "leader-eight-type-residual-forward:",
                  "known_identity": "leader-known-identity-forward:",
                  "dated_catchup": "leader-dated-catchup-forward:",
                  "alpha158": "recent-alpha158-forward:"}[family]
        source_key = sha256(json.dumps(str(Path(configured["source"]).resolve()), sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
        context = case["context"]
        if (context.get("account_id") != prefix + source_key + ":" + case_id or context.get("mode") != "historical_replay"
                or context != intent["context"] or state.get("day") != day or daily is None or daily.get("date") != day
                or len(case["journal"]) != len(case["daily"]) or source["binding"]["config"] != config.to_dict()):
            raise ValueError("Selected research book context/date/config differs from the opening product")
        latest = case["journal"][-1]
        if latest["decision_day"] != day or latest["execution_day"] != route["execution_day"]:
            raise ValueError("Latest saved journal differs from the queued next opening")
        journal_ref = intent["refs"]["latest_journal"]
        if family != "alpha158":
            sealed = _read(journal_ref["path"], refs, expected=_ref(journal_ref))
            if sealed != latest or _ref(journal_ref) != _ref(case["journal_files"][-1]):
                raise ValueError("Selected independent Ledger journal seal changed")
        elif _ref(journal_ref) != route["product_source_head"]:
            raise ValueError("Embedded Alpha158 journal belongs to another head")
        context_basis = "original_selected_replay_book_context"
        source_binding = "exact_pinned_selected_source_head"
    projected, reason = _project(state, config, day, context)
    if reason:
        return _unknown(base, reason)
    if daily is not None:
        if round(daily["cash"] * 100) != state["cash"] or (projected["cash"]["equity_cents"] is not None
                and round(daily["equity"] * 100) != projected["cash"]["equity_cents"]):
            raise ValueError("Saved daily cash/equity differs from its exact Ledger snapshot")
    executed, executed_reason = ((native_snapshot["executed_decision"], native_snapshot.get("executed_decision_reason"))
        if native_snapshot is not None else _executed(case, day, context, family, refs) if case is not None else
        (None, "fixed_replay_does_not_persist_actual_dated_publication_journals"))
    if coordinator_path.read_bytes() != before:
        raise ValueError("Coordinator changed during selected Ledger read")
    return {**base, **projected, "status": "saved_completed_research_ledger", "ledger_day": day,
        "context": context, "context_basis": context_basis, "source_binding_status": source_binding,
        "daily_summary": deepcopy(daily), "daily_summary_currency_unit": "CNY",
        "executed_decision": executed, "executed_decision_reason": executed_reason,
        "read_references": list(refs.values()), "independent_case_not_personal_account": True}
