import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading.research import stockdb_factor_features as module


def _provider(tmp_path):
    provider_view = tmp_path / "provider-view"
    provider_folder = provider_view / "qlib_provider"
    (provider_folder / "calendars").mkdir(parents=True)
    (provider_folder / "instruments").mkdir()
    (provider_folder / "calendars" / "day.txt").write_text("2026-10-13\n", encoding="utf-8")
    (provider_folder / "instruments" / "all.txt").write_text(
        "000001.SZSE\t2026-10-13\t2026-10-13\n", encoding="utf-8")
    price_view = tmp_path / "stockdb-snapshot"
    price_view.mkdir()
    result = {
        "status": "research_provider_completed",
        "provider_folder": str(provider_folder),
        "price_view": str(price_view),
        "calendar": [20261013],
        "dates": 1,
        "symbols": 1,
        "fields": module.PROVIDER_FIELDS,
        "execution_admitted": False,
        "model_trained": False,
    }
    (provider_view / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return provider_view


def _native(tmp_path, provider_view):
    native_view = tmp_path / "native-view"
    native_view.mkdir()
    (native_view / "features_0000.parquet").write_bytes(b"fixture")
    result = {
        "status": "full_native_features_completed",
        "provider_view": str(provider_view),
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": False,
        "factor_selection_performed": False,
        "symbols": 1,
        "rows": 1,
        "partitions": [{"file": "features_0000.parquet", "symbols": ["000001.SZSE"], "rows": 1}],
    }
    (native_view / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return native_view


def _candidates(tmp_path):
    rows = []
    for library, count in module.EXPECTED_LIBRARY_COUNTS.items():
        for number in range(count):
            name = f"{library}_{number}"
            rows.append({
                "factor": f"{library}/{name}", "library": library, "original_name": name,
                "family": "CLOSE" if library == "Alpha360" else "ROC",
                "formula": "$close", "train_direction": "high",
                "candidate_decision": "selected_cluster_representative",
            })
    path = tmp_path / "candidate_factors.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path, pd.DataFrame(rows)


def test_stockdb_feed_and_native_identity_are_strict(tmp_path):
    provider_view = _provider(tmp_path)
    feed = module.load_stockdb_feed(provider_view)
    assert feed.folder == (tmp_path / "stockdb-snapshot").resolve()
    assert feed.calendar == ["2026-10-13"]
    assert feed.symbols == ["000001.SZSE"]
    native_view = _native(tmp_path, provider_view)
    receipt = module.validate_native_view(native_view, provider_view, feed)
    assert receipt["status"] == "full_native_features_completed"

    bad = json.loads((provider_view / "result.json").read_text(encoding="utf-8"))
    bad["execution_admitted"] = True
    (provider_view / "result.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="untrained, unadmitted"):
        module.load_stockdb_feed(provider_view)


def test_current_feature_entrypoint_writes_only_raw_190_features(tmp_path, monkeypatch):
    provider_view = _provider(tmp_path)
    native_view = _native(tmp_path, provider_view)
    candidate_path, candidates = _candidates(tmp_path)
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-10-13"), "000001.SZSE")], names=["date", "symbol"])

    def fake_builder(feed, source, selected, dates):
        assert source["bulk_inputs"]["native_view"] == str(native_view)
        assert list(dates) == [pd.Timestamp("2026-10-13")]
        return pd.DataFrame(np.ones((1, len(selected))), index=index, columns=selected.factor.tolist()), object()

    monkeypatch.setattr(module, "build_monthly_features", fake_builder)
    out, record = module.build_current_features(provider_view, native_view, candidate_path,
                                                output=tmp_path / "out")
    assert record["status"] == "completed_stockdb_factor_features"
    assert record["result"]["rows"] == 1
    assert record["result"]["factors"] == 190
    assert record["ranking_generated"] is False
    assert record["paper_orders_generated"] is False
    features = pd.read_parquet(Path(record["result"]["feature_path"]))
    assert features.columns.tolist() == candidates.factor.tolist()
    assert not list(Path(record["artifacts"]).glob("*ranking*"))
    assert not list(Path(record["artifacts"]).glob("*order*"))
    assert json.loads(Path(out).read_text(encoding="utf-8"))["execution_admitted"] is False


def test_current_feature_entrypoint_rejects_unpinned_date(tmp_path):
    provider_view = _provider(tmp_path)
    native_view = _native(tmp_path, provider_view)
    candidate_path, _ = _candidates(tmp_path)
    with pytest.raises(ValueError, match="outside the pinned provider calendar"):
        module.build_current_features(provider_view, native_view, candidate_path,
                                      dates=["2026-10-14"], output=tmp_path / "out")
