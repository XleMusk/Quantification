import pytest

from trading.research.financial_factor_registry import F01, REGISTRY, get_definition


def test_f01_is_the_only_active_registered_factor_and_has_fixed_consumers():
    assert F01.purpose == "ACTIVE"
    assert [item.factor_id for item in REGISTRY.values() if item.purpose == "ACTIVE"] == ["F01"]
    assert get_definition("F01", consumer="f01_signal") == F01
    assert get_definition("cash_support").purpose == "DIAGNOSTIC"


def test_registry_rejects_arbitrary_or_unauthorized_factor_execution():
    with pytest.raises(KeyError):
        get_definition("eval('bad')")
    with pytest.raises(PermissionError):
        get_definition("cash_support", consumer="f01_signal")
