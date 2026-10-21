import pandas as pd
import pytest
from hashlib import sha256
import json

from trading.research import action_reviewed_amendment as amendment
from trading.research.bulk_feed import BulkResearchFeed
from trading.market_dataset import write_frame, write_json
from trading.research.market_replay import validate_prefix_inputs


@pytest.mark.parametrize("second_event", [False, True])
def test_reviewed_amendment_archives_both_documents_with_distinct_names(tmp_path, monkeypatch, second_event):
    event, reference, reviewed = (tmp_path / name for name in ("event", "reference", "reviewed"))
    event.mkdir()
    reference.mkdir()
    for name in ("final", "plan"):
        (reviewed / name).mkdir(parents=True)
        (reviewed / name / "notice.pdf").write_bytes(b"%PDF-synthetic-archive-fixture-" + name.encode())
    actions = pd.DataFrame([{"symbol": "000423.SZSE", "code": "sz.000423", "dividOperateDate": "2026-08-31"}])
    write_frame(event / "canonical_actions.parquet", actions)
    write_frame(reference / "raw_reference_changes.parquet", pd.DataFrame(columns=["code", "date",
                 "reference_formula_status", "reference_formula_basis"]))
    write_json(event / "result.json", {"status": "research_action_events_projected", "execution_admitted": False,
        "files": {"canonical_actions.parquet": sha256((event / "canonical_actions.parquet").read_bytes()).hexdigest()}})
    write_json(reference / "result.json", {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event), "files": {"raw_reference_changes.parquet": sha256((reference / "raw_reference_changes.parquet").read_bytes()).hexdigest()}})
    terms = {"symbol": "000423.SZSE", "dividOperateDate": "2026-08-31", "kind": "issuer_confirmed_cash_only_terms",
             "source_file": "final/notice.pdf", "plan_source_file": "plan/notice.pdf"}
    for field in ("source", "plan_source"):
        terms[field + "_sha256"] = sha256((reviewed / terms[field + "_file"]).read_bytes()).hexdigest()
    resolutions = [terms]
    if second_event:
        other = dict(terms, symbol="600000.SSE", dividOperateDate="2026-08-30",
                     source_file="other/notice.pdf")
        (reviewed / "other").mkdir()
        (reviewed / other["source_file"]).write_bytes(b"%PDF-other-issuer-fixture")
        other["source_sha256"] = sha256((reviewed / other["source_file"]).read_bytes()).hexdigest()
        resolutions.append(other)
    monkeypatch.setattr(amendment, "load_action_resolutions", lambda *_: resolutions)
    monkeypatch.setattr(amendment, "corrected_actions", lambda frame, *_: (frame, []))
    monkeypatch.setattr(amendment, "DATA_ROOT", tmp_path / "artifacts")
    folder, _, _ = amendment.amend(event, reference, reviewed,
                                   [(r["symbol"], r["dividOperateDate"]) for r in resolutions])
    manifest = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    audit = next(folder.glob("reviewed_amendments/*/audit.json"))
    stored = json.loads(audit.read_text(encoding="utf-8"))["resolutions"]
    all_names = []
    for saved, original in zip(stored, resolutions):
        assert saved["source_file"] != saved["plan_source_file"]
        for field in ("source", "plan_source"):
            name = saved[field + "_file"]
            all_names.append(name)
            assert (folder / name).read_bytes() == (reviewed / original[field + "_file"]).read_bytes()
            assert manifest["files"][name] == original[field + "_sha256"]
    assert len(set(all_names)) == len(all_names)


def test_reconciler_reset_index_cannot_modify_unselected_action(monkeypatch):
    actions = pd.DataFrame([
        {"symbol": "600000.SSE", "dividOperateDate": "2023-07-21", "dividCashPsBeforeTax": "0.32", "dividStocksPs": "0", "dividReserveToStockPs": ""},
        {"symbol": "301127.SZSE", "dividOperateDate": "2025-04-21", "dividCashPsBeforeTax": "0.099999", "dividStocksPs": "0.000000", "dividReserveToStockPs": ""},
    ], index=[0, 7134])
    terms = [{"symbol": "301127.SZSE", "dividOperateDate": "2025-04-21"}]
    def reconciled(rows, terms):
        frame = rows.drop(columns="_cache_file").reset_index(drop=True)
        frame["dividCashPsBeforeTax"], frame["dividReserveToStockPs"] = "0.0999985", "0"
        return frame, []
    monkeypatch.setattr(amendment, "reconcile_provider_actions", reconciled)
    result, _ = amendment.corrected_actions(actions, terms, "frozen.parquet")
    pd.testing.assert_series_equal(result.loc[0], actions.loc[0])
    assert result.at[7134, "dividCashPsBeforeTax"] == "0.0999985"
    assert result.at[7134, "dividReserveToStockPs"] == "0"
    assert actions.at[7134, "dividReserveToStockPs"] == ""


