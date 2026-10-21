import json
from pathlib import Path

import pytest

from trading.ths.account_risk import build_account_risk_view, evaluate_order_risk


def _package(*, source_scope="persistent_user_export", rows=None, cash_total=1000000,
             cash_frozen=0, cash_available=1000000):
    rows = rows if rows is not None else [
        {"symbol": "000001.SZSE", "quantity": 100, "sellable": 0, "price": "10.00"},
    ]
    return {
        "schema": "ths_structured_observation_package_v1",
        "status": "complete_structured_observation_package",
        "source_scope": source_scope,
        "source": "validated_ths_user_export_bundle",
        "source_manifest_status": "verified",
        "source_manifest_issues": [],
        "account_alias": "risk-test",
        "account_observed": True,
        "orders_observed": True,
        "trades_observed": True,
        "cashflows_observed": True,
        "holdings_candidate_semantic_verified": True,
        "account_summary": {"metrics_raw": {
            "cash_total": cash_total,
            "cash_frozen": cash_frozen,
            "cash_available": cash_available,
        }},
        "holdings_candidate": {"rows": rows},
        "ready_for_ledger_reconciliation": True,
        "current_ledger_import_candidate": source_scope == "persistent_user_export",
        "ledger_reconciled": False,
        "ledger_mutated": False,
        "native_verified": False,
        "observation_window": {"trading_date": "2026-10-09"},
    }


def test_account_risk_prefers_raw_quote_and_surfaces_t1_and_concentration():
    result = build_account_risk_view(
        _package(cash_total=1000000, cash_available=1000000),
        quote_observation={"prices": {"000001.SZSE": {"price_rmb": "12.00"}}},
    )
    assert result["status"] == "account_risk_attention"
    assert result["account_ready"] is True
    assert result["positions"][0]["mark_source"] == "raw_quote_observation"
    assert result["positions"][0]["market_value_cents"] == 120000
    assert result["sellability"]["t1_blocked_quantity"] == 100
    assert "t1_sell_blocked_quantity_present" in result["risk_flags"]
    assert result["ledger_binding"]["same_ledger_required"] is True


def test_account_risk_normalizes_native_cent_marks():
    package = _package(rows=[{
        "symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price_cents": 1234,
    }])
    result = build_account_risk_view(package)
    assert result["arithmetic_ready"] is True
    assert result["positions"][0]["mark_price_rmb"] == "12.34"
    assert result["positions"][0]["market_value_cents"] == 123400
    assert "missing_position_mark" not in result["blockers"]


def test_isolated_rehearsal_never_becomes_account_ready():
    result = build_account_risk_view(_package(source_scope="isolated_rehearsal"))
    assert result["status"] == "isolated_rehearsal_only"
    assert result["arithmetic_ready"] is True
    assert result["account_ready"] is False
    assert result["execution_admitted"] is False


def test_changed_structured_source_manifest_blocks_account_risk():
    package = _package()
    package["source_manifest_status"] = "source_changed"
    package["source_manifest_issues"] = [{"kind": "orders", "reason": "sha256_mismatch"}]

    result = build_account_risk_view(package)

    assert result["status"] == "blocked_missing_or_invalid_account_evidence"
    assert "structured_source_manifest_changed" in result["blockers"]
    assert result["source_manifest_status"] == "source_changed"
    assert result["source_manifest_issues"][0]["kind"] == "orders"
    assert result["account_ready"] is False


def test_persistent_export_without_verified_source_manifest_is_blocked():
    package = _package()
    package.pop("source_manifest_status")

    result = build_account_risk_view(package)

    assert result["status"] == "blocked_missing_or_invalid_account_evidence"
    assert "structured_source_manifest_unverified" in result["blockers"]
    assert result["account_ready"] is False


def test_pre_submit_snapshot_stays_arithmetic_only():
    from trading.ths.account_risk import package_from_account_observation

    package = package_from_account_observation({
        "account_alias": "risk-test",
        "cash_total": 1000000,
        "cash_frozen": 0,
        "cash_available": 1000000,
        "positions": [],
        "observed_at": "2026-10-09T09:30:00+08:00",
        "trading_date": "2026-10-09",
    })

    result = build_account_risk_view(package)

    assert result["status"] == "pre_submit_snapshot_only"
    assert result["arithmetic_ready"] is True
    assert result["account_ready"] is False
    assert result["source_scope"] == "pre_submit_account_snapshot"


def test_invalid_cash_and_missing_marks_are_blocked():
    result = build_account_risk_view(
        _package(cash_total=100, cash_frozen=200, cash_available=150,
                 rows=[{"symbol": "000001.SZSE", "quantity": 100, "sellable": 100}]),
    )
    assert result["status"] == "blocked_missing_or_invalid_account_evidence"
    assert "cash_frozen_exceeds_cash_total" in result["blockers"]
    assert "cash_available_exceeds_cash_total" in result["blockers"]
    assert "missing_position_mark" in result["blockers"]


