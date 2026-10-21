"""Saved annual input reuse, real feature/restoration behavior; no Engine."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_eight_type_annual_inputs as adapter
from trading.research import leader_eight_type_historical_native as native
from trading.research.baostock_daily_slice import digest
from trading.research.leader_daily_signals import _calendar_view


A, B = "600001.SSE", "600002.SSE"
CLOCK = "2026-10-18T03:30:00+08:00"


def _fixture(tmp_path, monkeypatch):
    parent, source_root = tmp_path / "annual", tmp_path / "native"
    parent.mkdir()
    source_root.mkdir()
    calendar = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2025,
                "closed_ranges": ["2025-01-01/2025-01-01"], "open_sessions": []}
    sessions, _, _ = _calendar_view(calendar, "2025-01-01", "2025-12-31", [])
    days = sessions[:7]
    prior = pd.bdate_range("2024-01-01", "2024-12-31").strftime("%Y-%m-%d").tolist()
    warmup, older = prior[-61:], prior[-62]
    halted = {warmup[10], warmup[20]}
    calendar_path, prior_path, master = (tmp_path / name for name in ("2025.json", "2024.json", "master.json"))
    native._save(calendar_path, calendar)
    native._save(prior_path, {"schema": calendar["schema"], "year": 2024,
                            "closed_ranges": [], "open_sessions": []})
    native._save(master, {"fixture": "saved_listing_source"})
    units = tmp_path / "turn_units.json"
    native._save(units, {"status": "official_provider_document_read", "turn_unit": "percent",
                        "retrieved_at": "2026-10-01T10:00:00+08:00"})
    monkeypatch.setattr(adapter, "UNIT_EVIDENCE", units)
    monkeypatch.setattr(native, "_now", lambda: CLOCK)
    records = {symbol: {"ipo_date": "2000-01-01", "reported_out_date": "", "board": "main",
                         "available_at": "2026-10-08T12:00:00+08:00", "source_basis": "fixture_saved_master"}
               for symbol in (A, B)}
    proof = {"master_run": str(master), "master_run_sha256": digest(master),
             "master_file": str(master), "master_sha256": digest(master),
             "original_available_at": "2026-10-08T12:00:00+08:00",
             "calendar": str(calendar_path), "calendar_sha256": digest(calendar_path),
             "posthoc_observed_at": CLOCK}

    def qualification(*args, observed_at, prior_calendars=()):
        verification = {**proof, "posthoc_observed_at": observed_at}
        if prior_calendars:
            verification["prior_calendars"] = [{"path": str(prior_path), "sha256": digest(prior_path)}]
        return native.PosthocListingQualification(deepcopy(records), tuple(sessions), verification,
                                                   tuple(prior + sessions) if prior_calendars else ())

    monkeypatch.setattr(native, "load_posthoc_listing", qualification)
    witness = {symbol: warmup[:5] for symbol in (A, B)}

    def rows(day, symbols=(A, B)):
        values = []
        for symbol in symbols:
            suspended = symbol == A and day in halted
            close = "11" if symbol == A and day >= days[0] else "10"
            previous = "11" if symbol == A and day > days[0] else "10"
            values.append({"date": day, "symbol": symbol, "code": "sh." + symbol[:6],
                "open": "10.6" if symbol == A and day == days[0] else close,
                "high": close, "low": "10.5" if symbol == A and day == days[0] else close,
                "close": close, "preclose": previous,
                "volume": "" if suspended else "10000000",
                "amount": "" if suspended else "100000000", "adjustflag": "3",
                "turn": "999" if day == days[-1] else "2" if symbol == A and day >= days[0] else "1",
                "tradestatus": "0" if suspended else "1", "pctChg": "10" if symbol == A and day == days[0] else "0",
                "peTTM": "", "pbMRQ": "", "psTTM": "", "pcfNcfTTM": "", "isST": "0"})
        return pd.DataFrame(values)

    raw = pd.concat([rows(day) for day in days], ignore_index=True)
    raw.to_parquet(parent / "raw_daily.parquet", index=False)
    rows(prior[-1]).to_parquet(parent / "context.parquet", index=False)
    native._save(parent / "cohort.json", {"first_D": days[0], "scope_members": [B, A],
        "future_membership_used": False, "valid_count_saturated_at61": {A: 61, B: 61}})
    native._save(parent / "listing_witness.json", witness)
    source_proof = {"source_view": str(source_root), "opened_at": CLOCK}
    for key in ("result", "plan", "receipt"):
        path = source_root / (key + ".json")
        native._save(path, {"fixture": key})
        source_proof.update({key + "_file": str(path), key + "_sha256": digest(path)})
    pin = lambda path: {"path": str(path), "sha256": digest(path)}
    plan = {"schema": adapter.ANNUAL_SCHEMA, "config": adapter._config(100000, 5).to_dict(),
            "start": days[0], "end": days[0], "through": days[-1],
            "calendars": [pin(calendar_path)], "master": pin(master), "native_source": source_proof}
    native._save(parent / "study_plan.json", plan)
    manifest = {"schema": adapter.ANNUAL_SCHEMA, "status": "completed_annual_inputs",
        "created_at": CLOCK, **native.FLAGS, "start": days[0], "end": days[0], "through": days[-1],
        "source_days": days, "context_day": prior[-1], "cohort_count": 2, "raw_rows": len(raw),
        "study_plan": pin(parent / "study_plan.json"),
        "files": {name: pin(parent / name) for name in adapter.CACHE_FILES},
        "qualification": native.with_prior_listing_witness(qualification(observed_at=CLOCK), witness, days[0]).verification}
    native._save(parent / "manifest.json", manifest)
    calls = []

    class Source:
        calendar, provenance = prior + sessions, source_proof

        def __init__(self, path):
            assert path == source_root

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def _unchanged(self):
            pass

        def frame(self, day, symbols=None):
            calls.append((day, symbols))
            assert day < prior[-1], "Cached context/annual groups must not be reread"
            result = rows(day, symbols)
            result.attrs["provenance"] = {"day": day, "row_group": self.calendar.index(day), "symbols": symbols}
            return result

    monkeypatch.setattr(adapter, "NativeHistorySource", Source)
    return parent, source_root, prior_path, days, warmup, older, raw, calls


def test_saved_annual_rows_feed_original_features_and_only_missing_symbol_warmup(tmp_path, monkeypatch):
    parent, source, prior, days, warmup, older, raw, calls = _fixture(tmp_path, monkeypatch)
    before = {name: (parent / name).read_bytes() for name in adapter.CACHE_FILES}
    output = tmp_path / "eight_types"
    result = adapter.build_inputs(parent, source, output, prior_calendars=[prior], restore_valid_warmup=True)
    assert result["scope_members"] == [A, B]  # Original annual order [B,A] keeps exactly the same set.
    assert calls[:60] == [(day, [A, B]) for day in warmup[:-1]]
    assert calls[60:] == [(older, [A])]  # B's original buffer was already complete.
    assert result["raw_file"] == str(parent / "raw_daily.parquet")
    assert result["context_raw_file"] == str(parent / "context.parquet")
    assert result["annual_cache_reuse"]["annual_native_groups_reread"] == 0
    assert result["annual_cache_reuse"]["monthly_momentum_batches_used_as_eight_type_features"] is False
    loaded, _ = native.load_inputs(output / "manifest.json")
    pd.testing.assert_frame_equal(loaded["raw"], raw, check_exact=True)
    panel = loaded["panel_by_day"][days[0]]
    assert panel["risk_features"][A]["date"] == days[0]
    assert panel["risk_features"][A]["sma60"] is not None
    turnover = next(item for item in panel["types"] if item["id"] == "turnover_leader")
    own = next(item for item in turnover["candidates"] if item["symbol"] == A)
    assert own["evidence"]["current_turn_percent"] == "2"
    assert own["evidence"]["previous_turn_percent"] == "1"
    for identity in ("cross_regime_candidate", "sector_catchup_leader"):
        item = next(item for item in panel["types"] if item["id"] == identity)
        assert A in item["unresolved_symbols"] and item["status"] == "unknown"
    assert result["fixed_protocol"] == adapter.PROTOCOL and result["pit_verified"] is False
    assert {name: (parent / name).read_bytes() for name in adapter.CACHE_FILES} == before

    # Reproduce the already-built legacy manifest's order-only loader failure,
    # then reuse its immutable payloads without invoking the producer again.
    legacy_folder = tmp_path / "legacy_unsorted"
    legacy_folder.mkdir()
    legacy = deepcopy(result)
    legacy["scope_members"] = [B, A]
    legacy_path = legacy_folder / "manifest.json"
    native._save(legacy_path, legacy)
    archive = legacy_folder / "source_implementation_before_order_fix.py"
    archive.write_bytes(Path(adapter.__file__).read_bytes())
    original_manifest_bytes = legacy_path.read_bytes()
    with pytest.raises(ValueError, match="Restored native prefix differs"):
        native.load_inputs(legacy_path)
    saved_read = pd.read_parquet
    with monkeypatch.context() as local:
        def only_restored_rows(path, **kwargs):
            assert str(path) == result["warmup_restoration"]["raw_file"]
            return saved_read(path, **kwargs)

        def no_rebuild(*args, **kwargs):
            raise AssertionError("Scope-order repair must not rebuild or open native sources")

        local.setattr(adapter.pd, "read_parquet", only_restored_rows)
        local.setattr(adapter, "NativeHistorySource", no_rebuild)
        local.setattr(native, "derive_panels", no_rebuild)
        repaired = adapter.repair_scope_order(legacy_path, tmp_path / "repaired")
    assert repaired["scope_members"] == [A, B]
    assert repaired["annual_scope_order_repair"]["original_adapter_archive"]["sha256"] == digest(archive)
    assert repaired["annual_scope_order_repair"]["parent_manifest"]["sha256"] == digest(legacy_path)
    for key in legacy:
        if key not in {"scope_members", "created_at"}:
            assert repaired[key] == legacy[key]
    assert legacy_path.read_bytes() == original_manifest_bytes
    reloaded, _ = native.load_inputs(tmp_path / "repaired" / "manifest.json")
    assert reloaded["panel_by_day"] == loaded["panel_by_day"]
    pd.testing.assert_frame_equal(reloaded["raw"], raw, check_exact=True)


def test_changed_annual_turn_bytes_are_rejected_before_any_source_read_or_output(tmp_path, monkeypatch):
    parent, source, prior, _, _, _, raw, calls = _fixture(tmp_path, monkeypatch)
    changed = raw.copy()
    changed.loc[0, "turn"] = "1234"
    changed.to_parquet(parent / "raw_daily.parquet", index=False)
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="Saved annual source changed"):
        adapter.build_inputs(parent, source, output, prior_calendars=[prior], restore_valid_warmup=True)
    assert calls == [] and not output.exists()
