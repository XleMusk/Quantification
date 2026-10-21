"""Only tiny synthetic fits; persisted inference must never fit or read labels."""
from copy import deepcopy
from datetime import datetime
import json

import numpy as np
import pandas as pd
import pytest
from lightgbm import LGBMClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from trading.research import shortline_frozen_model as m


SAVED_AT = "2026-10-16T00:30:00+08:00"
READ_AT = "2026-10-16T00:31:00+08:00"
DAY = "2026-10-15"
SEMANTICS = {"window": "synthetic_nine_observed_row_features", "market_scope": "synthetic_fixed_members"}


@pytest.fixture(scope="module")
def fitted():
    rng = np.random.default_rng(17)
    models = {"lr": {}, "lgbm": {}}
    for stage in (1, 2, 3):
        x = pd.DataFrame(rng.normal(size=(160, len(m.FEATURES))), columns=m.FEATURES)
        x.loc[::7, m.FEATURES[1]] = np.nan
        x.loc[::11, m.FEATURES[2]] = np.nan
        y = (x[m.FEATURES[0]] > x[m.FEATURES[0]].median()).astype(int)
        lr = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
            LogisticRegression(max_iter=1000, C=.5, solver="lbfgs"))
        lr.fit(x, y)
        imputer = SimpleImputer(strategy="median", add_indicator=True)
        transformed = imputer.fit_transform(x)
        lgbm = LGBMClassifier(objective="binary", n_estimators=200, learning_rate=.03, num_leaves=15,
            max_depth=-1, min_child_samples=50, subsample=.9, colsample_bytree=.9,
            reg_lambda=1., random_state=42, verbosity=-1, n_jobs=1)
        lgbm.fit(transformed, y)
        models["lr"][stage] = lr
        models["lgbm"][stage] = {"imputer": imputer, "model": lgbm}
    return models


def training_proof():
    return {"generation_id": "synthetic_fixed_generation", "feature_contract_id": "synthetic_nine_features_v1",
        "feature_semantics": deepcopy(SEMANTICS), "train_start": "2024-01-01", "train_end": "2024-12-31",
        "labels_complete_through": "2024-12-31", "training_panel_sha256": "a" * 64,
        "data_available_at": "2026-10-15T18:00:00+08:00", "recipe_id": m.RECIPE_ID,
        "label_end_purge_applied": True, "model_refit": True}


def score_frame():
    rows = []
    for i, stage in enumerate((1, 1, 3, 3)):
        values = {feature: (.13 * (i + 1) - .07 * j) for j, feature in enumerate(m.FEATURES)}
        if i % 2 == 0:
            values[m.FEATURES[1]] = np.nan
        rows.append({"date": DAY, "symbol": f"60000{i}.SSE", "stage": stage,
            "stage_reference_known": True, **values})
    return pd.DataFrame(rows)


def feature_proof():
    return {"day": DAY, "available_at": "2026-10-15T18:30:00+08:00",
        "feature_contract_id": "synthetic_nine_features_v1", "feature_semantics": deepcopy(SEMANTICS),
        "feature_names": list(m.FEATURES), "stage_membership_complete": True,
        "unknown_stage_symbols": [], "source_sha256": "b" * 64}


def saved(tmp_path, fitted):
    path = tmp_path / "generation"
    m.save_generation(fitted, path, training_proof=training_proof(), now=SAVED_AT)
    return path


def test_real_lr_and_lgbm_preprocessing_persisted_load_matches_before_save_probabilities(tmp_path, fitted):
    frame = score_frame()
    expected = {}
    for kind in ("lr", "lgbm"):
        expected[kind] = {}
        for stage, group in frame.groupby("stage"):
            values = group[list(m.FEATURES)]
            model = fitted[kind][stage]
            probability = (model.predict_proba(values)[:, 1] if kind == "lr" else
                model["model"].predict_proba(model["imputer"].transform(values))[:, 1])
            expected[kind].update(zip(group.symbol, probability))
    path = saved(tmp_path, fitted)
    generation = m.load_generation(path, now=READ_AT)
    for kind in ("lr", "lgbm"):
        result = m.score_day(generation, frame, day=DAY, model_kind=kind,
            feature_provenance=feature_proof(), now=READ_AT)
        scores = result["scores"].set_index("symbol")
        np.testing.assert_allclose(scores.loc[frame.symbol, "model_hit5_probability"],
            [expected[kind][s] for s in frame.symbol], rtol=1e-10, atol=1e-12)
        assert result["provenance"]["stage_counts"] == {"1": 2, "2": 0, "3": 2}
        assert result["provenance"]["absent_event_stages"] == [2]
        assert result["provenance"]["score_only_not_candidate_batch"] is True
        assert result["provenance"]["target_generation_allowed"] is False
        assert scores.paper_candidate_only.eq(False).all()
        assert len(scores) == 4


def test_score_is_load_only_and_future_label_columns_cannot_change_or_enter_output(tmp_path, fitted, monkeypatch):
    generation = m.load_generation(saved(tmp_path, fitted), now=READ_AT)
    frame = score_frame()
    expected = {kind: m.score_day(generation, frame, day=DAY, model_kind=kind,
        feature_provenance=feature_proof(), now=READ_AT)["scores"] for kind in ("lr", "lgbm")}
    def forbidden(*args, **kwargs):
        raise AssertionError("fit is forbidden during frozen scoring")
    for cls in (SimpleImputer, StandardScaler, LogisticRegression, LGBMClassifier):
        monkeypatch.setattr(cls, "fit", forbidden)
    monkeypatch.setattr(SimpleImputer, "fit_transform", forbidden)
    monkeypatch.setattr(StandardScaler, "fit_transform", forbidden)
    altered = frame.assign(hit_5=[True, False, True, False], run_end_date="2099-01-01",
        label_status="future", executable_hit_5=999, touchability_label_status="unknown_future")
    for kind in ("lr", "lgbm"):
        actual = m.score_day(generation, altered, day=DAY, model_kind=kind,
            feature_provenance=feature_proof(), now=READ_AT)
        pd.testing.assert_frame_equal(actual["scores"], expected[kind])
        assert not {"hit_5", "run_end_date", "label_status", "executable_hit_5", "touchability_label_status"}.intersection(actual["scores"].columns)


