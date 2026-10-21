"""Small mixed-source provider contracts, without collection or model fitting."""
from copy import deepcopy
from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest

from trading.research import recent_source_qlib_provider as adapter
from trading.research.baostock_daily_slice import digest
from trading.research.leader_close_preview import canonical
from trading.research.leader_recent_source_history import compose_recent_sources
from trading.research.tests.test_leader_recent_source_preview import setup as recent_assets


@pytest.fixture
def assets(recent_assets, monkeypatch):
    root, calendar, snapshot, base, packages, calls = recent_assets
    for folder, (raw, source) in packages.items():
        day = source["plan"]["day"]
        source["receipt"]["updated_at"] = day + "T16:00:00+08:00"
        if day == "2026-10-13":
            # Different real contracts can expose extra columns. Per-day raw
            # projection must preserve those strings while other days get null.
            raw["turn"] = "0.123400"
            raw["peTTM"] = ""
        raw.to_csv(Path(folder) / "raw_daily.csv", index=False)
        source["receipt"]["raw_daily_sha256"] = digest(Path(folder) / "raw_daily.csv")
        (Path(folder) / "receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
    monkeypatch.setattr(adapter, "load_complete", lambda path: deepcopy(packages[str(Path(path).resolve())]))
    def read(path, *, end):
        calls.append(end)
        return base.copy(), {"snapshot": str(snapshot), "manifest_end": "2026-10-13", "prefixes": ["0", "3", "6"], "partitions": []}
    monkeypatch.setattr(adapter, "read_physical_snapshot", read)
    return root, calendar, snapshot, base, packages, calls


def build(assets, *, name="provider"):
    root, calendar, snapshot, _, packages, _ = assets
    return adapter.build(list(reversed(packages)), snapshot, calendar, root / name)


def composed(assets):
    _, calendar, snapshot, _, packages, _ = assets
    return compose_recent_sources(list(deepcopy(packages).values()), snapshot, calendar,
        history_reader=adapter.read_physical_snapshot)


def test_halt_is_excluded_before_old_builder_raw_inventory_and_nan_gaps_remain(assets, monkeypatch):
    root, calendar, _, original, packages, calls = assets
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    initial = original.copy(deep=True)
    real_provider = adapter.provider_rows
    captured = []
    def checked(frame, out):
        captured.append(frame.copy())
        assert np.isfinite(frame.pct_chg).all() and frame.volume.gt(0).all() and frame.amount.gt(0).all()
        return real_provider(frame, out)
    monkeypatch.setattr(adapter, "provider_rows", checked)
    out, result = build(assets)
    assert result["schema"] == adapter.SCHEMA and result["provider_method_id"] == adapter.METHOD_ID
    assert result["status"] == "research_provider_completed"
    assert calls == ["2026-10-09"] and len(captured) == 1
    assert result["calendar"] == [20261009, 20261012, 20261013, 20261014]
    assert result["symbols"] == result["source_cohort_symbols"] == 3 and result["rows"] == 12
    assert result["nan_only_provider_symbols"] == 0
    filtered = captured[0]
    assert not ((filtered.code == "000002") & (filtered.date == 20261012)).any()
    assert not filtered.close.eq(999).any()
    gap = np.fromfile(out / "qlib_provider/features/sz000002/close.day.bin", dtype="<f4")
    assert len(gap) == 5 and gap[0] == 0 and np.isnan(gap[2]) and gap[3] > 0
    quality = result["source_quality"]["adapter_activity_filter"]
    assert quality["recent_sources"]["2026-10-12"]["exclusion_reason_counts"] == {"recent_explicit_suspension": 1}
    assert quality["adapter_excluded_rows"] == 1 and quality["missing_values_filled"] is False
    assert result["base_history_end"] == "2026-10-09" and result["original_reader_rows_after_cutoff_ignored"] == 1
    assert result["calendar_sha256"] == digest(calendar)
    raw_path = Path(result["recent_raw_observations_file"])
    assert raw_path.is_absolute() and result["recent_raw_observations_sha256"] == digest(raw_path)
    raw = pd.read_parquet(raw_path)
    assert len(raw) == result["recent_raw_observation_rows"] == 7
    for original_raw, source in packages.values():
        day = source["plan"]["day"]
        projection = raw.loc[raw.date.eq(day), result["original_raw_fields_by_day"][day]].reset_index(drop=True)
        pd.testing.assert_frame_equal(projection, original_raw.reset_index(drop=True))
    assert raw.loc[raw.date.eq("2026-10-12"), "turn"].isna().all()
    assert [proof["day"] for proof in result["source_proofs"]] == result["recent_source_days"]
    assert all(result[key] is False for key in ("model_trained", "execution_admitted", "selection_enabled",
        "strategy_validated", "future_labels_used_for_score", "model_refit", "raw_prices_imputed"))
    assert result["normalization_matches_original_qfq_factor_training"] is False
    pd.testing.assert_frame_equal(original, initial)
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_entirely_unobserved_code_gets_full_nan_grid_not_fake_price_or_scope_drop(assets):
    _, _, _, _, packages, _ = assets
    raw, source = next(iter(packages.values()))
    addition = raw.iloc[[1]].copy()
    addition["code"] = "sh.600777"
    new_raw = pd.concat([raw, addition], ignore_index=True)
    packages[source["source_folder"]] = new_raw, source
    source["ordinary_roster"].append({"code": "sh.600777", "symbol": "600777.SSE", "code_name": "halt only"})
    out, result = build(assets)
    assert result["symbols"] == 4 and result["rows"] == 16
    assert result["fully_excluded_source_codes"] == ["600777"] and result["nan_only_provider_symbols"] == 1
    for field in adapter.FIELDS:
        values = np.fromfile(out / f"qlib_provider/features/sh600777/{field}.day.bin", dtype="<f4")
        assert len(values) == 5 and values[0] == 0 and np.isnan(values[1:]).all()
    lines = (out / "qlib_provider/instruments/all.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("\t")[0] for line in lines] == ["SZ000001", "SZ000002", "SH600099", "SH600777"]
    cohort = pd.read_csv(result["source_cohort_file"], dtype={"code": str})
    assert cohort.set_index("code").loc["600777", "provider_input_rows"] == 0
    assert "600777.SSE" in set(pd.read_parquet(result["recent_raw_observations_file"]).symbol)


@pytest.mark.parametrize("field,value,reason", [("pct_chg", np.nan, "return_missing_or_nonpositive_gross_factor"),
    ("pct_chg", -100., "return_missing_or_nonpositive_gross_factor"),
    ("amount", 0., "amount_missing_or_nonpositive"), ("volume", np.nan, "volume_missing_or_nonpositive"),
    ("high", 0., "OHLC_missing_nonpositive_or_inconsistent")])
def test_explicit_invalid_observation_reason_never_becomes_zero_return(assets, field, value, reason):
    source = composed(assets)
    mask = source["combined"].date.eq("2026-10-14") & source["combined"].code.eq("000001")
    source["combined"].loc[mask, field] = value
    accepted, quality, _ = adapter._provider_input(source)
    assert quality["recent_sources"]["2026-10-14"]["exclusion_reason_counts"] == {reason: 1}
    assert not ((accepted.code == "000001") & (accepted.date == 20261014)).any()


def test_history_status_is_explicitly_unknown_and_recent_unknown_status_is_not_active(assets):
    source = composed(assets)
    source["loaded_sources"][0][0].loc[lambda frame: frame.code.eq("sz.000001"), "tradestatus"] = "unknown"
    accepted, quality, _ = adapter._provider_input(source)
    assert quality["historical_native_prefix"]["explicit_trade_status_available"] is False
    assert quality["recent_sources"]["2026-10-12"]["exclusion_reason_counts"] == {
        "recent_explicit_suspension": 1, "recent_trade_status_unknown": 1}
    assert not ((accepted.code == "000001") & (accepted.date == 20261012)).any()


def test_whole_observed_day_cannot_disappear_when_filtering_invalid_rows(assets):
    source = composed(assets)
    source["combined"].loc[lambda frame: frame.date.eq("2026-10-14"), "volume"] = 0
    with pytest.raises(ValueError, match="shorten the global"):
        adapter._provider_input(source)


@pytest.mark.parametrize("status", ["running", "failed_stopped_no_retry"])
def test_partial_source_stops_before_any_raw_read_or_output(assets, monkeypatch, status):
    root, _, _, _, packages, _ = assets
    (Path(next(iter(packages))) / "receipt.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    monkeypatch.setattr(adapter, "load_complete", lambda *a: pytest.fail("Partial source cannot reach strict loading"))
    _, result = build(assets)
    assert result["status"] in {"waiting_source_incomplete", "blocked_failed_source"}
    assert result["output_directory_created"] is False and not (root / "provider").exists()


@pytest.mark.parametrize("change", ["clock", "calendar"])
def test_actual_source_clock_or_calendar_failure_precedes_output(assets, change):
    root, _, _, _, packages, _ = assets
    source = next(iter(packages.values()))[1]
    if change == "clock":
        source["receipt"]["updated_at"] = "2099-01-01T16:00:00+08:00"
    else:
        source["plan"]["calendar_sha256"] = "different"
    with pytest.raises(ValueError):
        build(assets)
    assert not (root / "provider").exists()


def test_provider_contract_is_accepted_by_existing_native_extractor(assets):
    from trading.research.qlib_native_features import build as native_build
    out, result = build(assets)
    _, native = native_build(out, out.parent / "native", batch_size=2)
    assert native["status"] == "full_native_features_completed", native.get("error")
    assert native["symbols"] == result["symbols"] and native["rows"] == result["rows"] and native["features"] == 158
    assert native["model_trained"] is native["execution_admitted"] is False


def test_new_cli_routes_only_dispatch_local_adapters(monkeypatch):
    import run_committee
    from trading.research import recent_source_alpha158
    calls = []
    monkeypatch.setattr(adapter, "main", lambda args: calls.append(("provider", args)) or 0)
    monkeypatch.setattr(recent_source_alpha158, "main", lambda args: calls.append(("score", args)) or 0)
    assert run_committee.main(["recent-source-qlib-provider", "--out", "new"]) == 0
    assert run_committee.main(["recent-source-alpha158", "--provider-view", "existing"]) == 0
    assert calls == [("provider", ["--out", "new"]), ("score", ["--provider-view", "existing"])]
