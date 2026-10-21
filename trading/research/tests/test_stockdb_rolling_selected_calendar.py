"""Explicit subset continuation and fixed-calendar gap regression checks."""
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import stockdb_rolling_low_volatility as rolling


VARIANT = "low_volatility_252_20"
START, OLD_END, END = "2025-12-31", "2026-10-09", "2026-10-13"


def frame(dates):
    return pd.DataFrame([dict(code="600000", symbol="600000.SSE", date=day,
                              open=10., high=10., low=10., close=10., pre_close=10.,
                              volume=1_000_000., amount=10_000_000., is_st=False)
                         for day in dates])


def save_snapshot(path, rows):
    path.mkdir()
    bars = path / "bars.parquet"
    rows[list(rolling.SOURCE_COLUMNS)].to_parquet(bars, index=False)
    manifest = {"status": "complete_inventory_only", "execution_admitted": False,
                "chunks_expected": 1, "chunks": [{"parquet_file": bars.name,
                "parquet_sha256": sha256(bars.read_bytes()).hexdigest()}]}
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def saved_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    old = save_snapshot(tmp_path / "old", frame([START, OLD_END]))
    new = save_snapshot(tmp_path / "new", frame([START, OLD_END, "2026-10-12", END]))
    out, saved = rolling.run(old, START, OLD_END)
    return new, out / "run.json", saved


def test_selected_subset_resumes_saved_checkpoint_and_preserves_original_batch(saved_batch):
    new, previous, saved = saved_batch
    old_account = (Path(saved["artifacts"]) / VARIANT / "account.json").read_bytes()
    with pytest.raises(ValueError, match="variants"):
        rolling.run(new, START, END, variants=(VARIANT,), resume_run=previous)
    _, result = rolling.run(new, START, END, variants=(VARIANT,), resume_run=previous,
                            resume_selected_variants=True)
    assert result["original_variants"] == list(rolling.VARIANTS)
    assert result["variants"] == result["resume_selected_variants"] == [VARIANT]
    assert [row["signal"] for row in result["unselected_variant_results"]] == [
        rolling.VARIANTS[0], rolling.VARIANTS[2]]
    assert result["results"][0]["reused_sessions"] == 2
    assert result["results"][0]["newly_completed_sessions"] == 2
    assert result["account_reset"] is False
    assert (Path(saved["artifacts"]) / VARIANT / "account.json").read_bytes() == old_account


def test_selected_subset_still_rejects_wrong_config_and_non_subset(saved_batch):
    new, previous, saved = saved_batch
    with pytest.raises(ValueError, match="strategy, costs"):
        rolling.run(new, START, END, initial_cash=150000, variants=(VARIANT,),
                    resume_run=previous, resume_selected_variants=True)
    document = json.loads(previous.read_text(encoding="utf-8"))
    document["variants"] = [rolling.VARIANTS[0]]
    previous.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="variants"):
        rolling.run(new, START, END, variants=(VARIANT,), resume_run=previous,
                    resume_selected_variants=True)
    # A valid subset must still pass the original exact raw-prefix guard.
    document["variants"] = list(rolling.VARIANTS)
    previous.write_text(json.dumps(document), encoding="utf-8")
    bars = new / "bars.parquet"
    rows = pd.read_parquet(bars)
    rows.loc[rows.date.eq(OLD_END), "close"] = 11.
    rows.to_parquet(bars, index=False)
    manifest = json.loads((new / "manifest.json").read_text(encoding="utf-8"))
    manifest["chunks"][0]["parquet_sha256"] = sha256(bars.read_bytes()).hexdigest()
    (new / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="raw history prefix"):
        rolling.run(new, START, END, variants=(VARIANT,), resume_run=previous,
                    resume_selected_variants=True)