def test_missing_holdings_evidence_is_not_inferred_as_cash_only_account():
    package = _package(rows=[])
    package.pop("holdings_candidate")
    result = build_account_risk_view(package)
    assert result["arithmetic_ready"] is False
    assert "structured_holdings_evidence_missing" in result["blockers"]


def test_explicit_empty_holdings_are_valid_cash_only_evidence():
    package = _package(rows=[])
    result = build_account_risk_view(package)
    assert result["arithmetic_ready"] is True
    assert result["cash"]["holdings_value_cents"] == 0


def test_account_observation_adapter_does_not_infer_missing_holdings():
    from trading.ths.account_risk import package_from_account_observation

    account = {
        "account_alias": "risk-test",
        "cash_total": 1000000,
        "cash_frozen": 0,
        "cash_available": 1000000,
        "trading_date": "2026-10-09",
    }
    package = package_from_account_observation(account)
    result = build_account_risk_view(package)
    assert result["arithmetic_ready"] is False
    assert "structured_holdings_evidence_missing" in result["blockers"]


def test_account_observation_adapter_preserves_explicit_empty_holdings():
    from trading.ths.account_risk import package_from_account_observation

    account = {
        "account_alias": "risk-test",
        "cash_total": 1000000,
        "cash_frozen": 0,
        "cash_available": 1000000,
        "positions": [],
        "trading_date": "2026-10-09",
    }
    package = package_from_account_observation(account)
    result = build_account_risk_view(package)
    assert result["arithmetic_ready"] is True
    assert result["cash"]["holdings_value_cents"] == 0


def test_new_buy_uses_explicit_limit_for_projected_risk():
    package = _package(rows=[{
        "symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price": "12.00",
    }])
    result = evaluate_order_risk(package, [{
        "order_id": "new-order",
        "symbol": "000002.SZSE",
        "side": "buy",
        "qty": 100,
        "price_rmb": "5.00",
        "fee_cents": 500,
    }])
    assert result["status"] == "order_risk_ready_readonly"
    assert not any("current_mark_missing" in blocker for blocker in result["blockers"])
    assert result["orders"][0]["valuation_source"] == "explicit_order_limit"
    assert result["projected"]["buy_notional_cents"] == 50000


def test_order_fee_buffer_reduces_projected_cash():
    package = _package(rows=[])
    result = evaluate_order_risk(package, [{
        "order_id": "fee-order",
        "symbol": "000002.SZSE",
        "side": "buy",
        "qty": 100,
        "price_rmb": "5.00",
        "fee_cents": 123,
    }])
    assert result["status"] == "order_risk_ready_readonly"
    assert result["projected"]["available_cash_cents"] == 949877
    assert result["projected"]["buy_fees_cents"] == 123
    assert result["projected"]["fees_included"] is True


def test_account_cash_identity_is_required_even_when_fields_are_in_range():
    result = build_account_risk_view(_package(
        cash_total=1000000, cash_frozen=100000, cash_available=950000,
    ))
    assert result["arithmetic_ready"] is False
    assert "cash_total_minus_frozen_differs_from_available" in result["blockers"]


def test_account_risk_keeps_arithmetic_but_blocks_on_same_ledger_gap():
    result = build_account_risk_view(
        _package(rows=[]),
        reconciliation={
            "status": "reconciliation_required",
            "summary": {"issue_count": 2},
        },
    )
    assert result["arithmetic_ready"] is True
    assert result["account_ready"] is False
    assert result["status"] == "same_ledger_reconciliation_required"
    assert result["same_ledger_reconciliation"]["issue_count"] == 2
    assert "same_ledger_reconciliation_required" in result["execution_blockers"]
    assert result["cash"]["equity_cents"] == 1000000


def test_order_risk_refuses_new_batch_until_same_ledger_is_clear():
    result = evaluate_order_risk(
        _package(rows=[]),
        [{"order_id": "blocked", "symbol": "000002.SZSE", "side": "buy",
          "qty": 100, "price_rmb": "5.00", "fee_cents": 0}],
        reconciliation={
            "status": "reconciliation_required",
            "summary": {"issue_count": 1},
        },
    )
    assert result["status"] == "blocked"
    assert "same_ledger_reconciliation_required" in result["blockers"]
    assert "account_release_blocked" in result["blockers"]
    assert result["orders"] == []


