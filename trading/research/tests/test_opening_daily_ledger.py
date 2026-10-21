"""Pinned synthetic account layouts; no Engine advancement or broker calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

from fastapi import HTTPException
import pytest

from trading import api
from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.ledger import new_state
from trading.research import opening_daily as core
from trading.research import opening_daily_ledger as m
from trading.research.opening_daily_view import RUN_RELATIVE


NOW = datetime.fromisoformat("2026-10-17T17:00:00+08:00")
D, E, A = "2026-10-16", "2026-10-19", "600001.SSE"


def write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return core._ref(path)


def receipt(bound, day, execution):
    return {"schema": "replay_published_close_portfolio_intent_v1", "decision_day": day,
        "decision_at": bound["decided_at"], "execution_not_before": execution, "intent": bound,
        "input_kind": "explicit_published_close_PortfolioIntent_not_SignalFrame",
        "source_availability_verified_by_engine": False, "source_verified": False,
        "pit_verified": False, "execution_admitted": False}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    data, coordinator = tmp_path / "data", tmp_path / "data" / RUN_RELATIVE
    monkeypatch.setattr(core, "DATA_ROOT", data)
    monkeypatch.setattr(api.settings, "DATA_ROOT", data)
    config, case_id = StrategyConfig(), "uncapped"
    source = data / "alpha_source"
    source_key = core.sha256(json.dumps(str(source.resolve()), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
    context = RunContext("recent-alpha158-forward:" + source_key + ":" + case_id, TradingMode.REPLAY).to_dict()
    order_id = D + ":" + A + ":buy"
    state = new_state(config.initial_cash)
    state.update(day=D, cash=9_799_497, positions={A: [{"qty": 100, "available": E}, {"qty": 100, "available": E}]},
        prices={A: 1000}, orders={order_id: {"id": order_id, "day": D, "symbol": A, "side": "buy",
            "qty": 200, "filled": 200, "limit_price": 1000, "notional": 200000,
            "fees": 503, "reserve": 0, "status": "filled", "reason": None}},
        fills={order_id + ":0": {"order_id": order_id, "day": D, "symbol": A, "side": "buy", "qty": 100,
            "price": 1000, "fees": 501, "available_day": E},
            order_id + ":1": {"order_id": order_id, "day": D, "symbol": A, "side": "buy", "qty": 100,
            "price": 1000, "fees": 2, "available_day": E}},
        events=[{"day": D, "kind": "order_blocked", "symbol": "688001.SSE", "reason": "cash_below_one_lot"}])
    prior = PortfolioIntent.full_target({A: .16}, context=RunContext.from_dict(context),
        decided_at="2026-10-15T18:00:00+08:00", execution_not_before=D, source="research:synthetic_saved_only").to_dict()
    pub_ref = write(tmp_path / "synthetic_prior_pub.json", {"source_close_day": "2026-10-15", "proposed_execution_day": D})
    pub_ref.update(day="2026-10-15", execution_day=D, recorded_at=prior["decided_at"])
    current_intent = PortfolioIntent.full_target({A: .16}, context=RunContext.from_dict(context),
        decided_at=D + "T18:00:00+08:00", execution_not_before=E, source="research:synthetic_saved_only").to_dict()
    journal = [{"decision_day": "2026-10-15", "execution_day": D, "publication": pub_ref, "bound_intent": prior},
               {"decision_day": D, "execution_day": E, "bound_intent": current_intent}]
    daily = [{"date": "2026-10-15", "equity": 100000, "cash": 100000},
             {"date": D, "cash": state["cash"] / 100, "equity": 99994.97, "fees": 5.03,
              "consumed_published_close_intent": receipt(prior, "2026-10-15", D)}]
    source_head = {"binding": {"config": config.to_dict()}, "cases": {case_id: {"state": state,
        "context": context, "journal": journal, "daily": daily}}, **core.FLAGS}
    source_ref = write(source / "head.json", source_head)
    intent = {"schema": "saved_alpha158_account_intent_v1", "kind": "FULL_TARGET", "weights": {A: .16},
        "strategy_id": "alpha158:" + case_id, "case_id": case_id, "source_day": D, "execution_day": E,
        "execution_config": config.to_dict(), "context": context,
        "refs": {"head": source_ref, "latest_journal": source_ref}, **core.FLAGS}
    result_dir = data / "existing_opening"
    case_dir = result_dir / case_id
    write(case_dir / "intent.json", intent)
    write(case_dir / "result.json", {"source_cutoff": D, "execution_day": E,
                                    "saved_intent": str(case_dir / "intent.json")})
    result_ref = write(result_dir / "result.json", {"source_head_sha256": source_ref["sha256"],
        "cases": [{"case_id": case_id, "status": "paper_preview_only", "preview": str(case_dir / "result.json")} ]})
    route = {"id": "alpha", "family": "alpha158", "source": str(source)}
    registration_ref = write(coordinator / "registration.json", {"schema": core.SCHEMA, "method_id": core.METHOD,
        "run_directory": str(coordinator.resolve()), "routes": [route], **core.FLAGS})
    product = {"source_head": source_ref, "source_day": D, "execution_day": E, "status": "paper_candidates_only",
               "refs": {"result": result_ref}, **core.FLAGS}
    head = {"schema": core.SCHEMA, "method_id": core.METHOD, "registration": registration_ref,
        "routes": {"alpha": {"input_id": "c" * 64, "source_head": source_ref, "product": product,
            "committed_at": "2026-10-17T16:30:00+08:00", "attempt": str(coordinator / "unused_attempt.json")}}, **core.FLAGS}
    write(coordinator / "head.json", head)
    return {"data": data, "directory": coordinator, "source": source, "source_head": source_head,
        "head": head, "case_id": case_id, "intent": intent, "intent_path": case_dir / "intent.json"}


def repin(saved):
    reference = write(saved["source"] / "head.json", saved["source_head"])
    saved["intent"]["refs"] = {"head": reference, "latest_journal": reference}
    write(saved["intent_path"], saved["intent"])
    slot = saved["head"]["routes"]["alpha"]
    result_path = Path(slot["product"]["refs"]["result"]["path"])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["source_head_sha256"] = reference["sha256"]
    slot["source_head"] = slot["product"]["source_head"] = reference
    slot["product"]["refs"]["result"] = write(result_path, result)
    write(saved["directory"] / "head.json", saved["head"])


def test_actual_saved_multi_fill_fees_dated_t1_and_previous_decision_not_next_plan(saved, monkeypatch):
    for name in ("run", "register", "recover", "_factory", "_lock"):
        monkeypatch.setattr(core, name, lambda *a, **kw: pytest.fail("Ledger GET attempted writer/producer"))
    before = {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}
    result = m.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["order_count"] == 1 and result["fill_count"] == 2
    assert result["orders"][0]["fees"] == 503 and [row["fees"] for row in result["fills"]] == [501, 2]
    assert result["orders"][0]["price_unit"] == result["fills"][0]["price_unit"] == "cent"
    assert result["daily_summary"]["fees"] == 5.03
    assert result["positions"][0]["sellable_on_ledger_day"] == 0
    assert result["cash"]["total_cents"] == 9_799_497 and result["cash"]["available_cents"] == 9_799_497
    assert result["blocked_count"] == 1 and result["order_blocks"][0]["reason"] == "cash_below_one_lot"
    assert result["executed_decision"]["source_day"] == "2026-10-15"
    assert result["executed_decision"]["execution_day"] == result["ledger_day"] == D
    assert result["planned_execution_day"] == E
    assert before == {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}


def test_replaced_source_has_no_fallback_to_new_head_or_preview_copy(saved):
    saved["source_head"]["new_actual_generation"] = True
    write(saved["source"] / "head.json", saved["source_head"])
    result = m.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["status"] == "unavailable" and result["stale"] is True
    assert result["orders"] is None and result["fills"] is None
    assert result["order_count"] is None and result["fill_count"] is None


def test_incomplete_fill_collection_is_unknown_not_valid_zero(saved):
    del saved["source_head"]["cases"][saved["case_id"]]["state"]["fills"]
    repin(saved)
    result = m.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["status"] == "unavailable" and result["fill_count"] is None


def test_complete_empty_day_and_saved_block_are_distinct(saved):
    case = saved["source_head"]["cases"][saved["case_id"]]
    case["state"].update(orders={}, fills={}, positions={}, prices={}, cash=10_000_000)
    case["daily"][-1].update(cash=100000, equity=100000, fees=0)
    repin(saved)
    result = m.opening_daily_ledger(saved["data"], "alpha", saved["case_id"], now=NOW)
    assert result["order_count"] == result["fill_count"] == 0
    assert result["orders"] == result["fills"] == [] and result["blocked_count"] == 1


def test_bad_fill_join_or_case_selection_never_reads_another_book(saved):
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_ledger("alpha", "not_registered")
    assert caught.value.status_code == 404
    state = saved["source_head"]["cases"][saved["case_id"]]["state"]
    next(iter(state["fills"].values()))["order_id"] = "unmatched"
    repin(saved)
    with pytest.raises(HTTPException) as caught:
        api.saved_opening_daily_ledger("alpha", saved["case_id"])
    assert caught.value.status_code == 409
