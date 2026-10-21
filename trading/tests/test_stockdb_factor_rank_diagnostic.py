import json

import pandas as pd

from trading.research import stockdb_factor_rank_diagnostic as module


def test_rank_diagnostic_requires_current_gates_and_writes_no_orders(tmp_path):
    score_path = tmp_path / "scores.parquet"
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-10-13"), "000001.SZSE"),
         (pd.Timestamp("2026-10-13"), "000002.SZSE")],
        names=["date", "symbol"],
    )
    pd.DataFrame({"score": [2.0, 1.0]}, index=index).to_parquet(score_path)
    score_run = tmp_path / "score-run.json"
    score_run.write_text(json.dumps({
        "status": "completed_stockdb_factor_score_diagnostic",
        "execution_admitted": False,
        "cross_lineage_diagnostic": True,
        "result": {"score_path": str(score_path)},
    }), encoding="utf-8")
    artifacts = tmp_path / "elig-artifacts"
    artifacts.mkdir()
    pd.DataFrame([
        {"symbol": "sz.000001", "median_amount_20": 3e7, "listing_age_ok": True,
         "liquidity_ok": True, "is_st": False, "compatibility_known": True, "eligible_proxy": True},
        {"symbol": "sz.000002", "median_amount_20": 3e7, "listing_age_ok": True,
         "liquidity_ok": True, "is_st": False, "compatibility_known": True, "eligible_proxy": True},
    ]).to_csv(artifacts / "latest_eligibility_observation.csv", index=False)
    elig_path = tmp_path / "elig.json"
    elig_path.write_text(json.dumps({
        "status": "completed_research_only_eligibility_compatibility",
        "execution_admitted": False, "latest_session": "2026-10-13",
        "artifacts": str(artifacts),
    }), encoding="utf-8")
    trad_path = tmp_path / "trad.json"
    trad_path.write_text(json.dumps({
        "status": "completed_research_only_tradability_compare",
        "execution_admitted": False, "date": "2026-10-13",
        "tradestatus_disagreement_rows": 0, "is_st_disagreement_rows": 0,
    }), encoding="utf-8")
    out, record = module.run(score_run, elig_path, trad_path, top=2, output=tmp_path / "out")
    assert record["diagnostic_ranking_generated"] is True
    assert record["target_holdings_generated"] is False
    assert pd.read_csv(record["ranking_path"]).symbol.tolist() == ["000001.SZSE", "000002.SZSE"]
    assert json.loads(out.read_text(encoding="utf-8"))["paper_orders_generated"] is False

