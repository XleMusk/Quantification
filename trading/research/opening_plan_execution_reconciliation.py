"""Join a sealed saved opening plan to its exact saved Engine execution.

No price retrieval, sizing, Engine, Ledger mutation or symbol/date heuristic is
used. Legacy unsealed plans can report an exact session as pending, but cannot
acquire a retrospective sealed-plan comparison after execution.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from ..contracts import MARKET_TZ, RunContext, TradingMode


SCHEMA = "saved_opening_plan_execution_reconciliation_v1"
BINDING_SCHEMA = "saved_opening_plan_execution_binding_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "producer_called": False,
    "engine_advanced": False, "ledger_mutated": False, "returns_calculated": False}


def _read(path, expected=None):
    path = Path(path).resolve()
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Saved reconciliation input exceeds its bounded reader")
    data = path.read_bytes()
    reference = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and reference != _ref(expected):
        raise ValueError("Saved reconciliation input bytes changed")
    return json.loads(data.decode("utf-8-sig")), reference


def _ref(value):
    return {"path": str(Path(value.get("path") or value["file"]).resolve()), "sha256": value["sha256"]}


def _file(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Saved plan clock requires an explicit timezone")
    return result.astimezone(MARKET_TZ)


def _integer(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return int(number) if number.is_finite() and number == int(number) else None


def _csv(reference):
    if _file(reference["path"]) != reference:
        raise ValueError("Saved plan quantity rows changed")
    with Path(reference["path"]).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) > 20000:
        raise ValueError("Saved plan exceeds its row bound")
    return rows


def make_binding(preview, *, source=None):
    """Reuse existing identity fields when saving a NEW opening product."""
    intent, intent_ref = _read(preview["saved_intent"])
    artifacts, source = preview["artifacts"], source or {}
    refs = intent["refs"]
    publication = source.get("publication") or refs.get("publication")
    journal = source.get("latest_journal") or refs.get("latest_journal")
    run = source.get("run_directory") or intent.get("original_run_directory") or intent.get("run_directory")
    if not publication or not journal or not run:
        raise ValueError("Exact saved publication and independent D journal are required")
    context = intent.get("context")
    if not isinstance(context, dict) or RunContext.from_dict(context).mode != TradingMode.REPLAY:
        raise ValueError("Only one explicitly bound research account can be reconciled")
    version = intent.get("target_version")
    if type(version) is not int or version <= 0:
        raise ValueError("The saved processed-session target version is required")
    return {"schema": BINDING_SCHEMA, "case_id": intent["case_id"],
        "context": deepcopy(context), "source_day": intent["source_day"],
        "execution_day": intent["execution_day"], "target_version": version,
        "target_version_basis": intent.get("target_version_basis"),
        "run_directory": str(Path(run).resolve()), "publication": _ref(publication),
        "latest_journal": _ref(journal), "intent": intent_ref,
        "account": _file(Path(preview["source_artifacts"]) / "account.json"),
        "targets": _file(artifacts["target_holdings"]), "orders": _file(artifacts["order_plan"]),
        "created_at": preview["generated_at"], "execution_config": deepcopy(preview["execution_config"]),
        **FLAGS}


def _base(preview_path, now):
    return {"schema": SCHEMA, "read_at": now.isoformat(), "preview": str(Path(preview_path).resolve()),
        "status": "unknown", "reason": None, "rows": None, "cash": None,
        "plan_identity": None, "execution_day": None, **FLAGS}


def reconcile(preview_path, *, head_path=None, now=None):
    now = now or datetime.now(MARKET_TZ)
    result = _base(preview_path, now)
    try:
        preview, preview_ref = _read(preview_path)
        binding = preview.get("execution_binding")
        sealed = binding is not None
        if binding is None:
            # Existing plans are never rewritten. This permits a truthful
            # pre-execution observation, not a retrospective sealed comparison.
            binding = make_binding(preview)
        if binding.get("schema") != BINDING_SCHEMA or any(binding.get(k) is not v for k, v in FLAGS.items()):
            raise ValueError("Saved plan execution binding changed")
        run = Path(binding["run_directory"]).resolve()
        exact_head = run / "head.json"
        if head_path is not None and Path(head_path).resolve() != exact_head:
            raise ValueError("An execution from another research run cannot be matched")
        d, e, key, context = (binding[k] for k in ("source_day", "execution_day", "case_id", "context"))
        result.update(execution_day=e, source_run_directory=str(run), plan_identity={k: deepcopy(binding[k]) for k in
            ("context", "case_id", "source_day", "execution_day", "publication", "target_version")},
            plan_payload_sealed=sealed, preview_ref=preview_ref)
        if (preview.get("source_cutoff") != d or preview.get("execution_day") != e
                or RunContext.from_dict(context).mode != TradingMode.REPLAY
                or not _stamp(d + "T15:00:00+08:00") <= _stamp(binding["created_at"])
                    < _stamp(e + "T09:30:00+08:00") or _stamp(binding["created_at"]) > now):
            raise ValueError("Original plan was not saved before its exact E opening")
        intent, _ = _read(binding["intent"]["path"], binding["intent"])
        original, _ = _read(binding["account"]["path"], binding["account"])
        publication, _ = _read(binding["publication"]["path"], binding["publication"])
        journal, _ = _read(binding["latest_journal"]["path"], binding["latest_journal"])
        if (intent.get("context") != context or intent.get("case_id") != key
                or intent.get("source_day") != d or intent.get("execution_day") != e
                or intent.get("target_version") != binding["target_version"]
                or original.get("day") != d or publication.get("source_close_day") != d
                or publication.get("proposed_execution_day") != e
                or journal.get("decision_day", journal.get("day")) != d or journal.get("execution_day") != e
                or _ref(journal["publication"]) != binding["publication"]
                or _stamp(publication["recorded_at"]) > _stamp(binding["created_at"])):
            raise ValueError("Plan context, exact D/E, target version or publication binding conflicts")
        head, head_ref = _read(exact_head)
        case = head["cases"][key]
        daily, journals = case["daily"], case["journal"]
        if len(journals) != len(daily) or len(case["journal_files"]) != len(daily):
            raise ValueError("Saved account daily rows and independent journals are incomplete")
        matching = [i for i, row in enumerate(daily) if row["date"] == d]
        if (case["context"] != context or len(matching) != 1
                or matching[0] + 1 != binding["target_version"]
                or head.get("binding", {}).get("config") != binding["execution_config"]):
            raise ValueError("Execution book context/config or original target version conflicts")
        index = matching[0]
        if (journals[index] != journal or _ref(case["journal_files"][index]) != binding["latest_journal"]
                or round(daily[index]["cash"] * 100) != original["cash"]):
            raise ValueError("Original D journal or saved account balance conflicts")
        result.update(execution_head=head_ref, last_saved_session=case["state"].get("day"))
        found = [i for i, row in enumerate(daily) if row["date"] == e]
        if not found:
            if case["state"].get("day") is None or case["state"]["day"] < e:
                return {**result, "status": "pending", "reason": "exact_E_Engine_session_not_saved",
                    "last_saved_session": case["state"].get("day")}
            raise ValueError("Saved account passed E without its exact execution row")
        if not sealed:
            raise ValueError("Legacy plan quantities were not sealed before execution; no retrospective comparison")
        if len(found) != 1 or found[0] != index + 1:
            raise ValueError("E is not the exact next saved session of this target version")
        execution = daily[found[0]]
        execution_journal = journals[found[0]]
        execution_proof = _ref(case["journal_files"][found[0]])
        saved_execution_journal, _ = _read(execution_proof["path"], execution_proof)
        if saved_execution_journal != execution_journal:
            raise ValueError("Exact E independent journal differs from its saved head")
        if execution_journal.get("row_sha256") is not None:
            row_hash = sha256(json.dumps(execution, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if row_hash != execution_journal["row_sha256"]:
                raise ValueError("Exact E row differs from its independently saved session seal")
        expected_receipt = original.get("pending_published_close_intent")
        if execution.get("consumed_published_close_intent") != expected_receipt:
            raise ValueError("E did not record consumption of the exact original published target receipt")
        expected_risk = original.get("pending_risk_exits")
        if expected_risk is not None:
            if (expected_risk.get("context") != context or expected_risk.get("decided_on") != d
                    or expected_risk.get("execution_on") != e):
                raise ValueError("Original independent risk queue differs from this exact D/E book")
            # Do not treat a scheduled exit as an actual execution attempt.
            scheduled = [ev for ev in original.get("events", []) if ev.get("day") == d
                and ev.get("kind") == "risk_exit_scheduled" and ev.get("execution_not_before") == e
                and ev.get("symbols") == expected_risk.get("symbols")]
            if len(scheduled) != 1:
                raise ValueError("Independent risk queue lacks its exact original saved scheduling evidence")
        targets, planned_orders = _csv(binding["targets"]), _csv(binding["orders"])
        if len({r["symbol"] for r in targets}) != len(targets):
            raise ValueError("Saved target rows are not unique")
        state, events = case["state"], execution.get("events")
        if not isinstance(events, list):
            raise ValueError("Exact E Engine events are unavailable")
        if expected_risk is not None:
            consumed_risk = [ev for ev in events if ev.get("kind") == "risk_exit_execution"
                and ev.get("day") == e and ev.get("decided_on") == d
                and ev.get("symbols") == expected_risk.get("symbols")
                and ev.get("source") == expected_risk.get("source")
                and ev.get("reason") == expected_risk.get("reason")]
            if len(consumed_risk) != 1:
                raise ValueError("E has no exact saved consumption of the original independent risk queue")
        diagnostics = [ev for ev in events if ev.get("day") == e and ev.get("kind") == "target_execution_diagnostic"]
        orders = {oid: order for oid, order in state["orders"].items() if order.get("day") == e}
        fills = [(fid, fill) for fid, fill in state["fills"].items() if fill.get("day") == e]
        if any(fill.get("order_id") not in orders or fill.get("symbol") != orders[fill["order_id"]]["symbol"]
               for _, fill in fills):
            raise ValueError("E fill has no exact same-account parent order")
        symbols = sorted(set(r["symbol"] for r in targets) | {r["symbol"] for r in diagnostics}
            | {r["symbol"] for r in orders.values()})
        rows = []
        for symbol in symbols:
            plan = next((r for r in targets if r["symbol"] == symbol), None)
            trace = [deepcopy(r) for r in diagnostics if r["symbol"] == symbol]
            parents = [{"order_id": oid, **deepcopy(order)} for oid, order in orders.items() if order["symbol"] == symbol]
            callbacks = [{"fill_id": fid, **deepcopy(fill)} for fid, fill in fills if fill["symbol"] == symbol]
            blocks = [deepcopy(ev) for ev in events if ev.get("day") == e and ev.get("symbol") == symbol
                and ev.get("kind") in {"order_blocked", "rejected", "rebalance_deferred"}]
            planned_qty = _integer(plan.get("target_qty")) if plan else None
            actual_qty = trace[0].get("engine_target_quantity") if len(trace) == 1 else None
            reference_mark = _integer(plan.get("close_cents")) if plan else None
            actual_mark = trace[0].get("engine_mark") if len(trace) == 1 and trace[0].get("price_unit") == "cent" else None
            evidence = [{"source": "target_execution_diagnostic", "mechanical_reason": ev.get("mechanical_reason"),
                         "constraint_flags": deepcopy(ev.get("constraint_flags"))} for ev in trace]
            evidence += [{"source": ev["kind"], "reason": ev.get("reason")} for ev in blocks]
            rows.append({"symbol": symbol, "planned_target_quantity": planned_qty,
                "planned_orders": [deepcopy(r) for r in planned_orders if r["symbol"] == symbol],
                "actual_engine_target_quantity": actual_qty,
                "target_quantity_difference": actual_qty - planned_qty if type(actual_qty) is int and planned_qty is not None else None,
                "reference_close_cents": reference_mark, "recorded_engine_mark_cents": actual_mark,
                "reference_price_differs": reference_mark != actual_mark
                    if reference_mark is not None and actual_mark is not None else None,
                "price_difference_is_causal_attribution": False,
                "diagnostics": trace or None, "orders": parents, "fills": callbacks,
                "parent_order_count": len(parents), "fill_callback_count": len(callbacks),
                "fee_cents": sum(row["fees"] for row in callbacks)
                    if all(type(row.get("fees")) is int for row in callbacks) else None,
                "reason_evidence": evidence, "order_blocks": blocks,
                "status": "saved_execution_evidence" if trace or parents or blocks
                    else "no_recorded_attempt_no_cause_inferred"})
        return {**result, "status": "reconciled_saved_execution", "reason": None, "rows": rows,
            "consumed_published_receipt": deepcopy(execution.get("consumed_published_close_intent")),
            "independent_risk_queue": deepcopy(expected_risk),
            "cash": {"plan_D_balance_cents": original["cash"], "E_saved_balance_rmb": execution.get("cash"),
                "D_to_E_change_is_trade_only": False, "cash_bridge_not_calculated": True},
            "actual_reasons_are_saved_evidence_only": True,
            "fill_callbacks_are_independent_trades": False}
    except (KeyError, ValueError, TypeError, OSError) as error:
        return {**result, "status": "unknown", "reason": type(error).__name__ + ": " + str(error)}


def reconcile_saved_history(coordinator, route_id, case_id, *, latest_preview=None, latest=None, now=None):
    """Select an older exact product from the coordinator's existing attempts.

    The latest future plan is returned separately. Selection is chronological,
    never by result; an unknown latest covered execution is not replaced by an
    older successful comparison. No historical source head is reconstructed.
    """
    now = now or datetime.now(MARKET_TZ)
    result = {"status": "unknown", "reason": None, "latest_plan_reconciliation": latest,
        "last_executed_plan_reconciliation": None, "history_basis": "existing_committed_opening_attempts",
        "saved_plan_count": None, **FLAGS}
    try:
        from .opening_daily import (_load, _name, _input_id, _registration_for,
            _product_boundary, SCHEMA as COORDINATOR_SCHEMA, METHOD)
        directory = Path(coordinator).resolve()
        head_bytes = (directory / "head.json").read_bytes()
        head, registration = _load(directory)
        route_id = _name(route_id)
        if route_id not in {row["id"] for row in registration["routes"]}:
            raise ValueError("Unknown registered opening route")
        paths = list((directory / "attempts" / route_id).glob("*.json"))
        if len(paths) > 1024:
            raise ValueError("Saved opening attempt history exceeds its bounded reader")
        records = []
        anchor = _registration_for(head, route_id)
        for path in paths:
            attempt, reference = _read(path)
            identity = _input_id(anchor, route_id, attempt["source_head"])
            if (attempt.get("schema") != COORDINATOR_SCHEMA or attempt.get("method_id") != METHOD
                    or attempt.get("registration") != anchor or attempt.get("route_id") != route_id
                    or attempt.get("input_id") != identity or path.stem != identity):
                raise ValueError("Saved opening attempt identity changed")
            if not attempt.get("head_committed") or not attempt.get("product_validated"):
                continue
            product = attempt["product"]
            _product_boundary(attempt["source_head"], product)
            if "result" not in product["refs"]:
                continue  # The fixed-account pack has a different saved contract.
            records.append((product["source_day"], product["execution_day"], reference, product))
        records.sort(key=lambda row: (row[0], row[1]), reverse=True)
        if len({(row[0], row[1]) for row in records}) != len(records):
            raise ValueError("Multiple committed opening products claim the same exact D/E")
        result["saved_plan_count"] = len(records)
        for d, e, attempt_ref, product in records:
            # Future E may not yet have a saved account row. Avoid selecting it
            # as the last execution merely because the plan is newer.
            if e > now.date().isoformat():
                continue
            root_ref = _ref(product["refs"]["result"])
            catalog, _ = _read(root_ref["path"], root_ref)
            if catalog.get("source_head_sha256") != product["source_head"]["sha256"]:
                raise ValueError("Historical product index belongs to another original source head")
            cases = [row for row in catalog["cases"] if row.get("case_id") == case_id]
            if len(cases) != 1 or cases[0].get("status") != "paper_preview_only":
                continue
            from .opening_daily_detail import _case_directory
            expected = Path(root_ref["path"]).parent / _case_directory(case_id) / "result.json"
            preview = Path(cases[0]["preview"]).resolve()
            if preview != expected.resolve():
                raise ValueError("Historical case preview is outside its exact product")
            observation = (latest if latest is not None and latest_preview is not None
                and preview == Path(latest_preview).resolve() else reconcile(preview, now=now))
            identity = observation.get("plan_identity") or {}
            if identity and (identity.get("case_id") != case_id or identity.get("source_day") != d
                    or identity.get("execution_day") != e):
                raise ValueError("Historical plan does not belong to this exact saved D/E")
            if observation["status"] == "pending":
                continue
            # Unknown evidence remains visible rather than falling back to a
            # more flattering older complete comparison.
            result["last_executed_plan_reconciliation"] = {**observation,
                "saved_attempt": attempt_ref, "saved_product": root_ref}
            result.update(status="saved_execution_plan_selected" if observation["status"] == "reconciled_saved_execution"
                else "unknown", reason=observation.get("reason"))
            break
        if result["last_executed_plan_reconciliation"] is None:
            result.update(status="no_saved_plan_with_execution_session",
                reason="existing_saved_plan_history_has_no_exact_saved_E_execution")
        if (directory / "head.json").read_bytes() != head_bytes:
            raise ValueError("Opening coordinator changed during historical plan selection")
        return result
    except (KeyError, ValueError, TypeError, OSError) as error:
        return {**result, "status": "unknown", "reason": type(error).__name__ + ": " + str(error),
            "last_executed_plan_reconciliation": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--head", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.out and args.out.exists():
        raise FileExistsError("Use a new reconciliation observation file")
    result = reconcile(args.preview, head_path=args.head)
    if args.out:
        target = args.out.resolve()
        roots = [args.preview.resolve().parent]
        if result.get("source_run_directory"):
            roots.append(Path(result["source_run_directory"]).resolve())
        if args.head:
            roots.append(args.head.resolve().parent)
        if any(target == root or root in target.parents for root in roots):
            raise ValueError("Observation must stay outside original plan and account")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
