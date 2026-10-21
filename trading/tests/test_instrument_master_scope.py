import csv
import json
from pathlib import Path

import trading.research.instrument_master as instrument_master
from trading.research.instrument_master import MASTER_FIELDS, SCOPE_FIELDS


def _write(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _master_row(code, ipo, out_date="", status="active_observed"):
    exchange, number = code.split(".")
    exchange_name = {"sh": "SSE", "sz": "SZSE"}[exchange]
    symbol = f"{number}.{exchange_name}"
    board = "star" if code.startswith("sh.688") else "chinext" if code.startswith("sz.30") else "main"
    return dict(zip(MASTER_FIELDS, [f"{exchange_name}:{number}", symbol, code, "fixture", exchange_name,
        number, "ordinary_equity", board, f"stock_{board}_t1", "100", "100", "1000000",
        status, "2026-09-30", ipo, out_date, ipo, out_date, "false", "false", "false"]))


def test_research_scope_classifies_overlap_without_pit_or_execution_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(instrument_master, "ROOT", tmp_path / "root")
    monkeypatch.setattr(instrument_master, "DATA_ROOT", tmp_path / "data")
    master = tmp_path / "master"
    master.mkdir()
    _write(master / "instrument_master.csv", MASTER_FIELDS, [
        _master_row("sh.600000", "2000-01-01"),
        _master_row("sh.600001", "2027-01-01"),
        _master_row("sz.000001", "2000-01-01", "2025-01-01", "inactive_observed"),
    ])
    source = tmp_path / "universe.csv"
    _write(source, ["code"], [{"code": code} for code in ("sh.600000", "sh.600001", "sz.000001", "sh.689009")])

    output, manifest = instrument_master.build_research_scope(
        master, source, "2024-01-02", "2026-09-30", out=tmp_path / "out")

    assert manifest["included_count"] == 2
    assert manifest["missing_master_count"] == 1
    assert manifest["execution_admitted"] is False
    assert manifest["pit_qualified"] is False
    with (output / "research_scope.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == SCOPE_FIELDS
    by_code = {row["provider_code"]: row for row in rows}
    assert by_code["sh.600000"]["included_in_research_scope"] == "true"
    assert by_code["sh.600001"]["scope_reason"] == "listed_after_replay_end"
    assert by_code["sh.689009"]["scope_reason"] == "missing_instrument_master"
    assert json.loads((output / "run.json").read_text(encoding="utf-8"))["research_only"] is True
