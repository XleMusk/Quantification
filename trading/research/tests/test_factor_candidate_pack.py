import json

import pandas as pd
import pytest

from trading.research.factor_candidate_pack import build_candidate_pack


def _source(tmp_path, *, unsafe=False):
    run_dir = tmp_path / "source"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    run = {
        "status": "completed_research_diagnostics",
        "definitions_completed": 718,
        "completed_horizons": [20],
        "account_backtest": False,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "model_fit": False,
        "selection_parameters_searched": unsafe,
        "artifacts": str(artifacts),
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    summary = []
    definitions = [
        ("A", 1, True, False, "high", 0.10),
        ("B", 1, True, False, "high", 0.20),
        ("C", 2, True, True, "high", 0.30),
    ] + [(f"F{i:03d}", i + 2, False, False, "high", 0.0) for i in range(715)]
    for factor, cluster, positive, constant, direction, ic in definitions:
        for segment in ("train", "valid", "test"):
            summary.append({
                "factor": factor, "horizon": 20, "segment": segment,
                "train_direction": direction, "mean_rank_ic": ic,
                "oriented_mean_rank_ic": ic, "oriented_top_quintile_excess": ic,
                "ic_days": 100, "positive_oriented_ic_fraction": 0.6,
                "hac_t_horizon": 2.0, "pairs": 1000, "missing_forward_labels": 0,
                "diagnostic_positive": positive,
            })
    pd.DataFrame(summary).to_csv(artifacts / "decay_summary.csv", index=False)
    profiles = pd.DataFrame([
        {"name": "A", "formula": "a", "note": "", "original_name": "A", "library": "L",
         "family": "F", "category": "C", "window_1": 1, "window_2": 2, "redundancy_cluster": 1,
         "cluster_size": 2, "status": "SCREENED_DIAGNOSTIC_ONLY", "cross_section_constant": False,
         "exact_formula_reuse": False, "structural_sign_mirror": None},
        {"name": "B", "formula": "b", "note": "", "original_name": "B", "library": "L",
         "family": "F", "category": "C", "window_1": 1, "window_2": 3, "redundancy_cluster": 1,
         "cluster_size": 2, "status": "SCREENED_DIAGNOSTIC_ONLY", "cross_section_constant": False,
         "exact_formula_reuse": False, "structural_sign_mirror": None},
        {"name": "C", "formula": "c", "note": "", "original_name": "C", "library": "L",
         "family": "F", "category": "C", "window_1": 1, "window_2": 4, "redundancy_cluster": 2,
         "cluster_size": 1, "status": "SCREENED_DIAGNOSTIC_ONLY", "cross_section_constant": True,
         "exact_formula_reuse": False, "structural_sign_mirror": None},
    ] + [{"name": f"F{i:03d}", "formula": "f", "note": "", "original_name": f"F{i:03d}",
          "library": "L", "family": "F", "category": "C", "window_1": 1, "window_2": 5,
          "redundancy_cluster": i + 2, "cluster_size": 1, "status": "SCREENED_DIAGNOSTIC_ONLY",
          "cross_section_constant": False, "exact_formula_reuse": False,
          "structural_sign_mirror": None} for i in range(715)])
    profiles.to_csv(artifacts / "factor_profiles.csv", index=False)
    return run_dir / "run.json"


def test_candidate_pack_uses_train_only_and_one_representative(tmp_path):
    source = _source(tmp_path)
    _, _, _, eligible, selected = build_candidate_pack(source)
    assert eligible.factor.tolist() == ["A", "B"]
    assert selected.factor.tolist() == ["B"]
    assert selected.redundancy_cluster.tolist() == [1]


def test_candidate_pack_rejects_already_selected_source(tmp_path):
    source = _source(tmp_path, unsafe=True)
    with pytest.raises(ValueError, match="selection_parameters_searched"):
        build_candidate_pack(source)
