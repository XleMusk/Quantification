import json
from hashlib import sha256

import pandas as pd
import pytest

from trading.market_dataset import load_receipt, write_frame, write_json
from trading.research import action_terms
from trading.research.action_events import amend_distribution_reference, amend_secondary, build, reference_concordance
from trading.research.action_scope import pin
from trading.research.tests.test_action_events import event, pinned_inputs


def source_html(code="600000", cash="5", reserve="0"):
    return (f"<title>TEST({code})分红配股_新浪财经</title><table id='sharebonus_1'><thead><tr>"
            + "".join(f"<th><strong>{action_terms.HEADERS[i]}</strong></th>" for i in (0, 1, 5, 6, 7, 8, 9, 2, 3, 4))
            + "</tr></thead><tbody><tr>"
            + "".join(f"<td>{value}</td>" for value in ("2024-05-24", "0", reserve, cash, "实施", "2024-05-31", "2024-05-30", "--", "查看"))
            + "</tr></tbody></table>").encode("gb18030")


def primary():
    return {"resultcode": 200, "total": 1, "count": 1, "records": [{
        "F006D": "2024-05-24", "F018D": "2024-05-30", "F020D": "2024-05-31",
        "F023D": "2024-05-31", "F025D": None, "F012N": 5, "F010N": None, "F011N": None}]}


def action():
    return {**event("sh.600000", "2024-05-31", "0.5", "2024-05-30"),
            "dividCashStock": "10派5元（含税）"}


def test_literal_zero_completes_only_blank_reserve_with_secondary_label():
    row = action()
    result = action_terms.match_terms(row, action_terms.sina_rows(source_html(), "600000"), primary()["records"])
    assert result["reported_reserve_per_share"] == "0"
    assert result["confirmed_fields"] == ["dividReserveToStockPs"]
    assert not result["primary_share_terms_confirmed"] and not result["issuer_confirmed"]
    assert row["dividReserveToStockPs"] == "" and result["expected_provider_terms"]["dividStocksPs"] == "0.000000"


@pytest.mark.parametrize("change", [
    {"reserve_per10": "--"}, {"reserve_per10": ""}, {"reserve_per10": "1"},
    {"bonus_per10": "1"}, {"cash_per10": "0.5"}, {"record_date": "2024-05-29"},
    {"progress": "预案"}, {"notice_date": "2024-06-01"}, {"stock_date": "2024-06-01"}])
def test_unknown_positive_conflicting_and_unimplemented_secondary_terms_stay_unknown(change):
    rows = action_terms.sina_rows(source_html(), "600000")
    rows[0].update(change)
    with pytest.raises(ValueError):
        action_terms.match_terms(action(), rows, primary()["records"])


@pytest.mark.parametrize("change", [{"F011N": 1}, {"F012N": 0.5}, {"F023D": None}, {"F025D": "2024-05-31"}])
def test_primary_conflicts_and_missing_dates_are_not_overridden(change):
    rows = primary()["records"]
    rows[0].update(change)
    with pytest.raises(ValueError):
        action_terms.match_terms(action(), action_terms.sina_rows(source_html(), "600000"), rows)


def test_changed_identity_units_partial_and_duplicate_histories_fail():
    with pytest.raises(ValueError, match="issuer title"):
        action_terms.sina_rows(source_html("600001"), "600000")
    with pytest.raises(ValueError, match="headers"):
        action_terms.sina_rows(source_html().replace("每10股".encode("gb18030"), "每股".encode("gb18030")), "600000")
    with pytest.raises(ValueError, match="Complete"):
        action_terms.cninfo_rows(json.dumps({**primary(), "total": 2}).encode())
    rows = action_terms.sina_rows(source_html(), "600000")
    with pytest.raises(ValueError, match="Unique matching"):
        action_terms.match_terms(action(), rows * 2, primary()["records"])


