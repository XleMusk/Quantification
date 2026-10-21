from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.research import prospective_research as intake
from trading.research.low_risk import portfolio


def test_intake_does_not_relabel_old_days_or_skip_terminal_month_boundary():
    days = ["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-30", "2026-11-02"]
    assert intake.incoming_dates(days[:2], days[0], "2026-10-04", datetime(2026, 10, 8, 17)) == []
    assert intake.incoming_dates(days[:3], days[0], "2026-10-04", datetime(2026, 10, 9, 15)) == [days[1]]
    assert intake.incoming_dates(days, days[2], "2026-10-04", datetime(2026, 11, 2, 17)) == [days[3]]
    with pytest.raises(ValueError, match="pre-registration"):
        intake.incoming_dates(days, days[0], "2026-10-08", datetime(2026, 10, 9, 17))


def write_days(path, days, closes):
    schema = pa.schema([("date", pa.string()), ("code", pa.string()), ("close", pa.float64())])
    with pq.ParquetWriter(path, schema) as writer:
        for day, close in zip(days, closes):
            writer.write_table(pa.Table.from_pydict({"date": [day], "code": ["600000"], "close": [close]}, schema=schema))


def test_daily_append_accepts_same_prefix_and_rejects_revisions(tmp_path):
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    write_days(old, ["2026-09-29", "2026-09-30"], [10., 11.])
    write_days(new, ["2026-09-29", "2026-09-30", "2026-10-08"], [10., 11., 12.])
    intake.unchanged_daily_prefix(old, new, "2026-09-30")
    write_days(new, ["2026-09-29", "2026-09-30", "2026-10-08"], [10., 11.1, 12.])
    with pytest.raises(ValueError, match="repair separately"):
        intake.unchanged_daily_prefix(old, new, "2026-09-30")


def prepare_suite(tmp_path, monkeypatch, cutoff):
    root = tmp_path / "suite"
    root.mkdir()
    price = tmp_path / "prices"
    price.mkdir()
    intake.write_json(price / "result.json", {"status": "research_reconstruction_completed", "cutoff": cutoff})
    cases = [{"signal": signal, "recipe": None, "config": {}, "start": "2026-01-05",
              "anchor_day": "2026-09-30", "anchor_equity": 140000., "source_account": "original",
              "checkpoint": "original", "status": "waiting_for_new_complete_data", "forward_sessions": 0}
             for signal in (*intake.RULES, intake.MODEL)]
    protocol = {"id": intake.ID, "name": "unit", "signals": [*intake.RULES, intake.MODEL],
                "registered_at": "2026-10-04T02:48:00+08:00", "frozen_day": "2026-10-04",
                "terminal_session_policy": intake.TERMINAL_POLICY, "cases": deepcopy(cases),
                "default_inputs": {key: str(price) for key in ("price_view", "event_view", "reference_view", "listing_master", "native_view")}}
    head = {"id": intake.ID, "name": "unit", "execution_admitted": False,
            "status": "waiting_for_new_complete_data", "cases": cases, "forward_sessions": 0}
    intake.write_json(root / "protocol.json", protocol)
    intake.publish_head(root, head)
    monkeypatch.setattr(intake, "folder", lambda name: root)
    monkeypatch.setattr(intake, "ROOT", tmp_path)
    def no_feed(*args):
        raise AssertionError("No feed/model/account work should run in this input branch")
    monkeypatch.setattr(intake, "BulkResearchFeed", no_feed)
    return root, cases


def test_no_new_data_does_not_open_feed_or_change_account_cursor(tmp_path, monkeypatch):
    root, cases = prepare_suite(tmp_path, monkeypatch, "2026-09-30")
    _, result = intake.advance_suite("unit", current=datetime(2026, 10, 4, 17))
    assert result["status"] == "waiting_for_new_complete_data"
    assert result["cases"] == cases and result["forward_sessions"] == 0
    assert not (root / "batches").exists()


