import csv

import pytest

from trading.research.seat_hypothesis_signal_frame import (
    build_record,
    build_research_target,
    build_signal_frames,
    read_forward_labels,
)


FIELDS = [
    "trade_date", "symbol", "buy_seats", "sell_seats", "net_buy_cny",
    "net_buy_to_reported_turnover", "hypothesis_h1", "lag1_date",
    "stockdb_return_lag1", "baostock_return_lag1", "lag2_date",
    "stockdb_return_lag2", "baostock_return_lag2",
]


def _write_panel(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(symbol, day="2026-10-08", ratio="0.02", lag1="0.01", lag2="0.03"):
    return {
        "trade_date": day, "symbol": symbol, "buy_seats": "2", "sell_seats": "1",
        "net_buy_cny": "100000", "net_buy_to_reported_turnover": ratio,
        "hypothesis_h1": "True", "lag1_date": "2026-10-09",
        "stockdb_return_lag1": lag1, "baostock_return_lag1": lag1,
        "lag2_date": "2026-10-12", "stockdb_return_lag2": lag2,
        "baostock_return_lag2": lag2,
    }


def test_seat_hypothesis_enters_shared_signal_frame_and_selector(tmp_path):
    path = tmp_path / "labels.csv"
    _write_panel(path, [_row("000001.SZSE", ratio="0.02"), _row("600000.SSE", ratio="0.03")])

    frames = build_signal_frames(path, lag=1)
    frame = frames["2026-10-08"]
    assert [row["symbol"] for row in frame.ranked_candidates()] == ["600000.SSE", "000001.SZSE"]
    assert frame.context.mode.value == "historical_replay"
    assert dict(frame.signals[0].diagnostics)["label_available_lag1"] == 1.0
    assert dict(frame.signals[0].diagnostics)["label_source_agreement_lag1"] == 1.0

    target = build_research_target(frame, lag=1, holdings=2)
    assert target["weights"] == {"600000.SSE": 0.2, "000001.SZSE": 0.2}
    assert "future returns are diagnostics only" in target["signal_score_definition"]
    assert target["paper_orders_generated"] is False
    assert target["ledger_replay_completed"] is False


def test_record_keeps_missing_forward_labels_missing(tmp_path):
    path = tmp_path / "labels.csv"
    missing = _row("000001.SZSE", lag1="", lag2="")
    _write_panel(path, [missing])

    record = build_record(path, lag=2)
    diagnostic = record["signal_frames"]["2026-10-08"]["signals"][0]["diagnostics"]
    assert diagnostic["label_available_lag2"] == 0.0
    assert "stockdb_return_lag2" not in diagnostic
    assert record["future_labels_used_for_score"] is False
    assert record["engine_ledger_replay_completed"] is False


def test_rejects_formula_mismatch_and_duplicate_candidates(tmp_path):
    mismatch = tmp_path / "mismatch.csv"
    row = _row("000001.SZSE", ratio="0.009")
    _write_panel(mismatch, [row])
    with pytest.raises(ValueError, match="violates frozen formula"):
        read_forward_labels(mismatch, lag=1)

    duplicates = tmp_path / "duplicates.csv"
    _write_panel(duplicates, [_row("000001.SZSE"), _row("000001.SZSE")])
    with pytest.raises(ValueError, match="Duplicate seat candidate"):
        read_forward_labels(duplicates, lag=1)