def test_actual_pinned_builder_opt_in_preserves_default_and_account_boundaries(tmp_path, monkeypatch):
    inputs = pinned_inputs(tmp_path, monkeypatch)
    root = json.loads((inputs[0] / "result.json").read_text())["root"]
    from pathlib import Path
    root = Path(root)
    relative = "actions/600000.SSE/2024.csv"
    write_frame(root / relative, pd.DataFrame([action()]).drop(columns="_cache_file"))
    receipt = load_receipt(root, "600000.SSE")
    receipt["tasks"]["actions_2024"] = {"path": relative, "quality": {"rows": 1},
                                        "sha256": sha256((root / relative).read_bytes()).hexdigest()}
    write_json(root / "receipts/600000.SSE.json", receipt)
    view, _ = pin(root, tmp_path / "with_cash")
    baseline, baseline_result = build(view, *inputs[1:], tmp_path / "baseline")
    def source_fetch(session, method, url, **kwargs):
        if method == "GET":
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            return source_html()
        return json.dumps(primary()).encode()

    monkeypatch.setattr(action_terms, "fetch", source_fetch)
    terms, result = action_terms.collect(baseline, ["600000.SSE"], tmp_path / "terms")
    assert result["candidate_events"] == 1 and result["source_errors"] == 0
    monkeypatch.setattr(action_terms, "fetch", lambda *args, **kwargs: pytest.fail("Do not requery reused raw histories"))
    reused, reused_result = action_terms.collect(baseline, ["600000.SSE"], tmp_path / "reused", reuse_root=terms)
    assert reused_result["candidate_events"] == 1 and reused_result["reused_symbols"] == 1
    assert reused_result["records"][0]["observed_at"] == result["records"][0]["observed_at"]
    assert reused_result["records"][0]["secondary_request_headers"] == action_terms.SINA_HEADERS
    assert (terms / "600000.SSE/primary.json").read_bytes() == (reused / "600000.SSE/primary.json").read_bytes()
    amended, amended_result = build(view, *inputs[1:], tmp_path / "amended", secondary_terms=terms)
    original = pd.read_parquet(baseline / "canonical_actions.parquet")
    updated = pd.read_parquet(amended / "canonical_actions.parquet")
    old = original.loc[original.symbol.eq("600000.SSE")].iloc[0]
    new = updated.loc[updated.symbol.eq("600000.SSE")].iloc[0]
    assert old.dividReserveToStockPs == "" and new.dividReserveToStockPs == "0"
    for field in ("dividCashPsBeforeTax", "dividCashPsAfterTax", "dividStocksPs", "dividRegistDate", "dividPayDate"):
        assert old[field] == new[field]
    assert baseline_result["secondary_reported_reserve_completions"] == 0
    assert amended_result["secondary_reported_reserve_completions"] == 1
    assert not amended_result["execution_admitted"] and not amended_result["cash_share_actions_verified"]
    assert (amended / "secondary_reported_terms/600000.SSE/secondary.html").is_file()
    references = tmp_path / "references"
    references.mkdir()
    other = original.loc[original.symbol.ne(old.symbol)].iloc[0]
    gaps = reference_concordance(pd.DataFrame([{**old.to_dict(), "date": old.dividOperateDate,
        "close": 9.5, "preclose": 9.5, "tradestatus": 1, "previous_observed_close": 10.,
        "reference_delta": -.5, "has_reported_distribution": True, "has_reviewed_special_reference": False,
        "source_year_queried": True}, {**other.to_dict(), "date": other.dividOperateDate,
        "close": 9.97, "preclose": 9.97, "tradestatus": 1, "previous_observed_close": 10.,
        "reference_delta": -.03, "has_reported_distribution": True, "has_reviewed_special_reference": False,
        "source_year_queried": True}]), [], [])
    write_frame(references / "raw_reference_changes.parquet", gaps)
    gaps = pd.read_parquet(references / "raw_reference_changes.parquet")
    for name in ("reviewed_special_references.json", "cash_reference_evidence.json", "distribution_reference_evidence.json"):
        write_json(references / name, [])
    write_json(references / "result.json", {"status": "research_reference_presence_diagnostic",
        "event_view": str(baseline), "execution_admitted": False, "limitations": ["test-only"],
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in references.iterdir() if p.is_file()}})
    original_actions = (baseline / "canonical_actions.parquet").read_bytes()
    original_gaps = (references / "raw_reference_changes.parquet").read_bytes()
    targeted, targeted_refs, targeted_result, _ = amend_secondary(baseline, references, terms,
        [(old.symbol, old.dividOperateDate)], tmp_path / "targeted", tmp_path / "targeted_refs")
    target_actions = pd.read_parquet(targeted / "canonical_actions.parquet")
    assert target_actions.columns.equals(original.columns)
    pd.testing.assert_frame_equal(target_actions.loc[target_actions.symbol.ne(old.symbol)],
                                  original.loc[original.symbol.ne(old.symbol)])
    ref = pd.read_parquet(targeted_refs / "raw_reference_changes.parquet")
    assert ref.columns.equals(gaps.columns) and ref.iloc[0].reference_formula_status == "reference_match"
    assert ref.iloc[0].expected_reference_rmb == "9.5"
    pd.testing.assert_frame_equal(ref.iloc[1:].fillna(""), gaps.iloc[1:].fillna(""))
    assert targeted_result["secondary_amendment"]["reference_rows_refreshed"] == 1
    assert not targeted_result["secondary_amendment"]["raw_prices_rescanned"]
    assert (baseline / "canonical_actions.parquet").read_bytes() == original_actions
    assert (references / "raw_reference_changes.parquet").read_bytes() == original_gaps
    with pytest.raises(FileExistsError, match="never overwrite"):
        amend_secondary(baseline, references, terms, [(old.symbol, old.dividOperateDate)], targeted, targeted_refs)
    with pytest.raises(ValueError, match="one matched archived candidate"):
        amend_secondary(baseline, references, terms, [(old.symbol, "2026-07-01")], tmp_path / "unknown")
    assert not (tmp_path / "unknown").exists()
    distribution = tmp_path / "distribution"
    pdf = distribution / "supplements/sources/test.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-test-only-reference")
    reference_terms = {key: old[key] for key in ("symbol", "code", "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate",
                                                "dividCashPsBeforeTax", "dividStocksPs")}
    reference_terms.update(dividReserveToStockPs="0", reference_adjustment_cash_ps="0.5", reference_adjustment_share_ratio="0",
        source_date="2024-05-24", source_url="https://example.invalid/test-only.pdf", source_file="supplements/sources/test.pdf",
        source_sha256=sha256(pdf.read_bytes()).hexdigest(), source_pages=[1])
    write_json(distribution / "supplements/distribution_references.json", [reference_terms])
    targeted_actions_bytes = (targeted / "canonical_actions.parquet").read_bytes()
    revised_refs, revised_result = amend_distribution_reference(targeted, targeted_refs, distribution, tmp_path / "revised_refs")
    revised = pd.read_parquet(revised_refs / "raw_reference_changes.parquet")
    assert revised.iloc[0].reference_formula_basis == "reviewed_cash_reference"
    assert revised.iloc[0].reference_formula_status == "reference_match"
    pd.testing.assert_frame_equal(revised.iloc[1:], ref.iloc[1:])
    assert not revised_result["distribution_amendment"]["entitlements_changed"]
    assert (targeted / "canonical_actions.parquet").read_bytes() == targeted_actions_bytes
    changed = original.copy()
    changed.loc[changed.symbol.eq("600000.SSE"), "dividCashPsBeforeTax"] = "0.6"
    with pytest.raises(ValueError):
        action_terms.apply_reported_terms(changed, terms)


