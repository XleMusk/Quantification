import json

import numpy as np
import pandas as pd

from trading.research import stockdb_alpha158_lgbm_signal_frame as module


def _diagnostic(tmp_path):
    scores = pd.DataFrame({
        "score_seed17": [0.1, 0.3, 0.2],
        "score_seed29": [0.2, 0.4, 0.1],
        "score_seed43": [0.15, 0.35, 0.12],
        "rank_mean": [0.2, 0.9, 0.5],
    }, index=pd.MultiIndex.from_tuples([
        (pd.Timestamp("2026-10-13"), "000001.SZSE"),
        (pd.Timestamp("2026-10-13"), "600000.SSE"),
        (pd.Timestamp("2026-10-13"), "000002.SZSE"),
    ], names=["date", "symbol"]))
    score_path = tmp_path / "scores.parquet"
    scores.to_parquet(score_path)
    diagnostic = tmp_path / "diagnostic.json"
    diagnostic.write_text(json.dumps({
        "status": "completed_stockdb_alpha158_lgbm_diagnostic",
        "date": "2026-10-13",
        "score_path": str(score_path),
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": False,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
    }), encoding="utf-8")
    return diagnostic


def test_signal_frame_is_replay_only_and_top_sorted(tmp_path):
    signal, meta = module.read_diagnostic(_diagnostic(tmp_path), top=2)
    assert signal.context.mode.value == "historical_replay"
    assert [row["symbol"] for row in signal.ranked_candidates()] == ["600000.SSE", "000002.SZSE"]
    assert meta["signal_count"] == 2


def test_target_preview_uses_shared_selector_and_keeps_gates_closed(tmp_path):
    signal, _ = module.read_diagnostic(_diagnostic(tmp_path), top=3)
    target = module.build_target(signal, holdings=2)
    assert target["weight_sum"] == 0.4
    assert target["target_holdings_generated"] is False
    assert target["paper_orders_generated"] is False
    assert target["execution_admitted"] is False
