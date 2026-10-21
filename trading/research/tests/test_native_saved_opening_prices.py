"""Synthetic exact-day price/metadata contracts; no market or model runs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import native_saved_opening as native
from trading.research import native_saved_opening_prices as prices
from trading.research.baostock_all_stock_probe import EXPECTED_FIELDS
from trading.research.daily_source_probe import WHEEL_SHA256
from trading.research.tests.test_native_saved_opening import A, B, D, E, NOW, fixture_native, write


C, S, U = "600003.SSE", "600004.SSE", "000003.SZSE"


def augment_native_prices(bundle, *, raw_adjustflag="3"):
    """Add one small dated source to the existing synthetic native fixture.

    Called before build_source. Root integration tests can reuse this instead of
    constructing another native account layout.
    """
    b = bundle
    parent = b["run"].parent
    folder = parent / "exact-close"
    roster_folder = parent / "dated-roster"
    codes = ["sh.600001", "sh.600003", "sh.600004", "sz.000002", "sz.000003"]
    statuses = ["1", "0", "1", "1", "1"]
    roster = pd.DataFrame({"code": codes, "tradeStatus": statuses, "code_name": ["synthetic"]*len(codes)})
    roster_raw = write(roster_folder / "raw.csv", roster.to_csv(index=False).encode())
    roster_receipt = {"schema": "baostock_historical_all_stock_status_probe_v1", "day": D, "response_day": D,
        "method": "query_all_stock", "kwargs": {"day": D}, "status": "completed_provider_status_list",
        "quality_issues": [], "provider_error_code": "0", "fields_returned": EXPECTED_FIELDS,
        "sdk": {"sha256": WHEEL_SHA256}, "raw_csv": roster_raw["file"], "raw_sha256": roster_raw["sha256"],
        "rows_preserved": len(codes), "created_at": D+"T18:00:00+08:00", "completed_at": D+"T18:00:02+08:00",
        "execution_admitted": False, "price_bars_generated": False}
    write(roster_folder / "receipt.json", roster_receipt)
    records = []
    for index, code in enumerate(codes):
        close = ["10.0000", "", "12.0000", "20.0000", "30.0000"][index]
        records.append({"date": D, "code": code, "open": close, "high": close, "low": close,
            "close": close, "preclose": close, "volume": "1000", "amount": "10000",
            "adjustflag": "3", "tradestatus": "" if index == 4 else statuses[index],
            "pctChg": "0", "isST": "" if index == 2 else "0"})
    frame = pd.DataFrame(records, columns=prices.FIELDS.split(","))
    frame.loc[0, "adjustflag"] = raw_adjustflag
    raw_ref = write(folder / "raw_daily.csv", frame.to_csv(index=False).encode())
    calendar = b["head"]["binding"]["calendar"]
    flags = {k: False for k in ("execution_admitted", "selection_enabled", "strategy_validated",
        "source_verified", "pit_verified", "stockdb_modified", "ledger_mutated")}
    plan = {"schema": "baostock_bulk_exact_dated_close_inventory_v1", "day": D,
        "roster_folder": str(roster_folder), "roster_raw_sha256": roster_raw["sha256"],
        "roster_total_rows": len(codes), "ordinary_symbols": len(codes),
        "symbols": [A, C, S, B, U], "adjustflag": "3", "fields": prices.FIELDS,
        "calendar": calendar["file"], "calendar_sha256": calendar["sha256"], **flags}
    receipt = {"schema": plan["schema"], "day": D, "status": prices.COMPLETE,
        "full_dated_roster_inventory_complete": True, "pending_symbols": [], "preserved_failed_batches": [],
        "raw_daily_sha256": raw_ref["sha256"], "completed_rows": len(codes), "completed_symbols": len(codes),
        "created_at": D+"T18:00:03+08:00", "run_started_at": D+"T18:00:03+08:00",
        "updated_at": D+"T18:00:05+08:00", "completed_at": D+"T18:00:05+08:00", **flags}
    plan_ref, receipt_ref = write(folder / "plan.json", plan), write(folder / "receipt.json", receipt)
    clocks = prices._acquisition_times({"plan": plan, "receipt": receipt, "source_folder": str(folder)}, folder)
    proof = {"day": D, "source_folder": str(folder), "raw_file": raw_ref["file"], "raw_sha256": raw_ref["sha256"],
        "plan_file": plan_ref["file"], "plan_sha256": plan_ref["sha256"],
        "receipt_file": receipt_ref["file"], "receipt_sha256": receipt_ref["sha256"],
        "roster_raw_sha256": roster_raw["sha256"], "calendar_sha256": calendar["sha256"],
        "source_available_at": D+"T18:00:05+08:00", "ordinary_inventory_rows": len(codes),
        "read_only_verified": True, "full_dated_roster_inventory_complete": True,
        "receipt_status": prices.COMPLETE, **clocks}
    b["head"]["raw_sources"][-1] = deepcopy(proof)
    journal_path = Path(b["head"]["journal_files"][-1]["file"])
    journal = json.loads(journal_path.read_text())
    journal["raw_sources_sha256"] = native._hash(b["head"]["raw_sources"])
    b["head"]["journal_files"][-1] = write(journal_path, journal)
    write(b["run"] / "head.json", b["head"])
    panel = {"last_source_day": D, "feature_contract_id": b["record"]["spec"]["feature_contract_id"],
        "available_at": D+"T18:01:30+08:00", "execution_admitted": False,
        "source_proof": {"day_sources": [{**proof, "component_id": "close_D", "component_kind": "strict_dated_close_csv"}],
            "component_sources": [{"component_id": "close_D", "component_kind": "strict_dated_close_csv",
                "source_days": [D], "source_proof": deepcopy(proof)}]}}
    panel_ref = write(parent / "panel.json", panel)
    batch_ref = write(parent / "score-batch.json", {"schema": "native_shortline_frozen_score_batch_v1", "panel_result": panel_ref["file"],
        "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False})
    score_path = Path(b["record"]["source_publication"]["file"])
    score_ref = write(score_path, {"schema": "native_shortline_raw_score_publication_v1",
        "published_at": D+"T18:02:00+08:00", "batch_result_file": batch_ref["file"],
        "files": [{"path": str(Path(batch_ref["file"]).relative_to(score_path.parent)),
            "sha256": batch_ref["sha256"], "byte_count": batch_ref["byte_count"]}],
        "execution_admitted": False, "model_refit": False, "future_labels_used_for_score": False})
    b["record"]["source_publication"] = score_ref
    registration_path = b["plan"].parent / "registration.json"
    registration = json.loads(registration_path.read_text())
    registration["source_publication"] = score_ref
    registration_ref = write(registration_path, registration)
    next(row for row in b["record"]["files"] if row["path"] == "registration.json").update(
        sha256=registration_ref["sha256"], byte_count=registration_ref["byte_count"])
    write(b["plan"], b["record"])
    b.update(price_folder=folder, raw_ref=raw_ref, price_proof=proof, panel_path=Path(panel_ref["file"]))
    return b


def loaded(b):
    native.build_source(b["run"], b["plan"], b["source"], current=NOW)
    source, _, facts = native._load(b["source"], NOW)
    return source, facts


def test_exact_close_only_keeps_null_suspension_and_unknowns_without_ledger_marks(tmp_path, monkeypatch):
    b = augment_native_prices(fixture_native(tmp_path, monkeypatch, decision="FULL"))
    source, facts = loaded(b)
    facts["head"]["cases"]["lr"]["state"]["prices"][A] = 999999
    quotes, evidence = prices.read_quotes(source, facts, {A, B, C, S, U, "600999.SSE"}, current=NOW)
    assert quotes[A]["close_cents"] == 1000 and quotes[B]["close_cents"] == 2000
    assert quotes[S]["is_st"] is None and quotes[U]["tradestatus"] is None
    assert C not in quotes and evidence["null_price_symbols"] == [C]
    assert evidence["suspended_symbols"] == [C] and evidence["missing_symbols"] == ["600999.SSE"]
    assert evidence["unknown_ST_symbols"] == [S] and evidence["unknown_trade_status_symbols"] == [U]
    assert evidence["read_at"] == NOW.isoformat() and evidence["available_at"] < evidence["plan_published_at"]
    assert not evidence["panel_metadata_originally_pinned_by_score_publication"]
    prices.validate_quote_evidence(evidence, current=NOW)


def test_missing_proof_is_unavailable_and_never_uses_held_mark(tmp_path, monkeypatch):
    b = fixture_native(tmp_path, monkeypatch, decision="FULL")
    source, facts = loaded(b)
    quotes, evidence = prices.read_quotes(source, facts, {A, B}, current=NOW)
    assert quotes == {} and evidence["status"] == "unavailable" and evidence["refs"] == []
    prices.validate_quote_evidence(evidence, current=NOW)


@pytest.mark.parametrize("problem", ["raw_hash", "component_day", "future_source", "adjusted"])
def test_declared_bad_source_never_falls_back_to_another_day(tmp_path, monkeypatch, problem):
    b = augment_native_prices(fixture_native(tmp_path, monkeypatch, decision="FULL"),
                              raw_adjustflag="2" if problem == "adjusted" else "3")
    source, facts = loaded(b)
    if problem == "raw_hash":
        (b["price_folder"] / "raw_daily.csv").write_bytes(b"changed")
    elif problem == "component_day":
        panel = json.loads(b["panel_path"].read_text()); panel["source_proof"]["day_sources"][0]["day"] = E
        write(b["panel_path"], panel)
    elif problem == "future_source":
        facts["head"]["raw_sources"][-1]["source_available_at"] = E+"T09:31:00+08:00"
    with pytest.raises(ValueError, match="hash|same exact source|inventory|adjustflag|other days"):
        prices.read_quotes(source, facts, {A}, current=NOW)


def test_price_evidence_detects_changes_before_pack_without_reloading_rows(tmp_path, monkeypatch):
    b = augment_native_prices(fixture_native(tmp_path, monkeypatch, decision="FULL"))
    source, facts = loaded(b)
    _, evidence = prices.read_quotes(source, facts, {A, B}, current=NOW)
    (b["price_folder"] / "raw_daily.csv").write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        prices.validate_quote_evidence(evidence, current=NOW)
