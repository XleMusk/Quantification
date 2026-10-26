import json
from pathlib import Path

import pytest

from trading.research.factor_replay_comparison import compare_replays, write_artifacts


def _write_replay(root: Path, *, net_return: float = 0.1, admitted: bool = False,
                  sessions: int = 3, start: str = "2024-01-01") -> Path:
    root.mkdir(parents=True)
    days = [f"2024-01-0{i}" for i in range(1, sessions + 1)]
    (root / "daily.csv").write_text(
        "date,equity\n" + "\n".join(f"{day},{100000 + i * 1000}" for i, day in enumerate(days, 1)) + "\n",
        encoding="utf-8",
    )
    (root / "orders.csv").write_text("id\norder-1\n", encoding="utf-8")
    (root / "fills.csv").write_text("id\nfill-1\nfill-2\n", encoding="utf-8")
    record = {
        "status": "completed_research_only_mature_factor_replay",
        "strategy_id": "mature_factor_balanced_ridge_h20",
        "start_date": start,
        "end_date": days[-1],
        "replay_sessions": sessions,
        "symbols": 2,
        "selected_event_rows": 4,
        "blocked_order_event_count": 1,
        "execution_admitted": admitted,
        "strategy_validated": False,
        "ledger_mutated": False,
        "pit_verified": False,
        "metrics": {
            "net_return": net_return,
            "max_drawdown": -0.05,
            "sharpe_zero_rf_252": 0.5,
            "fees": 10.0,
            "turnover_value": 1000.0,
            "stale_valuation_days": 0,
        },
    }
    (root / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def test_compare_replays_aligns_equity_and_keeps_no_adoption(tmp_path):
    baseline = _write_replay(tmp_path / "baseline", net_return=0.10)
    variant = _write_replay(tmp_path / "variant", net_return=0.12)
    result = compare_replays(baseline, variant)

    assert result["status"] == "COMPLETED_RESEARCH_ONLY_HORIZON_COMPARISON"
    assert result["machine_status"]["execution_admitted"] is False
    assert result["machine_status"]["decision"] == "NO_ADOPTION_RESEARCH_ONLY"
    assert result["metric_delta_variant_minus_baseline"]["net_return"] == pytest.approx(0.02)
    assert result["aligned_daily"]["days"] == 3
    assert result["comparison"]["baseline"]["orders"] == 1
    assert result["comparison"]["variant"]["fills"] == 2


def test_compare_replays_rejects_admitted_input(tmp_path):
    baseline = _write_replay(tmp_path / "baseline")
    variant = _write_replay(tmp_path / "variant", admitted=True)
    with pytest.raises(ValueError, match="execution_admitted=true"):
        compare_replays(baseline, variant)


def test_compare_replays_rejects_window_mismatch(tmp_path):
    baseline = _write_replay(tmp_path / "baseline", sessions=3)
    variant = _write_replay(tmp_path / "variant", sessions=2)
    with pytest.raises(ValueError, match="Replay windows differ at end_date"):
        compare_replays(baseline, variant)


def test_write_artifacts_is_research_only(tmp_path):
    baseline = _write_replay(tmp_path / "baseline")
    variant = _write_replay(tmp_path / "variant", net_return=0.12)
    result = compare_replays(baseline, variant)
    receipt = tmp_path / "out" / "receipt.json"
    report = tmp_path / "out" / "REPORT.md"
    write_artifacts(result, receipt, report)

    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved["machine_status"]["paper_allowed"] is False
    assert "NO_TRADE" in report.read_text(encoding="utf-8")
