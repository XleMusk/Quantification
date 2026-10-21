import json

import pandas as pd

from trading.research import stockdb_factor_signal_frame as module


def _rank(tmp_path):
    ranking = tmp_path / "ranking.csv"
    pd.DataFrame({"rank": [1, 2, 3], "symbol": ["600000.SSE", "000001.SZSE", "000002.SZSE"],
                  "score": [0.9, 0.8, 0.7]}).to_csv(ranking, index=False)
    path = tmp_path / "rank.json"
    path.write_text(json.dumps({
        "status": "completed_stockdb_factor_rank_diagnostic", "date": "2026-10-13",
        "ranked_count": 3, "ranking_path": str(ranking),
        "execution_admitted": False, "ranking_generated": False,
        "target_holdings_generated": False, "paper_orders_generated": False,
        "ths_payload_generated": False,
    }), encoding="utf-8")
    return path


def test_factor_rank_maps_to_replay_signal_frame(tmp_path):
    signal, meta = module.read_rank(_rank(tmp_path), top=3)
    assert signal.context.mode.value == "historical_replay"
    assert signal.ranked_candidates()[0]["symbol"] == "600000.SSE"
    assert meta["signal_count"] == 3


def test_factor_target_preview_keeps_execution_closed(tmp_path):
    signal, _ = module.read_rank(_rank(tmp_path), top=3)
    target = module.build_target(signal, holdings=2)
    assert target["weight_sum"] == 0.4
    assert target["target_holdings_generated"] is False
    assert target["execution_admitted"] is False
