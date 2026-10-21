import json

import pandas as pd
import pytest

from trading.research.factors import catalog, write_provider
from trading.research.qlib_native_features import build


def source_provider(tmp_path):
    view = tmp_path / "provider"
    view.mkdir()
    calendar = ["2023-01-03", "2023-01-04"]
    symbols = ["000001.SZSE", "600000.SSE", "688485.SSE"]
    index = pd.MultiIndex.from_product([pd.to_datetime(calendar), symbols], names=["date", "symbol"])
    panel = pd.DataFrame({"open": 10., "high": 11., "low": 9., "close": 10., "volume": 1000.,
                          "vwap": 10., "factor": 1.}, index=index).reset_index()
    provider = view / "qlib_provider"
    write_provider(panel, calendar, provider)
    catalog().to_csv(view / "alpha158_catalog.csv", index=False)
    (view / "result.json").write_text(json.dumps({"status": "research_provider_completed", "provider_folder": str(provider),
        "execution_admitted": False, "model_trained": False, "dates": 2, "symbols": 3,
        "fields": ["open", "high", "low", "close", "volume", "vwap", "factor"]}))
    return view


def test_native_batches_cover_every_code_calendar_and_definition_without_selection(tmp_path):
    view = source_provider(tmp_path)
    folder, result = build(view, tmp_path / "features", batch_size=2)
    assert result["status"] == "full_native_features_completed", result.get("error")
    assert (result["symbols"], result["rows"], result["features"], result["binary_files"]) == (3, 6, 158, 21)
    assert [part["rows"] for part in result["partitions"]] == [4, 2]
    frame = pd.concat([pd.read_parquet(folder / part["file"]) for part in result["partitions"]]).sort_index()
    assert not frame.index.has_duplicates and len(frame.columns) == 158
    assert frame.index.get_level_values("symbol").unique().tolist() == ["000001.SZSE", "600000.SSE", "688485.SSE"]
    assert not result["model_trained"] and not result["factor_selection_performed"] and not result["execution_admitted"]


def test_binary_truncation_fails_before_any_native_feature_partition(tmp_path):
    view = source_provider(tmp_path)
    (view / "qlib_provider/features/sh600000/close.day.bin").write_bytes(b"broken")
    folder, result = build(view, tmp_path / "features")
    assert result["status"] == "failed_native_features" and "calendar length" in result["error"]
    assert result["symbols"] == result["rows"] == 0 and not list(folder.glob("features_*.parquet"))


def test_missing_native_identities_cannot_be_redefined_as_a_completed_subset(tmp_path, monkeypatch):
    from trading.research import qlib_native_features
    view = source_provider(tmp_path)
    monkeypatch.setattr(qlib_native_features, "features_from_provider", lambda *args: pd.DataFrame())
    _, result = build(view, tmp_path / "features")
    assert result["status"] == "failed_native_features" and "omitted/relabelled" in result["error"]
    assert result["symbols"] == 0


@pytest.mark.parametrize("size", [0, 513])
def test_memory_batch_setting_never_changes_scope_or_creates_invalid_job(tmp_path, size):
    with pytest.raises(ValueError, match="Batch size"):
        build(tmp_path / "unused", tmp_path / "output", batch_size=size)
    assert not (tmp_path / "output").exists()


def test_project_route_only_dispatches_native_feature_command(monkeypatch):
    import run_committee
    from trading.research import qlib_native_features
    calls = []
    monkeypatch.setattr(qlib_native_features, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["qlib-native-features", "--provider-view", "local"]) == 0
    assert calls == [["--provider-view", "local"]]
