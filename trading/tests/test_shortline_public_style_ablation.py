import pandas as pd
import pytest

from trading.research.shortline_public_style_ablation import (
    PUBLIC_STYLE_FEATURES,
    _merge_public_style_features,
)


def test_style_sidecar_only_attaches_missing_columns_without_suffixes(tmp_path):
    frame = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "date": [pd.Timestamp("2025-01-02")],
            "stage": [1],
            "market_breadth": [0.25],
            "market_row_count": [100], "market_up_count": [50], "market_down_count": [25],
        }
    )
    sidecar = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "date": [pd.Timestamp("2025-01-02")],
            "stage": [1],
            **{name: [float(i + 1)] for i, name in enumerate(PUBLIC_STYLE_FEATURES)},
            "market_breadth": [0.25],
        }
    )
    sidecar_path = tmp_path / "style.csv"
    sidecar.to_csv(sidecar_path, index=False)

    merged, info = _merge_public_style_features(frame, tmp_path / "panel.parquet", sidecar_path)

    assert not any(name.endswith(("_x", "_y")) for name in merged.columns)
    assert merged.loc[0, "market_breadth"] == 0.25
    assert merged.loc[0, "market_amount"] == 2.0
    assert info["style_merge_match_rows"] == 1
    assert info["style_feature_nonnull_rows"] == 1
    assert info["signal_sidecar_quality"]["checked_dates"] == 1


def test_implicit_or_explicit_contaminated_v1_is_rejected(tmp_path):
    frame = pd.DataFrame({"symbol": ["000001.SZSE"], "date": [pd.Timestamp("2026-01-05")], "stage": [1]})
    legacy = tmp_path / "emotion_event_features_v1.csv"
    legacy.write_text("do not read this retired sidecar", encoding="utf-8")
    with pytest.raises(ValueError, match="Explicit --style-panel"):
        _merge_public_style_features(frame, tmp_path / "panel.parquet", None)
    with pytest.raises(ValueError, match="Known contaminated"):
        _merge_public_style_features(frame, tmp_path / "panel.parquet", legacy)


def test_renamed_breadth_mismatch_fails_and_corrected_sidecar_loads_without_fit(tmp_path, monkeypatch):
    from sklearn.linear_model import LogisticRegression

    def forbid_fit(*args, **kwargs):
        raise AssertionError("Sidecar quality reading must not fit a model")

    monkeypatch.setattr(LogisticRegression, "fit", forbid_fit)
    frame = pd.DataFrame({
        "symbol": ["000001.SZSE"], "date": [pd.Timestamp("2026-01-05")], "stage": [1],
        "market_row_count": [100], "market_up_count": [60], "market_down_count": [20],
    })
    sidecar = pd.DataFrame({
        "symbol": ["000001.SZSE"], "date": ["2026-01-05"], "stage": [1],
        "market_breadth": [0.8], "market_amount": [1000000],
        "amount_rel20_pre": [.8], "pct_median20_pre": [.1],
    })
    source = tmp_path / "renamed.csv"
    sidecar.to_csv(source, index=False)
    with pytest.raises(ValueError, match="market_breadth mismatch"):
        _merge_public_style_features(frame, tmp_path / "panel.parquet", source)
    sidecar["market_breadth"] = .4
    sidecar.to_csv(source, index=False)
    merged, info = _merge_public_style_features(frame, tmp_path / "panel.parquet", source)
    assert merged["market_breadth"].tolist() == [.4]
    assert info["signal_sidecar_quality"]["max_absolute_breadth_delta"] == 0
    assert info["signal_sidecar_quality"]["model_refit"] is False
    assert info["signal_sidecar_quality"]["execution_admitted"] is False
