import json
import os
from datetime import datetime

import pandas as pd
import pytest

from trading.research.regime_scoreboard import (
    TZ, register_observation, score_observations,
)


def setup_files(tmp_path, *, generation="20261014_160000", origin="2026-10-14"):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "calendar_id": "test", "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}))
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09",
            "2026-10-12", "2026-10-13", "2026-10-14"]
    prices = [90., 91., 92., 93., 94., 96., 98., 100.]
    frame = pd.DataFrame({"date": days, "close": prices, "target_id": "overall", "symbol": "sh000001"})
    path = history_dir / "overall__sh000001.csv"
    frame.to_csv(path, index=False)
    target = {"target_id": "overall", "symbol": "sh000001", "latest_date": origin,
        "prediction_date": None, "actual_ret_pct": None, "correct": None, "signal": "看空",
        "score": -.2, "params": {"threshold": .03}, "train_accuracy": .5,
        "diagnostics": {"pred_base_date": origin, "close": float(frame.loc[frame.date == origin, "close"].iloc[0])}}
    report = tmp_path / f"market_regime_{generation}.json"
    report.write_text(json.dumps({"regime": {"label": "退潮防守"}, "targets": [target]}), encoding="utf-8")
    stamp = datetime.strptime(generation, "%Y%m%d_%H%M%S").replace(tzinfo=TZ).timestamp()
    os.utime(report, (stamp, stamp))
    return report, history_dir, calendar, frame, path


def test_registration_pending_is_frozen_and_does_not_use_future_baseline_labels(tmp_path):
    report, history, calendar, frame, path = setup_files(tmp_path)
    # A future cache tail must not flip the rising trailing-only majority.
    future = pd.DataFrame({"date": ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"],
        "close": [80., 70., 60., 50., 40.], "target_id": "overall", "symbol": "sh000001"})
    pd.concat([frame, future]).to_csv(path, index=False)
    output = tmp_path / "output"
    registered = register_observation(report, output, history, calendar,
        observed_at=datetime(2026, 10, 15, 1, 0, tzinfo=TZ))
    assert registered["targets"][0]["prospective_eligible"]
    assert registered["targets"][0]["baselines"]["1"]["signal"] == "看多"
    before = (output / "registrations").glob("*.json")
    frozen_path = next(before)
    frozen = frozen_path.read_bytes()
    again = register_observation(report, output, history, calendar,
        observed_at=datetime(2026, 10, 16, 16, 0, tzinfo=TZ))
    assert again["reused"] and frozen_path.read_bytes() == frozen
    score = score_observations(output, observed_at=datetime(2026, 10, 15, 8, 0, tzinfo=TZ))
    assert score["closed_as_of"] == "2026-10-14"
    assert {r["status"] for r in score["rows"]} == {"pending"}
    assert [r["target_session"] for r in score["rows"]] == ["2026-10-15", "2026-10-21"]
    assert score["summary_by_horizon"]["1"]["signal_accuracy"] is None
    assert not score["execution_admitted"] and not score["model_refit"]


def test_exact_successor_gap_keeps_pending_and_late_registration_excluded(tmp_path):
    report, history, calendar, frame, path = setup_files(tmp_path)
    output = tmp_path / "output"
    registered = register_observation(report, output, history, calendar,
        observed_at=datetime(2026, 10, 15, 16, 0, tzinfo=TZ))
    assert not registered["targets"][0]["prospective_eligible"]
    # Only the fifth successor exists. No positional shift may substitute it for day1.
    extension = pd.DataFrame({"date": ["2026-10-21"], "close": [80.], "target_id": "overall", "symbol": "sh000001"})
    pd.concat([frame, extension]).to_csv(path, index=False)
    result = score_observations(output, observed_at=datetime(2026, 10, 21, 16, 0, tzinfo=TZ))
    assert all(r["status"] == "pending" for r in result["rows"])
    extension = pd.DataFrame({"date": ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"],
        "close": [90., 88., 86., 84., 80.], "target_id": "overall", "symbol": "sh000001"})
    pd.concat([frame, extension]).to_csv(path, index=False)
    result = score_observations(output, observed_at=datetime(2026, 10, 21, 16, 0, tzinfo=TZ))
    assert all(r["status"] == "scored" and r["correct"] for r in result["rows"])
    assert result["summary_by_horizon"]["1"]["prospective_scored"] == 0
    assert result["summary_by_horizon"]["1"]["retrospective_scored_excluded"] == 1


def test_matured_outcome_uses_saved_signal_and_rejects_origin_revisions(tmp_path):
    report, history, calendar, frame, path = setup_files(tmp_path)
    output = tmp_path / "output"
    register_observation(report, output, history, calendar,
        observed_at=datetime(2026, 10, 15, 1, 0, tzinfo=TZ))
    extra = pd.DataFrame({"date": ["2026-10-15"], "close": [90.], "target_id": "overall", "symbol": "sh000001"})
    frame = pd.concat([frame, extra])
    frame.to_csv(path, index=False)
    score = score_observations(output, observed_at=datetime(2026, 10, 15, 16, 0, tzinfo=TZ))
    metrics = score["summary_by_horizon"]["1"]
    assert metrics["signal_accuracy"] == 1 and metrics["majority_accuracy"] == 0
    assert metrics["nochange_accuracy"] == 0 and metrics["unique_origin_days"] == 1
    frame.loc[frame.date == "2026-10-14", "close"] = 101.
    frame.to_csv(path, index=False)
    score = score_observations(output, observed_at=datetime(2026, 10, 15, 16, 0, tzinfo=TZ))
    assert all(r["reason"] == "registered_origin_close_revised" for r in score["rows"])
    assert score["summary_by_horizon"]["1"]["prospective_scored"] == 0


def test_report_labels_and_same_origin_replacements_cannot_be_prospective(tmp_path):
    report, history, calendar, _, _ = setup_files(tmp_path)
    output = tmp_path / "output"
    now = datetime(2026, 10, 15, 1, 0, tzinfo=TZ)
    register_observation(report, output, history, calendar, observed_at=now)
    payload = json.loads(report.read_text())
    payload["targets"][0]["score"] = -.7
    report.write_text(json.dumps(payload))
    os.utime(report, (now.timestamp(), now.timestamp()))
    with pytest.raises(ValueError, match="already frozen"):
        register_observation(report, output, history, calendar, observed_at=now)
    payload["targets"][0]["correct"] = True
    report.write_text(json.dumps(payload))
    os.utime(report, (now.timestamp(), now.timestamp()))
    with pytest.raises(ValueError, match="reconstructed"):
        register_observation(report, output, history, calendar, observed_at=now)
