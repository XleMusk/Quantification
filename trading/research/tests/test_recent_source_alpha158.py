"""Synthetic scope/prefix checks, not market or model-performance evidence."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.research import recent_source_alpha158 as m


def test_active_scope_uses_each_own_raw_status_and_never_scores_halt_ST_or_missing_grid():
    raw = pd.DataFrame([
        {"date": day, "code": code, "tradestatus": status, "isST": st,
         "open": 10, "high": 11, "low": 9, "close": 10, "volume": volume,
         "amount": 100000, "pctChg": pct}
        for day, code, status, st, volume, pct in [
            ("2026-10-12", "sh.600000", "1", "0", "10000", "1"),
            ("2026-10-12", "sh.600001", "0", "0", "", ""),
            ("2026-10-12", "sh.600002", "1", "1", "10000", "1"),
            ("2026-10-13", "sh.600000", "0", "0", "", ""),
            ("2026-10-13", "sh.600001", "1", "0", "10000", "-1"),
        ]])
    index, coverage = m._active_scope(raw, ["2026-10-12", "2026-10-13"])
    assert index.tolist() == [(pd.Timestamp("2026-10-12"), "600000.SSE"), (pd.Timestamp("2026-10-13"), "600001.SSE")]
    assert [row["active_non_ST_valid_observations"] for row in coverage] == [1, 1]
    assert coverage[0]["suspended_rows_retained_unscored"] == 1
    assert coverage[0]["ST_rows_retained_unscored"] == 1


def test_future_date_does_not_change_earlier_frozen_scores_or_rank_pool(monkeypatch):
    def predict(frame, models, columns):
        result = pd.DataFrame({f"score_seed{seed}": frame["x"] * scale for seed, scale in ((17, 1), (29, -1), (43, 2))})
        result["rank_mean"] = result.rank(pct=True).mean(axis=1)
        return result
    monkeypatch.setattr(m, "_frozen_predictions", predict)
    index = pd.MultiIndex.from_product([pd.to_datetime(["2026-10-12", "2026-10-13"]), ["000001.SZSE", "600000.SSE"]], names=["date", "symbol"])
    earlier = pd.DataFrame({"x": [1, 2, 20, 40]}, index=index)
    future = pd.DataFrame({"x": [-500, 900]}, index=pd.MultiIndex.from_product([[pd.Timestamp("2026-10-14")], ["000001.SZSE", "600000.SSE"]], names=index.names))
    expected = m._datewise_predictions(earlier, {}, ["x"])
    actual = m._datewise_predictions(pd.concat([earlier, future]), {}, ["x"])
    pd.testing.assert_frame_equal(actual.loc[expected.index], expected)
    assert set(expected.index) == set(earlier.index)


def test_native_dated_query_retains_full_lookback_and_future_prefix_invariance(tmp_path):
    from trading.research.full_market_alpha158 import provider_rows
    from trading.research.factors import catalog, features_from_provider
    days = pd.bdate_range("2026-01-02", periods=85)
    source = pd.DataFrame([{"code": "600000", "date": int(day.strftime("%Y%m%d")),
        "open": 10 + i * .02, "high": 10.2 + i * .02, "low": 9.9 + i * .02,
        "close": 10.05 + i * .02, "volume": 10000 + (i % 7) * 1200 + i * 20,
        "amount": (10000 + (i % 7) * 1200 + i * 20) * (10.05 + i * .02),
        "pct_chg": .5 + 2 * np.sin(i * .7)} for i, day in enumerate(days)])
    small, complete = tmp_path / "prefix", tmp_path / "full"
    small.mkdir(); complete.mkdir()
    prefix_provider, _, _ = provider_rows(source.iloc[:82], small)
    full_provider, _, _ = provider_rows(source, complete)
    definitions = catalog()
    requested = days[80:82].strftime("%Y-%m-%d").tolist()
    prefix = features_from_provider(prefix_provider, ["600000.SSE"], requested, definitions, [])
    all_features = features_from_provider(full_provider, ["600000.SSE"], days.strftime("%Y-%m-%d").tolist(), definitions, [])
    pd.testing.assert_frame_equal(prefix, all_features.loc[prefix.index], check_exact=False, rtol=1e-7, atol=1e-8)
    assert len(prefix) == 2 and prefix.columns.tolist() == definitions.name.tolist()
    assert pd.notna(prefix["ROC60"]).all()
    assert pd.notna(prefix["CORD60"]).all()


def assets(tmp_path, monkeypatch):
    provider = tmp_path / "provider"; provider.mkdir()
    (provider / "result.json").write_text("{}", encoding="utf8")
    columns = [f"x{i}" for i in range(158)]
    definitions = pd.DataFrame({"name": columns, "formula": ["$close"] * 158})
    definitions.to_csv(provider / "alpha158_catalog.csv", index=False)
    symbols = ["000001.SZSE", "600000.SSE", "600001.SSE"]
    requested = ["2026-10-12", "2026-10-13"]
    raw = pd.DataFrame([{"date": day, "code": ("sz." if symbol.startswith("0") else "sh.") + symbol[:6],
        "tradestatus": "0" if symbol == "600001.SSE" else "1", "isST": "0", "open": 10,
        "high": 11, "low": 9, "close": 10, "volume": "" if symbol == "600001.SSE" else "10000",
        "amount": "100000", "pctChg": "1"} for day in requested for symbol in symbols])
    record = {"provider_method_id": "synthetic", "created_at": "2026-10-15T08:00:00+08:00", "source_proofs": [],
              "recent_raw_observations_file": str(provider / "raw.parquet"), "recent_raw_observations_sha256": "test"}
    monkeypatch.setattr(m, "_provider", lambda *args: (provider, record, provider, requested, requested, symbols, raw, definitions))
    model = tmp_path / "model.json"
    model.write_text("{}", encoding="utf8")
    definitions.to_csv(tmp_path / "alpha158_catalog.csv", index=False)
    folders = {}
    for seed in m.SEEDS:
        folder = tmp_path / str(seed); folder.mkdir()
        (folder / "model.txt").write_text("synthetic", encoding="utf8")
        (folder / "preprocessing.csv").write_text("synthetic", encoding="utf8")
        folders[seed] = folder
    metadata = {"models_refit": False, "config": {"segments": {"train": ["2024-01-02", "2024-12-31"], "valid": ["2025-01-02", "2025-12-31"]}}}
    monkeypatch.setattr(m, "_resolve_model", lambda _: (model, metadata, folders))
    def feature_builder(provider, batch, requested, definitions, windows):
        assert windows == []
        index = pd.MultiIndex.from_product([pd.to_datetime(requested), batch], names=["date", "symbol"]).sort_values()
        return pd.DataFrame(1., index=index, columns=definitions.name.tolist())
    def predict(frame, paths, columns):
        result = pd.DataFrame(1., index=frame.index, columns=[f"score_seed{seed}" for seed in m.SEEDS])
        result["rank_mean"] = .5
        assert "600001.SSE" not in frame.index.get_level_values("symbol")
        return result
    monkeypatch.setattr(m, "_frozen_predictions", predict)
    return provider, model, feature_builder, metadata


def test_whole_requested_grid_but_only_current_active_scores_no_account_mutation(tmp_path, monkeypatch):
    provider, model, builder, _ = assets(tmp_path, monkeypatch)
    out = tmp_path / "scores"
    result = m.run(provider, model, start="2026-10-12", end="2026-10-13", out=out, batch_size=2, _feature_builder=builder)
    assert result["native_symbols"] == 3 and result["native_feature_rows"] == 6
    assert result["score_rows"] == 4
    assert all(row["unobserved_or_out_of_scope_grid_rows"] == 1 for row in result["coverage"])
    assert all(result[key] is value for key, value in m.FLAGS.items())
    assert pd.read_parquet(result["score_path"]).symbol.nunique() == 2
    with pytest.raises(FileExistsError):
        m.run(provider, model, start="2026-10-12", end="2026-10-13", out=out, _feature_builder=builder)


def test_omitted_grid_member_fails_without_making_scores_or_targets(tmp_path, monkeypatch):
    provider, model, builder, _ = assets(tmp_path, monkeypatch)
    def broken(*args):
        return builder(*args).iloc[1:]
    out = tmp_path / "failed"
    with pytest.raises(ValueError, match="omitted"):
        m.run(provider, model, start="2026-10-12", end="2026-10-13", out=out, _feature_builder=broken)
    result = json.loads((out / "result.json").read_text(encoding="utf8"))
    assert result["status"] == "failed_recent_source_alpha158_scores"
    assert result["execution_admitted"] is False and not (out / "dated_scores.parquet").exists()


def test_label_overlap_rejected_before_creating_output(tmp_path, monkeypatch):
    provider, model, builder, metadata = assets(tmp_path, monkeypatch)
    metadata["config"]["segments"]["valid"][-1] = "2026-10-12"
    with pytest.raises(ValueError, match="labels overlap"):
        m.run(provider, model, start="2026-10-12", end="2026-10-13", out=tmp_path / "bad", _feature_builder=builder)
    assert not (tmp_path / "bad").exists()


def test_same_names_but_future_formula_cannot_claim_no_future_features(tmp_path, monkeypatch):
    provider, model, builder, _ = assets(tmp_path, monkeypatch)
    catalog = pd.read_csv(tmp_path / "alpha158_catalog.csv")
    catalog.loc[0, "formula"] = "Ref($close,-1)"
    catalog.to_csv(tmp_path / "alpha158_catalog.csv", index=False)
    with pytest.raises(ValueError, match="frozen training definitions"):
        m.run(provider, model, start="2026-10-12", end="2026-10-13", out=tmp_path / "bad", _feature_builder=builder)
    assert not (tmp_path / "bad").exists()