def test_fixed_calendar_rejects_missing_or_closed_raw_day_before_output_and_records_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(rolling, "ROOT", tmp_path / "project")
    monkeypatch.setattr(rolling, "DATA_ROOT", tmp_path / "data")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                                   "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    days = [START, "2026-10-09", "2026-10-12", END]
    missing = save_snapshot(tmp_path / "missing", frame([day for day in days if day != "2026-10-12"]))
    with pytest.raises(ValueError, match="missing_open_sessions=.*2026-10-12"):
        rolling.run(missing, "2026-10-09", END, variants=(VARIANT,), calendars=[calendar])
    assert not (tmp_path / "project").exists() and not (tmp_path / "data").exists()
    weekend = save_snapshot(tmp_path / "weekend", frame(days + ["2026-10-10"]))
    with pytest.raises(ValueError, match="noncalendar_raw_sessions=.*2026-10-10"):
        rolling.run(weekend, "2026-10-09", END, variants=(VARIANT,), calendars=[calendar])
    # Scope starts in 2025 while only 2026 is verified; include every 2026 open day.
    dates = [START, *pd.bdate_range("2026-01-01", END).strftime("%Y-%m-%d")]
    good = save_snapshot(tmp_path / "good", frame(dates))
    out, result = rolling.run(good, START, END, variants=(VARIANT,), calendars=[calendar])
    proof = result["calendar_verification"]
    assert proof["verified_years"] == [2026] and proof["unverified_replay_years"] == [2025]
    assert proof["entire_replay_calendar_verified"] is False
    assert proof["files"][0]["sha256"] == sha256(calendar.read_bytes()).hexdigest()
    assert "2026-10-12" in proof["files"][0]["expected_open_sessions"]
    protocol = json.loads((Path(result["artifacts"]) / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["calendar_verification"] == proof


def test_explicit_reconstructed_old_snapshot_requires_exact_manifest_and_preserves_resume_checks(saved_batch, tmp_path):
    import shutil

    new, previous, saved = saved_batch
    protocol = json.loads((Path(saved["artifacts"]) / "protocol.json").read_text(encoding="utf-8"))
    original = Path(protocol["snapshot"])
    rebuilt = tmp_path / "reconstructed_old"
    shutil.copytree(original, rebuilt)
    # Simulate later metadata appends to the old source without revising its original chunks.
    manifest_file = original / "manifest.json"
    changed = json.loads(manifest_file.read_text(encoding="utf-8"))
    changed["later_append"] = True
    manifest_file.write_text(json.dumps(changed), encoding="utf-8")
    kwargs = dict(variants=(VARIANT,), resume_run=previous, resume_selected_variants=True)
    with pytest.raises(ValueError, match="Previously pinned StockDB manifest changed"):
        rolling.run(new, START, END, **kwargs)
    with pytest.raises(ValueError, match="Previously pinned StockDB manifest changed"):
        rolling.run(new, START, END, resume_source_snapshot=original, **kwargs)
    with pytest.raises(ValueError, match="requires --resume-run"):
        rolling.run(new, START, END, resume_source_snapshot=rebuilt)
    out, result = rolling.run(new, START, END, resume_source_snapshot=rebuilt, **kwargs)
    assert result["results"][0]["reused_sessions"] == 2
    assert result["results"][0]["newly_completed_sessions"] == 2
    assert result["resume_original_snapshot"] == str(original.resolve())
    proof = result["explicit_reconstructed_resume_snapshot"]
    assert proof["path"] == str(rebuilt.resolve())
    assert proof["manifest_sha256"] == sha256((rebuilt / "manifest.json").read_bytes()).hexdigest()
    new_protocol = json.loads((out / "protocol.json").read_text(encoding="utf-8"))
    assert new_protocol["explicit_reconstructed_resume_snapshot"] == proof
    assert new_protocol["resume_original_snapshot"] == result["resume_original_snapshot"]
    assert json.loads(manifest_file.read_text(encoding="utf-8")) == changed
    # The reconstruction override does not bypass verification of pinned chunk bytes.
    (rebuilt / "bars.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed|hash|integrity|Chunk|chunk"):
        rolling.run(new, START, END, resume_source_snapshot=rebuilt, **kwargs)
