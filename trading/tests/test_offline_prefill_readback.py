import copy

from trading.research.offline_strategy_plan import validate_prefill_readback_template


def _template():
    return {
        "schema": "ths_prefill_readback_template_v1",
        "execution_day": "2026-10-12",
        "strategy_id": "low_volatility_252_20",
        "client_interacted": False,
        "rows": [{
            "sequence": 1,
            "symbol": "000001.SZSE",
            "side": "buy",
            "readback_applicable": True,
            "expected": {"symbol_field": "000001", "price_field": "11.59", "qty_field": "100"},
            "readback": {"symbol_field": None, "price_field": None, "qty_field": None},
        }],
    }


def test_batch_readback_starts_pending():
    result = validate_prefill_readback_template(_template())
    assert result["status"] == "pending_authorized_client_readback"
    assert result["readback_complete"] is False
    assert result["pending_field_count"] == 3


def test_batch_readback_accepts_exact_fields():
    payload = _template()
    payload["rows"][0]["readback"] = {"symbol_field": "000001", "price_field": "11.59", "qty_field": "100"}
    result = validate_prefill_readback_template(payload)
    assert result["status"] == "readback_verified"
    assert result["readback_complete"] is True
    assert result["verified_row_count"] == 1


def test_batch_readback_rejects_field_mismatch():
    payload = copy.deepcopy(_template())
    payload["rows"][0]["readback"] = {"symbol_field": "000001", "price_field": "11.60", "qty_field": "100"}
    result = validate_prefill_readback_template(payload)
    assert result["status"] == "readback_mismatch"
    assert result["mismatch_field_count"] == 1
    assert result["execution_admitted"] is False