def test_cli_routes_only_to_explicit_public_source_acquisition(monkeypatch):
    import run_committee
    calls = []
    monkeypatch.setattr(action_terms, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["stock-action-terms", "--execute"]) == 0
    assert calls == [["--execute"]]


def test_collection_rejects_already_completed_secondary_view_before_io(tmp_path):
    write_json(tmp_path / "result.json", {"status": "research_action_events_projected",
                                        "execution_admitted": False,
                                        "secondary_reported_reserve_completions": 452})
    destination = tmp_path / "must_not_create"
    with pytest.raises(ValueError, match="primary-only event view"):
        action_terms.collect(tmp_path, ["600000.SSE"], destination)
    assert not destination.exists()


def test_prefix_publication_preserves_old_unknowns_and_unrestricted_evidence(tmp_path):
    from copy import deepcopy
    before, fresh = tmp_path / "before", tmp_path / "fresh"
    old = {"symbol": "600000.SSE", "date": "2024-05-31", "reported_reserve_per_share": "0"}
    past_new, future = {**old, "date": "2025-05-30"}, {**old, "date": "2026-05-29"}
    record = {"status": "completed_secondary_action_terms", "event_view": "same_primary",
              "execution_admitted": False, "source_errors": 0, "candidate_events": 1,
              "records": [{"symbol": old["symbol"], "candidates": [old]}]}
    write_json(before / "result.json", record)
    unrestricted = deepcopy(record)
    unrestricted["records"][0]["candidates"] = [old, past_new, future]
    unrestricted["candidate_events"] = 3
    write_json(fresh / "result.json", unrestricted)
    folder, result = action_terms.freeze_prefix_terms(fresh, before, "2026-01-01", tmp_path / "pinned")
    assert result["records"][0]["candidates"] == [old, future]
    assert result["candidate_events"] == 2
    assert result["prefix_freeze"]["deferred_past_candidates"] == [(old["symbol"], past_new["date"])]
    assert json.loads((folder / "unrestricted_source_result.json").read_text()) == unrestricted
    assert json.loads((fresh / "result.json").read_text()) == unrestricted
    unrestricted["records"][0]["candidates"][0] = {**old, "reported_reserve_per_share": "1"}
    write_json(fresh / "result.json", unrestricted)
    with pytest.raises(ValueError, match="Prior completed terms changed"):
        action_terms.freeze_prefix_terms(fresh, before, "2026-01-01", tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
