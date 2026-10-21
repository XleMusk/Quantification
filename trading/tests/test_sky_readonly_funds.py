import pytest

from trading.ths.sky_readonly_funds import parse_funds_tree


ALIAS = "模拟炒股-UI**54"


def tree():
    return '\n'.join([
        'Window: "网上股票交易系统5.0", App: xiadan.exe.',
        '1 组合框 (settable, string) Value: 模拟炒股-UI**54 ID: 2322',
        '2 文本 40.98 ID: 1012', '3 文本 0.00 ID: 1013',
        '4 文本 40.98 ID: 1016', '5 文本 0.00 ID: 1017',
        '6 文本 117045.40 ID: 1014', '7 文本 117086.38 ID: 1015'])


def test_realistic_funds_keep_units_and_do_not_claim_complete_account_or_freshness():
    result = parse_funds_tree(tree(), expected_account=ALIAS)
    assert result["cash_balance_cents"] == 4098
    assert result["reported_total_assets_cents"] == 11708638
    assert all(result[key] is False for key in ("holdings_complete", "orders_complete", "fills_complete",
               "cashflows_complete", "fees_known", "freshness_verified", "ready_for_prefill", "execution_admitted"))


@pytest.mark.parametrize("damaged", [tree().replace("117086.38", "117086.39"),
                                    tree() + '\n8 文本 40.98 ID: 1012',
                                    tree().replace("UI**54", "UI**55")])
def test_ambiguous_wrong_account_and_one_cent_asset_conflict_are_rejected(damaged):
    with pytest.raises(ValueError):
        parse_funds_tree(damaged, expected_account=ALIAS)
