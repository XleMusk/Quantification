import json

import pandas as pd

from trading.research.shortline_panel_replay import load_raw_bars, replay, write_result


def test_shortline_panel_replay_uses_shared_engine_and_keeps_gate_closed(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    dates = ["20251222", "20251223", "20251224", "20251225", "20251226",
             "20251229", "20251230", "20251231", "20260102", "20260105", "20260106", "20260107"]
    rows = []
    for code, base in (("000001", 10.0), ("000002", 20.0)):
        for index, day in enumerate(dates):
            close = base + index * 0.1
            rows.append({
                "code": code, "date": int(day), "open": close, "high": close + 0.2,
                "low": close - 0.1, "close": close, "volume": 100000,
                "amount": close * 100000, "pct_chg": 1.0 if index else 0.0,
                "is_st": False, "pre_close": close / (1.01 if index else 1.0),
            })
    pd.DataFrame(rows).to_parquet(snapshot / "2026-01_0.parquet", index=False)
    predictions = tmp_path / "predictions.csv"
    pd.DataFrame([
        {"event_id": "000001.SZSE|2026-01-02|s1", "symbol": "000001.SZSE", "date": "2026-01-02",
         "stage": 1, "hit_4": 0, "hit_5": 1, "model_hit5_probability": 0.2,
         "paper_candidate_only": True},
        {"event_id": "000002.SZSE|2026-01-02|s1", "symbol": "000002.SZSE", "date": "2026-01-02",
         "stage": 1, "hit_4": 1, "hit_5": 0, "model_hit5_probability": 0.1,
         "paper_candidate_only": True},
    ]).to_csv(predictions, index=False)

    result = replay(predictions, snapshot, start="2025-12-22", end="2026-01-07",
                    top_per_day=1, holdings=1, initial_cash=100000, max_symbols=1,
                    symbol_cap_method="first_seen")
    assert result["engine_ledger_replay_completed"] is True
    assert result["selected_event_rows"] == 1
    assert result["max_symbols"] == 1
    assert result["symbol_cap_method"] == "first_seen"
    assert result["future_labels_used_for_score"] is False
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert result["strategy_validated"] is False
    assert len(result["orders"]) >= 1

    out = write_result(result, tmp_path / "out")
    audit = json.loads((tmp_path / "out" / "audit.json").read_text(encoding="utf-8"))
    assert audit["checks"]["shared_engine_ledger"] is True
    assert json.loads(open(out["result"], encoding="utf-8").read())["execution_admitted"] is False


def test_shortline_panel_replay_accepts_stockdb_daily_snapshot(tmp_path):
    snapshot = tmp_path / "stockdb_snapshot"
    snapshot.mkdir()
    rows = []
    for index, day in enumerate(["20260102", "20260105", "20260106", "20260107"]):
        close = 10.0 + index * 0.1
        rows.append({
            "symbol": "000001.SZSE", "date": day, "code": "000001",
            "open": close, "high": close + 0.2, "low": close - 0.1,
            "close": close, "pre_close": close / (1.01 if index else 1.0),
            "volume": 100000, "amount": close * 100000,
        })
    pd.DataFrame(rows).to_csv(snapshot / "daily.csv", index=False)
    bars = load_raw_bars(snapshot, {"000001.SZSE"}, start="2026-01-02", end="2026-01-07")
    assert bars["date"].tolist() == ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    assert bars["preclose_for_limits"].iloc[1] == 10.0
    predictions = tmp_path / "predictions.csv"
    pd.DataFrame([{
        "event_id": "000001.SZSE|2026-01-05|s1", "symbol": "000001.SZSE",
        "date": "2026-01-05", "stage": 1, "model_hit5_probability": 0.2,
        "paper_candidate_only": True,
    }]).to_csv(predictions, index=False)

    result = replay(predictions, snapshot, start="2026-01-02", end="2026-01-07",
                    top_per_day=1, holdings=1, initial_cash=100000)
    assert result["engine_ledger_replay_completed"] is True
    assert result["selected_event_rows"] == 1
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
