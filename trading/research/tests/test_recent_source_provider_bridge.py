"""Explicit history-prefix bridge, without network, accounts or run outputs."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_recent_source_history as history
from trading.research import leader_recent_source_preview as recent
from trading.research import provider_close_batches as provider
from trading.research.leader_forward_run import _check_source_binding
from trading.research.tests.test_leader_recent_source_preview import setup as recent_assets


@pytest.fixture
def assets(recent_assets, monkeypatch):
    root, calendar, snapshot, original, packages, base_calls = recent_assets
    for folder, (raw, source) in packages.items():
        day = source["plan"]["day"]
        source["plan"].update(schema="baostock_full_dated_close_inventory_v1", symbols=[row["symbol"] for row in source["ordinary_roster"]],
                              ordinary_symbols=len(raw))
        source["receipt"].update(schema="baostock_full_dated_close_inventory_v1", updated_at=day+"T16:00:00+08:00",
            created_at=day+"T15:01:00+08:00")
        for row, status in zip(source["ordinary_roster"], raw.tradestatus):
            row["tradeStatus"] = status
        folder = Path(folder)
        (folder/"plan.json").write_text(json.dumps(source["plan"]), encoding="utf-8")
        (folder/"receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
        (folder/"roster").mkdir()
        (folder/"roster"/"receipt.json").write_text(json.dumps({"created_at": day+"T15:00:01+08:00", "completed_at": day+"T15:00:02+08:00"}), encoding="utf-8")
    result = recent.derive_previews(list(packages), snapshot, calendar, root/"view")
    view = root/"view"/"recent_source_lineage.json"
    counts = {}
    def load(folder):
        key = str(Path(folder).resolve())
        counts[key] = counts.get(key, 0) + 1
        return deepcopy(packages[key])
    monkeypatch.setattr(provider.close_source, "load_complete", load)
    def read(path, *, end):
        base_calls.append(end)
        return original.copy(deep=True), {"snapshot": str(snapshot), "manifest_end": "2026-10-13", "prefixes": ["0", "3", "6"], "partitions": []}
    monkeypatch.setattr(provider, "read_physical_snapshot", read)
    base_calls.clear()
    return root, calendar, snapshot, original, packages, view, counts, base_calls


def build(assets, days=("2026-10-14",)):
    _, calendar, snapshot, _, packages, view, _, _ = assets
    chosen = [folder for folder, (_, source) in packages.items() if source["plan"]["day"] in days]
    return provider.build_provider_close_bundle(chosen, snapshot, calendar, recent_source_view=view)


def test_helper_binds_manifest_only_without_strict_raw_load(assets, monkeypatch):
    _, _, _, _, _, view, _, _ = assets
    monkeypatch.setattr(provider.close_source, "load_complete", lambda *a: pytest.fail("Manifest helper must not strictload raw"))
    binding = history.read_recent_source_view(view)
    assert binding["method_id"] == "fixed_recent_provider_history_prefix_v1"
    assert binding["base_history_end"] == "2026-10-09"
    assert binding["view_sha256"] == history.digest(view)
    assert binding["view_sessions"] == ["2026-10-12", "2026-10-13", "2026-10-14"]


def test_exact_overlap_reuses_current_load_and_only_current_days_form_signal_batches(assets):
    _, _, _, original, _, view, loads, reads = assets
    untouched = original.copy(deep=True)
    result = build(assets)
    _check_source_binding(result, {"history_source_view": result["recent_source_history"]})
    with pytest.raises(ValueError, match="explicit source-history supplement"):
        _check_source_binding(result, {})
    assert reads == ["2026-10-09"] and len(loads) == 3 and set(loads.values()) == {1}
    assert result["source_days_complete"] == ["2026-10-14"]
    assert list(result["provider_inventory_by_day"]) == ["2026-10-14"]
    for batches in result["batches"].values():
        assert list(batches) == ["2026-10-14"]
        proof = batches["2026-10-14"]["recent_source_history"]
        assert proof["source_days_used"] == ["2026-10-12", "2026-10-13", "2026-10-14"]
        assert [item["day"] for item in proof["source_proofs"]] == proof["source_days_used"]
        assert proof["view_sha256"] == history.digest(view)
    assert result["candidate_table"].date.eq("2026-10-14").all()
    rows = result["raw_bars"]
    assert not rows.duplicated(["date", "symbol"]).any()
    assert rows.loc[rows.date.eq("2026-10-13") & rows.symbol.eq("000001.SZSE"), "close"].item() == 12.1
    assert rows.loc[rows.date.ge("2026-10-12"), "provider_raw_observation"].eq(True).all()
    assert rows.loc[rows.date.eq("2026-10-12") & rows.symbol.eq("000002.SZSE"), "volume"].isna().all()
    assert "600099.SSE" in set(rows.loc[rows.date.eq("2026-10-12"), "symbol"])
    assert result["source_metadata"]["recent_source_history"] == result["recent_source_history"]
    assert result["recent_source_history"] == history.read_recent_source_view(view)
    roles = {item["day"]: item["roles"] for item in result["source_packages"]}
    assert roles["2026-10-13"] == ["recent_history_prefix"]
    assert roles["2026-10-14"] == ["recent_history_prefix", "requested_current"]
    pd.testing.assert_frame_equal(original, untouched)


def future_source(assets, day, previous_close, close):
    root, calendar, _, _, packages, _, _, _ = assets
    original = next(value for value in packages.values() if value[1]["plan"]["day"] == "2026-10-14")
    raw, source = deepcopy(original)
    raw["date"] = day
    raw[["open", "high", "low", "close"]] = str(close)
    raw["preclose"] = str(previous_close)
    raw["pctChg"] = str((close/previous_close-1)*100)
    raw["amount"] = pd.to_numeric(raw.volume)*close
    raw["amount"] = raw.amount.astype(str)
    folder = root/day.replace("-", "")
    folder.mkdir()
    raw.to_csv(folder/"raw_daily.csv", index=False)
    source["source_folder"] = str(folder.resolve())
    source["plan"].update(day=day)
    source["receipt"].update(day=day, raw_daily_sha256=history.digest(folder/"raw_daily.csv"),
        updated_at=day+"T16:00:00+08:00", created_at=day+"T15:01:00+08:00")
    (folder/"plan.json").write_text(json.dumps(source["plan"]), encoding="utf-8")
    (folder/"receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
    (folder/"roster").mkdir()
    (folder/"roster"/"receipt.json").write_text(json.dumps({"completed_at": day+"T15:00:02+08:00"}), encoding="utf-8")
    packages[str(folder.resolve())] = raw, source


def after_future_close(monkeypatch):
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-19T18:00:00+08:00")
    monkeypatch.setattr(history, "datetime", Later)


def test_future_sources_use_fixed_prefix_as_history_without_future_proofs_in_earlier_batch(assets, monkeypatch):
    after_future_close(monkeypatch)
    future_source(assets, "2026-10-15", 13.31, 14.641)
    first = build(assets, ("2026-10-15",))
    future_source(assets, "2026-10-16", 14.641, 16.1051)
    extended = build(assets, ("2026-10-15", "2026-10-16"))
    _check_source_binding(extended, {"history_source_view": extended["recent_source_history"]})
    assert extended["source_days_complete"] == ["2026-10-15", "2026-10-16"]
    for family in provider.FAMILIES:
        before = first["batches"][family]["2026-10-15"]
        after = extended["batches"][family]["2026-10-15"]
        assert before == after
        assert before["recent_source_history"]["source_days_used"][-1] == "2026-10-15"
        assert all(proof["day"] <= "2026-10-15" for proof in before["recent_source_history"]["source_proofs"])


def test_gap_between_prefix_and_current_is_rejected_without_inventing_a_missing_session(assets, monkeypatch):
    after_future_close(monkeypatch)
    future_source(assets, "2026-10-16", 13.31, 14.641)
    with pytest.raises(ValueError, match="consecutive"):
        build(assets, ("2026-10-16",))


def test_prefix_current_overlap_requires_same_folder_even_with_equal_raw_hash(assets):
    root, _, _, _, packages, _, _, _ = assets
    original_folder = next(folder for folder, (_, source) in packages.items() if source["plan"]["day"] == "2026-10-14")
    raw, source = deepcopy(packages[original_folder])
    other = root/"different_14_source"
    other.mkdir(); source["source_folder"] = str(other.resolve())
    packages[str(other.resolve())] = raw, source
    _, calendar, snapshot, _, _, view, _, _ = assets
    with pytest.raises(ValueError, match="exact same source folder"):
        provider.build_provider_close_bundle(other, snapshot, calendar, recent_source_view=view)


@pytest.mark.parametrize("changed", ["native_manifest", "future_clock", "boundary", "source_hash"])
def test_changed_frozen_view_or_proof_is_rejected(assets, changed):
    _, _, snapshot, _, packages, view, _, _ = assets
    if changed == "native_manifest":
        (snapshot/"manifest.json").write_text("[]", encoding="utf-8")
    elif changed == "source_hash":
        first = next(source for _, source in packages.values() if source["plan"]["day"] == "2026-10-12")
        first["receipt"]["raw_daily_sha256"] = "changed"
    else:
        value = json.loads(view.read_text())
        value["derived_at" if changed == "future_clock" else "execution_admitted"] = "2099-01-01T00:00:00+08:00" if changed == "future_clock" else True
        view.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        build(assets)


def test_explicit_prefix_does_not_admit_earlier_signal_days_or_change_default_append_only(assets):
    _, calendar, snapshot, _, packages, _, _, _ = assets
    with pytest.raises(ValueError, match="cannot precede"):
        build(assets, ("2026-10-13",))
    current = next(folder for folder, (_, source) in packages.items() if source["plan"]["day"] == "2026-10-14")
    default = provider.build_provider_close_bundle(current, snapshot, calendar)
    assert "recent_source_history" not in default and "recent_source_history" not in default["source_metadata"]
    assert all("recent_source_history" not in batch for values in default["batches"].values() for batch in values.values())
    original_13 = default["raw_bars"].loc[lambda rows: rows.date.eq("2026-10-13") & rows.symbol.eq("000001.SZSE")]
    assert original_13.close.item() == 999 and original_13.provider_raw_observation.isna().all()
