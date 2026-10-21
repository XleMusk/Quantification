"""Small local disclosure batches only; no collector, SQL, prices or Engine."""
from copy import deepcopy
from datetime import datetime
import gzip
import json
from pathlib import Path

import pytest

from trading.research import seat_disclosure_catalog as catalog
from trading.research import seat_hypothesis_snapshot as snapshot
from trading.research.seats import REPORTS, URL


NOW = datetime.fromisoformat("2026-10-16T12:00:00+08:00")
REASON = "日涨幅偏离值达到7%的前5只证券"


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source(tmp_path, name, *, day="2025-09-26", empty=False, matched=True, available=None):
    root = tmp_path / name
    root.mkdir()
    clock = available or day + "T17:04:00+08:00"
    seats = [{"TRADE_DATE": day + " 00:00:00", "SECUCODE": "600000.SH", "SECURITY_CODE": "600000",
        "EXPLANATION": REASON, "OPERATEDEPT_NAME": "Synthetic broker branch " + code,
        "OPERATEDEPT_CODE": code, "BUY": buy, "SELL": sell, "NET": buy - sell,
        "ACCUM_AMOUNT": 1000., "TRADE_ID": "synthetic-single-board"}
        for code, buy, sell in (("a", 200., 20.), ("b", 150. if matched else 10., 10. if matched else 20.))]
    rows = {"summary": [{"TRADE_DATE": day, "SECUCODE": "600000.SH", "SECURITY_CODE": "600000",
        "EXPLANATION": REASON, "ACCUM_AMOUNT": 1000.}], "buy": seats, "sell": deepcopy(seats)}
    if empty:
        rows = {kind: [] for kind in REPORTS}
    for kind, values in rows.items():
        sort = "TRADE_DATE,SECURITY_CODE" + (",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL" if kind != "summary" else "")
        doc = {"request": {"reportName": REPORTS[kind], "columns": "ALL",
            "filter": f"(TRADE_DATE>='{day}')(TRADE_DATE<='{day}')", "pageSize": 500, "pageNumber": 1,
            "sortColumns": sort, "sortTypes": ",".join("1" for _ in sort.split(",")), "source": "WEB", "client": "WEB"},
            "response": {"success": True, "result": {"pages": 1, "count": len(values), "data": values}},
            "retrieved_at": clock, "published_at": None}
        with gzip.open(root / f"{kind}_0001.json.gz", "wt", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False)
    manifest = {"query": {"source": "Eastmoney", "url": URL, "start": day, "end": day,
        "codes": [], "pagination_version": 2}, "status": "ready",
        "reports": {kind: {"pages": 1, "rows": len(values)} for kind, values in rows.items()},
        "publication_timestamps_available": False, "completeness": "provider_pages_only", "completed_at": clock}
    save(root / "manifest.json", manifest)
    return root


def empty_receipt(root, out, *, completed="2025-09-26T17:05:00+08:00"):
    manifest = catalog._read(root / "manifest.json")
    record = {"schema": catalog.EMPTY_RECEIPT_SCHEMA, "status": "completed_exact_day_empty_disclosure",
        "event_day": manifest["query"]["start"], "as_of": completed, "completed_at": completed,
        "source_manifest": catalog._ref(root / "manifest.json"), "reports": {kind: {
            "report_name": report, "provider_count": 0, "all_pages_completed": True,
            "page_refs": [catalog._ref(root / f"{kind}_0001.json.gz")]} for kind, report in REPORTS.items()}}
    save(out, record)
    return out


def batch_and_catalog(tmp_path, name, **kwargs):
    raw = source(tmp_path, name + "_raw", **kwargs)
    batch = catalog.build_batch(raw, tmp_path / (name + "_batch"), now=NOW)
    saved = catalog.append_catalog(batch["reference"]["file"], tmp_path / (name + "_catalog.json"), now=NOW)
    return raw, batch, catalog.load_catalog(saved["reference"]["file"], now=NOW)


