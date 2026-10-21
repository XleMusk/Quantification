"""Explicit new shortline model generations; JSON/native artifacts, never pickle.

There is no fit entry point. Export accepts already fitted objects from a
separately recorded new training run. Inference only loads their frozen state.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ("amount_rank_pct", "volume_rank_pct", "market_limitup_rate", "market_down_rate",
    "range_pct", "close_location", "ret_5_observed_rows", "ret_10_observed_rows", "ret_20_observed_rows")
STAGES = (1, 2, 3)
MODEL_KINDS = ("lr", "lgbm")
SCHEMA = "shortline_frozen_model_generation_v1"
SCORE_SCHEMA = "shortline_frozen_single_date_score_v1"
RECIPE_ID = "shortline_nine_feature_stagewise_median_indicator_lr_lgbm_v1"
LR_PARAMS = {"C": .5, "max_iter": 1000, "solver": "lbfgs"}
LGBM_PARAMS = {"objective": "binary", "n_estimators": 200, "learning_rate": .03,
    "num_leaves": 15, "max_depth": -1, "min_child_samples": 50, "subsample": .9,
    "colsample_bytree": .9, "reg_lambda": 1., "random_state": 42, "verbosity": -1, "n_jobs": 1}
FLAGS = {"research_only": True, "candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "current_signal_admitted": False,
    "future_labels_used_for_score": False, "orders_generated": False, "ledger_mutated": False,
    "account_accessed": False, "network_accessed": False, "old_model_weights_recovered": False}
CN = timezone(timedelta(hours=8))


def _now(now=None):
    value = datetime.now(CN) if now is None else now
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An explicit timezone-aware actual clock is required")
    return value.astimezone(CN)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Exact canonical source dates are required")
    return value


def _hash(path):
    value = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _finite(values, name, *, size=None):
    value = np.asarray(values, dtype=float)
    if (size is not None and value.shape != (size,)) or not np.isfinite(value).all():
        raise ValueError("Incomplete or nonfinite frozen " + name)
    return value


def _proof(proof, actual):
    value = deepcopy(proof)
    for key in ("generation_id", "feature_contract_id"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("New model generations need an explicit " + key)
    if (not isinstance(value.get("feature_semantics"), dict) or not value["feature_semantics"]
            or value.get("recipe_id") != RECIPE_ID or value.get("model_refit") is not True
            or value.get("label_end_purge_applied") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("training_panel_sha256", "")))):
        raise ValueError("Explicit new-refit training, fixed feature semantics and purged-label source proof required")
    start, end, labels = (_day(value[key]) for key in ("train_start", "train_end", "labels_complete_through"))
    available = _now(value.get("data_available_at")) if value.get("data_available_at") is not None else None
    if (not start <= end or not start <= labels <= end or available is None
            or not _now(labels + "T15:00:00+08:00") <= available <= actual):
        raise ValueError("Training source/label completion or actual availability is inconsistent")
    json.dumps(value, allow_nan=False)
    return value


def _imputer_state(imputer):
    if not isinstance(imputer, SimpleImputer):
        raise ValueError("Only the fixed fitted SimpleImputer recipe is serializable")
    params = imputer.get_params()
    missing = params["missing_values"]
    if (params["strategy"] != "median" or params["add_indicator"] is not True
            or not isinstance(missing, (float, np.floating)) or not np.isnan(missing)
            or list(getattr(imputer, "feature_names_in_", [])) != list(FEATURES)
            or getattr(imputer, "n_features_in_", None) != len(FEATURES)):
        raise ValueError("Median/NaN/add_indicator input order must be recoverable from the fitted imputer")
    statistics = np.asarray(imputer.statistics_, dtype=float)
    if statistics.shape != (len(FEATURES),) or np.isinf(statistics).any():
        raise ValueError("The fitted imputer statistics are incomplete")
    kept = np.flatnonzero(~np.isnan(statistics)).tolist()
    indicator = np.asarray(imputer.indicator_.features_)
    if (not kept or indicator.ndim != 1 or not np.issubdtype(indicator.dtype, np.integer)
            or indicator.tolist() != sorted(set(indicator.tolist()))
            or any(index < 0 or index >= len(FEATURES) for index in indicator)):
        raise ValueError("The fitted missing-indicator feature indices cannot be restored")
    names = [FEATURES[index] for index in kept] + ["missingindicator_" + FEATURES[index] for index in indicator]
    if list(imputer.get_feature_names_out(FEATURES)) != names:
        raise ValueError("The fitted imputer output order cannot be reproduced exactly")
    state = {"strategy": "median", "missing_values": "NaN", "add_indicator": True,
        "keep_empty_features": bool(params.get("keep_empty_features", False)), "input_features": list(FEATURES),
        "statistics": [None if np.isnan(value) else float(value) for value in statistics],
        "kept_feature_indices": kept, "indicator_feature_indices": indicator.tolist(), "output_features": names}
    # Compare all-missing/observed patterns with the fitted transformer. A
    # serialized approximation is refused instead of silently changing columns.
    probe = np.tile(np.where(np.isnan(statistics), 2., statistics), (3, 1))
    probe[0, :] = np.nan
    probe[1, ::2] = np.nan
    expected = imputer.transform(pd.DataFrame(probe, columns=FEATURES))
    if not np.array_equal(expected, _transform(probe, state)):
        raise ValueError("The full fitted imputer transformation is not exactly reproducible")
    return state


def _validate_imputer(state):
    if (state.get("strategy") != "median" or state.get("missing_values") != "NaN"
            or state.get("add_indicator") is not True or type(state.get("keep_empty_features")) is not bool
            or state.get("input_features") != list(FEATURES)):
        raise ValueError("Frozen preprocessing no longer has the fixed input contract")
    stats = state.get("statistics")
    if not isinstance(stats, list) or len(stats) != len(FEATURES):
        raise ValueError("All frozen median statistics are required")
    kept = [index for index, value in enumerate(stats) if value is not None]
    if not kept or state.get("kept_feature_indices") != kept:
        raise ValueError("Frozen kept-column indices disagree with median statistics")
    _finite([stats[index] for index in kept], "median statistics")
    indicators = state.get("indicator_feature_indices")
    if (not isinstance(indicators, list) or any(type(index) is not int or index not in range(len(FEATURES)) for index in indicators)
            or indicators != sorted(set(indicators))
            or state.get("output_features") != [FEATURES[index] for index in kept]
                + ["missingindicator_" + FEATURES[index] for index in indicators]):
        raise ValueError("Frozen imputer output/indicator order changed")


def _transform(values, state):
    _validate_imputer(state)
    missing = np.isnan(values)
    kept, indicators = state["kept_feature_indices"], state["indicator_feature_indices"]
    stats = np.asarray([np.nan if value is None else value for value in state["statistics"]])
    filled = np.where(missing, stats, values)[:, kept]
    return np.column_stack([filled, missing[:, indicators].astype(float)])


def save_generation(fitted_models, out, *, training_proof, now=None):
    """Persist six already-fitted objects from a separately recorded new fit."""
    actual, out = _now(now), Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new model-generation directory; old artifacts are immutable")
    proof = _proof(training_proof, actual)
    if set(fitted_models) != set(MODEL_KINDS) or any(set(fitted_models[kind]) != set(STAGES) for kind in MODEL_KINDS):
        raise ValueError("A generation must contain LR and LightGBM for all three explicit stages")
    import lightgbm as lgb
    import sklearn
    states, models = {}, {}
    for kind in MODEL_KINDS:
        for stage in STAGES:
            fitted = fitted_models[kind][stage]
            if kind == "lr":
                if not isinstance(fitted, Pipeline) or len(fitted.steps) != 3:
                    raise ValueError("LR export requires the exact fitted three-step pipeline")
                imputer, scaler, model = [step for _, step in fitted.steps]
                if (not isinstance(scaler, StandardScaler) or not scaler.with_mean or not scaler.with_std
                        or not isinstance(model, LogisticRegression)
                        or any(model.get_params().get(key) != value for key, value in LR_PARAMS.items())):
                    raise ValueError("LR preprocessing/model recipe changed")
            else:
                if not isinstance(fitted, dict) or set(fitted) != {"imputer", "model"}:
                    raise ValueError("LightGBM export requires its actual fitted imputer and classifier")
                imputer, model = fitted["imputer"], fitted["model"]
                if (not isinstance(model, lgb.LGBMClassifier)
                        or any(model.get_params().get(key) != value for key, value in LGBM_PARAMS.items())):
                    raise ValueError("LightGBM recipe changed")
            if np.asarray(getattr(model, "classes_", [])).tolist() != [0, 1]:
                raise ValueError("Only an explicitly fitted binary 0/1 hit-five classifier is supported")
            state = _imputer_state(imputer)
            count = len(state["output_features"])
            if getattr(model, "n_features_in_", None) != count:
                raise ValueError("Classifier input dimension differs from complete preprocessing output")
            if kind == "lr":
                if getattr(scaler, "n_features_in_", None) != count:
                    raise ValueError("Scaler input dimension differs from imputer output")
                scale = _finite(scaler.scale_, "scaler scale", size=count)
                if (scale <= 0).any():
                    raise ValueError("Frozen scaling needs strictly positive scales")
                state["scaler"] = {"with_mean": True, "with_std": True,
                    "mean": _finite(scaler.mean_, "scaler mean", size=count).tolist(),
                    "scale": scale.tolist(), "variance": _finite(scaler.var_, "scaler variance", size=count).tolist()}
                if np.asarray(model.coef_).shape != (1, count):
                    raise ValueError("Only the exact binary logistic coefficient row is supported")
                models[(kind, stage)] = {"classes": [0, 1], "coefficient": _finite(model.coef_[0], "LR coefficients", size=count).tolist(),
                    "intercept": float(_finite(model.intercept_, "LR intercept", size=1)[0])}
            else:
                booster = model.booster_
                if (booster.num_feature() != count or booster.num_model_per_iteration() != 1
                        or booster.num_trees() < 1 or getattr(model, "best_iteration_", 0) not in (0, -1)):
                    raise ValueError("Only the complete fixed binary native LightGBM model is supported")
                state["native_model_feature_names"] = booster.feature_name()
                models[(kind, stage)] = booster
            states[(kind, stage)] = state
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + ".", dir=out.parent) as temporary:
        folder, entries = Path(temporary), []
        for kind in MODEL_KINDS:
            for stage in STAGES:
                preprocessing = folder / f"preprocessing_{stage}_{kind}.json"
                _write(preprocessing, states[(kind, stage)])
                model_file = folder / (f"lr_{stage}.json" if kind == "lr" else f"lgbm_{stage}.txt")
                if kind == "lr":
                    _write(model_file, models[(kind, stage)])
                else:
                    models[(kind, stage)].save_model(str(model_file), num_iteration=-1)
                entries.append({"model_kind": kind, "stage": stage,
                    "preprocessing_file": preprocessing.name, "preprocessing_sha256": _hash(preprocessing),
                    "model_file": model_file.name, "model_sha256": _hash(model_file)})
        completed = _now(now)
        manifest = {"schema": SCHEMA, "status": "completed_new_frozen_generation",
            "created_at": completed.isoformat(), "model_available_at": completed.isoformat(),
            "generation_id": proof["generation_id"], "features": list(FEATURES), "recipe_id": RECIPE_ID,
            "feature_contract_id": proof["feature_contract_id"], "feature_semantics": proof["feature_semantics"],
            "training_proof": proof, "models": entries, "model_refit": True,
            "serialization": "complete_preprocessing_JSON_LR_coefficients_and_native_LightGBM_not_pickle",
            "versions": {"sklearn": sklearn.__version__, "lightgbm": lgb.__version__, "numpy": np.__version__}, **FLAGS}
        _write(folder / "manifest.json", manifest)
        folder.rename(out)
    return manifest


class FrozenShortlineGeneration:
    """Loaded coefficients/native boosters; no fitting or parameter search."""
    def __init__(self, path, *, now=None):
        actual, path = _now(now), Path(path).resolve()
        self.path = path / "manifest.json" if path.is_dir() else path
        self.manifest = json.loads(self.path.read_text(encoding="utf-8"))
        record = self.manifest
        if (record.get("schema") != SCHEMA or record.get("status") != "completed_new_frozen_generation"
                or record.get("model_refit") is not True or record.get("features") != list(FEATURES)
                or record.get("recipe_id") != RECIPE_ID or any(record.get(key) != value for key, value in FLAGS.items())
                or _now(record["created_at"]) != _now(record["model_available_at"])
                or _now(record["model_available_at"]) > actual):
            raise ValueError("A complete unchanged new generation available at the actual load clock is required")
        proof = _proof(record["training_proof"], _now(record["created_at"]))
        if any(record[key] != proof[key] for key in ("generation_id", "feature_contract_id", "feature_semantics")):
            raise ValueError("Training and scoring feature lineage differ")
        entries = record["models"]
        if (len(entries) != 6 or {(row["model_kind"], row["stage"]) for row in entries}
                != {(kind, stage) for kind in MODEL_KINDS for stage in STAGES}):
            raise ValueError("All six distinct stage classifiers must be frozen")
        import lightgbm as lgb
        self._models, self._states, self._pins = {}, {}, {self.path: _hash(self.path)}
        for row in entries:
            key = row["model_kind"], row["stage"]
            paths = {}
            for field in ("preprocessing", "model"):
                relative = Path(row[field + "_file"])
                if relative.name != str(relative) or relative.is_absolute():
                    raise ValueError("Frozen model artifacts must remain inside their generation")
                file = self.path.parent / relative
                if _hash(file) != row[field + "_sha256"]:
                    raise ValueError("Frozen model/preprocessing bytes changed")
                self._pins[file] = row[field + "_sha256"]
                paths[field] = file
            state = json.loads(paths["preprocessing"].read_text(encoding="utf-8"))
            _validate_imputer(state)
            count = len(state["output_features"])
            if key[0] == "lr":
                scaler = state.get("scaler", {})
                if scaler.get("with_mean") is not True or scaler.get("with_std") is not True:
                    raise ValueError("Complete fixed scaling metadata required")
                _finite(scaler["mean"], "scaler mean", size=count)
                if (_finite(scaler["scale"], "scaler scale", size=count) <= 0).any():
                    raise ValueError("Frozen scales must be positive")
                _finite(scaler["variance"], "scaler variance", size=count)
                model = json.loads(paths["model"].read_text(encoding="utf-8"))
                if model["classes"] != [0, 1]:
                    raise ValueError("Class order cannot change at load")
                _finite(model["coefficient"], "LR coefficients", size=count)
                _finite([model["intercept"]], "LR intercept", size=1)
            else:
                model = lgb.Booster(model_file=str(paths["model"]))
                if (model.num_feature() != count or model.num_model_per_iteration() != 1
                        or model.feature_name() != state["native_model_feature_names"]
                        or model.params.get("objective") != "binary"):
                    raise ValueError("Native binary LightGBM feature/objective contract changed")
            self._models[key], self._states[key] = model, state
        self._signatures = {file: (file.stat().st_size, file.stat().st_mtime_ns) for file in self._pins}

    def _unchanged(self):
        if any((file.stat().st_size, file.stat().st_mtime_ns) != signature for file, signature in self._signatures.items()):
            raise ValueError("Loaded frozen generation changed before scoring")


def load_generation(path, *, now=None):
    return FrozenShortlineGeneration(path, now=now)


def score_day(generation, frame, *, day, model_kind, feature_provenance, now=None):
    """Single-date probabilities only; never select candidates or make targets."""
    actual, day = _now(now), _day(day)
    loaded = generation if isinstance(generation, FrozenShortlineGeneration) else load_generation(generation, now=actual)
    loaded._unchanged()
    record, proof = loaded.manifest, deepcopy(feature_provenance)
    if (model_kind not in MODEL_KINDS or _now(record["model_available_at"]) > actual
            or day <= record["training_proof"]["train_end"]):
        raise ValueError("An explicitly available LR or LightGBM generation is required")
    unknown_symbols = proof.get("unknown_stage_symbols")
    if (not isinstance(unknown_symbols, list)
            or any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(?:SSE|SZSE)", symbol) for symbol in unknown_symbols)
            or len(unknown_symbols) != len(set(unknown_symbols))):
        raise ValueError("The full source unknown-stage symbol list must be explicit and distinct")
    complete = proof.get("stage_membership_complete") is True and not unknown_symbols
    partial = (proof.get("stage_membership_complete") is False
        and proof.get("known_stage_partial_research_only") is True)
    if (proof.get("day") != day or proof.get("feature_names") != list(FEATURES)
            or proof.get("feature_contract_id") != record["feature_contract_id"]
            or proof.get("feature_semantics") != record["feature_semantics"]
            or not (complete or partial)
            or not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("source_sha256", "")))
            or not proof.get("available_at")
            or not _now(day + "T15:00:00+08:00") <= _now(proof["available_at"]) <= actual):
        raise ValueError("One complete exact-dated feature/stage batch with identical model lineage is required")
    required = {"date", "symbol", "stage", "stage_reference_known", *FEATURES}
    if (not isinstance(frame, pd.DataFrame) or frame.empty or not required <= set(frame.columns)
            or frame.columns.duplicated().any()):
        raise ValueError("All nine actual feature columns and exact stage identities are required")
    if (not frame.date.map(lambda value: isinstance(value, str) and value == day).all()
            or not frame.symbol.map(lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9]{6}\.(?:SSE|SZSE)", value))).all()
            or frame.symbol.duplicated().any()
            or not frame.stage.map(lambda value: not isinstance(value, (bool, np.bool_)) and pd.notna(value)
                and value in STAGES and float(value).is_integer()).all()
            or not frame.stage_reference_known.map(lambda value: isinstance(value, (bool, np.bool_)) and bool(value)).all()):
        raise ValueError("Mixed dates, duplicate identities or unknown stage membership cannot be scored")
    if set(unknown_symbols).intersection(frame.symbol):
        raise ValueError("Unknown-stage members must not acquire probabilities through the known-stage partial path")
    if partial and (not isinstance(proof.get("source_scope"), dict) or not proof["source_scope"]
            or "decision_eligible" not in frame
            or not frame.decision_eligible.map(lambda value: isinstance(value, (bool, np.bool_)) and bool(value)).all()):
        raise ValueError("Partial research scores need the complete saved source scope and explicit eligible known-stage rows")
    values = frame.loc[:, list(FEATURES)].apply(pd.to_numeric, errors="raise").to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(values).any() or np.isnan(values).all(axis=1).any():
        raise ValueError("Nonfinite or wholly absent observations must not become imputed pseudo-probabilities")
    probabilities = np.full(len(frame), np.nan)
    stages = frame.stage.to_numpy()
    for stage in STAGES:
        mask = stages == stage
        if not mask.any():
            continue
        key = model_kind, stage
        transformed = _transform(values[mask], loaded._states[key])
        if not np.isfinite(transformed).all():
            raise ValueError("Frozen preprocessing did not produce complete finite model inputs")
        model = loaded._models[key]
        if model_kind == "lr":
            scaler = loaded._states[key]["scaler"]
            scaled = (transformed - np.asarray(scaler["mean"])) / np.asarray(scaler["scale"])
            logits = scaled @ np.asarray(model["coefficient"]) + model["intercept"]
            if not np.isfinite(logits).all():
                raise ValueError("Invalid frozen logistic linear predictors")
            probability = np.exp(-np.logaddexp(0., -logits))
        else:
            probability = np.asarray(model.predict(transformed, num_iteration=-1, num_threads=1))
        if probability.shape != (int(mask.sum()),) or not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
            raise ValueError("Frozen classifier probabilities must be finite and within [0,1]")
        probabilities[mask] = probability
    scores = frame.loc[:, ["symbol", "date", "stage", *FEATURES]].copy()
    scores.insert(0, "event_id", frame.event_id.to_numpy() if "event_id" in frame else
        [f"{symbol}|{day}|s{int(stage)}" for symbol, stage in zip(frame.symbol, frame.stage)])
    scores["model_hit5_probability"], scores["paper_candidate_only"] = probabilities, False
    scores["model_kind"], scores["generation_id"] = model_kind, record["generation_id"]
    completed = _now(now)
    loaded._unchanged()
    stage_counts = {str(stage): int((stages == stage).sum()) for stage in STAGES}
    return {"scores": scores, "provenance": {"schema": SCORE_SCHEMA, "day": day,
        "status": "partial_known_stage_frozen_single_date_probabilities_only" if partial
            else "completed_frozen_single_date_probabilities_only", "scored_at": completed.isoformat(),
        "score_available_at": completed.isoformat(), "model_available_at": record["model_available_at"],
        "generation_id": record["generation_id"], "model_kind": model_kind,
        "manifest_file": str(loaded.path), "manifest_sha256": loaded._pins[loaded.path],
        "model_refit": False, "feature_contract_id": record["feature_contract_id"],
        "feature_semantics": deepcopy(record["feature_semantics"]), "features": list(FEATURES),
        "feature_source": proof, "stage_counts": stage_counts,
        "stage_membership_complete": complete, "known_stage_partial_research_only": partial,
        "unknown_stage_symbols": list(unknown_symbols), "batch_complete_for_target": False,
        "absent_event_stages": [stage for stage in STAGES if not stage_counts[str(stage)]],
        "input_rows": len(frame), "score_rows": len(scores), "future_label_columns_projected_out": True,
        "score_only_not_candidate_batch": True, "target_generation_allowed": False,
        "all_false_candidate_flags_are_not_an_empty_portfolio_target": True, **FLAGS}}
