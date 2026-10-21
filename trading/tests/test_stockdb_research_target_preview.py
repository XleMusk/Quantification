import json

import pandas as pd

from trading.research import stockdb_research_target_preview as module


def test_research_target_preview_is_weight_only(tmp_path):
    rank_dir = tmp_path / "rank"
    rank_dir.mkdir()
    rank_csv = rank_dir / "rank.csv"
    pd.DataFrame([
        {"rank": 1, "symbol": "000001.SZSE", "score": 2.0},
        {"rank": 2, "symbol": "000002.SZSE", "score": 1.0},
    ]).to_csv(rank_csv, index=False)
    rank_json = rank_dir / "run.json"
    rank_json.write_text(json.dumps({
        "status": "completed_stockdb_factor_rank_diagnostic", "execution_admitted": False,
        "date": "2026-10-13", "ranking_path": str(rank_csv),
    }), encoding="utf-8")
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({
        "status": "completed_stockdb_action_gate_diagnostic", "execution_admitted": False,
        "ranked_action_count": 0,
    }), encoding="utf-8")
    out, result = module.run(rank_json, gate, top=2, output=tmp_path / "out")
    assert result["research_target_generated"] is True
    assert result["target_holdings_generated"] is False
    assert abs(result["weight_sum"] - 1.0) < 1e-9
    assert json.loads(out.read_text(encoding="utf-8"))["paper_orders_generated"] is False