@pytest.fixture(autouse=True)
def forbid_external_runtime(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Catalog tests must not collect, use SQL or run Engine")
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("trading.engine.advance", forbidden)


def test_new_batch_reuses_fixed_two_recipes_and_exact_source_clock(tmp_path):
    raw, batch, loaded = batch_and_catalog(tmp_path, "initial")
    first = loaded["batches"][0]
    assert first["source_available_at"] == "2025-09-26T17:04:00+08:00"
    assert first["created_at"] == NOW.isoformat()
    assert first["recipes"]["legacy_repeated_sum"]["supported"][0]["score"] == .16
    assert first["recipes"]["stockday_unique"]["supported"][0]["score"] == .32
    for recipe in catalog.RECIPES:
        facts = first["recipes"][recipe]
        assert len(facts["candidates"]) == len(facts["panel_observations"]) == 1
        assert facts["clocks"][("2025-09-26", "600000.SSE")]["available_at"] == first["source_available_at"]
    assert raw in loaded["packages"]
    assert (raw / "buy_0001.json.gz").resolve() in loaded["files"]
    assert batch["execution_admitted"] is False and loaded["record"]["engine_run"] is False


def test_existing_snapshot_reused_without_normalizing_or_rederiving(tmp_path, monkeypatch):
    raw = source(tmp_path, "raw")
    old = tmp_path / "old_snapshot"
    monkeypatch.setattr(snapshot, "_now", lambda: NOW)
    snapshot.build(raw, old)
    original = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Existing snapshots must not be rederived")
    monkeypatch.setattr(catalog.seats, "normalize", forbidden)
    monkeypatch.setattr(snapshot, "_panels", forbidden)
    batch = catalog.build_batch(raw, tmp_path / "batch", snapshot_result=old / "result.json", now=NOW)
    record = catalog._read(batch["reference"]["file"])
    assert record["snapshot_ref"] == catalog._ref(old / "result.json")
    assert not (tmp_path / "batch/snapshot").exists()
    assert all(path.read_bytes() == data for path, data in original.items())


def test_append_preserves_late_older_event_order_and_actual_ancestry(tmp_path):
    _, _, initial = batch_and_catalog(tmp_path, "initial")
    late = source(tmp_path, "late_raw", day="2025-09-25", available="2025-09-29T17:00:00+08:00")
    batch = catalog.build_batch(late, tmp_path / "late_batch", now=NOW)
    out = tmp_path / "next_catalog.json"
    catalog.append_catalog(batch["reference"]["file"], out, parent_catalog=initial["reference"]["file"], now=NOW)
    current = catalog.load_catalog(out, now=NOW)
    assert [b["event_day"] for b in current["batches"]] == ["2025-09-26", "2025-09-25"]
    assert current["record"]["batches"][:-1] == initial["record"]["batches"]
    assert catalog.assert_catalog_prefix(current, initial)
    fork = deepcopy(initial)
    fork["reference"] = {**initial["reference"], "file": str(tmp_path / "unrelated.json")}
    with pytest.raises(ValueError, match="descendant"):
        catalog.assert_catalog_prefix(current, fork)
    parent = Path(initial["reference"]["file"])
    parent.write_bytes(parent.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        catalog.load_catalog(out, now=NOW)


def test_same_event_exact_repetition_is_idempotent_and_revision_rejected(tmp_path):
    raw, batch, initial = batch_and_catalog(tmp_path, "initial")
    original_bytes = Path(initial["reference"]["file"]).read_bytes()
    duplicate = tmp_path / "unused_catalog.json"
    result = catalog.append_catalog(batch["reference"]["file"], duplicate,
        parent_catalog=initial["reference"]["file"], now=NOW)
    assert result["status"] == "idempotent_existing_exact_source_batch" and not duplicate.exists()
    revised_source = source(tmp_path, "revised_raw", matched=False)
    revised = catalog.build_batch(revised_source, tmp_path / "revised_batch", now=NOW)
    with pytest.raises(ValueError, match="revision_conflict"):
        catalog.append_catalog(revised["reference"]["file"], duplicate,
            parent_catalog=initial["reference"]["file"], now=NOW)
    assert not duplicate.exists() and Path(initial["reference"]["file"]).read_bytes() == original_bytes
    assert raw.exists()


def test_missing_failed_and_unproven_zero_sources_never_create_success_batch(tmp_path):
    missing = tmp_path / "missing"
    for root, expected in ((missing, "pending"),):
        result = catalog.build_batch(root, tmp_path / "no_output", now=NOW)
        assert result["status"] == expected and not (tmp_path / "no_output").exists()
    failed = tmp_path / "failed"
    failed.mkdir()
    save(failed / "manifest.json", {"status": "failed", "error": "synthetic response failed"})
    assert catalog.inspect_source(failed, now=NOW)["status"] == "failed"
    raw = source(tmp_path, "zero_raw", empty=True)
    result = catalog.build_batch(raw, tmp_path / "zero_unproven", now=NOW)
    assert result["status"] == "pending" and result["reason"] == "empty_disclosure_completion_evidence_missing"
    assert not (tmp_path / "zero_unproven").exists()


def test_complete_empty_receipt_is_rechecked_and_both_recipe_batches_are_empty(tmp_path):
    raw = source(tmp_path, "empty_raw", empty=True)
    receipt = empty_receipt(raw, tmp_path / "empty_receipt.json")
    batch = catalog.build_batch(raw, tmp_path / "empty_batch", completion_receipt=receipt, now=NOW)
    assert batch["status"] == "completed_empty"
    assert batch["source_available_at"] == "2025-09-26T17:05:00+08:00"
    out = tmp_path / "catalog.json"
    catalog.append_catalog(batch["reference"]["file"], out, now=NOW)
    loaded = catalog.load_catalog(out, now=NOW)
    assert receipt.resolve() in loaded["files"]
    for facts in loaded["batches"][0]["recipes"].values():
        assert facts["candidates"] == facts["supported"] == facts["panel_observations"] == []
        assert facts["clocks"] == {}
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        catalog.load_catalog(out, now=NOW)


@pytest.mark.parametrize("case", ["missing_report", "backdated", "wrong_page_hash"])
def test_zero_receipt_needs_all_exact_reports_pages_and_actual_clocks(tmp_path, case):
    raw = source(tmp_path, "empty_raw", empty=True)
    receipt = empty_receipt(raw, tmp_path / "receipt.json")
    record = catalog._read(receipt)
    if case == "missing_report":
        del record["reports"]["sell"]
    elif case == "backdated":
        record["as_of"] = record["completed_at"] = "2025-09-26T17:00:00+08:00"
    else:
        record["reports"]["buy"]["page_refs"][0]["sha256"] = "0" * 64
    save(receipt, record)
    with pytest.raises(ValueError):
        catalog.build_batch(raw, tmp_path / "output", completion_receipt=receipt, now=NOW)
    assert not (tmp_path / "output").exists()


def test_candidate_empty_is_not_claimed_as_empty_disclosure_and_raw_hash_is_live(tmp_path):
    raw, _, loaded = batch_and_catalog(tmp_path, "not_matched", matched=False)
    assert loaded["batches"][0]["status"] == "completed_nonempty"
    assert all(not f["supported"] and f["panel_observations"] for f in loaded["batches"][0]["recipes"].values())
    page = raw / "buy_0001.json.gz"
    page.write_bytes(page.read_bytes() + b"\n")
    with pytest.raises((ValueError, OSError, EOFError)):
        catalog.load_catalog(loaded["reference"]["file"], now=NOW)


def test_future_snapshot_or_output_overwrite_is_rejected_before_success(tmp_path):
    raw = source(tmp_path, "raw")
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "user.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        catalog.build_batch(raw, output, now=NOW)
    assert marker.read_text(encoding="utf-8") == "keep"
    with pytest.raises(ValueError, match="inside"):
        catalog.build_batch(raw, raw / "nested", now=NOW)
    batch = catalog.build_batch(raw, tmp_path / "valid", now=NOW)
    snapshot_path = Path(catalog._read(batch["reference"]["file"])["snapshot_ref"]["file"])
    with pytest.raises(ValueError, match="future"):
        catalog.build_batch(raw, tmp_path / "future", snapshot_result=snapshot_path,
            now=datetime.fromisoformat("2025-09-27T12:00:00+08:00"))
    assert not (tmp_path / "future").exists()


def test_catalog_parent_prefix_cannot_drop_or_change_initial_batch(tmp_path):
    _, _, initial = batch_and_catalog(tmp_path, "initial")
    late = source(tmp_path, "second_raw", day="2025-09-29")
    batch = catalog.build_batch(late, tmp_path / "second_batch", now=NOW)
    out = tmp_path / "second_catalog.json"
    catalog.append_catalog(batch["reference"]["file"], out, parent_catalog=initial["reference"]["file"], now=NOW)
    record = catalog._read(out)
    record["batches"][0] = deepcopy(record["batches"][1])
    save(out, record)
    with pytest.raises(ValueError, match="prefix"):
        catalog.load_catalog(out, now=NOW)