def test_frozen_cash_remains_part_of_projected_account_equity():
    package = _package(rows=[], cash_total=1000000,
                       cash_frozen=200000, cash_available=800000)
    result = evaluate_order_risk(package, [{
        "symbol": "600000.SSE", "side": "buy", "qty": 200,
        "price_rmb": "10.00", "fee_cents": 500,
    }])
    assert result["status"] == "order_risk_ready_readonly"
    assert result["projected"]["equity_cents"] == 999500
    assert result["projected"]["total_cash_cents"] == 799500
    assert result["projected"]["available_cash_cents"] == 599500
    assert result["projected"]["frozen_cash_cents"] == 200000
    assert result["projected"]["gross_weight"] == 0.2


@pytest.mark.parametrize("sells_first", [False, True])
def test_unfilled_sell_proceeds_cannot_fund_new_buys(sells_first):
    package = _package(rows=[{
        "symbol": "600000.SSE", "quantity": 100, "sellable": 100, "price": "10.00",
    }], cash_total=6000, cash_available=6000)
    sell = {"symbol": "600000.SSE", "side": "sell", "qty": 100,
            "price_rmb": "10.00", "fee_cents": 500}
    buy = {"symbol": "000002.SZSE", "side": "buy", "qty": 100,
           "price_rmb": "5.00", "fee_cents": 500}
    result = evaluate_order_risk(package, [sell, buy] if sells_first else [buy, sell],
                                 max_single_weight=1, max_gross_weight=1,
                                 minimum_cash_buffer=0)
    assert result["status"] == "order_risk_blocked"
    assert "buy_funding_exceeds_observed_available_cash" in result["blockers"]
    assert result["projected"]["available_cash_cents"] > 0
    assert result["projected"]["cash_after_buy_reservation_cents"] == -44500
    assert result["projected"]["buy_funding_available_cents"] == -44500
    assert result["projected"]["post_fill_cash_cents"] == 55000
    assert result["projected"]["unfilled_sell_proceeds_spendable"] is False


def test_missing_fee_evidence_is_not_zero_and_explicit_zero_is_preserved():
    order = {"symbol": "000002.SZSE", "side": "buy", "qty": 100, "price_rmb": "5.00"}
    missing = evaluate_order_risk(_package(rows=[]), [order])
    assert missing["status"] == "order_risk_blocked"
    assert "order_proposed-1:fee_evidence_missing" in missing["blockers"]
    explicit = evaluate_order_risk(_package(rows=[]), [order], fee_buffer_cents=0)
    assert explicit["status"] == "order_risk_ready_readonly"
    assert explicit["orders"][0]["fee_cents"] == 0


def test_order_risk_applies_a_share_lot_and_price_tick_rules():
    package = _package(rows=[], cash_total=1000000, cash_available=1000000)
    package["observation_window"] = {"trading_date": "2026-10-09"}
    result = evaluate_order_risk(package, [{
        "order_id": "bad-lot", "symbol": "000002.SZSE", "side": "buy",
        "qty": 99, "price_rmb": "5.001", "fee_cents": 0,
    }])
    assert result["status"] == "order_risk_blocked"
    assert "order_bad-lot:buy_lot_must_be_100" in result["blockers"]
    assert "order_bad-lot:price_tick_must_be_0.01_rmb" in result["blockers"]


def test_order_risk_enforces_explicit_price_limits_and_trade_status():
    package = _package(rows=[], cash_total=1000000, cash_available=1000000)
    package["observation_window"] = {"trading_date": "2026-10-09"}
    blocked = evaluate_order_risk(
        package,
        [{"order_id": "limit-up", "symbol": "000002.SZSE", "side": "buy",
          "qty": 100, "price_rmb": "11.01", "fee_cents": 0}],
        quote_observation={"prices": {"000002.SZSE": {
            "price_rmb": "10.00", "preclose_rmb": "10.00", "is_st": False,
            "tradable": True,
        }}},
    )
    assert blocked["status"] == "order_risk_blocked"
    assert "order_limit-up:price_above_limit_up" in blocked["blockers"]
    assert blocked["price_limit_checks"][0]["status"] == "checked"


def test_order_risk_does_not_infer_st_status_or_tradability():
    package = _package(rows=[], cash_total=1000000, cash_available=1000000)
    package["observation_window"] = {"trading_date": "2026-10-09"}
    result = evaluate_order_risk(
        package,
        [{"order_id": "unknown-status", "symbol": "000002.SZSE", "side": "buy",
          "qty": 100, "price_rmb": "10.00", "fee_cents": 0}],
        quote_observation={"prices": {"000002.SZSE": {
            "price_rmb": "10.00", "preclose_rmb": "10.00",
        }}},
    )
    assert "order_unknown-status:price_limit_st_status_unknown" in result["blockers"]
    assert result["price_limit_checks"][0]["status"] == "blocked_status_unknown"


