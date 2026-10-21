"""Synthetic behavior checks; no market data, fitted model, or account calls."""

import numpy as np
import pandas as pd
import pytest

from trading.research import shortline_objective_models as models
from trading.research import shortline_remaining_edge_stats as edge_stats


def _features():
    rows = []
    for stage in models.STAGES:
        rows.append({
            "event_id": f"synthetic_s{stage}", "date": "2026-01-05",
            "symbol": f"00000{stage}.SZSE", "stage": stage,
            **{feature: float(stage) for feature in models.FEATURES},
        })
    rows[0][models.FEATURES[0]] = np.nan
    return pd.DataFrame(rows)


def _preprocessing():
    return {
        "strategy": "median", "missing_values": "NaN", "add_indicator": True,
        "keep_empty_features": False, "input_features": list(models.FEATURES),
        "statistics": [0.0] * len(models.FEATURES),
        "kept_feature_indices": list(range(len(models.FEATURES))),
        "indicator_feature_indices": [], "output_features": list(models.FEATURES),
    }


def test_feature_only_scoring_refuses_all_declared_future_label_columns():
    feature_frame = _features()
    for label in models.LABEL_COLUMNS:
        contaminated = feature_frame.assign(**{label: 1})
        with pytest.raises(ValueError, match="feature-only events; no future labels"):
            models.score_features(({}, {}), contaminated)


def test_frozen_stub_inference_uses_saved_preprocessing_without_fit(monkeypatch):
    class FrozenBooster:
        def __init__(self, prediction):
            self.prediction = prediction
            self.calls = []

        def predict(self, values, *, num_threads):
            self.calls.append((values.copy(), num_threads))
            return np.full(len(values), self.prediction)

        def fit(self, *args, **kwargs):
            raise AssertionError("Inference must never fit a booster")

    def forbidden_fit(*args, **kwargs):
        raise AssertionError("Inference must never create a model generation")

    monkeypatch.setattr(models, "fit_generation", forbidden_fit)
    payloads = {
        (arm, stage): (
            _preprocessing(), FrozenBooster(.2 + stage / 100 if arm == "binary" else -stage / 100),
        )
        for arm in models.ARMS for stage in models.STAGES
    }
    result = models.score_features(({}, payloads), _features())
    assert len(result) == 6
    assert set(result.model_kind) == {"binary", "return"}
    assert result.loc[result.model_kind.eq("return"), "model_score"].lt(0).all()
    assert result.groupby("model_kind").event_id.nunique().to_dict() == {"binary": 3, "return": 3}
    for (arm, stage), (_, booster) in payloads.items():
        assert len(booster.calls) == 1
        values, threads = booster.calls[0]
        assert threads == 1
        assert values.shape == (1, len(models.FEATURES))
        assert np.isfinite(values).all()
        assert values[0, 0] == (0 if stage == 1 else stage)


def test_fixed_tenth_top5_negative_return_and_unknown_day_reach_dynamic_stats():
    days = ["2026-01-05", "2026-01-06"]
    rows = []
    for arm in models.ARMS:
        for day in days:
            for stage in models.STAGES:
                for ordinal in range(30):
                    value = (stage * 100 + ordinal) / 1000
                    rows.append({
                        "event_id": f"{day}_s{stage}_{ordinal:02d}", "date": day,
                        "symbol": f"{stage * 100 + ordinal:06d}.SZSE", "stage": stage,
                        "model_kind": arm,
                        "model_score": .9 - value if arm == "binary" else -value,
                    })
    original = pd.DataFrame(rows)
    scopes = {
        days[0]: {"stage_membership_complete": True},
        days[1]: {"stage_membership_complete": False},
    }
    selection, decisions, plans = models.select_scores(original, scopes, days)
    for arm in models.ARMS:
        known = selection.loc[selection.model_kind.eq(arm) & selection.date.eq(days[0])]
        assert known.groupby("stage").rank_pass.sum().to_dict() == {1: 3, 2: 3, 3: 3}
        selected = known.loc[known.candidate_selected]
        assert selected.symbol.tolist() == [
            "000100.SZSE", "000101.SZSE", "000102.SZSE", "000200.SZSE", "000201.SZSE",
        ]
        assert plans[arm][days[0]]["decision"] == "FULL_TARGET"
        assert len(plans[arm][days[0]]["symbols"]) == 5
        assert plans[arm][days[1]]["decision"] == "NO_CHANGE"
        assert plans[arm][days[1]]["symbols"] == []
        unknown = selection.loc[selection.model_kind.eq(arm) & selection.date.eq(days[1])]
        assert not unknown.candidate_selected.any()
        assert not unknown.rank_pass.any()
        if arm == "return":
            assert known.model_score.lt(0).all()
            assert len(selected) == 5
    assert len(decisions) == 4

    # The existing saved-outcome statistics support new arm identities without
    # a label/model shim; these arbitrary fixture outcomes are not market returns.
    observations = selection.assign(
        entry_status="proxy_clear", h1_exit_status="proxy_clear", h5_exit_status="proxy_clear",
        h1_return=np.where(selection.candidate_selected, .02, -.01),
        h5_return=np.where(selection.candidate_selected, .02, -.01),
    )
    analysis = edge_stats.analyze(observations, days)
    headline = analysis["summary"].loc[
        analysis["summary"].stage.eq("all")
        & analysis["summary"].horizon.eq("h5")
        & analysis["summary"].population.eq("entry_exit_proxy_clear")
        & analysis["summary"].origin_sample.eq("all_days")
        & analysis["summary"].segment.eq("all")
        & analysis["summary"].weighting.eq("day_equal")
    ]
    assert set(headline.model_kind) == {"binary", "return"}
    assert headline.selected_count.tolist() == [5, 5]
    assert headline.paired_days_count.tolist() == [1, 1]
