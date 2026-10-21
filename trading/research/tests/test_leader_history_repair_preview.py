"""Synthetic source-boundary checks, not market/strategy evidence."""
import json

import pandas as pd
import pytest

from trading.research import leader_environment_run, leader_history_repair_preview as repair
from trading.research.baostock_daily_slice import FIELDS, digest


def close(day, rows):
    return pd.DataFrame([[day, code, price, price, price, price, previous, volume, amount,
                          "3", status, pct, "0"]
                         for code, price, previous, pct, status, volume, amount in rows],
                        columns=FIELDS.split(","))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}), encoding="utf-8")
    history = tmp_path / "history"
    history.mkdir()
    (history / "manifest.json").write_text("{}", encoding="utf-8")
    repair_raw = close("2026-10-12", [
        ("sz.000001", "11", "10", "10", "1", "10000000", "110000000"),
        ("sz.000002", "10", "10", "", "0", "", ""),
        ("sh.600099", "10", "10", "0", "1", "10000000", "100000000"),
    ])
    latest_raw = close("2026-10-14", [
        ("sz.000001", "13.31", "12.1", "10", "1", "10000000", "133100000"),
        ("sz.000002", "12.1", "11", "10", "1", "10000000", "121000000"),
    ])
    base = pd.concat([
        repair.canonical(close("2026-10-09", [
            ("sz.000001", "10", "10", "0", "1", "10000000", "100000000"),
            ("sz.000002", "10", "10", "0", "1", "10000000", "100000000"),
        ])),
        repair.canonical(close("2026-10-13", [
            ("sz.000001", "12.1", "11", "10", "1", "10000000", "121000000"),
            ("sz.000002", "11", "10", "10", "1", "10000000", "110000000"),
        ])),
    ], ignore_index=True)
    metadata = {"snapshot": str(history), "prefixes": ["0", "3", "6"], "partitions": []}
    packages = {}
    for key, raw in (("repair", repair_raw), ("current", latest_raw)):
        folder = tmp_path / key
        folder.mkdir()
        raw.to_csv(folder / "raw_daily.csv", index=False)
        source = {"source_folder": str(folder), "read_only_verified": True,
                  "plan": {"day": raw.date.iloc[0], "calendar_sha256": digest(calendar), "roster_raw_sha256": "synthetic"},
                  "receipt": {"day": raw.date.iloc[0], "status": repair.COMPLETE,
                              "raw_daily_sha256": digest(folder / "raw_daily.csv"),
                              "full_dated_roster_inventory_complete": True},
                  "ordinary_roster": [{"code": code, "symbol": code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE"),
                                       "code_name": "synthetic"} for code in raw.code]}
        (folder / "receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
        packages[str(folder)] = (raw, source)
    monkeypatch.setattr(repair, "load_complete", lambda path: packages[str(path)])
    monkeypatch.setattr(repair, "read_physical_snapshot", lambda *args, **kwargs: (base.copy(deep=True), metadata))
    return tmp_path, calendar, history, base, packages


def run(inputs):
    root, calendar, history, _, _ = inputs
    return repair.derive_preview(root / "repair", root / "current", history, calendar, root / "out")


def test_partial_source_waits_without_reserving_final_out_or_loading_raw(inputs, monkeypatch):
    root, _, _, _, _ = inputs
    path = root / "repair" / "receipt.json"
    saved = json.loads(path.read_text())
    saved.update(status="running", full_dated_roster_inventory_complete=False,
                 completed_symbols=20, ordinary_symbols=5223)
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setattr(repair, "load_complete", lambda _: pytest.fail("Partial aggregate must not be read"))
    result = run(inputs)
    assert result["status"] == "waiting_source_incomplete"
    assert result["sources"][0]["completed_symbols"] == 20
    assert not (root / "out").exists()
    assert result["execution_admitted"] is False


def test_failed_source_blocks_without_candidate_fallback(inputs):
    root, _, _, _, _ = inputs
    (root / "repair" / "receipt.json").write_text(json.dumps({"status": "failed_stopped_no_retry"}), encoding="utf-8")
    result = run(inputs)
    assert result["status"] == "blocked_failed_source"
    assert not (root / "out").exists()


def test_mixed_view_fills_only_empty_session_keeps_own_roster_and_current_raw(inputs, monkeypatch):
    root, _, _, original, packages = inputs
    untouched = original.copy(deep=True)
    result = run(inputs)
    current = pd.read_csv(root / "out" / "current_features.csv", dtype={"code": str, "symbol": str})
    first = current.set_index("symbol").loc["000001.SZSE"]
    second = current.set_index("symbol").loc["000002.SZSE"]
    assert first.stage == 3 and first.calendar_contiguous
    # An observed source halt is retained as a gap, not zero-filled into a bar.
    assert pd.isna(second.stage)
    lineage = result["history_repair_lineage"]
    assert lineage["repair_inventory_rows"] == 3 and lineage["repair_only_codes_count"] == 1
    assert lineage["original_repair_day_rows"] == 0
    assert lineage["exact_recent_sessions"] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert lineage["current_raw_fields_unchanged"] is True
    assert lineage["continuous_history_claimed"] is False
    assert result["producer_coverage"][0]["status"] == "incomplete"
    assert len(pd.read_csv(root / "out" / "repair_dated_roster.csv")) == 3
    assert result["created_at"] == lineage["derived_at"]
    assert lineage["actual_signal_publication_verified"] is False
    assert all(result[key] is value for key, value in repair.BOUNDARY.items())
    pd.testing.assert_frame_equal(original, untouched)
    # The existing environment reader accepts the ordinary complete-preview
    # contract and independently checks all current physical values.
    monkeypatch.setattr(leader_environment_run, "load_complete", lambda path: packages[str(path)])
    envelope, features, proof = leader_environment_run.read_preview(root / "out")
    assert envelope["history_inventory_complete"] is False
    assert len(features) == 2 and proof["features_match_current_active_raw"] is True
    assert proof["history_repair"]["repair_day"] == "2026-10-12"
    assert proof["history_repair"]["derived_at"] == result["created_at"]
    assert proof["history_repair"]["historical_publication_verified"] is False


def test_any_existing_repair_day_rows_refuse_silent_replacement(inputs, monkeypatch):
    root, _, _, base, packages = inputs
    existing = pd.concat([base, repair.canonical(packages[str(root / "repair")][0].iloc[:1])], ignore_index=True)
    monkeypatch.setattr(repair, "read_physical_snapshot", lambda *args, **kwargs: (existing, {}))
    with pytest.raises(ValueError, match="wholly empty day"):
        run(inputs)
    assert not (root / "out").exists()


def test_environment_reader_keeps_unsafe_repair_lineage_out(inputs, monkeypatch):
    root, _, _, _, packages = inputs
    result = run(inputs)
    result["history_repair_lineage"]["future_bars_used"] = True
    (root / "out" / "result.json").write_text(json.dumps(result), encoding="utf-8")
    monkeypatch.setattr(leader_environment_run, "load_complete", lambda path: packages[str(path)])
    with pytest.raises(ValueError, match="repair lineage"):
        leader_environment_run.read_preview(root / "out")


def test_exact_pinned_predecessors_and_calendar_identity_are_required(inputs):
    root, _, _, _, packages = inputs
    packages[str(root / "repair")][1]["plan"]["calendar_sha256"] = "changed"
    with pytest.raises(ValueError, match="same pinned calendar"):
        run(inputs)
    assert not (root / "out").exists()
