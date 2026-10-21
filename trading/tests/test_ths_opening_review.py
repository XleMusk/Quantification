"""Synthetic pinned opening files and broker evidence, never a client/account."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.config import StrategyConfig
from trading.research.tests.test_opening_daily_detail import saved as detail_fixture, write
from trading.ths import opening_review as review
from trading.ths import saved_intent as consumer
from trading.ths.adapter import check_plan


NOW = datetime.fromisoformat("2026-10-19T09:35:00+08:00")
SYMBOL = "600001.SSE"


@pytest.fixture
def saved(tmp_path, monkeypatch):
    # Reuse the saved-parent fixture without running its existing tests. Only
    # the final read_request canonical loader is mocked; the real handoff,
    # saved-intent sizing, THS adapter and order-risk diagnostic all run.
    result = detail_fixture.__wrapped__(tmp_path, monkeypatch)
    head = result["head"]
    product = head["routes"]["fixed"]["product"]
    refs = product["refs"]
    config = StrategyConfig().to_dict()
    intent = json.loads(Path(refs["intent"]["path"]).read_text())
    intent.update(execution_config=config, research_only=True, prefill_allowed=False)
    refs["intent"] = write(Path(refs["intent"]["path"]), intent)
    preview_path = Path(refs["preview"]["path"])
    preview = json.loads(preview_path.read_text())
    handoff_path = Path(preview["artifacts"]["provisional_target_holdings"]).parent / "ths_manual_handoff.json"
    write(handoff_path, {"schema": "ths_manual_prefill_handoff_v1",
        "task_method": "saved_fixed_account_next_session_v1", "strategy_id": intent["strategy_id"],
        "source_cutoff": intent["source_day"], "execution_day": intent["execution_day"],
        "execution_admitted": False, "submission": "manual_only", "saved_account_intent": intent})
    preview["artifacts"]["ths_manual_handoff"] = str(handoff_path)
    preview["execution_config"] = config
    refs["preview"] = write(preview_path, preview)
    result["pack"]["strategies"][0]["execution_config"] = config
    refs["opening_pack"] = write(result["pack_path"], result["pack"])
    registration_path = result["directory"] / "registration.json"
    registration = json.loads(registration_path.read_text())
    registration["adopt_refs"]["fixed"] = deepcopy(refs)
    head["registration"] = write(registration_path, registration)
    write(result["directory"] / "head.json", head)
    monkeypatch.setattr(consumer, "read_request", lambda payload, *, current=None: deepcopy(payload))
    monkeypatch.setattr("trading.engine.advance", lambda *a, **k: pytest.fail("No Engine advance"))
    monkeypatch.setattr("trading.ths.journal.initialize", lambda *a, **k: pytest.fail("No journal"))
    return result


def account(sellable=500):
    return {"kind": "account", "source": "ths_user_export", "account_alias": "synthetic-broker",
        "observed_at": NOW.isoformat(), "trading_date": "2026-10-19", "cash_total": 10_000_000,
        "cash_frozen": 0, "cash_available": 10_000_000, "cash_basis": "cash_balance_includes_frozen",
        "positions": {SYMBOL: {"quantity": 500, "sellable": sellable}}, "marks": {SYMBOL: 1000}}


def quotes():
    return {"schema": "ths_raw_quote_observation_v1", "source": "ths_controls",
        "account_alias": "synthetic-broker", "observed_at": NOW.isoformat(), "trading_date": "2026-10-19",
        "price_basis": "raw_rmb", "prices": {SYMBOL: {"price_rmb": "10.00", "preclose_rmb": "10.00",
            "is_st": False, "tradable": True}}}


def files(saved):
    return {p: p.read_bytes() for folder in (saved["data"], saved["pack_path"].parents[2])
        for p in folder.rglob("*") if p.is_file()}


def test_missing_evidence_and_exact_execution_date_remain_explicit_without_writes(saved):
    before = files(saved)
    missing = review.build_review(saved["data"], "fixed", "synthetic_fixed", current=NOW)
    assert missing["status"] == "awaiting_account_evidence" and missing["missing"] == ["account_observation"]
    assert missing["saved_intent"]["kind"] == "NO_CHANGE" and missing["saved_intent"]["weights"] is None
    quote_gap = review.build_review(saved["directory"], "fixed", "synthetic_fixed", account_observation=account(), current=NOW)
    assert quote_gap["status"] == "awaiting_broker_quotes"
    assert quote_gap["missing"] == ["broker_quotes.prices." + SYMBOL]
    partial_quotes = quotes()
    partial_quotes.pop("observed_at")
    partial = review.build_review(saved["data"], "fixed", "synthetic_fixed",
        account_observation=account(), broker_quotes=partial_quotes, current=NOW)
    assert partial["missing"] == ["broker_quotes.observed_at"] and partial["diagnostic"] is None
    future = review.build_review(saved["data"], "fixed", "synthetic_fixed", current=datetime.fromisoformat("2026-10-17T19:00:00+08:00"))
    expired = review.build_review(saved["data"], "fixed", "synthetic_fixed", current=datetime.fromisoformat("2026-10-20T09:35:00+08:00"))
    assert future["status"] == "awaiting_execution_day" and expired["status"] == "expired"
    assert all(r["next_plan"] is None and r["prefill_allowed"] is False and r["diagnostic"] is None for r in (missing, quote_gap, future, expired))
    assert before == files(saved)


def test_complete_inputs_call_original_diagnostic_and_never_promote_or_shrink_t1_exit(saved, monkeypatch):
    original, calls = review.prepare_opening_pack, []
    def observed(*args, **kwargs):
        calls.append(kwargs["strategy_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(review, "prepare_opening_pack", observed)
    before, snapshot, observed_quotes = files(saved), account(), quotes()
    result = review.build_review(saved["data"], "fixed", "synthetic_fixed",
        account_observation=snapshot, broker_quotes=observed_quotes, current=NOW)
    plan = result["diagnostic"]["candidates"][0]["plan"]
    assert calls == ["synthetic_fixed"] and result["status"] == "research_diagnostic_only"
    assert result["research_positions"][0]["quantity"] == 300
    assert plan["adjustments"][0]["held_qty"] == 500 and plan["adjustments"][0]["target_qty"] == 0
    assert plan["diagnostic_next_plan"]["qty"] == 500 and result["next_plan"] is None
    with pytest.raises(ValueError, match="not eligible"):
        check_plan(plan["diagnostic_next_plan"], current=NOW)
    blocked = review.build_review(saved["data"], "fixed", "synthetic_fixed",
        account_observation=account(sellable=100), broker_quotes=quotes(), current=NOW)
    adjustment = blocked["diagnostic"]["candidates"][0]["plan"]["adjustments"][0]
    assert adjustment["target_qty"] == 0 and adjustment["remaining_exit_qty"] == 500
    assert adjustment["validation_reason"] == "insufficient_sellable_T1"
    assert blocked["prefill_allowed"] is False and before == files(saved)


def test_source_change_and_supplied_wrong_account_or_public_quote_are_rejected(saved):
    mismatched = quotes()
    mismatched["account_alias"] = "another-synthetic-account"
    with pytest.raises(ValueError, match="same-account"):
        review.build_review(saved["data"], "fixed", "synthetic_fixed", account_observation=account(), broker_quotes=mismatched, current=NOW)
    public = quotes()
    public["source"] = "public_market_close"
    with pytest.raises(ValueError, match="same-account"):
        review.build_review(saved["data"], "fixed", "synthetic_fixed", account_observation=account(), broker_quotes=public, current=NOW)
    wrong_day = account()
    wrong_day["trading_date"] = "2026-10-16"
    with pytest.raises(ValueError, match="exact execution day"):
        review.build_review(saved["data"], "fixed", "synthetic_fixed", account_observation=wrong_day, broker_quotes=quotes(), current=NOW)
    (saved["source"] / "head.json").write_text('{"new_source":true}')
    with pytest.raises(ValueError, match="source changed"):
        review.build_review(saved["data"], "fixed", "synthetic_fixed", current=NOW)


def test_bootstrap_notready_preserves_original_full_weights_and_no_account(saved, monkeypatch):
    from trading.research import native_lowvol_saved_opening
    monkeypatch.setattr(native_lowvol_saved_opening, "source_dependencies_current", lambda _: True)
    head, directory = saved["head"], saved["directory"]
    product = head["routes"]["fixed"]["product"]
    source_ref = head["routes"]["fixed"]["source_head"]
    case_id, strategy = "low_volatility_252_20", "native_lowvol:low_volatility_252_20"
    intent_path = saved["data"] / "bootstrap_product/intent.json"
    intent = {"schema": "saved_native_lowvol_preopen_intent_v1", "strategy_id": strategy,
        "case_id": case_id, "source_day": "2026-10-16", "execution_day": "2026-10-19",
        "kind": "FULL_TARGET", "weights": {SYMBOL: .04}, "original_weights": {SYMBOL: .04},
        "remaining_exit_symbols": [], "refs": {"head": source_ref}, **{k:v for k,v in product.items() if k in ("research_only", "candidate_panel_only", "execution_admitted", "selection_enabled", "strategy_validated", "prefill_allowed")}}
    write(intent_path, intent)
    result = {"source_family": "native_lowvol", "source_cutoff": "2026-10-16", "execution_day": "2026-10-19",
        "source_head_sha256": source_ref["sha256"], "source_run_directory": str(saved["source"]),
        "cases": [{"case_id": case_id, "strategy_id": strategy, "status": "not_ready",
            "reason": "published_research_plan_account_not_created", "account_count": 0,
            "kind": "FULL_TARGET", "weights": {SYMBOL: .04}, "intent": str(intent_path)}],
        **{k:v for k,v in intent.items() if k in ("candidate_panel_only", "execution_admitted", "selection_enabled", "strategy_validated")}}
    result_ref = write(intent_path.parent / "result.json", result)
    product.update(refs={"result": result_ref}, ready_case_count=0, not_ready_case_count=1, candidate_order_count=None)
    registration = json.loads((directory / "registration.json").read_text())
    registration["routes"][0]["family"] = "native_lowvol"
    head["registration"] = write(directory / "registration.json", registration)
    write(directory / "head.json", head)
    before = files(saved)
    actual = review.build_review(saved["data"], "fixed", case_id, current=NOW)
    assert actual["status"] == "source_not_ready" and actual["saved_intent"]["weights"] == {SYMBOL: .04}
    assert actual["account_count"] == 0 and actual["cash_rmb"] is actual["positions"] is None
    assert actual["diagnostic"] is actual["next_plan"] is None and actual["account_created"] is False
    assert before == files(saved)