def test_future_snapshot_failure_is_recorded_without_account_reset(tmp_path, monkeypatch):
    root, cases = prepare_suite(tmp_path, monkeypatch, "2026-10-05")
    _, result = intake.advance_suite("unit", current=datetime(2026, 10, 4, 17))
    assert result["status"] == "failed_preparation" and "future dates" in result["error"]
    assert result["cases"] == cases and result["execution_admitted"] is False
    failure = json.loads((next((root / "batches").iterdir()) / "run.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed_preparation" and failure["results"] == []


def continuing_suite(tmp_path, monkeypatch):
    root, cases = prepare_suite(tmp_path, monkeypatch, "2026-11-02")
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    for case in cases:
        case.update(config=portfolio().to_dict(), completed_sessions=1, manifest={})
        if case["signal"] in intake.RULES:
            case["recipe"] = "low_vol_control" if case["signal"] == intake.RULES[0] else "low_vol_half_momentum_control"
    protocol.update(cases=deepcopy(cases), training_directions={})
    intake.write_json(root / "protocol.json", protocol)
    head = json.loads((root / "head.json").read_text(encoding="utf-8"))
    head["cases"] = deepcopy(cases)
    intake.publish_head(root, head)
    days = ["2026-09-30", "2026-10-08", "2026-10-30", "2026-11-02"]
    base = SimpleNamespace(calendar=days, manifest={}, close=lambda: None)
    monkeypatch.setattr(intake, "BulkResearchFeed", lambda *args: base)
    monkeypatch.setattr(intake, "validate_extension", lambda *args: None)
    monkeypatch.setattr(intake, "low_risk_features", lambda *args: {})
    monkeypatch.setattr(intake, "portfolio_metrics", lambda rows, equity: {"observed_sessions": len(rows)})
    shared = pd.DataFrame({"RSQR30": [.2], "WVMA20": [.1]}, index=pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-10-30"), "600000.SSE")], names=["date", "symbol"]))
    native_calls, replay_calls, checkpoints = [], [], []
    def read_native(feed, view, dates):
        assert feed is base
        native_calls.append(dates)
        return shared
    monkeypatch.setattr(intake, "read_native_monthends", read_native)
    def family(feed, values, native, recipe, directions, end):
        assert native is shared and directions == protocol["training_directions"]
        return SimpleNamespace(case=intake.RULES[0] if recipe == "low_vol_control" else intake.RULES[1])
    monkeypatch.setattr(intake, "FamilyFeed", family)
    monkeypatch.setattr(intake, "ScoreFeed", lambda *args: SimpleNamespace(case=intake.MODEL))
    def checkpoint(path, config, dates):
        assert config == portfolio()
        checkpoints.append(str(path))
        saved = dates if Path(path).name in (*intake.RULES, intake.MODEL) else dates[:1]
        return {"state": {"day": saved[-1]}, "daily": [{"date": day} for day in saved]}
    monkeypatch.setattr(intake, "load_replay_checkpoint", checkpoint)
    def replay(feed, config, dates, destination, checkpoint):
        replay_calls.append((feed.case, deepcopy(checkpoint)))
        return {"status": "completed", "completed_sessions": len(dates)}
    monkeypatch.setattr(intake, "baseline_ledger_result", replay)
    return root, shared, native_calls, replay_calls, checkpoints


def test_model_preparation_failure_does_not_block_fixed_common_pool_rules(tmp_path, monkeypatch):
    root, shared, native_calls, replay_calls, checkpoints = continuing_suite(tmp_path, monkeypatch)
    frozen = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    def unavailable(*args):
        raise FileNotFoundError("Frozen LightGBM weights unavailable")
    monkeypatch.setattr(intake, "predict_new_native", unavailable)
    point = datetime(2026, 11, 2, 17)
    _, result = intake.advance_suite("unit", current=point)
    assert result["status"] == "partial" and result["forward_sessions"] == 0
    assert native_calls == [["2026-10-30"]]
    assert [name for name, _ in replay_calls] == list(intake.RULES)
    assert all(case["forward_sessions"] == 2 and case["last_day"] == "2026-10-30" for case in result["cases"][:2])
    model = result["cases"][-1]
    assert model["status"] == "failed_preparation" and model["checkpoint"] == "original"
    assert model["completed_sessions"] == 1 and model["anchor_equity"] == 140000.
    batch = json.loads((Path(result["last_batch"]) / "run.json").read_text(encoding="utf-8"))
    assert batch["status"] == "partial" and list(batch["preparation_errors"]) == [intake.MODEL]
    assert not (Path(result["last_batch"]) / "historical_scores.parquet").exists()
    assert json.loads((root / "protocol.json").read_text(encoding="utf-8")) == frozen

    # Repairing the model resumes its original account without replaying the completed rules.
    before = len(replay_calls)
    monkeypatch.setattr(intake, "predict_new_native", lambda *args: (pd.DataFrame({"score": [1.]}), shared))
    _, recovered = intake.advance_suite("unit", current=point)
    assert [name for name, _ in replay_calls[before:]] == [intake.MODEL]
    assert replay_calls[-1][1]["state"]["day"] == "2026-09-30"
    assert native_calls == [["2026-10-30"]]
    assert recovered["status"] == "completed" and recovered["forward_sessions"] == 2
    assert "error" not in recovered["cases"][-1]


def test_rule_native_pool_failure_does_not_block_model_or_widen_rule_pool(tmp_path, monkeypatch):
    root, shared, native_calls, replay_calls, checkpoints = continuing_suite(tmp_path, monkeypatch)
    monkeypatch.setattr(intake, "predict_new_native", lambda *args: (pd.DataFrame({"score": [1.]}), shared))
    def unavailable(*args):
        raise ValueError("Missing native month-end batch")
    monkeypatch.setattr(intake, "read_native_monthends", unavailable)
    _, result = intake.advance_suite("unit", current=datetime(2026, 11, 2, 17))
    assert result["status"] == "partial" and result["forward_sessions"] == 0
    assert [name for name, _ in replay_calls] == [intake.MODEL]
    assert all(case["status"] == "failed_preparation" and case["checkpoint"] == "original"
               and case["forward_sessions"] == 0 for case in result["cases"][:2])
    assert result["cases"][-1]["forward_sessions"] == 2
    assert result["execution_admitted"] is False


def test_valuation_only_rule_extension_does_not_require_native_decision_batch(tmp_path, monkeypatch):
    root, shared, native_calls, replay_calls, checkpoints = continuing_suite(tmp_path, monkeypatch)
    base = SimpleNamespace(calendar=["2026-09-30", "2026-10-08", "2026-10-09"], manifest={}, close=lambda: None)
    monkeypatch.setattr(intake, "BulkResearchFeed", lambda *args: base)
    monkeypatch.setattr(intake, "predict_new_native", lambda *args: (pd.DataFrame({"score": [1.]}), shared))
    def family(feed, values, native, recipe, directions, end):
        assert native.empty and native.columns.tolist() == list(intake.NATIVE_COLUMNS)
        assert native.index.names == ["date", "symbol"]
        return SimpleNamespace(case=intake.RULES[0] if recipe == "low_vol_control" else intake.RULES[1])
    monkeypatch.setattr(intake, "FamilyFeed", family)
    _, result = intake.advance_suite("unit", current=datetime(2026, 11, 2, 17))
    assert native_calls == []
    assert [name for name, _ in replay_calls] == [*intake.RULES, intake.MODEL]
    assert result["status"] == "completed" and result["forward_sessions"] == 1
