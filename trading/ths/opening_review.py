"""Read one registered research case against explicit, fresh THS evidence.

No store, client, account or strategy is created. Research candidates remain
ineligible for prefill even when their observed-account arithmetic is complete.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from ..engine import shares_for_target
from ..rules import execution_rule
from ..research.opening_daily_detail import opening_daily_detail, _intent, _read
from ..research.opening_daily_view import RUN_RELATIVE
from .adapter import validate_cash_balance, validate_snapshot
from .handoff import prepare_opening_pack
from .ledger_import import marks_of
from .saved_intent import _quote, _quote_records


SCHEMA = "ths_saved_opening_review_v1"
FLAGS = {"readonly": True, "research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "prefill_allowed": False, "submitted": False, "client_interacted": False,
    "saved": False, "journal_initialized": False, "ledger_mutated": False,
    "engine_advanced": False, "account_created": False, "source_tasks_mutated": False}


def _root(value):
    path = Path(value).resolve()
    if tuple(path.parts[-len(RUN_RELATIVE.parts):]) == RUN_RELATIVE.parts:
        return path.parents[len(RUN_RELATIVE.parts) - 1]
    return path


def _pins(references):
    for ref in references.values():
        if sha256(Path(ref["path"]).read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("Saved opening review source changed during read")


def _account_missing(account, execution, current):
    if account is None:
        return ["account_observation"]
    if not isinstance(account, dict):
        raise ValueError("An explicit normalized THS account observation is required")
    missing = ["account_observation." + key for key in ("kind", "source", "account_alias",
        "observed_at", "trading_date", "cash_total", "cash_frozen", "cash_available", "cash_basis", "positions")
        if key not in account or account[key] is None]
    if missing:
        return missing
    if account["kind"] != "account" or account["trading_date"] != execution:
        raise ValueError("THS account observation must match the saved exact execution day")
    if not isinstance(account["positions"], dict):
        raise ValueError("THS holdings must be an explicit symbol mapping")
    for symbol, row in account["positions"].items():
        if not isinstance(row, dict):
            raise ValueError("Invalid THS holding row")
        missing.extend(f"account_observation.positions.{symbol}.{key}" for key in ("quantity", "sellable")
            if row.get(key) is None)
        if row.get("quantity") and symbol not in account.get("marks", {}):
            missing.append("account_observation.marks." + symbol)
    if missing:
        return missing
    validate_snapshot(account, current)
    validate_cash_balance(account)
    marks_of(account)
    return []


def _required_quotes(intent, account):
    held = {s: row["quantity"] for s, row in account["positions"].items() if row["quantity"]}
    if intent["kind"] == "NO_CHANGE":
        return sorted(s for s in intent.get("remaining_exit_symbols", []) if held.get(s, 0))
    config = StrategyConfig(**intent["execution_config"]).validate()
    marks = marks_of(account)
    equity = account["cash_total"] + sum(marks[s] * q for s, q in held.items())
    wanted = []
    for symbol in sorted(set(held) | set(intent["weights"])):
        weight, quantity = intent["weights"].get(symbol, 0), held.get(symbol, 0)
        if weight and not quantity:
            wanted.append(symbol)
        else:
            desired = (shares_for_target(equity, weight, marks[symbol],
                execution_rule(symbol, intent["execution_day"])) if weight else 0)
            if desired != quantity:
                wanted.append(symbol)
    return wanted


def _quote_missing(quotes, account, wanted, execution, current):
    if quotes is None:
        return ["broker_quotes.prices." + s for s in wanted]
    # A supplied quote is checked even when NC needs no changed-symbol price.
    if not isinstance(quotes, dict):
        raise ValueError("Broker quotes must be an explicit account-bound observation")
    missing = ["broker_quotes." + key for key in ("schema", "source", "account_alias", "observed_at",
        "trading_date", "price_basis", "prices") if key not in quotes or quotes[key] is None]
    if missing:
        return missing
    if quotes["trading_date"] != execution:
        raise ValueError("Broker quotes must match the saved exact execution day")
    records = _quote_records(quotes, account, current)
    missing = []
    for symbol in wanted:
        row = records.get(symbol)
        if row is None:
            missing.append("broker_quotes.prices." + symbol)
            continue
        if not isinstance(row, dict):
            raise ValueError("Invalid THS broker quote row")
        gaps = [f"broker_quotes.prices.{symbol}.{key}" for key in
            ("price_rmb", "preclose_rmb", "is_st", "tradable") if row.get(key) is None]
        missing.extend(gaps)
        if not gaps:
            _quote(symbol, quotes, records, account, current)
    return missing


def build_review(data_root, route_id, case_id, *, account_observation=None,
                 broker_quotes=None, current=None):
    """Project exactly one committed case; never admit it as a broker signal."""
    current = current or datetime.now(MARKET_TZ)
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("Review time requires an explicit timezone")
    root = _root(data_root)
    details = opening_daily_detail(root, route_id, now=current)
    base = {"schema": SCHEMA, "read_at": current.isoformat(), "route_id": route_id,
        "case_id": case_id, "strategy_id": None, "source_day": None, "execution_day": None,
        "source_head": None, "product_source_head": None, "saved_intent": None,
        "diagnostic": None, "next_plan": None, "missing": [], "refs": {},
        "account_alias": None, "account_observed_at": None, "quote_observed_at": None,
        "research_positions": None, "research_targets": None, "research_orders": None,
        "fills": None, "returns": None, **FLAGS}
    if details["status"] == "unavailable":
        return {**base, "status": "unavailable", "reason": details.get("reason"),
            "missing": ["saved_committed_opening_product"]}
    route = details["route"]
    if (route.get("source_changed") is not False or route.get("stale") is not False
            or route.get("source_head") != route.get("product_source_head")):
        raise ValueError("Opening source changed; refresh the registered product before review")
    selected = [row for row in details["cases"] if row["case_id"] == case_id]
    if len(selected) != 1:
        raise KeyError("Unknown or ambiguous case in the registered opening route")
    case = selected[0]
    references = {ref["path"]: ref for ref in details["read_references"]}
    references[route["source_head"]["path"]] = route["source_head"]
    coordinator = root / RUN_RELATIVE / "head.json"
    references[str(coordinator.resolve())] = {"path": str(coordinator.resolve()),
        "sha256": sha256(coordinator.read_bytes()).hexdigest()}

    def read(path, expected=None):
        expected = expected or references.get(str(Path(path).resolve()))
        return _read(path, references, expected=expected)

    strategy = case["strategy_id"]
    base.update(strategy_id=strategy, source_day=route["source_day"], execution_day=route["execution_day"],
        source_head=route["source_head"], product_source_head=route["product_source_head"],
        saved_intent=deepcopy(case.get("intent")), research_positions=deepcopy(case.get("positions")),
        research_targets=deepcopy(case.get("targets")), research_orders=deepcopy(case.get("orders")),
        original_missing=deepcopy(case.get("missing")), release_blockers=deepcopy(case.get("release_blockers")),
        price_basis="research_raw_close_rmb_not_BrokerQuote")
    today = current.astimezone(MARKET_TZ).date().isoformat()
    temporal = "expired" if today > route["execution_day"] else (
        "awaiting_execution_day" if today < route["execution_day"] else "execution_day")
    base["temporal_status"] = temporal

    def finish(status, missing=(), **fields):
        _pins(references)
        return {**base, "status": status, "missing": list(missing), "refs": deepcopy(route["refs"]),
            "read_references": list(references.values()),
            "nested_hash_basis": details["nested_hash_basis"], **fields}

    if case["status"] != "paper_preview_only":
        source = case.get("saved_source") or {}
        # Initial native low-volatility plans deliberately have no saved book
        # or opening pack. Preserve their real weights without creating either.
        if route["family"] == "native_lowvol" and source.get("intent"):
            expected = Path(route["refs"]["result"]["path"]).parent / "intent.json"
            if Path(source["intent"]).resolve() != expected.resolve():
                raise ValueError("Not-ready native intent escaped its registered result directory")
            value = read(expected)
            base["saved_intent"] = _intent(value, route, strategy, case_id)
        elif "weights" in source:
            base["saved_intent"] = {"kind": source.get("kind"), "weights": deepcopy(source["weights"])}
        return finish("source_not_ready", case.get("missing") or [case.get("reason") or "saved_case_not_ready"],
            reason=case.get("reason"), account_count=source.get("account_count"),
            cash_rmb=None, equity_rmb=None, positions=None)

    preview = read(case["refs"]["preview"]["path"], case["refs"]["preview"])
    if route["family"] == "fixed":
        intent = read(route["refs"]["intent"]["path"], route["refs"]["intent"])
    else:
        expected = Path(case["refs"]["preview"]["path"]).parent / "intent.json"
        if Path(preview["saved_intent"]).resolve() != expected.resolve():
            raise ValueError("Saved intent escaped its registered case directory")
        intent = read(expected)
    if _intent(intent, route, strategy, case_id) != case["intent"]:
        raise ValueError("Saved review intent changed after case selection")
    handoff_path = Path(preview["artifacts"]["ths_manual_handoff"]).resolve()
    allowed_parent = (Path(preview["artifacts"]["provisional_target_holdings"]).resolve().parent
        if route["family"] == "fixed" else Path(case["refs"]["preview"]["path"]).parent)
    if handoff_path != allowed_parent / "ths_manual_handoff.json":
        raise ValueError("THS handoff escaped its registered case artifact directory")
    handoff = read(handoff_path)
    if (handoff.get("task_method") != "saved_fixed_account_next_session_v1"
            or handoff.get("strategy_id") != strategy or handoff.get("source_cutoff") != route["source_day"]
            or handoff.get("execution_day") != route["execution_day"]
            or handoff.get("saved_account_intent", intent) != intent):
        raise ValueError("Saved handoff differs from the exact registered intent/date")
    pack = read(route["refs"]["opening_pack"]["path"], route["refs"]["opening_pack"])
    if temporal != "execution_day":
        return finish(temporal, ["exact_execution_day_account_and_broker_quotes"],
            reason="saved_execution_day_has_passed" if temporal == "expired" else "saved_execution_day_not_reached")
    missing = _account_missing(account_observation, route["execution_day"], current)
    if missing:
        return finish("awaiting_account_evidence", missing)
    base.update(account_alias=account_observation["account_alias"],
        account_observed_at=account_observation["observed_at"])
    wanted = _required_quotes(intent, account_observation)
    base["required_broker_quote_symbols"] = wanted
    missing = _quote_missing(broker_quotes, account_observation, wanted, route["execution_day"], current)
    if missing:
        return finish("awaiting_broker_quotes", missing)
    base["quote_observed_at"] = broker_quotes.get("observed_at") if broker_quotes is not None else None
    diagnostic = prepare_opening_pack(pack, account_observation, broker_quotes, strategy_id=strategy,
        current=current, sizing_mode="saved_intent", saved_intent=intent)
    row = diagnostic["candidates"][0]
    if len(diagnostic["candidates"]) != 1 or row["strategy_id"] != strategy:
        raise ValueError("The review crossed independent research cases")
    if row["status"] == "research_diagnostic_only":
        result = row["plan"]
        if result.get("next_plan") is not None or result.get("prefill_allowed") is not False:
            raise ValueError("A research review cannot acquire a prefill plan")
        return finish("research_diagnostic_only", diagnostic=diagnostic)
    if row["status"] != "not_ready":
        raise ValueError("Saved research review was promoted to a current broker plan")
    return finish("research_diagnostic_not_ready", [row.get("error") or "saved_diagnostic_not_ready"],
        diagnostic=diagnostic)
