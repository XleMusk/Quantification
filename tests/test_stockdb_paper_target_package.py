import json

import pytest

from trading.research.stockdb_paper_target_package import build_paper_target_package


def _target():
    return {
        "strategy_id": "unit-factor",
        "decision_day": "2026-10-13",
        "research_only": True,
        "execution_admitted": False,
        "weights": {"600000.SSE": 0.1, "000001.SZSE": 0.1},
        "signal_frame": {"signals": [
            {"security": {"code": "600000", "exchange": "SSE"}, "diagnostics": {"rank": 1}},
            {"security": {"code": "000001", "exchange": "SZSE"}, "diagnostics": {"rank": 2}},
        ]},
    }


def _quotes():
    return {
        "schema": "ths_raw_quote_observation_v1", "price_basis": "raw_rmb",
        "observed_at": "2026-10-14T12:02:00+08:00", "source": "unit",
        "prices": {"600000.SSE": {"price_rmb": "10.00"}, "000001.SZSE": {"price_rmb": "10.00"}},
    }


def test_build_paper_target_package_is_local_and_unadmitted():
    package = build_paper_target_package(_target(), _quotes(), 10000,
                                         execution_day="2026-10-14", settlement_day="2026-10-15")
    assert len(package["target_holdings"]) == len(package["order_plan"]) == 2
    assert all(row["execution_admitted"] is False for row in package["ths_prefill_payloads"])
    assert package["result"]["execution_admitted"] is False
    assert package["result"]["submitted"] is False
    assert package["projected_account"]["positions"]["600000.SSE"] == 100


def test_package_rejects_non_raw_quote():
    quotes = _quotes()
    quotes["price_basis"] = "adjusted"
    try:
        build_paper_target_package(_target(), quotes, 10000,
                                   execution_day="2026-10-14", settlement_day="2026-10-15")
    except ValueError as exc:
        assert "raw RMB" in str(exc)
    else:
        raise AssertionError("adjusted quotes must not enter the paper package")


def test_unsupported_route_remains_projected_but_has_no_ths_fields(tmp_path):
    target, quotes = _target(), _quotes()
    target["weights"]["301109.SZSE"] = target["weights"].pop("000001.SZSE")
    target["signal_frame"]["signals"][1]["security"]["code"] = "301109"
    quotes["prices"]["301109.SZSE"] = quotes["prices"].pop("000001.SZSE")
    package = build_paper_target_package(target, quotes, 10000,
                                         execution_day="2026-10-14", settlement_day="2026-10-15",
                                         output_dir=tmp_path)
    blocked = package["ths_prefill_payloads"][1]
    assert blocked["qty_field"] == blocked["price_field"] == blocked["symbol_field"] == ""
    assert package["projected_account"]["positions"]["301109.SZSE"] == 100
    assert package["result"]["route_blocked_count"] == 1
    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert saved == package["result"]
    assert (tmp_path / "target_holdings.csv").exists()


@pytest.mark.parametrize("field,value", [("execution_admitted", True), ("research_only", False)])
def test_execution_permission_cannot_be_promoted(field, value):
    target = _target()
    target[field] = value
    with pytest.raises(ValueError, match="research-only"):
        build_paper_target_package(target, _quotes(), 10000,
                                   execution_day="2026-10-14", settlement_day="2026-10-15")
