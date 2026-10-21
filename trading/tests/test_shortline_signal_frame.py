import json

import pytest

from trading.research.shortline_signal_frame import build_research_target, build_signal_frame, read_candidate_rows


def _write_candidates(path, rows):
    path.write_text(
        "symbol,date,stage,model_hit5_probability,candidate_rank_pct,paper_candidate_only\n"
        + "\n".join(",".join(str(row[key]) for key in (
            "symbol", "date", "stage", "model_hit5_probability", "candidate_rank_pct", "paper_candidate_only"
        )) for row in rows)
        + "\n",
        encoding="utf-8",
    )


def test_shortline_preview_enters_shared_signal_frame_only_for_marked_candidates(tmp_path):
    path = tmp_path / "candidates.csv"
    _write_candidates(path, [
        {"symbol": "003037.SZSE", "date": "2026-10-13", "stage": 1,
         "model_hit5_probability": 0.03, "candidate_rank_pct": 0.18, "paper_candidate_only": "True"},
        {"symbol": "000668.SZSE", "date": "2026-10-13", "stage": 1,
         "model_hit5_probability": 0.09, "candidate_rank_pct": 0.09, "paper_candidate_only": "True"},
        {"symbol": "002811.SZSE", "date": "2026-10-13", "stage": 2,
         "model_hit5_probability": 0.05, "candidate_rank_pct": 0.08, "paper_candidate_only": "False"},
    ])

    frame, record = build_signal_frame(path)

    assert [row["symbol"] for row in frame.ranked_candidates()] == ["000668.SZSE", "003037.SZSE"]
    assert record["signal_count"] == 2
    assert record["execution_admitted"] is False
    assert record["paper_orders_generated"] is False

    target = build_research_target(frame)
    assert target["weights"] == {"000668.SZSE": 0.2, "003037.SZSE": 0.2}
    assert target["weight_sum"] == 0.4
    assert target["target_holdings_generated"] is False


def test_shortline_preview_rejects_duplicate_marked_symbols(tmp_path):
    path = tmp_path / "duplicates.csv"
    _write_candidates(path, [
        {"symbol": "000668.SZSE", "date": "2026-10-13", "stage": 1,
         "model_hit5_probability": 0.09, "candidate_rank_pct": 0.09, "paper_candidate_only": "True"},
        {"symbol": "000668.SZSE", "date": "2026-10-13", "stage": 2,
         "model_hit5_probability": 0.08, "candidate_rank_pct": 0.10, "paper_candidate_only": "True"},
    ])
    with pytest.raises(ValueError, match="Duplicate shortline candidate symbol"):
        read_candidate_rows(path)
