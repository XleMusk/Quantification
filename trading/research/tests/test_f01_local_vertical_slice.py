from decimal import Decimal

from trading.research.f01_local_vertical_slice import parse_prior_parent_profit


def test_prior_parent_profit_parser_preserves_parentheses_and_commas():
    assert parse_prior_parent_profit("1.归属 187,599,319.75 44,726,227.50") == Decimal("44726227.50")
    assert parse_prior_parent_profit("净利润 (361,967,511.52) 1,445,811,341.87") == Decimal("1445811341.87")
