"""Bridge saved dragon and frozen-model accounts into the existing opening pack.

Every case stays independent. This reads saved decisions and raw closes; it
neither chooses candidates nor advances an account or manufactures next-day bars.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re

import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..contracts import MARKET_TZ
from . import opening_pack
from .next_session_preview import _fixed_daily_rows, _symbol

SCHEMA = "leader_eight_type_saved_account_opening_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "prefill_allowed": False,
    "engine_advanced": False, "ledger_mutated": False,
    "account_created": False, "broker_interacted": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _cents(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Finite positive saved raw close required")
    return int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _quote_rows(rows, day, wanted):
    result = {}
    for row in rows:
        if str(row["date"]) != day:
            continue
        symbol = _symbol(row.get("symbol") or row["code"])
        if symbol not in wanted:
            continue
        if symbol in result:
            raise ValueError("Duplicate exact-day saved raw quote")
        close = row["close"]
        if close in (None, "", 0, "0"):
            continue
        price = _cents(close)
        result[symbol] = {"trading_date": day, "close_cents": price,
            "close_rmb": f"{Decimal(price)/100:.2f}",
            "tradestatus": int(row["tradestatus"]) if row["tradestatus"] in (0, 1, "0", "1") else None,
            "is_st": int(row["isST"]) if row["isST"] in (0, 1, "0", "1") else None}
    return result


def _quotes(publication, day, wanted):
    """Read the exact saved D rows once, preserving missing/status unknowns."""
    manifest_path = Path(publication["input_manifest"])
    if sha256(manifest_path.read_bytes()).hexdigest() != publication["input_manifest_sha256"]:
        raise ValueError("Saved input manifest changed")
    manifest = _read(manifest_path)
    raw = Path(manifest["raw_file"])
    if sha256(raw.read_bytes()).hexdigest() != manifest["raw_sha256"]:
        raise ValueError("Saved raw price input changed")
    rows = pq.read_table(raw, columns=["date", "code", "close", "tradestatus", "isST"],
        filters=[("date", "==", day)]).to_pylist()
    return _quote_rows(rows, day, wanted)


def _source_quotes(publication, day, wanted, source_family):
    if source_family in {"eight_type", "dated_catchup", "eight_type_residual", "known_identity"}:
        return _quotes(publication, day, wanted)
    if source_family == "leader20":
        from .leader_published_inputs import SCHEMA as INPUT_SCHEMA
        proof = publication["publication_input_bundle"]
        path = Path(proof["file"]).resolve()
        if sha256(path.read_bytes()).hexdigest() != proof["sha256"]:
            raise ValueError("Published own input bundle changed")
        bundle = _read(path)
        raw = Path(bundle["raw_bars_file"]).resolve()
        raw.relative_to(path.parent)
        if (bundle.get("schema") != INPUT_SCHEMA or day not in bundle["source_days_complete"]
                or bundle["recorded_at"] != proof["recorded_at"] or bundle["origin"] != proof["origin"]
                or sha256(raw.read_bytes()).hexdigest() != bundle["raw_bars_sha256"]):
            raise ValueError("Published own dated raw input changed")
        columns = ["date", "code", "symbol", "close", "tradestatus", "isST"]
        encoded = bundle["json_encoded_object_columns"]
        if (not isinstance(encoded, list) or len(encoded) != len(set(encoded))
                or not set(encoded) <= set(pq.read_schema(raw).names)):
            raise ValueError("Saved raw mixed-column encoding metadata changed")
        rows = pq.read_table(raw, columns=columns, filters=[("date", "==", day)]).to_pylist()
        for row in rows:
            for column in set(encoded).intersection(columns):
                row[column] = json.loads(row[column])
        return _quote_rows(rows, day, wanted)
    if source_family != "alpha158":
        raise ValueError("Unsupported saved account source family")
    publication_bytes = Path(publication["file"]).read_bytes()
    if sha256(publication_bytes).hexdigest() != publication["sha256"]:
        raise ValueError("Published Alpha158 target bytes changed")
    source = json.loads(publication_bytes.decode("utf-8-sig"))["source"]
    provider_path = Path(source["provider_view"])/"result.json"
    if sha256(provider_path.read_bytes()).hexdigest() != source["provider_result_sha256"]:
        raise ValueError("Published Alpha158 provider reference changed")
    provider = _read(provider_path)
    raw = Path(provider["recent_raw_observations_file"])
    raw.resolve().relative_to(provider_path.parent.resolve())
    if (sha256(raw.read_bytes()).hexdigest() != provider["recent_raw_observations_sha256"]
            or provider["recent_raw_observations_sha256"] != source["raw_observations_sha256"]):
        raise ValueError("Published Alpha158 raw price reference changed")
    rows = pq.read_table(raw, columns=["date", "code", "close", "tradestatus", "isST"],
        filters=[("date", "==", day)]).to_pylist()
    return _quote_rows(rows, day, wanted)


def _case_preview(directory, intent, case, quotes, *, current, reconciliation_source=None):
    state = case["state"]
    config = StrategyConfig(**intent["execution_config"]).validate()
    day, execution = intent["source_day"], intent["execution_day"]
    positions = {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items()
                 if sum(lot["qty"] for lot in lots)}
    equity = int((Decimal(str(case["daily"][-1]["equity"])) * 100).quantize(Decimal(1)))
    sizing = {**state, "pending_targets": intent["weights"]}
    targets, orders, missing, trade_status = _fixed_daily_rows(
        sizing, None, positions, quotes, equity, config, day, execution, [],
        explicit_exit_symbols=intent["remaining_exit_symbols"])
    if any(row["target_qty"] is None for row in targets):
        return {"case_id": intent["case_id"], "status": "not_ready",
                "reason": "saved_full_target_has_unknown_exact_source_close", "missing": missing, **FLAGS}
    for row in targets:
        if row["symbol"] in intent["remaining_exit_symbols"]:
            row["target_action"] = "saved_queued_risk_exit"
    for row in orders:
        if row["symbol"] in intent["remaining_exit_symbols"]:
            row["reason"] = "saved_queued_risk_exit"
    # Sell-first is the same staging used by Engine and the manual adapter.
    orders.sort(key=lambda row: (0 if row["side"] == "sell" else 1, row["symbol"]))
    directory.mkdir(parents=True, exist_ok=False)
    from .opening_pack import _write_csv
    target_path, order_path = directory/"target_holdings.csv", directory/"order_plan.csv"
    _write_csv(target_path, targets, list(targets[0]) if targets else
        ["symbol", "current_qty", "target_qty", "delta_qty", "price_basis"])
    _write_csv(order_path, orders, list(orders[0]) if orders else ["symbol", "side", "qty", "price_rmb", "status"])
    # Explicit read-only copy for the existing copied-Ledger preflight. This is
    # not registration, new_state, a durable account writer or an execution.
    _write(directory/"account.json", state)
    _write(directory/"intent.json", intent)
    blockers = ["saved_research_account_not_user_account", "broker_quote_missing",
                "sellable_quantity_and_t1_status_missing"]
    handoff = {"schema": "ths_manual_prefill_handoff_v1",
        "task_method": "saved_fixed_account_next_session_v1", "source_task_family": intent.get("source_family", "eight_type"),
        "strategy_id": intent["strategy_id"], "source_cutoff": day, "execution_day": execution,
        "submission": "manual_only", "status": "research_preview_not_ready_for_prefill",
        "saved_account_intent": intent, "rank_template": [],
        "target_template": [{"symbol": row["symbol"], "target_qty": row["target_qty"],
            "target_weight": row["target_weight"], "price_basis": "research_raw_close_rmb"} for row in targets],
        "supported_sizing_modes": ["saved_intent"], "trade_status": trade_status,
        "release_control": {"state": "blocked_research_saved_account_only",
            "execution_release_allowed": False, "blockers": blockers}, **FLAGS}
    _write(directory/"ths_manual_handoff.json", handoff)
    count = sum(bool(row.get("qty")) for row in orders)
    preview = {"status": "paper_preview_only", "task_method": handoff["task_method"],
        "source_task_family": handoff["source_task_family"], "strategy_id": intent["strategy_id"],
        "source_run": str(Path(intent["run_directory"])/"head.json"),
        "source_artifacts": str(directory), "source_cutoff": day, "execution_day": execution,
        "generated_at": current.isoformat(), "execution_config": config.to_dict(),
        "trade_status": trade_status, "execution_scope": "strict", "rank_source": "saved_account_intent_no_reranking",
        "rank_is_current_for_execution_day": None, "rank_carry_forward_allowed": False,
        "release_state": "blocked_research_saved_account_only", "release_blockers": blockers,
        "cash_cents": state["cash"], "cash_rmb": state["cash"]/100,
        "terminal_equity_cents": equity, "terminal_equity_rmb": equity/100,
        "position_count": len(positions), "provisional_order_count": count,
        "provisional_missing_quote_symbols": missing, "orders_created": 0,
        "adapter_handoff_status": handoff["status"], "source_refs": deepcopy(intent["refs"]),
        "source_account_is_readonly_copy": True, "saved_intent": str(directory/"intent.json"),
        "artifacts": {"target_holdings": str(target_path), "order_plan": str(order_path),
            "provisional_target_holdings": str(target_path), "provisional_order_plan": str(order_path),
            "ths_manual_handoff": str(directory/"ths_manual_handoff.json")}, **FLAGS}
    # Only new products receive this identity seal. Existing plans and research
    # accounts remain byte-for-byte unchanged; no Engine or sizing is rerun.
    from .opening_plan_execution_reconciliation import make_binding
    try:
        preview["execution_binding"] = make_binding(preview, source=reconciliation_source)
    except (ValueError, KeyError) as error:
        preview["execution_binding_unavailable_reason"] = str(error)
    _write(directory/"result.json", preview)
    return {"case_id": intent["case_id"], "status": "paper_preview_only", "preview": str(directory/"result.json"),
        "trade_status": trade_status, "candidate_order_count": count,
        "queued_exit_symbols": intent["remaining_exit_symbols"], **FLAGS}


def _save_holding_reviews(pack_path, pack, reviews, ready, source_family):
    """Seal the already verified reviews inside this newly created product."""
    if not reviews:
        return
    keys = [row["case_id"] for row in ready]
    if any(key not in reviews for key in keys):
        raise ValueError("A saved leader opening review is missing its exact case")
    selected = [reviews[key] for key in keys]
    for row in selected:
        if (row["schema"] != "saved_leader_holding_review_v1" or row["source_family"] != source_family
                or row["source_day"] != pack["source_cutoff"] or row["execution_day"] != pack["execution_day"]
                or row["saved_position_review"]["case_id"] != row["strategy_id"]):
            raise ValueError("Saved opening holding review crossed dates, family or case")
    data_directory = Path(pack["artifacts"]["candidate_orders"]).resolve().parent
    sidecar = data_directory / "holding_review.json"
    if sidecar.exists():
        raise FileExistsError("Holding reviews can only be sealed into a new opening pack")
    record = {"schema": "saved_leader_opening_holding_review_v1", "source_family": source_family,
        "source_day": pack["source_cutoff"], "execution_day": pack["execution_day"],
        "case_reviews": selected, "account_basis": "saved_research_positions_not_personal_account",
        "current_risk_recomputed": False, **FLAGS}
    _write(sidecar, record)
    reference = {"path": str(sidecar), "sha256": sha256(sidecar.read_bytes()).hexdigest()}
    pack["holding_review_ref"] = reference
    pack["artifacts"]["holding_review"] = str(sidecar)
    # Preserve opening_pack's existing output/data mirror without changing its
    # shared builder or any original saved intent/target/order bytes.
    _write(data_directory / "opening_pack.json", pack)
    _write(Path(pack_path) / "opening_pack.json", pack)


def build(run_dir, execution_day, out, *, current=None, source_family="eight_type"):
    if source_family in {"eight_type", "dated_catchup", "eight_type_residual", "known_identity"}:
        from .saved_leader_account_intent import export_requests
    elif source_family in {"leader20", "alpha158"}:
        from .saved_published_account_intent import export_requests
    else:
        raise ValueError("Unsupported saved account source family")
    current = current or datetime.now(MARKET_TZ)
    directory = Path(out).resolve()
    if directory.exists():
        raise FileExistsError("Use a new eight-type opening output directory")
    reviews = {}
    intents = (export_requests(run_dir, execution_day, current=current, source_family=source_family,
                              holding_reviews=reviews)
        if source_family in {"eight_type", "dated_catchup", "eight_type_residual", "known_identity"} else
        export_requests(run_dir, execution_day, current=current))
    head_path = Path(run_dir).resolve()/"head.json"
    head_bytes = head_path.read_bytes() if head_path.exists() else None
    head = json.loads(head_bytes.decode("utf-8-sig")) if head_bytes is not None else {}
    head_hash = sha256(head_bytes).hexdigest() if head_bytes is not None else None
    ready = [intent for intent in intents.values() if intent.get("status") != "not_ready"]
    if any(intent.get("source_family", "eight_type") != source_family for intent in ready):
        raise ValueError("Requested source family differs from the saved account schema")
    if any(intent["refs"]["head"]["sha256"] != head_hash for intent in ready):
        raise ValueError("Eight-type account changed after intent validation")
    days = {intent["source_day"] for intent in ready}
    if len(days) > 1:
        raise ValueError("Cases on different saved days cannot share an opening pack")
    wanted = set()
    for intent in ready:
        state = head["cases"][intent["case_id"]]["state"]
        wanted.update(s for s, lots in state["positions"].items() if sum(l["qty"] for l in lots))
        wanted.update(intent["weights"] or {})
    quotes = _source_quotes(next(p for p in head["publications"] if p["day"] in days),
        next(iter(days)), wanted, source_family) if ready else {}
    directory.mkdir(parents=True, exist_ok=False)
    rows = []
    for case_id, intent in intents.items():
        # Original twenty-case IDs contain colons. Keep their strategy identity
        # intact while using an ordinary Windows filename for the output copy.
        child = case_id if re.fullmatch(r"[A-Za-z0-9_-]+", case_id) else "case_" + sha256(case_id.encode()).hexdigest()[:24]
        rows.append(intent if intent.get("status") == "not_ready" else
            _case_preview(directory/child, intent, head["cases"][case_id], quotes, current=current))
    final_hash = sha256(head_path.read_bytes()).hexdigest() if head_path.exists() else None
    if final_hash != head_hash:
        raise ValueError("Eight-type source changed before opening pack creation")
    previews = [row["preview"] for row in rows if row["status"] == "paper_preview_only"]
    pack_path, pack = opening_pack.build(previews, execution_day=execution_day) if previews else (None, None)
    if pack is not None and source_family in {"eight_type", "dated_catchup", "eight_type_residual", "known_identity"}:
        _save_holding_reviews(pack_path, pack, reviews,
            [row for row in rows if row["status"] == "paper_preview_only"], source_family)
    result = {"schema": SCHEMA if source_family == "eight_type" else "published_strategy_saved_account_opening_v1",
        "source_family": source_family, "status": "paper_candidates_only" if previews else "not_ready",
        "generated_at": current.isoformat(), "source_run_directory": str(Path(run_dir).resolve()),
        "source_head_sha256": head_hash, "source_cutoff": next(iter(days)) if days else None,
        "execution_day": execution_day, "case_count": len(rows), "preview_count": len(previews),
        "not_ready_count": len(rows)-len(previews), "cases": rows,
        "opening_pack": str(pack_path/"opening_pack.json") if pack_path else None,
        "candidate_order_count": pack["candidate_order_count"] if pack else None,
        "independent_cases_combined_into_account": False, "automatic_strategy_selection": False,
        "candidate_orders_are_fills": False, **FLAGS}
    _write(directory/"result.json", result)
    return directory, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-family", choices=["eight_type", "dated_catchup", "eight_type_residual", "known_identity", "leader20", "alpha158"], default="eight_type")
    args = parser.parse_args(argv)
    directory, result = build(args.run_dir, args.execution_day, args.out, source_family=args.source_family)
    print(json.dumps({"result": str(directory/"result.json"), "opening_pack": result["opening_pack"],
                      "preview_count": result["preview_count"]}, ensure_ascii=False))
    return 0