def test_order_risk_exposes_new_buy_as_t1_blocked_and_rejects_same_batch_sale():
    package = _package(rows=[], cash_total=1000000, cash_available=1000000)
    package["observation_window"] = {"trading_date": "2026-10-09"}
    buy = {"order_id": "buy", "symbol": "000002.SZSE", "side": "buy",
           "qty": 100, "price_rmb": "5.00", "fee_cents": 0}
    result = evaluate_order_risk(package, [buy])
    assert result["status"] == "order_risk_ready_readonly"
    assert result["projected"]["positions"] == [{
        "symbol": "000002.SZSE", "quantity": 100, "sellable": 0, "t1_blocked": 100,
    }]
    assert result["projected"]["t1_blocked_quantity"] == 100

    blocked = evaluate_order_risk(package, [
        buy,
        {"order_id": "sell", "symbol": "000002.SZSE", "side": "sell",
         "qty": 100, "price_rmb": "5.00", "fee_cents": 0},
    ])
    assert blocked["status"] == "order_risk_blocked"
    assert "order_sell:sell_qty_exceeds_sellable" in blocked["blockers"]


def test_existing_rehearsal_package_is_consumable_without_treating_it_as_native():
    path = Path(
        r"D:\QuantificationData\trading\research\ths_bundle_rehearsal\20261011_order_reconcile_v1"
    ) / "closure_structured_check" / "structured_observation_package_v1.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = build_account_risk_view(payload)
    assert result["source_scope"] == "isolated_rehearsal"
    assert result["account_ready"] is False
    assert result["execution_admitted"] is False


def test_api_keeps_partial_native_receipt_primary_over_rehearsal(monkeypatch):
    import trading.api as api

    native = {
        "schema": "ths_structured_observation_package_v1",
        "status": "partial_structured_observation_package",
        "source_scope": "native_readonly_receipt",
        "source": "native_readonly_receipt",
        "account_observed": False,
        "orders_observed": False,
        "trades_observed": False,
        "cashflows_observed": False,
        "ready_for_ledger_reconciliation": False,
        "unknowns": ["native structured orders rows are absent"],
    }
    rehearsal = _package(source_scope="isolated_rehearsal")
    monkeypatch.setattr(api, "_latest_complete_structured_observation_package", lambda: rehearsal)
    monkeypatch.setattr(api, "_latest_native_structured_observation_package", lambda: native)
    monkeypatch.setattr(api, "_quote_observation_preview", lambda: {
        "status": "template_ready", "source": None, "observed_at": None,
        "trading_date": None, "price_count": 0, "missing_symbols": [],
        "canonical_inputs_ready": False,
    })
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {
        "status": "ledger_imported",
        "ledger_mutated": True,
    })

    result = api.operator_package_account_risk()

    assert result["source_scope"] == "native_readonly_receipt"
    assert result["primary_evidence"] == "native_readonly_receipt"
    assert result["account_ready"] is False
    assert result["execution_admitted"] is False
    assert result["native_observation"]["orders_observed"] is False
    assert result["isolated_rehearsal_view"]["source_scope"] == "isolated_rehearsal"
    assert result["same_ledger_reconciliation_status"] == "isolated_rehearsal_only"
    assert result["same_ledger_imported"] is False
    assert result["ledger_mutated"] is True


def test_order_risk_exposes_detailed_same_ledger_bundle(monkeypatch):
    import trading.api as api
    from trading.ths import account_risk

    package = _package()
    native = {
        "status": "partial_structured_observation_package",
        "source_scope": "native_readonly_receipt",
        "source_file": "native.json",
        "account_observed": False,
        "orders_observed": False,
        "trades_observed": False,
        "cashflows_observed": False,
        "ready_for_ledger_reconciliation": False,
        "unknowns": ["native structured orders rows are absent"],
    }
    journal = {"status": "reconciliation_required", "ledger_mutated": False}
    bundle = {
        "schema": "ths_same_ledger_bundle_reconciliation_v1",
        "status": "reconciliation_required",
        "issues": [{"order_id": "O1", "code": "fees_cents_mismatch"}],
        "rows": [],
        "summary": {"issue_count": 1},
        "ledger_mutated": False,
        "execution_admitted": False,
    }
    monkeypatch.setattr(api, "_operator_account_risk_inputs", lambda: {
        "package": package,
        "native_package": native,
    })
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: journal)
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: bundle)
    monkeypatch.setattr(account_risk, "evaluate_order_risk", lambda *args, **kwargs: {
        "status": "order_risk_ready_readonly",
        "blockers": [],
    })

    result = api.operator_package_account_order_risk(
        api.ThsAccountOrderRiskInput(orders=[])
    )

    assert result["same_ledger_bundle_reconciliation"] is bundle
    assert result["same_ledger_bundle_reconciliation"]["issues"][0]["code"] == "fees_cents_mismatch"
    assert result["same_ledger_reconciliation_status"] == "reconciliation_required"
    assert result["ledger_mutated"] is False
