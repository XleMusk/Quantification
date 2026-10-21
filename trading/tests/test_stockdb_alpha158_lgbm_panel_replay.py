import json

import pandas as pd

from trading.research import stockdb_alpha158_lgbm_panel_replay as module


def _batch(tmp_path):
    scores = pd.DataFrame([
        {"date": "2026-09-01", "symbol": "000001.SZSE", "rank_mean": 0.9, "score_seed17": 1, "score_seed29": 1, "score_seed43": 1},
        {"date": "2026-09-01", "symbol": "000002.SZSE", "rank_mean": 0.8, "score_seed17": 0, "score_seed29": 0, "score_seed43": 0},
        {"date": "2026-09-01", "symbol": "000003.SZSE", "rank_mean": 0.7, "score_seed17": -1, "score_seed29": -1, "score_seed43": -1},
    ])
    score_path = tmp_path / "scores.parquet"
    scores.to_parquet(score_path, index=False)
    run = tmp_path / "result.json"
    run.write_text(json.dumps({
        "status": "completed_research_only_dated_score_batch",
        "score_path": str(score_path), "model_refit": False,
        "future_labels_used_for_score": False, "execution_admitted": False,
        "strategy_validated": False, "ledger_mutated": False,
        "target_holdings_generated": False, "paper_orders_generated": False,
        "ths_payload_generated": False,
    }), encoding="utf-8")
    return run


def test_load_scores_selects_score_only_top_rows(tmp_path):
    rows, meta = module.load_scores(_batch(tmp_path), top_per_day=2)
    assert rows["symbol"].tolist() == ["000001.SZSE", "000002.SZSE"]
    assert meta["future_labels_used_for_selection"] is False


def test_first_seen_cap_is_deterministic():
    rows = pd.DataFrame({"date": ["2026-09-01", "2026-09-02", "2026-09-02"],
                         "symbol": ["000002.SZSE", "000001.SZSE", "000003.SZSE"],
                         "score": [0.9, 0.8, 0.7]})
    capped, dropped = module._first_seen_cap(rows, 2)
    assert capped["symbol"].unique().tolist() == ["000002.SZSE", "000001.SZSE"]
    assert dropped == 1
