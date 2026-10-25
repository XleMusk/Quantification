import json

import pandas as pd
import pytest

from trading.research import stockdb_factor_monthly_ridge_replay as module


def _run(tmp_path, *, unsafe=False):
    scores = pd.DataFrame(
        {"score": [0.9, 0.8, 0.7]},
        index=pd.MultiIndex.from_tuples(
            [("2026-09-30", "000002.SZSE"), ("2026-09-30", "000001.SZSE"),
             ("2026-10-13", "000001.SZSE")], names=["date", "symbol"]),
    )
    score_path = tmp_path / "scores.parquet"
    scores.to_parquet(score_path)
    run = tmp_path / "run.json"
    flags = {flag: False for flag in module.REQUIRED_SAFE_FLAGS}
    if unsafe:
        flags["target_holdings_generated"] = True
    run.write_text(json.dumps({
        "status": "completed_stockdb_factor_monthly_ridge_diagnostic",
        "result": {"score_path": str(score_path)}, **flags,
    }), encoding="utf-8")
    return run


def test_load_scores_is_score_only_and_deterministic(tmp_path):
    rows, meta = module.load_scores(_run(tmp_path), top_per_day=1)
    assert rows[["date", "symbol"]].to_records(index=False).tolist() == [
        ("2026-09-30", "000002.SZSE"), ("2026-10-13", "000001.SZSE")
    ]
    assert meta["future_labels_used_for_selection"] is False
    assert meta["model_refit"] is False


def test_first_seen_cap_and_research_flag_validation(tmp_path):
    rows = pd.DataFrame({"date": ["2026-09-30", "2026-10-13", "2026-10-13"],
                         "symbol": ["000002.SZSE", "000001.SZSE", "000003.SZSE"],
                         "score": [0.9, 0.8, 0.7]})
    capped, dropped = module._first_seen_cap(rows, 2)
    assert capped["symbol"].unique().tolist() == ["000002.SZSE", "000001.SZSE"]
    assert dropped == 1
    with pytest.raises(ValueError, match="non-research flags"):
        module.load_scores(_run(tmp_path, unsafe=True))


def test_feed_rejects_non_replay_and_last_score_day():
    bars = pd.DataFrame([
        {"date": "2026-09-30", "symbol": "000001.SZSE", "open": 10, "high": 10,
         "low": 10, "close": 10, "preclose_for_limits": 10, "volume": 100,
         "amount": 1000, "is_st": 0},
        {"date": "2026-10-01", "symbol": "000001.SZSE", "open": 10, "high": 10,
         "low": 10, "close": 10, "preclose_for_limits": 10, "volume": 100,
         "amount": 1000, "is_st": 0},
    ])
    candidates = pd.DataFrame([{"date": "2026-09-30", "symbol": "000001.SZSE", "score": 1.0}])
    feed = module.MatureFactorFeed(bars, candidates)
    config = module._config(100000, 1)
    context = module.RunContext("test", module.TradingMode.REPLAY)
    assert feed.signal_frame("2026-10-01", config, context).reason == "no_monthly_ridge_score_or_next_session"
    with pytest.raises(ValueError, match="replay-only"):
        feed.signal_frame("2026-09-30", config, module.RunContext("test", module.TradingMode.PAPER))
