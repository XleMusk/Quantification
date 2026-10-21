"""Fixed public-style feature ablation on the corrected shortline panel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .shortline_split import closed_label_window, purge_training_labels
from .shortline_style_quality import reject_known_legacy_sidecar, validate_sidecar_breadth


BASE_FEATURES = [
    "amount_rank_pct", "volume_rank_pct", "market_limitup_rate", "market_down_rate",
    "range_pct", "close_location", "ret_5_observed_rows", "ret_10_observed_rows", "ret_20_observed_rows",
]
PUBLIC_STYLE_FEATURES = ["market_breadth", "market_amount", "amount_rel20_pre", "pct_median20_pre"]


def _metric(fn, y, score):
    try:
        return float(fn(y, score))
    except ValueError:
        return None


def _merge_public_style_features(frame: pd.DataFrame, panel: Path, style_panel: Path | None) -> tuple[pd.DataFrame, dict]:
    """Attach the frozen emotion/style sidecar when the corrected panel lacks it."""
    missing = sorted(set(PUBLIC_STYLE_FEATURES) - set(frame.columns))
    if not missing:
        quality = validate_sidecar_breadth(frame, frame, panel)
        return frame, {"style_panel": None, "style_merge_rows": len(frame), "style_merge_match_rows": len(frame),
                       "signal_sidecar_quality": quality}
    if style_panel is None:
        raise ValueError("Explicit --style-panel is required; implicit emotion_event_features_v1.csv is retired after confirmed price/breadth contamination")
    source = Path(style_panel)
    reject_known_legacy_sidecar(source)
    if not source.exists():
        raise ValueError(f"Panel missing public-style features {missing} and sidecar not found: {source}")
    if source.suffix.lower() == ".csv":
        sidecar = pd.read_csv(source, parse_dates=["date"])
    else:
        sidecar = pd.read_parquet(source)
        sidecar["date"] = pd.to_datetime(sidecar["date"])
    keys = ["symbol", "date", "stage"]
    required = set(keys) | set(PUBLIC_STYLE_FEATURES)
    absent = sorted(required - set(sidecar.columns))
    if absent:
        raise ValueError(f"Style sidecar missing columns: {absent}")
    if sidecar.duplicated(keys).any():
        raise ValueError(f"Style sidecar has duplicate keys: {source}")
    quality = validate_sidecar_breadth(sidecar, frame, source)
    # Attach only absent columns so a panel that already carries a subset of
    # the style fields does not create pandas' duplicate ``_x``/``_y`` names.
    attach = sidecar[keys + missing]
    merged = frame.merge(attach, on=keys, how="left", validate="many_to_one", indicator="_public_style_match")
    # Keep a compact provenance/coverage record; the model's frozen imputer
    # handles sidecar gaps without changing the compared event population.
    info = {
        "style_panel": str(source.resolve()),
        "style_merge_rows": int(len(merged)),
        "style_merge_match_rows": int(merged["_public_style_match"].eq("both").sum()),
        "style_feature_nonnull_rows": int(merged[PUBLIC_STYLE_FEATURES].notna().all(axis=1).sum()),
        "signal_sidecar_quality": quality,
    }
    return merged.drop(columns=["_public_style_match"]), info


def run(panel: Path, output: Path | None = None, style_panel: Path | None = None) -> tuple[Path, dict]:
    frame = pd.read_parquet(panel)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.loc[frame.label_status.eq("complete") & frame.research_eligible.astype(bool)].copy()
    frame, style_info = _merge_public_style_features(frame, panel, style_panel)
    folds = [("2025", "2024-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
             ("2026_ytd", "2024-01-01", "2025-12-31", "2026-01-01", "2026-10-13")]
    variants = {"base_v1": BASE_FEATURES, "public_style_v2": BASE_FEATURES + PUBLIC_STYLE_FEATURES}
    rows = []
    for variant, features in variants.items():
        missing = sorted(set(features) - set(frame.columns))
        if missing:
            raise ValueError(f"Panel missing {variant} features: {missing}")
        for fold, train_start, train_end, test_start, test_end in folds:
            train, train_stats = purge_training_labels(frame, train_start, train_end)
            test, test_stats = closed_label_window(frame, test_start, test_end)
            for stage in (1, 2, 3):
                tr, te = train.loc[train.stage.eq(stage)], test.loc[test.stage.eq(stage)]
                if tr.empty or te.empty:
                    continue
                model = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                                       LogisticRegression(max_iter=1000, C=0.5, solver="lbfgs"))
                y_train, y_test = tr.hit_5.astype(int), te.hit_5.astype(int)
                model.fit(tr[features], y_train)
                probability = model.predict_proba(te[features])[:, 1]
                top = np.argsort(-probability, kind="stable")[:max(1, int(np.ceil(len(probability) * .10)))]
                rows.append({"variant": variant, "fold": fold, "stage": stage,
                             "train_events": len(tr), "test_events": len(te),
                             "purged_train_events": train_stats["purged_rows"],
                             "purged_test_events": test_stats["purged_rows"],
                             "test_hit5_rate": float(y_test.mean()),
                             "auc": _metric(roc_auc_score, y_test, probability),
                             "average_precision": _metric(average_precision_score, y_test, probability),
                             "top10pct_hit5_rate": float(y_test.iloc[top].mean()),
                             "execution_admitted": False})
    result_rows = pd.DataFrame(rows)
    identifier = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output).resolve() if output else Path(panel).parent / f"public_style_ablation_{identifier}"
    out.mkdir(parents=True, exist_ok=False)
    result_rows.to_csv(out / "metrics.csv", index=False)
    piv = result_rows.pivot_table(index=["fold", "stage"], columns="variant", values="auc")
    record = {"status": "completed_public_style_ablation", "panel": str(Path(panel).resolve()),
              "features": variants, "metrics": rows, "execution_admitted": False,
              "paper_target_admitted": False, "output": str(out),
              "style_feature_provenance": style_info,
              "limitations": ["Fixed ablation only; no parameter search or fresh holdout.",
                              "Close-based continuation labels are not executable fills.",
                              "No current preview, target, paper order or THS payload was generated."]}
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join(["# Public-style shortline feature ablation", "",
        "Base v1 versus fixed public-style v2 additions on the same purged time folds.",
        f"Output: `{out}`.", "No execution or paper target was admitted."]) + "\n", encoding="utf-8")
    return out / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--style-panel", type=Path,
                        help="Explicit corrected signal sidecar; the retired implicit v1 sidecar is refused")
    args = parser.parse_args(argv)
    path, result = run(args.panel, args.out, args.style_panel)
    print(json.dumps({"result": str(path), "status": result["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