def test_invalidated_snapshot_rejected_before_any_parquet_read(tmp_path):
    event = tmp_path / "event"
    event.mkdir()
    (event / "INVALID.json").write_text('{"reason":"bad source amendment"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalidated research snapshot"):
        BulkResearchFeed(tmp_path / "prices", event, tmp_path / "reference")


def test_reference_reset_index_keeps_raw_and_unselected_rows():
    gaps = pd.DataFrame([
        {"code": "sh.600000", "date": "2023-07-21", "close": 7., "reference_formula_status": "unresolved_blank_terms", "reference_error_rmb": float("nan")},
        {"code": "sz.301127", "date": "2025-04-21", "close": 14.95, "reference_formula_status": "unresolved_blank_terms", "reference_error_rmb": float("nan")},
    ], index=[0, 7134])
    mask = gaps.code.eq("sz.301127")
    refreshed = gaps.loc[mask].reset_index(drop=True)
    refreshed["reference_formula_status"] = "reference_match"
    refreshed["reference_error_rmb"] = .0000015
    result = amendment.patch_reference_rows(gaps, mask, refreshed)
    pd.testing.assert_series_equal(result.loc[0], gaps.loc[0])
    pd.testing.assert_series_equal(result["close"], gaps["close"])
    assert result.at[7134, "reference_formula_status"] == "reference_match"
    refreshed["close"] = 999
    with pytest.raises(ValueError, match="Raw or unselected"):
        amendment.patch_reference_rows(gaps, mask, refreshed)


@pytest.mark.parametrize("existing_basis", [None, "0.25"])
def test_bonus_tax_basis_copied_only_to_selected_action(monkeypatch, existing_basis):
    actions = pd.DataFrame([
        {"symbol": "600000.SSE", "dividOperateDate": "2025-06-01", "dividCashPsBeforeTax": "0.32", "dividStocksPs": "0", "dividReserveToStockPs": ""},
        {"symbol": "000028.SZSE", "dividOperateDate": "2026-06-24", "dividCashPsBeforeTax": "0.616", "dividStocksPs": "0.100000", "dividReserveToStockPs": ""},
    ], index=[0, 7134])
    if existing_basis is not None:
        actions["bonus_taxable_cash_ps"] = [existing_basis, ""]
    def reconciled(rows, terms):
        frame = rows.drop(columns="_cache_file").reset_index(drop=True)
        frame["dividReserveToStockPs"], frame["bonus_taxable_cash_ps"] = "0", "0.1"
        return frame, []
    monkeypatch.setattr(amendment, "reconcile_provider_actions", reconciled)
    result, _ = amendment.corrected_actions(actions, [{"symbol": "000028.SZSE", "dividOperateDate": "2026-06-24"}], "frozen.parquet")
    pd.testing.assert_series_equal(result.loc[0, actions.columns], actions.loc[0])
    assert result.at[0, "bonus_taxable_cash_ps"] == (existing_basis or "")
    assert result.at[7134, "bonus_taxable_cash_ps"] == "0.1"
    assert result.at[7134, "dividStocksPs"] == "0.100000"
    assert result.at[7134, "dividReserveToStockPs"] == "0"


def test_resume_rejects_past_bonus_tax_basis_change(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    frame = pd.DataFrame([dict(symbol="000028.SZSE", dividOperateDate="2026-06-24", code="sz.000028",
        dividRegistDate="2026-06-23", dividPayDate="2026-06-24", dividStockMarketDate="2026-06-24",
        dividCashPsBeforeTax="0.616", dividStocksPs="0.100000", dividReserveToStockPs="0")])
    frame.to_parquet(before / "canonical_actions.parquet", index=False)
    frame["bonus_taxable_cash_ps"] = ""
    frame.to_parquet(after / "canonical_actions.parquet", index=False)
    unchanged = dict(price_view="p", raw_view="r", listing_master="m", source_codes=[], canonical_codes=[],
                     unsupported_instruments=[], reference_view="same")
    old, new = {**unchanged, "event_view": before}, {**unchanged, "event_view": after}
    assert validate_prefix_inputs(old, new, "2026-06-24") == []
    frame["bonus_taxable_cash_ps"] = "0.1"
    frame.to_parquet(after / "canonical_actions.parquet", index=False)
    with pytest.raises(ValueError, match="Past economic action changed"):
        validate_prefix_inputs(old, new, "2026-06-24")
    assert validate_prefix_inputs(old, new, "2026-06-23") == []
