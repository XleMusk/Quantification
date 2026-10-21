from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from trading.research.family_pipeline import RAW_COLUMNS, evaluate_fold, fit_fold, raw_components, report
from trading.research.family_ridge import FAMILIES, family_matrix


def inputs():
    calendar = pd.bdate_range("2024-01-02", "2025-04-30")
    months = calendar.to_series().groupby(calendar.to_period("M")).max().to_list()
    symbols = pd.Index([f"S{i:03}" for i in range(101)], name="symbol")
    rank = np.arange(101) / 101 - .5
    raw = pd.concat({day: pd.DataFrame({"risk": rank, "momentum": -rank,
                                       "RSQR30": rank, "WVMA20": -rank}, index=symbols)
                     for day in months}, names=["date", "symbol"]).sort_index()[list(RAW_COLUMNS)]
    labels = pd.DataFrame(np.tile(rank / 5, (len(calendar), 1)), index=calendar, columns=symbols)
    return raw, labels, calendar


def test_native_directions_and_family_selection_ignore_unmatured_and_future_labels():
    raw, labels, calendar = inputs()
    day = pd.Timestamp("2025-01-31")
    current, model, split = fit_fold(raw, labels, calendar, day)
    assert model["last_training_exit"] <= pd.Timestamp(model["training_cutoff"]) < day
    assert model["native_directions"]["RSQR30"]["sign"] == 1
    assert model["native_directions"]["WVMA20"]["sign"] == -1
    assert model["selected_families"] == ["risk", "volume_stability"]
    changed = labels.copy()
    for anchor in raw.index.get_level_values("date").unique():
        endpoint = calendar.get_loc(anchor) + 21
        if endpoint >= len(calendar) or calendar[endpoint] >= day:
            changed.loc[anchor] = -1e6 * labels.loc[anchor]
    future = raw.copy()
    future.loc[future.index.get_level_values("date") > day] *= -1e6
    other, other_model, _ = fit_fold(future, changed, calendar, day)
    assert model == other_model
    pd.testing.assert_frame_equal(current, other)
    assert split.loc[split.purged_cross_boundary.gt(0), "labeled_rows"].eq(0).all()


def test_centered_inverse_removes_old_full_year_signs_including_ties():
    pool = [{"symbol": str(i), "volatility": i + 1, "momentum": 3 - i,
             "RSQR30": i // 2, "WVMA20": i // 2} for i in range(4)]
    day = pd.Timestamp("2024-01-31")
    native = pd.concat({day: pd.DataFrame(pool).set_index("symbol")[["RSQR30", "WVMA20"]]}, names=["date", "symbol"])
    frames = []
    for signs in ({"RSQR30": {"sign": 1}, "WVMA20": {"sign": -1}},
                  {"RSQR30": {"sign": -1}, "WVMA20": {"sign": 1}}):
        cached = pd.concat({day: family_matrix(pool, signs)}, names=["date", "symbol"])
        frames.append(raw_components(cached, native, signs))
    pd.testing.assert_frame_equal(frames[0], frames[1])
    bad = cached.copy()
    bad["volume_stability"] *= 2
    with pytest.raises(ValueError, match="inverted"):
        raw_components(bad, native, signs)


def test_missing_head_is_unknown_not_replaced_and_feature_members_survive():
    raw, labels, calendar = inputs()
    current, _, _ = fit_fold(raw, labels, calendar, "2025-01-31")
    selected = current.expected_return.idxmax()
    outcome = labels.loc["2025-01-31"].copy()
    outcome.loc[selected] = np.nan
    row, panel = evaluate_fold(current, outcome, pd.Timestamp("2025-01-31"), 1)
    assert row["head_known_labels"] == 0 and row["complete_head_relative_label"] is None
    assert panel.loc[panel.head_selected].index.tolist() == [selected]
    assert len(panel) == 101 and row["unknown_labels"] == 1


def test_insufficient_months_and_noncalendar_decisions_cannot_shorten_training():
    raw, labels, calendar = inputs()
    current, skipped, _ = fit_fold(raw, labels, calendar, "2024-08-30")
    assert current is None and skipped["matured_months"] < 8
    with pytest.raises(ValueError, match="calendar"):
        fit_fold(raw, labels, calendar, "2025-02-01")
    labels.loc["2024-05-31"] = np.nan
    with pytest.raises(ValueError, match="mature labels"):
        fit_fold(raw, labels, calendar, "2025-01-31")


def test_report_preserves_unknown_partial_metrics_and_numpy_counts(tmp_path):
    record = {"status": "failed_mature_month_pipeline_ledger", "artifacts": "saved",
              "diagnostics": {"positive_ic_months": np.int64(1)},
              "results": [{"signal": "candidate", "status": "failed", "completed_sessions": 81,
                           "error": "missing action"}]}
    report(tmp_path, record)
    text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "failed/81|unknown|unknown" in text and "missing action" in text


def test_saved_reader_rejects_same_day_label_fit_and_keeps_development_boundary(tmp_path, monkeypatch):
    from trading import replay_reports, settings

    monkeypatch.setattr(settings, "ROOT", tmp_path)
    identifier = "20261005_085918_d645dc01"
    source = {"status": "completed_return_cost_diagnostics", "dataset": "prices", "scope": "research_only",
              "start": "2025-01-02", "end": "2026-09-30", "sessions": 424,
              "protocol": {"portfolio": {}, "stock_inputs": {"price_view": "p", "native_view": "n", "listing_master": "l"}}}
    folder = tmp_path / "outputs/market_replay" / identifier
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps(source), encoding="utf-8")
    record = {"purpose": replay_reports.PIPELINE_KIND, "source_run": identifier,
              "status": "failed_mature_month_pipeline_ledger", "execution_admitted": False,
              "results": [{"signal": replay_reports.PIPELINE_SIGNAL}],
              "protocol": {"id": replay_reports.PIPELINE_KIND, "execution_admitted": False, "fresh_holdout_claimed": False,
                           **source["protocol"]},
              "models": [{"date": "2025-01-27", "training_cutoff": "2025-01-24", "last_training_exit": "2024-12-30"}]}
    normalized = replay_reports._pipeline_record(record)
    assert normalized["sessions"] == 424
    assert "NOT independently validated" in normalized["protocol"]["limitations"][0]
    changed = deepcopy(record)
    changed["models"][0]["last_training_exit"] = "2025-01-27"
    with pytest.raises(ValueError, match="prior mature"):
        replay_reports._pipeline_record(changed)
    changed = deepcopy(record)
    changed["protocol"]["fresh_holdout_claimed"] = True
    with pytest.raises(ValueError, match="unadmitted"):
        replay_reports._pipeline_record(changed)
