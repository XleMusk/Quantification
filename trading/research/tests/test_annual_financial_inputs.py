import pandas as pd
import pytest

from trading.research.annual_financial_inputs import join_period, select_asof


def statement(codes, **columns):
    return pd.DataFrame({"SECURITY_CODE": codes, "SECUCODE": [code + ".SH" for code in codes],
        "REPORT_DATE": "2025-12-31 00:00:00", "NOTICE_DATE": "2026-07-25 00:00:00",
        "available_to_system_at": "2026-10-04T22:05:00+08:00", "INDUSTRY_NAME": "source-industry",
        "REPORT_TYPE_CODE": "001", "DATA_STATE": "2", **columns})


def test_outer_join_retains_missing_statement_zero_and_profit_scope():
    tables = {
        "balance": statement(["600000", "600001"], TOTAL_ASSETS=[100, 200],
                             TOTAL_LIABILITIES=[60, 120], TOTAL_EQUITY=[40, 80], MONETARYFUNDS=[0, None]),
        "income": statement(["600000"], TOTAL_OPERATE_INCOME=[100], PARENT_NETPROFIT=[0],
                            OPERATE_COST=[None], TOTAL_PROFIT=[3], INCOME_TAX=[1]),
        "cashflow": statement(["600001"], NETCASH_OPERATE=[-5]),
    }
    panel = join_period(tables, "2025-12-31").set_index("security_code")
    assert panel.loc["600000", "parent_net_profit"] == 0
    assert panel.loc["600000", "parent_net_profit_status"] == "observed_numeric"
    assert pd.isna(panel.loc["600000", "total_net_profit"])
    assert panel.loc["600000", "total_net_profit_status"] == "field_absent_from_source"
    assert panel.loc["600000", "operating_cost_status"] == "source_null"
    assert panel.loc["600000", "operating_cashflow_status"] == "statement_not_observed"
    assert pd.isna(panel.loc["600000", "operating_cashflow"])
    assert panel.loc["600001", "income_statement_observed"] == False
    assert panel.loc["600001", "parent_net_profit_status"] == "statement_not_observed"
    assert panel.loc["600001", "operating_cashflow"] == -5
    assert panel.loc["600000", "balance_notice_at"] == "2026-07-25 00:00:00"
    assert panel.loc["600000", "balance_retrieved_at"] == "2026-10-04T22:05:00+08:00"
    assert not panel.pit_verified.any() and not panel.execution_admitted.any()


def test_historical_notice_cannot_backdate_retrieval_availability():
    tables = {
        "balance": statement(["600000"], TOTAL_ASSETS=[100], TOTAL_LIABILITIES=[60], TOTAL_EQUITY=[40], MONETARYFUNDS=[0]),
        "income": statement(["600000"], TOTAL_OPERATE_INCOME=[100], PARENT_NETPROFIT=[0],
                            OPERATE_COST=[50], TOTAL_PROFIT=[3], INCOME_TAX=[1]),
        "cashflow": statement(["600000"], NETCASH_OPERATE=[10]),
    }
    panel = join_period(tables, "2025-12-31")
    fields = ["total_assets", "operating_cashflow", "revenue", "operating_cost"]
    past, reasons = select_asof(panel, "2025-12-31", "2026-09-30T15:00:00+08:00", fields)
    assert past.empty and reasons.not_received_by_asof.all()
    now, _ = select_asof(panel, "2025-12-31", "2026-10-04T22:05:00+08:00", fields)
    assert len(now) == 1 and not now.execution_admitted.any()
    with pytest.raises(ValueError, match="timezone"):
        select_asof(panel, "2025-12-31", "2026-10-04 22:05:00", fields)
