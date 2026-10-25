import json

import pandas as pd

from trading.research.f01_readiness import audit


def test_annual_only_input_is_blocked_for_single_quarter_f01(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    pd.DataFrame({
        "symbol": ["600000.SSE"], "report_day": ["2025-12-31"],
        "parent_net_profit": [100], "parent_net_profit_status": ["observed_numeric"],
        "total_assets": [1000], "total_assets_status": ["observed_numeric"],
    }).to_parquet(root / "annual_inputs.parquet", index=False)
    (root / "result.json").write_text(json.dumps({"status": "completed_annual_input_consumer",
        "pit_verified": False, "units_verified": False}), encoding="utf-8")
    result = audit(root)
    assert result["status"] == "BLOCKED_DATA"
    assert "single_quarter_inputs_missing" in result["blockers"]
    assert result["execution_admitted"] is False
