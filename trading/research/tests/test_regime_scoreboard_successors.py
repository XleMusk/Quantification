"""Opt-in evidence intake checks; never update a registered history."""
from datetime import datetime
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research.regime_scoreboard import TZ, register_observation, score_observations
from trading.research.tests.test_regime_scoreboard import setup_files


def source_case(tmp_path, *, origin_close=100., empty=False):
    report, history, calendar, _, history_path = setup_files(tmp_path)
    output = tmp_path / "output"
    register_observation(report, output, history, calendar, observed_at=datetime(2026, 10, 15, 1, tzinfo=TZ))
    registration = next((output / "registrations").glob("*.json"))
    source = tmp_path / "successor"
    source.mkdir()
    days = ["2026-10-14", "2026-10-15", "2026-10-16"]
    raw = source / "overall.csv"
    pd.DataFrame([] if empty else [[days[0], "sh.000001", origin_close],
        [days[1], "sh.000001", 90.], [days[2], "sh.000001", 95.]],
        columns=["date", "code", "close"]).to_csv(raw, index=False)
    entry = {"target_id": "overall", "registered_symbol": "sh000001", "provider_code": "sh.000001",
        "started_at": "2026-10-16T18:00:00+08:00", "completed_at": "2026-10-16T18:00:01+08:00",
        "status": "source_response_incomplete_preserved" if empty else "exact_dates_returned",
        "error_code": "0", "file": str(raw), "sha256": sha256(raw.read_bytes()).hexdigest(),
        "rows": 0 if empty else 3, "fields": ["date", "code", "close"], "dates": [] if empty else days}
    receipt = {"schema": "registered_market_proxy_bounded_successor_source_v1", "status": "completed_bounded_source_queries",
        "registration": str(registration), "registration_sha256": sha256(registration.read_bytes()).hexdigest(),
        "source_start": days[0], "source_end": days[-1], "expected_dates": days, "max_queries": 1,
        "query_method": "query_history_k_data_plus", "fields": "date,code,close", "frequency": "d", "adjustflag": "3",
        "started_at": "2026-10-16T18:00:00+08:00", "finished_at": "2026-10-16T18:00:02+08:00",
        "old_caches_modified": False, "model_refit": False, "execution_admitted": False,
        "pit_verified": False, "source_independently_verified": False, "entries": [entry]}
    path = source / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return output, path, raw, history_path, registration


def test_successor_opt_in_scores_exact_next_session_without_rewriting_frozen_sources(tmp_path):
    out, source, _, history, registration = source_case(tmp_path)
    history_before, registration_before = history.read_bytes(), registration.read_bytes()
    now = datetime(2026, 10, 16, 19, tzinfo=TZ)
    default = score_observations(out, as_of="2026-10-16", observed_at=now)
    assert all(row["status"] == "pending" for row in default["rows"])
    result = score_observations(out, as_of="2026-10-16", observed_at=now, successor_source=source)
    one, five = result["rows"]
    assert one["status"] == "scored" and one["correct"] and one["actual_return"] == pytest.approx(-.1)
    assert five["status"] == "pending" and five["target_session"] == "2026-10-21"
    assert result["summary_by_horizon"]["1"]["unique_origin_days"] == 1
    assert history.read_bytes() == history_before and registration.read_bytes() == registration_before


def test_successor_origin_precision_conflict_is_excluded_but_direction_is_preserved(tmp_path):
    out, source, _, _, _ = source_case(tmp_path, origin_close=100.0001)
    result = score_observations(out, as_of="2026-10-16", observed_at=datetime(2026, 10, 16, 19, tzinfo=TZ), successor_source=source)
    assert all(row["status"] == "blocked" for row in result["rows"])
    one = result["rows"][0]
    assert one["reason"] == "blocked_registered_origin_precision_conflict"
    assert one["actual_return"] is None and not one["included_in_prospective_metrics"]
    assert one["excluded_source_diagnostic"]["source_actual_signal"] == "看空"
    assert result["summary_by_horizon"]["1"]["prospective_scored"] == 0


@pytest.mark.parametrize("mutation,match", [("future", "future"), ("hash", "hash"), ("mapping", "every registered target")])
def test_successor_rejects_future_clock_changed_bytes_or_target_mapping(tmp_path, mutation, match):
    out, source, _, _, _ = source_case(tmp_path)
    record = json.loads(source.read_text())
    if mutation == "future":
        record["finished_at"] = "2026-10-17T18:00:00+08:00"
    elif mutation == "hash":
        record["entries"][0]["sha256"] = "0" * 64
    else:
        record["entries"][0]["registered_symbol"] = "sz399006"
    source.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        score_observations(out, observed_at=datetime(2026, 10, 16, 19, tzinfo=TZ), successor_source=source)
    assert not (out / "scoreboard.json").exists()


def test_empty_success_is_pending_unknown_never_a_zero_return(tmp_path):
    out, source, _, _, _ = source_case(tmp_path, empty=True)
    result = score_observations(out, observed_at=datetime(2026, 10, 16, 19, tzinfo=TZ), successor_source=source)
    assert all(row["status"] == "pending" and row["actual_return"] is None for row in result["rows"])
    assert result["rows"][0]["successor_source"]["status"] == "empty_success_no_prices"
    assert result["summary_by_horizon"]["1"]["prospective_scored"] == 0
