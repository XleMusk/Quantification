import pytest

from trading.research.ths_prefill_readback_view import build_view


def _payload(route_status="route_available"):
    return {
        "execution_day": "2026-10-27",
        "rows": [{"sequence": 1, "symbol": "000001.SZSE", "price_rmb": "4.10", "qty": "100",
                   "route_status": route_status, "route_reason": "manual_only"}],
    }


def test_readback_normalizes_form_values_without_submission():
    result = build_view(strategy_id="fixed", sequence=1, symbol="000001.SZSE", price="4.1", qty="100",
                        payload=_payload())

    assert result["status"] == "readback_verified"
    assert result["normalized_readback"] == {"symbol": "000001", "price": "4.10", "qty": "100"}
    assert result["submitted"] is False
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False


def test_readback_mismatch_is_explicit():
    result = build_view(strategy_id="fixed", sequence=1, symbol="000001", price="4.11", qty="200",
                        payload=_payload())

    assert result["status"] == "readback_mismatch"
    assert {row["field"] for row in result["mismatches"]} == {"price", "qty"}


def test_unavailable_route_does_not_compare_or_submit():
    result = build_view(strategy_id="fixed", sequence=1, symbol="000001", price="4.10", qty="100",
                        payload=_payload("route_unavailable"))

    assert result["status"] == "route_unavailable"
    assert result["expected"] is None
    assert result["submitted"] is False


def test_missing_sequence_is_explicit_error():
    with pytest.raises(KeyError, match="sequence"):
        build_view(strategy_id="fixed", sequence=2, symbol="000001", price="4.10", qty="100",
                   payload=_payload())
