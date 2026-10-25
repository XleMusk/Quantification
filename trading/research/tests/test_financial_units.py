from decimal import Decimal

import pytest

from trading.research.financial_units import normalize_amount


def test_unit_conversion_preserves_decimal_value_and_raw_provenance():
    million = normalize_amount("1.25", "million_yuan")
    yuan = normalize_amount("1250000", "yuan")
    assert million["value"] == yuan["value"] == Decimal("1250000")
    assert million["raw_value"] == Decimal("1.25") and million["raw_unit"] == "million_yuan"
    assert million["conversion_verified"] is True


@pytest.mark.parametrize("value,unit", [("NaN", "yuan"), ("1", "unknown"), (True, "yuan")])
def test_unknown_or_nonfinite_units_never_default_to_yuan(value, unit):
    with pytest.raises(ValueError):
        normalize_amount(value, unit)
