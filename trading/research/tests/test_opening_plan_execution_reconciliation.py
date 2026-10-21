"""Saved-only contract fixtures; no Engine, source, or account advancement."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, TradingMode
from trading.ledger import new_state
from trading.research import opening_daily as coordinator
from trading.research import opening_plan_execution_reconciliation as m
from trading.research.leader_eight_type_opening import _case_preview


D, E, F, A = "2026-10-16", "2026-10-19", "2026-10-20", "600001.SSE"
NOW = datetime.fromisoformat(E + "T21:00:00+08:00")
KEY = "space_height_leader__unguarded__uncapped"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return m._file(path)


def receipt(context, d, e):
    return {"schema": "synthetic_saved_receipt", "context": context,
        "source_day": d, "execution_not_before": e, "weights": {A: .16}}


def make_preview(root, name, run, case, config, publication, journal, d, e):
    intent = {"case_id": KEY, "strategy_id": KEY, "source_day": d, "execution_day": e,
        "run_directory": str(run), "context": case["context"], "target_version": len(case["daily"]),
        "target_version_basis": "processed_session_count", "kind": "FULL_TARGET", "weights": {A: .16},
        "remaining_exit_symbols": [], "execution_config": config.to_dict(),
        "refs": {"publication": publication, "latest_journal": journal}}
    path = root / name / KEY
    _case_preview(path, intent, case, {A: {"trading_date": d, "close_cents": 1000}},
        current=datetime.fromisoformat(d + "T20:00:00+08:00"))
    return path / "result.json"


@pytest.fixture
def saved(tmp_path):
    config = StrategyConfig(initial_cash=100000, holdings=5, gross_limit=.8, single_limit=.2)
    context = RunContext("saved-only-exact-research-book", TradingMode.REPLAY).to_dict()
    run = tmp_path / "run"
    pub = write(tmp_path / "publication_D.json", {"source_close_day": D,
        "proposed_execution_day": E, "recorded_at": D + "T18:00:00+08:00"})
    journal = {"decision_day": D, "execution_day": E, "publication": pub}
    journal_ref = write(run / "journal_D.json", journal)
    state = new_state(100000)
    state.update(day=D, pending_targets={A: .16},
        pending_published_close_intent=receipt(context, D, E))
    case = {"context": context, "state": state, "daily": [{"date": D, "cash": 100000, "equity": 100000}],
        "journal": [journal], "journal_files": [journal_ref]}
    head = {"binding": {"config": config.to_dict()}, "cases": {KEY: case}}
    source = write(run / "head.json", head)
    preview = make_preview(tmp_path, "opening_D", run, case, config, pub, journal_ref, D, E)
    return {"root": tmp_path, "run": run, "head": head, "case": case, "config": config,
        "context": context, "preview": preview, "source_D": source}


def append_execution(saved):
    case = saved["case"]
    old_receipt = deepcopy(case["state"]["pending_published_close_intent"])
    pub = write(saved["root"] / "publication_E.json", {"source_close_day": E,
        "proposed_execution_day": F, "recorded_at": E + "T18:00:00+08:00"})
    diagnostic = {"kind": "target_execution_diagnostic", "day": E, "symbol": A,
        "engine_target_quantity": 1400, "engine_mark": 1100, "price_unit": "cent",
        "requested_quantity": 1400, "filled_quantity": 1000,
        "mechanical_reason": "saved_liquidity_constraint", "constraint_flags": ["participation_limit"],
        "cash_available_before_cents": 10000000, "cash_available_after_cents": 8899486}
    events = [diagnostic, {"kind": "order_blocked", "day": E, "symbol": A,
        "reason": "saved_participation_limit"}]
    row = {"date": E, "cash": 88994.86, "equity": 99994.86,
        "consumed_published_close_intent": old_receipt, "events": events}
    order = {"day": E, "symbol": A, "side": "buy", "qty": 1400, "filled": 1000,
        "status": "cancelled", "fees": 514}
    case["state"].update(day=E, cash=8899486,
        positions={A: [{"qty": 1000, "available": F}]}, prices={A: 1100},
        orders={"parent": order}, fills={
            "callback1": {"day": E, "order_id": "parent", "symbol": A, "side": "buy", "qty": 500, "fees": 503},
            "callback2": {"day": E, "order_id": "parent", "symbol": A, "side": "buy", "qty": 500, "fees": 11}},
        pending_published_close_intent=receipt(saved["context"], E, F), events=events)
    case["daily"].append(row)
    journal = {"decision_day": E, "execution_day": F, "publication": pub,
        "row_sha256": m.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
    journal_ref = write(saved["run"] / "journal_E.json", journal)
    case["journal"].append(journal)
    case["journal_files"].append(journal_ref)
    write(saved["run"] / "head.json", saved["head"])
    return pub, journal_ref


def test_new_product_seals_existing_identity_and_pending_does_not_change_source(saved):
    before = {p: p.read_bytes() for p in saved["root"].rglob("*") if p.is_file()}
    preview = json.loads(saved["preview"].read_text())
    assert preview["execution_binding"]["target_version"] == 1
    result = m.reconcile(saved["preview"], now=NOW)
    assert result["status"] == "pending" and result["last_saved_session"] == D
    assert result["plan_payload_sealed"] is True and result["engine_advanced"] is False
    assert all(p.read_bytes() == value for p, value in before.items())


def test_exact_execution_preserves_callbacks_partial_quantities_reasons_and_cash_boundary(saved):
    append_execution(saved)
    result = m.reconcile(saved["preview"], now=NOW)
    assert result["status"] == "reconciled_saved_execution", result
    row = result["rows"][0]
    assert (row["planned_target_quantity"], row["actual_engine_target_quantity"],
        row["target_quantity_difference"]) == (1600, 1400, -200)
    assert (row["parent_order_count"], row["fill_callback_count"], row["fee_cents"]) == (1, 2, 514)
    assert sum(f["qty"] for f in row["fills"]) == 1000
    assert row["diagnostics"][0]["cash_available_before_cents"] == 10000000
    assert row["reason_evidence"][0]["constraint_flags"] == ["participation_limit"]
    assert row["order_blocks"][0]["reason"] == "saved_participation_limit"
    assert row["reference_price_differs"] is True and row["price_difference_is_causal_attribution"] is False
    assert result["cash"]["D_to_E_change_is_trade_only"] is False
    assert result["fill_callbacks_are_independent_trades"] is False


@pytest.mark.parametrize("conflict", ["context", "target_version", "consumed_receipt"])
def test_exact_binding_conflicts_never_match_by_symbol_or_date(saved, conflict):
    append_execution(saved)
    if conflict == "context":
        saved["case"]["context"] = RunContext("other-book", TradingMode.REPLAY).to_dict()
    elif conflict == "target_version":
        preview = json.loads(saved["preview"].read_text())
        preview["execution_binding"]["target_version"] = 2
        write(saved["preview"], preview)
    else:
        saved["case"]["daily"][-1]["consumed_published_close_intent"]["weights"] = {A: .2}
    write(saved["run"] / "head.json", saved["head"])
    result = m.reconcile(saved["preview"], now=NOW)
    assert result["status"] == "unknown" and result["rows"] is None


def test_legacy_plan_can_be_pending_but_cannot_be_sealed_retrospectively(saved):
    preview = json.loads(saved["preview"].read_text())
    del preview["execution_binding"]
    write(saved["preview"], preview)
    assert m.reconcile(saved["preview"], now=NOW)["status"] == "pending"
    append_execution(saved)
    result = m.reconcile(saved["preview"], now=NOW)
    assert result["status"] == "unknown" and "not sealed before execution" in result["reason"]


def test_existing_attempt_history_keeps_old_executed_plan_separate_from_latest_pending(saved):
    pub_e, journal_e = append_execution(saved)
    source_e = m._file(saved["run"] / "head.json")
    latest_path = make_preview(saved["root"], "opening_E", saved["run"], saved["case"],
        saved["config"], pub_e, journal_e, E, F)
    directory = saved["root"] / "coordinator"
    reg_ref = write(directory / "registration.json", {"schema": coordinator.SCHEMA,
        "method_id": coordinator.METHOD, "run_directory": str(directory.resolve()),
        "routes": [{"id": "eight_type", "family": "eight_type", "source": str(saved["run"])}],
        **coordinator.FLAGS})
    committed = None
    for d, e, path, source in [(D, E, saved["preview"], saved["source_D"]), (E, F, latest_path, source_e)]:
        catalog_ref = write(path.parent.parent / "result.json", {"source_head_sha256": source["sha256"],
            "cases": [{"case_id": KEY, "status": "paper_preview_only", "preview": str(path)}]})
        product = {"source_head": source, "source_day": d, "execution_day": e,
            "refs": {"result": catalog_ref}, "status": "paper_candidates_only", **coordinator.FLAGS}
        identity = coordinator._input_id(reg_ref, "eight_type", source)
        attempt = {"schema": coordinator.SCHEMA, "method_id": coordinator.METHOD,
            "registration": reg_ref, "route_id": "eight_type", "source_head": source,
            "input_id": identity, "product_validated": True, "head_committed": True,
            "status": "committed", "product": product, **coordinator.FLAGS}
        write(directory / "attempts" / "eight_type" / (identity + ".json"), attempt)
        committed = {"input_id": identity, "source_head": source, "product": product}
    write(directory / "head.json", {"schema": coordinator.SCHEMA, "method_id": coordinator.METHOD,
        "registration": reg_ref, "routes": {"eight_type": committed}, **coordinator.FLAGS})
    latest = m.reconcile(latest_path, now=NOW)
    result = m.reconcile_saved_history(directory, "eight_type", KEY,
        latest_preview=latest_path, latest=latest, now=NOW)
    assert result["status"] == "saved_execution_plan_selected", result
    assert result["latest_plan_reconciliation"]["status"] == "pending"
    old = result["last_executed_plan_reconciliation"]
    assert old["status"] == "reconciled_saved_execution"
    assert old["plan_identity"]["source_day"] == D and old["execution_day"] == E
    assert old["preview"] != latest["preview"] and result["saved_plan_count"] == 2
