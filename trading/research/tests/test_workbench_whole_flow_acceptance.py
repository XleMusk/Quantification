from __future__ import annotations

from trading.research.workbench_whole_flow_acceptance import build_acceptance


def _lifecycle():
    return {
        "schema": "quantification_workbench_job_lifecycle_receipt_v1",
        "api": {
            "stages": ["submitted", "compute", "result", "download"],
            "execution_admitted": False,
        },
    }


def _browser():
    return {
        "schema": "quantification_workbench_job_lifecycle_browser_receipt_v1",
        "flow": [{"stage": stage} for stage in ["submit", "compute", "result", "download"]],
        "execution_admitted": False,
        "live_trading": False,
        "broker_or_ths_interacted": False,
    }


def test_whole_flow_separates_local_engineering_from_market_scope():
    result = build_acceptance(_lifecycle(), _browser())

    assert result["status"] == "ENGINEERING_VERIFIED_MARKET_SCOPE_BLOCKED"
    assert result["engineering_scope"]["stage_contract_ok"] is True
    assert result["engineering_scope"]["browser_observed_contract"] == [
        "submitted", "compute", "result", "download"
    ]
    assert result["engineering_scope"]["status"] == "VERIFIED_LOCAL_RESEARCH_ONLY"
    assert result["market_account_scope"]["status"] == "BLOCKED_EXTERNAL_EVIDENCE"
    assert result["execution_admitted"] is False


def test_whole_flow_does_not_upgrade_missing_download_stage():
    browser = _browser()
    browser["flow"] = browser["flow"][:-1]

    result = build_acceptance(_lifecycle(), browser)

    assert result["status"] == "ACCEPTANCE_INCOMPLETE"
    assert result["engineering_scope"]["status"] == "INCOMPLETE"
