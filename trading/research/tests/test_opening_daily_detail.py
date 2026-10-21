"""Synthetic pinned parent files and unsealed legacy CSVs; no producer calls."""
import csv
from datetime import datetime
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from trading import api
from trading.research import opening_daily as core
from trading.research import opening_daily_detail as detail
from trading.research.opening_daily_view import RUN_RELATIVE


NOW = datetime.fromisoformat("2026-10-17T16:00:00+08:00")
D, E, SYMBOL = "2026-10-16", "2026-10-19", "600001.SSE"


def write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return core._ref(path)


def csv_write(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def saved(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(core, "DATA_ROOT", data)
    monkeypatch.setattr(api.settings, "DATA_ROOT", data)
    source = data / "source"
    source_ref = write(source / "head.json", {"synthetic_only": True})
    config = {"synthetic_saved_config": True}
    signal = "synthetic_fixed"
    output = tmp_path / "repo/outputs/paper_rebalance/preview_id"
    preview_data = data / "research/paper_rebalance/preview_id"
    targets = preview_data / "provisional_target_holdings.csv"
    csv_write(targets, [{"symbol": SYMBOL, "current_qty": 300, "target_qty": 0, "delta_qty": -300,
        "target_weight": "", "close_cents": 1000, "price_basis": "research_raw_close_rmb"}],
        ["symbol", "current_qty", "target_qty", "delta_qty", "target_weight", "close_cents", "price_basis"])
    intent = {"schema": "saved_fixed_account_intent_v1", "kind": "NO_CHANGE", "weights": None, "remaining_exit_symbols": [SYMBOL],
        "source_day": D, "execution_day": E, "strategy_id": signal, "source_known_at": D + "T19:00:00+08:00",
        "execution_config": config, "refs": {"source_run": {"path": str(tmp_path / "saved_run.json"), "sha256": "a" * 64}}, **core.FLAGS}
    intent_ref = write(data / "existing_intent.json", intent)
    preview = {"status": "paper_preview_only", "strategy_id": signal, "source_cutoff": D, "execution_day": E,
        "source_run": intent["refs"]["source_run"]["path"], "execution_config": config,
        "trade_status": "PAPER_EXIT_PENDING", "cash_rmb": 10000, "terminal_equity_rmb": 13000,
        "release_blockers": ["synthetic_broker_quote_missing"], "provisional_missing_quote_symbols": [],
        "artifacts": {"provisional_target_holdings": str(targets)}, **core.FLAGS}
    preview_ref = write(output / "result.json", preview)
    pack_dir = tmp_path / "repo/outputs/opening_pack/pack_id"
    pack_data = data / "research/opening_pack/pack_id"
    orders = pack_data / "candidate_orders.csv"
    checks = pack_data / "ledger_preflight_by_strategy.csv"
    order = {"strategy_id": signal, "symbol": SYMBOL, "side": "sell", "qty": 300, "price_rmb": "10.00",
             "estimated_notional_rmb": "3000.00", "source_status": "paper_preview_not_created",
             "source_reason": "saved_queued_risk_exit"}
    csv_write(orders, [order], list(order))
    check = {"strategy_id": signal, "symbol": SYMBOL, "side": "sell", "qty": 300,
             "preflight_status": "rejected", "preflight_reason": "insufficient_sellable_T1", "reserved_cents": 0}
    csv_write(checks, [check], list(check))
    pack = {"schema": "a_share_opening_pack_v1", "status": "paper_candidates_only", "execution_admitted": False,
        "automatic_strategy_selection": False, "source_cutoff": D, "execution_day": E,
        "strategies": [{"strategy_id": signal, "preview": str(output / "result.json")}],
        "artifacts": {"candidate_orders": str(orders), "ledger_preflight_by_strategy": str(checks)},
        "ledger_preflight_by_strategy": {signal: {"status": "completed", "submitted": 0, "rejected": 1,
                                                  "fills_created": 0, "saved_ledger_mutated": False}}}
    pack_ref = write(pack_dir / "opening_pack.json", pack)
    refs = {"preview": preview_ref, "intent": intent_ref, "opening_pack": pack_ref}
    route = {"id": "fixed", "family": "fixed", "signal": signal, "source": str(source),
             "adopt": {key: value["path"] for key, value in refs.items()}}
    coordinator = data / RUN_RELATIVE
    registration_ref = write(coordinator / "registration.json", {"schema": core.SCHEMA, "method_id": core.METHOD,
        "run_directory": str(coordinator.resolve()), "routes": [route], "adopt_refs": {"fixed": refs}, **core.FLAGS})
    product = {"source_head": source_ref, "source_day": D, "execution_day": E, "status": "paper_candidates_only",
               "case_count": 1, "ready_case_count": 1, "not_ready_case_count": 0, "candidate_order_count": 1,
               "refs": refs, **core.FLAGS}
    head = {"schema": core.SCHEMA, "method_id": core.METHOD, "registration": registration_ref,
        "routes": {"fixed": {"input_id": "c" * 64, "source_head": source_ref, "product": product,
            "attempt": str(coordinator / "unopened_attempt.json"), "committed_at": "2026-10-17T15:30:00+08:00"}}, **core.FLAGS}
    write(coordinator / "head.json", head)
    return {"data": data, "directory": coordinator, "source": source, "head": head,
            "pack": pack, "pack_path": pack_dir / "opening_pack.json", "targets": targets, "orders": orders}


def test_saved_nc_exit_and_original_rejection_are_read_without_new_checks(saved, monkeypatch):
    for name in ("run", "register", "recover", "_factory", "_lock"):
        monkeypatch.setattr(core, name, lambda *a, **kw: pytest.fail("Detail attempted a producer or writer"))
    before = {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}
    result = detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
    case = result["cases"][0]
    assert case["intent"]["kind"] == "NO_CHANGE" and case["intent"]["weights"] is None
    assert case["targets"][0]["target_qty"] == 0 and case["positions"][0]["quantity"] == 300
    assert case["positions"][0]["sellable_on_source_day"] is None
    assert case["orders"][0]["preflight"]["preflight_reason"] == "insufficient_sellable_T1"
    assert case["orders"][0]["price_basis"] == "research_raw_close_rmb"
    assert case["fills"] is None and case["returns"] is None
    assert result["nested_payload_hashes_previously_pinned"] is False
    assert case["holding_review_status"] == "not_saved" and case["holding_review"] is None
    assert before == {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}


def test_stale_head_does_not_replace_saved_case_dates_or_unknown_quantity(saved):
    write(saved["source"] / "head.json", {"synthetic_generation": 2})
    csv_write(saved["targets"], [{"symbol": SYMBOL, "current_qty": 300, "target_qty": "", "delta_qty": "",
        "target_weight": "", "close_cents": "", "price_basis": "research_raw_close_rmb"}],
        ["symbol", "current_qty", "target_qty", "delta_qty", "target_weight", "close_cents", "price_basis"])
    result = detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
    assert result["route"]["stale"] is True and result["route"]["source_day"] == D
    assert result["cases"][0]["targets"][0]["target_qty"] is None
    assert result["cases"][0]["targets"][0]["delta_qty"] is None


def test_changed_registered_pack_is_409_and_unknown_route_404(saved):
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_detail("../../arbitrary")
    assert caught.value.status_code == 404
    saved["pack_path"].write_bytes(saved["pack_path"].read_bytes() + b" ")
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_detail("fixed")
    assert caught.value.status_code == 409


def test_nested_artifact_cannot_escape_registered_case_tree(saved):
    saved["pack"]["artifacts"]["candidate_orders"] = str(saved["data"] / "unrelated.csv")
    new_ref = write(saved["pack_path"], saved["pack"])
    saved["head"]["routes"]["fixed"]["product"]["refs"]["opening_pack"] = new_ref
    write(saved["directory"] / "head.json", saved["head"])
    with pytest.raises(ValueError, match="escaped"):
        detail.opening_daily_detail(saved["data"], "fixed", now=NOW)


def test_missing_coordinator_is_unavailable_without_creation(tmp_path):
    result = detail.opening_daily_detail(tmp_path, "fixed", now=NOW)
    assert result["status"] == "unavailable" and result["cases"] == []
    assert not (tmp_path / RUN_RELATIVE).exists()


def test_invalid_saved_weight_is_api409_instead_of_decimal_exception(saved):
    csv_write(saved["targets"], [{"symbol": SYMBOL, "current_qty": 300, "target_qty": 0,
        "delta_qty": -300, "target_weight": "bad", "close_cents": 1000,
        "price_basis": "research_raw_close_rmb"}],
        ["symbol", "current_qty", "target_qty", "delta_qty", "target_weight", "close_cents", "price_basis"])
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_detail("fixed")
    assert caught.value.status_code == 409
    assert "numeric or unknown" in caught.value.detail


def test_notready_case_keeps_unknown_arrays_and_original_missing(saved):
    directory = saved["directory"]
    registration = json.loads((directory / "registration.json").read_text(encoding="utf-8"))
    registration["routes"][0]["family"] = "eight_type"
    result = {"source_cutoff": D, "execution_day": E, "source_run_directory": str(saved["source"]),
        "source_head_sha256": saved["head"]["routes"]["fixed"]["source_head"]["sha256"],
        "cases": [{"case_id": "synthetic_unknown", "status": "not_ready", "reason": "saved_feature_unknown",
                   "missing": ["synthetic_feature"]}], **core.FLAGS}
    result_ref = write(saved["data"] / "existing_notready_result.json", result)
    saved["head"]["registration"] = write(directory / "registration.json", registration)
    product = saved["head"]["routes"]["fixed"]["product"]
    product.update(refs={"result": result_ref}, ready_case_count=0, not_ready_case_count=1, candidate_order_count=None)
    write(directory / "head.json", saved["head"])
    result = detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
    case = result["cases"][0]
    assert case["status"] == "not_ready" and case["missing"] == ["synthetic_feature"]
    assert case["positions"] is None and case["targets"] is None and case["orders"] is None


def leader_product(saved):
    """Convert the saved legacy fixture, not a producer, into one sealed case."""
    directory, data = saved["directory"], saved["data"]
    case_id = "synthetic_leader"
    result_path = data / "saved_leader/result.json"
    case_dir = result_path.parent/case_id
    case_dir.mkdir(parents=True)
    (case_dir/"target_holdings.csv").write_bytes(saved["targets"].read_bytes())
    old_refs = saved["head"]["routes"]["fixed"]["product"]["refs"]
    intent = json.loads(Path(old_refs["intent"]["path"]).read_text(encoding="utf-8"))
    source_head = saved["head"]["routes"]["fixed"]["source_head"]
    intent.update(schema="saved_leader_eight_type_account_intent_v1", strategy_id=case_id, case_id=case_id,
        context={"account_id": "synthetic_leader", "mode": "historical_replay"},
        refs={"head": source_head, "latest_journal": {"path": "synthetic_not_read", "sha256": "j"*64}})
    write(case_dir/"intent.json", intent)
    preview = json.loads(Path(old_refs["preview"]["path"]).read_text(encoding="utf-8"))
    preview.update(strategy_id=case_id, source_refs={"head": source_head},
        saved_intent=str(case_dir/"intent.json"),
        artifacts={"provisional_target_holdings": str(case_dir/"target_holdings.csv")})
    write(case_dir/"result.json", preview)
    pack = saved["pack"]
    pack["strategies"] = [{"strategy_id": case_id, "preview": str(case_dir/"result.json")}]
    pack["ledger_preflight_by_strategy"] = {case_id: next(iter(pack["ledger_preflight_by_strategy"].values()))}
    for path in (saved["orders"], Path(pack["artifacts"]["ledger_preflight_by_strategy"])):
        path.write_text(path.read_text(encoding="utf-8-sig").replace("synthetic_fixed", case_id), encoding="utf-8-sig")
    review = {"schema": "saved_leader_holding_review_v1", "strategy_id": case_id,
        "source_family": "eight_type", "source_day": D, "execution_day": E,
        "source_known_at": intent["source_known_at"], "refs": intent["refs"],
        "saved_position_review": {"case_id": case_id, "processed_day": D, "context": intent["context"],
            "error": None, "blocking_evidence": None, "positions": [{"symbol": SYMBOL,
                "quantity_shares": 300, "sellable_on_book_day_shares": 200, "t1_locked_shares": 100,
                "anchor": {"status": "matched_saved_FIFO_fill", "fill_id": "original_fill",
                    "fill_day": "2026-10-15", "evidence": {"original_entry": "synthetic_entry_reason"}},
                "assessment": {"status": "invalid", "facts": {"structure_price": 11},
                    "reasons": ["current_close_below_fixed_entry_structure"]},
                "queued_risk_exit": {"execution_on": E}}],
            "latest_order_blocks": [{"symbol": SYMBOL, "day": D, "kind": "order_blocked", "reason": "saved_T1"}]},
        **core.FLAGS}
    sidecar = Path(pack["artifacts"]["candidate_orders"]).parent/"holding_review.json"
    side_ref = write(sidecar, {"schema": "saved_leader_opening_holding_review_v1", "source_family": "eight_type",
        "source_day": D, "execution_day": E, "case_reviews": [review], "current_risk_recomputed": False, **core.FLAGS})
    pack["holding_review_ref"] = side_ref
    pack["artifacts"]["holding_review"] = str(sidecar)
    pack_ref = write(saved["pack_path"], pack)
    result_ref = write(result_path, {"source_cutoff": D, "execution_day": E,
        "source_run_directory": str(saved["source"]), "source_head_sha256": source_head["sha256"],
        "opening_pack": str(saved["pack_path"]),
        "cases": [{"case_id": case_id, "status": "paper_preview_only", "preview": str(case_dir/"result.json")}],
        **core.FLAGS})
    registration = json.loads((directory/"registration.json").read_text(encoding="utf-8"))
    registration["routes"][0]["family"] = "eight_type"
    saved["head"]["registration"] = write(directory/"registration.json", registration)
    saved["head"]["routes"]["fixed"]["product"]["refs"] = {"result": result_ref, "opening_pack": pack_ref}
    write(directory/"head.json", saved["head"])
    return sidecar, review


def test_product_bound_holding_reasons_remain_saved_after_source_head_advances(saved, monkeypatch):
    sidecar, _ = leader_product(saved)
    write(saved["source"]/"head.json", {"synthetic_new_positions": {SYMBOL: 9999}})
    from trading.research import saved_leader_account_intent as source
    monkeypatch.setattr(source, "read_saved_positions", lambda *a, **kw: pytest.fail("Old pack reread current positions"))
    before = sidecar.read_bytes()
    result = detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
    case = result["cases"][0]
    assert result["route"]["stale"] is True and case["holding_review_status"] == "saved_product_bound"
    assert case["positions"][0]["quantity"] == 300 and case["positions"][0]["sellable_on_source_day"] == 200
    held = case["holding_review"]["positions"][0]
    assert held["anchor"]["fill_id"] == "original_fill" and held["assessment"]["status"] == "invalid"
    assert held["queued_risk_exit"]["execution_on"] == E
    assert case["holding_review"]["latest_order_blocks"][0]["reason"] == "saved_T1"
    assert sidecar.read_bytes() == before


def test_holding_review_bytes_are_verified_instead_of_falling_back_to_new_head(saved):
    sidecar, _ = leader_product(saved)
    sidecar.write_bytes(sidecar.read_bytes()+b" ")
    with pytest.raises(ValueError, match="bytes changed"):
        detail.opening_daily_detail(saved["data"], "fixed", now=NOW)


@pytest.mark.parametrize("field", ["context", "quantity"])
def test_resealed_other_account_or_quantity_cannot_describe_product_holdings(saved, field):
    sidecar, review = leader_product(saved)
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    if field == "context":
        record["case_reviews"][0]["saved_position_review"]["context"]["account_id"] = "other_case"
    else:
        record["case_reviews"][0]["saved_position_review"]["positions"][0]["quantity_shares"] = 999
    saved["pack"]["holding_review_ref"] = write(sidecar, record)
    pack_ref = write(saved["pack_path"], saved["pack"])
    saved["head"]["routes"]["fixed"]["product"]["refs"]["opening_pack"] = pack_ref
    write(saved["directory"]/"head.json", saved["head"])
    with pytest.raises(ValueError, match="exact source intent|quantities differ"):
        detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
