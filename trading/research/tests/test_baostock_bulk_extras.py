"""Local bulk extras derivation preserves raw strings, roster and unknowns."""
import json

import pandas as pd
import pytest

from trading.research import baostock_bulk_close as bulk
from trading.research import baostock_bulk_extras as extras
from trading.research.tests.test_baostock_bulk_close import sources, import_package, rewrite_bulk


def test_nullable_halt_negative_valuation_and_actual_clocks_are_preserved(sources):
    probe, _, _, target = sources
    def values(frame):
        frame.loc[frame.code.eq("sz.000002"), "turn"] = ""
        frame.loc[frame.code.eq("sz.000001"), ["peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"]] = ["-3.25", "-2", "0", "-7"]
    rewrite_bulk(probe, values)
    package, _ = import_package(sources)
    before = {str(p): bulk.digest(p) for p in package.rglob("*") if p.is_file()}
    folder, record = extras.build(package, target.parent / "extras")
    rows = pd.read_parquet(folder / "dated_extras.parquet")
    raw = pd.read_csv(package / "bulk_source/2026-10-13_stock.csv", dtype=str, keep_default_na=False)
    expected = raw.set_index("code").loc[rows.code].reset_index()
    assert rows[list(expected)].equals(expected)
    halt = rows.loc[rows.tradestatus.eq("0")].iloc[0]
    assert halt.turn == "" and pd.isna(halt.turn_numeric)
    active = rows.loc[rows.code.eq("sz.000001")].iloc[0]
    assert active.peTTM == "-3.25" and active.peTTM_numeric == -3.25
    assert active.psTTM_numeric == 0 and active.pcfNcfTTM_numeric == -7
    assert record["statistics"]["ordinary"]["rows"] == 3
    assert record["statistics"]["halt"]["fields"]["turn"]["numeric_null_rows"] == 1
    assert record["statistics"]["active"]["fields"]["peTTM"]["numeric_negative_rows"] == 1
    assert record["original_bulk_rows"] == 4 and record["excluded_nonordinary_codes"] == ["sh.689009"]
    assert record["turn_numeric_unit"] == "percent" and record["turn_scaled"] is False
    assert record["raw_acquisition_finished_at"] == "2026-10-14T03:33:53+08:00"
    assert record["roster_available_at"] == "2026-10-15T10:53:59+08:00"
    assert record["actual_derived_at"] >= record["source_inputs_available_at"]
    assert record["available_at"] == record["actual_derived_at"]
    for key in ("pit_verified", "provider_contacted", "execution_admitted", "environment_or_strategy_updated", "numeric_missing_filled_with_zero"):
        assert record[key] is False
    assert before == {str(p): bulk.digest(p) for p in package.rglob("*") if p.is_file()}


def test_original_bulk_sha_change_is_rejected_before_output(sources):
    package, _ = import_package(sources)
    path = package / "bulk_source/2026-10-13_stock.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    out = package.parent / "extras"
    with pytest.raises(ValueError, match="raw hash"):
        extras.build(package, out)
    assert not out.exists()


def test_own_roster_mismatch_cannot_become_an_extras_subset(sources):
    package, _ = import_package(sources)
    probe = package / "bulk_source"
    rewrite_bulk(probe, lambda frame: frame.loc.__setitem__((frame.code.eq("sz.000001"), "code"), "sz.000003"))
    # Even after local hashes are updated, exact membership must still match
    # the independently preserved own-day roster, not an inner-join subset.
    plan_path = package / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan.update(bulk_raw_sha256=bulk.digest(probe / "2026-10-13_stock.csv"),
                bulk_receipt_sha256=bulk.digest(probe / "receipt.json"))
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    out = package.parent / "extras"
    with pytest.raises(ValueError, match="code set differs"):
        extras.build(package, out)
    assert not out.exists()


def test_nonfinite_and_unparseable_extras_remain_unknown_without_row_drop(sources):
    probe, _, _, target = sources
    def values(frame):
        frame.loc[frame.code.eq("sz.000001"), ["turn", "peTTM"]] = ["NaN", "unavailable"]
    rewrite_bulk(probe, values)
    package, _ = import_package(sources)
    folder, result = extras.build(package, target.parent / "extras")
    rows = pd.read_parquet(folder / "dated_extras.parquet")
    row = rows.loc[rows.code.eq("sz.000001")].iloc[0]
    assert row.turn == "NaN" and row.peTTM == "unavailable"
    assert pd.isna(row.turn_numeric) and pd.isna(row.peTTM_numeric)
    assert len(rows) == result["ordinary_rows"] == 3
    for name in ("turn", "peTTM"):
        assert result["statistics"]["ordinary"]["fields"][name]["nonblank_unparseable_or_nonfinite_rows"] == 1


def test_per_stock_projection_is_unsupported_and_existing_output_not_overwritten(tmp_path, monkeypatch):
    package = tmp_path / "perstock"
    package.mkdir()
    for name in ("plan.json", "receipt.json"):
        (package / name).write_text(json.dumps({"schema": "baostock_full_dated_close_inventory_v1"}))
    out = tmp_path / "extras"
    with pytest.raises(ValueError, match="per-stock projection unsupported"):
        extras.build(package, out)
    assert not out.exists()
    out.mkdir()
    sentinel = out / "retained.txt"
    sentinel.write_text("existing evidence")
    monkeypatch.setattr(bulk, "load_complete_bulk", lambda *a, **k: pytest.fail("Existing out must fail before input read"))
    with pytest.raises(FileExistsError):
        extras.build(package, out)
    assert sentinel.read_text() == "existing evidence"


def test_cli_is_registered_without_starting_a_provider(capsys):
    from run_committee import main
    with pytest.raises(SystemExit) as error:
        main(["baostock-bulk-extras", "--help"])
    assert error.value.code == 0
    assert "--package" in capsys.readouterr().out