@pytest.mark.parametrize("fault", ["missing_feature", "all_missing_row", "empty_frame", "feature_order", "unknown_stage"])
def test_missing_or_unusable_feature_and_membership_evidence_is_not_imputed_into_scores(tmp_path, fitted, fault):
    generation = m.load_generation(saved(tmp_path, fitted), now=READ_AT)
    frame, proof = score_frame(), feature_proof()
    if fault == "missing_feature":
        frame = frame.drop(columns=m.FEATURES[0])
    elif fault == "all_missing_row":
        frame.loc[0, list(m.FEATURES)] = np.nan
    elif fault == "empty_frame":
        frame = frame.iloc[:0]
    elif fault == "feature_order":
        proof["feature_names"] = list(reversed(m.FEATURES))
    else:
        proof["stage_membership_complete"] = False
        proof["unknown_stage_symbols"] = ["600099.SSE"]
    with pytest.raises(ValueError):
        m.score_day(generation, frame, day=DAY, model_kind="lr", feature_provenance=proof, now=READ_AT)


def test_artifact_and_feature_actual_clocks_and_training_label_cutoff_are_enforced(tmp_path, fitted):
    path = saved(tmp_path, fitted)
    with pytest.raises(ValueError):
        m.load_generation(path, now="2026-10-16T00:29:00+08:00")
    generation = m.load_generation(path, now=READ_AT)
    proof = feature_proof()
    proof["available_at"] = "2026-10-16T00:32:00+08:00"
    with pytest.raises(ValueError):
        m.score_day(generation, score_frame(), day=DAY, model_kind="lr", feature_provenance=proof, now=READ_AT)
    training = training_proof()
    training["train_end"] = training["labels_complete_through"] = DAY
    same_day = tmp_path / "same_day_training"
    m.save_generation(fitted, same_day, training_proof=training, now=SAVED_AT)
    with pytest.raises(ValueError):
        m.score_day(m.load_generation(same_day, now=READ_AT), score_frame(), day=DAY,
            model_kind="lr", feature_provenance=feature_proof(), now=READ_AT)
    with pytest.raises(ValueError):
        m.load_generation(path, now=datetime(2026, 10, 16))


def test_changed_artifact_and_missing_stage_models_are_rejected_before_output(tmp_path, fitted):
    path = saved(tmp_path, fitted)
    model_file = next(path.glob("lr_*.json"))
    value = json.loads(model_file.read_text(encoding="utf8"))
    value["synthetic_tamper"] = True
    model_file.write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError):
        m.load_generation(path, now=READ_AT)
    missing = {kind: dict(stages) for kind, stages in fitted.items()}
    missing["lr"].pop(2)
    out = tmp_path / "missing_stage_generation"
    with pytest.raises(ValueError):
        m.save_generation(missing, out, training_proof=training_proof(), now=SAVED_AT)
    assert not out.exists()


def test_partial_known_stage_optin_preserves_unknown_members_without_probabilities(tmp_path, fitted):
    generation = m.load_generation(saved(tmp_path, fitted), now=READ_AT)
    frame = score_frame().assign(decision_eligible=True)
    unknown = ["600099.SSE", "000099.SZSE"]  # Preserve source order, not sorted/filtered.
    proof = feature_proof()
    proof.update(stage_membership_complete=False, unknown_stage_symbols=list(unknown),
        source_scope={"scope": "synthetic_full_original_day_roster", "member_count": 6,
            "known_stage_count": 4, "unknown_stage_count": 2})
    with pytest.raises(ValueError):
        m.score_day(generation, frame, day=DAY, model_kind="lr", feature_provenance=proof, now=READ_AT)
    proof["known_stage_partial_research_only"] = True
    for kind in ("lr", "lgbm"):
        result = m.score_day(generation, frame, day=DAY, model_kind=kind,
            feature_provenance=proof, now=READ_AT)
        scores, provenance = result["scores"], result["provenance"]
        assert scores.symbol.tolist() == frame.symbol.tolist()
        assert not set(unknown).intersection(scores.symbol)
        assert len(scores) == 4
        assert scores.model_hit5_probability.between(0, 1).all()
        assert scores.paper_candidate_only.eq(False).all()
        assert provenance["status"] == "partial_known_stage_frozen_single_date_probabilities_only"
        assert provenance["stage_membership_complete"] is False
        assert provenance["known_stage_partial_research_only"] is True
        assert provenance["unknown_stage_symbols"] == unknown
        assert provenance["feature_source"] == proof
        assert provenance["batch_complete_for_target"] is False
        assert provenance["target_generation_allowed"] is False
    assert proof["unknown_stage_symbols"] == unknown
    overlap = deepcopy(proof)
    overlap["unknown_stage_symbols"] = [unknown[0], frame.symbol.iloc[0]]
    with pytest.raises(ValueError):
        m.score_day(generation, frame, day=DAY, model_kind="lr", feature_provenance=overlap, now=READ_AT)
    ineligible = frame.copy()
    ineligible.loc[0, "decision_eligible"] = False
    with pytest.raises(ValueError):
        m.score_day(generation, ineligible, day=DAY, model_kind="lr", feature_provenance=proof, now=READ_AT)
