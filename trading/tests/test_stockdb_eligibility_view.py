from pathlib import Path

import pandas as pd
import pytest

from trading.research.stockdb_eligibility_view import apply_dated_eligibility, load_eligibility_view


def _scores():
    return pd.DataFrame([
        {"date": "2026-01-30", "symbol": "000001.SZSE", "score": 0.3},
        {"date": "2026-01-30", "symbol": "600000.SSE", "score": 0.2},
    ])


def _bars():
    return pd.DataFrame([
        {"date": "2026-01-30", "symbol": "000001.SZSE", "open": 10, "close": 11,
         "volume": 100, "amount": 20_000_000, "is_st": False},
        {"date": "2026-01-30", "symbol": "600000.SSE", "open": 10, "close": 11,
         "volume": 100, "amount": 1_000_000, "is_st": False},
    ])


def test_dated_eligibility_is_explicit_and_liquidity_bounded():
    view = apply_dated_eligibility(_scores(), _bars(), min_amount_cny=10_000_000)
    assert view["eligible_for_research"].tolist() == [True, False]
    assert view["liquid"].tolist() == [True, False]
    assert view["observed_bar"].all()


def test_missing_bar_is_not_eligible():
    view = apply_dated_eligibility(_scores(), _bars().iloc[:1], min_amount_cny=0)
    row = view.loc[view["symbol"].eq("600000.SSE")].iloc[0]
    assert not bool(row["observed_bar"])
    assert not bool(row["eligible_for_research"])


def test_duplicate_raw_bar_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        apply_dated_eligibility(_scores(), pd.concat([_bars(), _bars()], ignore_index=True))


def test_loader_rejects_non_research_view(tmp_path: Path):
    artifact = tmp_path / "rows.parquet"
    pd.DataFrame([{"date": "2026-01-30", "symbol": "000001.SZSE",
                   "score": 0.3, "eligible_for_research": True}]).to_parquet(artifact, index=False)
    run = tmp_path / "run.json"
    run.write_text('{"status":"completed_stockdb_dated_eligibility_view",'
                   '"execution_admitted":true,"pit_verified":false,'
                   f'"result":{{"eligibility_path":"{artifact.as_posix()}"}}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="research-only"):
        load_eligibility_view(run)
