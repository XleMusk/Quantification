from trading.research.f2_bridge_vertical_slice import run_bridge_vertical_slice
from trading.research.f2_fact_bridge import BLOCKED as BRIDGE_BLOCKED, READY as BRIDGE_READY, SCHEMA as BRIDGE_SCHEMA
from trading.research.f2_vertical_slice import BLOCKED, COMPLETED


DAY = "2024-05-01"


def _typed(symbol, metric, value, start, end):
    return {
        "fact_id": f"{symbol}:{metric}:{end}",
        "issuer_id": symbol,
        "metric_id": metric,
        "period_start": start,
        "period_end": end,
        "period_kind": "single_quarter",
        "scope_group": "consolidated_parent",
        "currency": "CNY",
        "unit": "yuan",
        "value": str(value),
        "available_at": {"start": "2024-04-30T12:00:00+08:00",
                          "end": "2024-04-30T12:00:00+08:00"},
        "status": "OBSERVED",
    }


def _inputs():
    symbol = "000001.SZSE"
    bridge = {
        "schema": BRIDGE_SCHEMA,
        "status": BRIDGE_READY,
        "decision_day": DAY,
        "rows": [{
            "symbol": symbol,
            "status": BRIDGE_READY,
            "historical_evidence": {
                "status": "HISTORICAL_EVIDENCE_READY",
                "pit_asof_safe": True,
                "evidence_kind": "historical_archive",
                "reason_codes": [],
            },
            "facts": [
                _typed(symbol, "parent_net_profit", 1200000, "2024-01-01", "2024-03-31"),
                _typed(symbol, "total_assets", 50000000, "2024-01-01", "2024-03-31"),
            ],
        }],
    }
    prior = {symbol: _typed(symbol, "parent_net_profit", 900000,
                            "2023-10-01", "2023-12-31")}
    quotes = {symbol: 10.0}
    return bridge, prior, quotes


def test_ready_bridge_with_explicit_prior_reaches_shared_paper_path(tmp_path):
    bridge, prior, quotes = _inputs()
    result = run_bridge_vertical_slice(
        bridge, prior, quotes,
        execution_day="2024-05-02", settlement_day="2024-05-03",
        initial_cash=100000, output_dir=tmp_path,
    )
    assert result["status"] == COMPLETED
    assert result["execution_admitted"] is False
    assert result["account_replay"] is True
    assert result["source_event_count"] == 1
    assert result["vertical_slice"]["package"]["paper_order_count"] == 1
    assert (tmp_path / "bridge_vertical_slice_receipt.json").is_file()


def test_blocked_bridge_never_creates_factor_or_paper_order(tmp_path):
    bridge, prior, quotes = _inputs()
    bridge["status"] = BRIDGE_BLOCKED
    result = run_bridge_vertical_slice(
        bridge, prior, quotes,
        execution_day="2024-05-02", settlement_day="2024-05-03",
        initial_cash=100000, output_dir=tmp_path,
    )
    assert result["status"] == BLOCKED
    assert result["reason"] == "bridge_not_ready"
    assert result["account_replay"] is False
    assert "vertical_slice" not in result


def test_missing_explicit_prior_blocks_before_signal(tmp_path):
    bridge, _, quotes = _inputs()
    result = run_bridge_vertical_slice(
        bridge, {}, quotes,
        execution_day="2024-05-02", settlement_day="2024-05-03",
        initial_cash=100000, output_dir=tmp_path,
    )
    assert result["status"] == BLOCKED
    assert result["reason"] == "explicit_join_inputs_missing"
    assert result["details"]["missing"][0]["reason"] == "explicit_prior_or_required_current_fact_missing"
    assert result["account_replay"] is False
