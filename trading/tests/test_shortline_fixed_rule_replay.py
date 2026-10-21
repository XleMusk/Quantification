import json

import pandas as pd
import pytest

from trading.research.shortline_fixed_rule_replay import load_fixed_rule_predictions, run


def _snapshot(path):
    path.mkdir()
    dates = ["20260105", "20260106", "20260107", "20260108", "20260109"]
    rows = []
    for code, base in (("000001", 10.0), ("000002", 20.0), ("000003", 30.0)):
        for i, day in enumerate(dates):
            close = base + i * 0.1
            rows.append({
                "code": code, "date": int(day), "open": close, "high": close + 0.2,
                "low": close - 0.1, "close": close, "volume": 100000,
                "amount": close * 100000, "pct_chg": 1.0 if i else 0.0,
                "is_st": False, "pre_close": close / (1.01 if i else 1.0),
            })
    pd.DataFrame(rows).to_parquet(path / "2026-01.parquet", index=False)


def test_fixed_rule_uses_signal_fields_identity_and_counts_without_future_labels(tmp_path):
    emotion = tmp_path / "emotion.csv"
    pd.DataFrame([
        {"symbol": "000001.SZSE", "date": "2026-01-05", "stage": 1,
         "amount_rel20_pre": 0.8, "market_breadth": 0.1},
        {"symbol": "000002.SZSE", "date": "2026-01-05", "stage": 1,
         "amount_rel20_pre": 1.1, "market_breadth": 0.1},
        {"symbol": "000003.SZSE", "date": "2026-01-06", "stage": 1,
         "amount_rel20_pre": 0.9, "market_breadth": -0.1},
    ]).to_csv(emotion, index=False)
    annotation = tmp_path / "events.parquet"
    pd.DataFrame([
        {"event_id": "000001.SZSE|2026-01-05|s1", "symbol": "000001.SZSE", "date": "2026-01-05", "stage": 1,
         "hit_4": 1, "hit_5": 1, "market_row_count": 10, "market_up_count": 3, "market_down_count": 2},
        {"event_id": "000002.SZSE|2026-01-05|s1", "symbol": "000002.SZSE", "date": "2026-01-05", "stage": 1,
         "hit_4": 0, "hit_5": 0, "market_row_count": 10, "market_up_count": 3, "market_down_count": 2},
        {"event_id": "000003.SZSE|2026-01-06|s1", "symbol": "000003.SZSE", "date": "2026-01-06", "stage": 1,
         "hit_4": 0, "hit_5": 0, "market_row_count": 10, "market_up_count": 2, "market_down_count": 3},
    ]).to_parquet(annotation, index=False)
    predictions, info = load_fixed_rule_predictions(emotion, annotation, start="2026-01-01", end="2026-01-09")
    assert predictions.symbol.tolist() == ["000001.SZSE"]
    assert predictions.model_hit5_probability.tolist() == [1.0]
    assert info["future_labels_loaded"] is False
    assert info["future_labels_used_for_selection"] is False
    assert info["signal_sidecar_quality"]["checked_dates"] == 2


def test_fixed_rule_replay_uses_shared_panel_and_keeps_gates_closed(tmp_path):
    emotion = tmp_path / "emotion.csv"
    pd.DataFrame([
        {"symbol": "000001.SZSE", "date": "2026-01-05", "stage": 1,
         "amount_rel20_pre": 0.8, "market_breadth": 0.1},
        {"symbol": "000002.SZSE", "date": "2026-01-05", "stage": 1,
         "amount_rel20_pre": 0.9, "market_breadth": 0.1},
    ]).to_csv(emotion, index=False)
    annotation = tmp_path / "events.parquet"
    pd.DataFrame([
        {"event_id": f"{code}.SZSE|2026-01-05|s1", "symbol": f"{code}.SZSE", "date": "2026-01-05", "stage": 1,
         "hit_4": 1, "hit_5": 1, "market_row_count": 10, "market_up_count": 3, "market_down_count": 2}
        for code in ("000001", "000002")
    ]).to_parquet(annotation, index=False)
    snapshot = tmp_path / "snapshot"
    _snapshot(snapshot)
    out = tmp_path / "out"
    path, result = run(emotion, annotation, snapshot, out, start="2026-01-05", end="2026-01-09",
                        top_per_day=1, holdings=1, max_symbols=1, symbol_cap_method="first_seen")
    assert result["engine_ledger_replay_completed"] is True
    assert result["strategy_id"] == "shortline_fixed_emotion_rule_v1"
    assert result["future_labels_used_for_score"] is False
    assert result["execution_admitted"] is False
    assert result["ledger_mutated"] is False
    assert (out / "fixed_rule_predictions.csv").is_file()
    audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    assert audit["checks"]["fixed_rule_signal_day_fields_only"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["execution_admitted"] is False


def test_fixed_rule_refuses_known_contaminated_sidecar_before_loading_annotation(tmp_path):
    legacy = tmp_path / "emotion_event_features_v1.csv"
    with pytest.raises(ValueError, match="Known contaminated"):
        load_fixed_rule_predictions(legacy, tmp_path / "missing.parquet")
