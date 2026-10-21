import json

import pandas as pd

from trading.research import stockdb_action_gate_diagnostic as module


def test_action_gate_keeps_adjustments_out_of_rank_route(tmp_path):
    ranking_dir = tmp_path / "ranking"
    ranking_dir.mkdir()
    ranking_csv = ranking_dir / "diagnostic_ranking.csv"
    pd.DataFrame([{"rank": 1, "symbol": "000001.SZSE", "score": 1.0}]).to_csv(ranking_csv, index=False)
    ranking = ranking_dir / "result.json"
    ranking.write_text(json.dumps({
        "status": "completed_stockdb_factor_rank_diagnostic",
        "execution_admitted": False,
        "ranking_path": str(ranking_csv),
    }), encoding="utf-8")
    probe = tmp_path / "probe"
    probe.mkdir()
    pd.DataFrame([{"code": "sz.000002", "dividOperateDate": "2026-10-13",
                   "foreAdjustFactor": "1", "backAdjustFactor": "1", "adjustFactor": "1"}]).to_csv(
        probe / "2026-10-13_adjustment.csv", index=False)
    receipt = probe / "receipt.json"
    receipt.write_text(json.dumps({
        "status": "completed_probe", "execution_admitted": False, "days": ["2026-10-13"],
        "entries": [{"kind": "adjustment", "file": "2026-10-13_adjustment.csv"}],
    }), encoding="utf-8")
    _, result = module.run(ranking, receipt, output=tmp_path / "out")
    assert result["ranked_action_count"] == 0
    assert result["action_semantics_verified"] is False
    assert result["paper_orders_generated"] is False

