from trading.research import operator_account_risk_view


def _common_inputs():
    return {
        "package": {
            "source_scope": "isolated_rehearsal",
            "status": "isolated_rehearsal_only",
            "source_file": "rehearsal.json",
            "source_paths": {"account": "rehearsal.json"},
        },
        "complete_package": {"source_scope": "isolated_rehearsal"},
        "native_package": {
            "status": "missing",
            "source_scope": "missing",
            "unknowns": ["account_export_missing"],
        },
        "quote_preview": {"status": "missing", "price_count": 0},
        "quote_observation": None,
        "journal_reconciliation": {"ledger_mutated": False},
        "same_ledger_bundle_reconciliation": {"status": "isolated_rehearsal_only"},
        "same_ledger_compat_fields": lambda _bundle, _journal: {"same_ledger_imported": False},
    }


def test_account_view_keeps_rehearsal_and_execution_boundaries_explicit():
    values = _common_inputs()
    calls = []

    def builder(package, **kwargs):
        calls.append((package, kwargs))
        return {
            "status": "isolated_rehearsal_only",
            "source_scope": package["source_scope"],
            "ledger_binding": {"ready_for_ledger_reconciliation": False, "ledger_reconciled": False},
        }

    result = operator_account_risk_view.build_account_risk_view(
        **values, account_risk_builder=builder
    )
    assert len(calls) == 2
    assert result["primary_evidence"] == "isolated_rehearsal"
    assert result["native_observation"]["unknowns"] == ["account_export_missing"]
    assert result["ledger_mutated"] is False
    assert result["next_action"] == "capture a complete persistent THS account/orders/trades/cashflows export"


def test_order_view_uses_same_package_without_mutating_ledger():
    values = _common_inputs()
    values.pop("complete_package")
    values.pop("quote_preview")
    values.pop("quote_observation")
    result = operator_account_risk_view.build_order_risk_view(
        **values,
        orders=[{"symbol": "000001.SZSE", "side": "buy", "qty": 100}],
        quote_observation=None,
        order_risk_builder=lambda package, orders, **kwargs: {
            "status": "paper_only",
            "source_scope": package["source_scope"],
            "order_count": len(orders),
        },
    )
    assert result["order_count"] == 1
    assert result["primary_evidence"] == "isolated_rehearsal"
    assert result["ledger_mutated"] is False


def test_api_routes_delegate_to_account_risk_view(monkeypatch):
    from trading import api

    expected = {"schema": "delegated"}
    monkeypatch.setattr(api, "_operator_account_risk_inputs", lambda: {
        "package": {}, "complete_package": {}, "native_package": {},
        "quote_preview": {}, "quote_observation": None,
    })
    monkeypatch.setattr(api, "_ths_journal_reconciliation_view", lambda: {})
    monkeypatch.setattr(api, "_same_ledger_bundle_reconciliation_view", lambda: {})
    monkeypatch.setattr(api, "_same_ledger_compat_fields", lambda *_: {})
    monkeypatch.setattr(api.operator_account_risk_view, "build_account_risk_view", lambda **_: expected)
    monkeypatch.setattr(api.operator_account_risk_view, "build_order_risk_view", lambda **_: expected)
    assert api.operator_package_account_risk() == expected
    assert api.operator_package_account_order_risk(api.ThsAccountOrderRiskInput(orders=[])) == expected
