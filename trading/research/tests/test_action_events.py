from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.market_dataset import load_receipt, normalize_symbol, write_frame, write_json
from trading.research.action_events import (build, check_references, distribution_reference_evidence,
                                           project_actions, reference_changes, reference_concordance)
from trading.research.action_scope import pin, prepare
from trading.research.price_boundaries import EFFECTIVE, NEW, OLD
from trading.research.tests.test_action_scope import source_views
from trading.research.tests.test_price_boundaries import rename


def event(code=OLD, day="2023-06-06", cash="0.03", registration="2023-06-05", payment=None):
    return {"code": code, "symbol": normalize_symbol(code).vt_symbol, "dividOperateDate": day,
            "dividRegistDate": registration, "dividPayDate": payment or day, "dividStockMarketDate": "",
            "dividCashPsBeforeTax": cash, "dividStocksPs": "0.000000", "dividReserveToStockPs": "",
            "dividCashPsAfterTax": "0.027 or 0.03", "_cache_file": "test-only-source.csv"}


def test_old_dividend_is_preserved_and_projected_to_price_identity_not_new_trade_code():
    frame = pd.DataFrame([event()])
    original = frame.copy(deep=True)
    projected, canonical, outside, audit = project_actions(frame, "2023-01-01", "2026-09-30", rename())
    assert frame.equals(original) and len(projected) == len(canonical) == 1 and outside.empty and audit == []
    row = canonical.iloc[0]
    assert row.code == NEW and row.symbol == "302132.SZSE" and row.source_code == row.security_id == OLD
    assert row.tradable_code == row.registration_code == row.payment_code == OLD
    assert row.dividReserveToStockPs == "" and row.dividCashPsAfterTax == "0.027 or 0.03"


def test_registration_and_payment_crossing_rename_use_their_own_dates():
    frame = pd.DataFrame([event(NEW, EFFECTIVE, registration="2025-02-14", payment="2025-02-18")])
    _, canonical, _, _ = project_actions(frame, "2023-01-01", "2026-09-30", rename())
    row = canonical.iloc[0]
    assert row.registration_code == OLD and row.tradable_code == row.payment_code == NEW


def test_cross_code_same_event_reuses_existing_reconciler_with_all_source_rows_in_audit():
    frame = pd.DataFrame([event(), event(NEW)])
    projected, canonical, _, audit = project_actions(frame, "2023-01-01", "2026-09-30", rename())
    assert len(projected) == 2 and len(canonical) == len(audit) == 1
    assert {row["source_code"] for row in audit[0]["source_rows"]} == {OLD, NEW}
    assert audit[0]["status"] == "same_economic_event_collapsed"


@pytest.mark.parametrize("change", [{"dividCashPsBeforeTax": "0.04"}, {"dividPayDate": "2023-06-07"}])
def test_conflicting_alias_economic_events_are_never_dropped(change):
    frame = pd.DataFrame([event(), {**event(NEW), **change}])
    with pytest.raises(ValueError, match="Conflicting provider action"):
        project_actions(frame, "2023-01-01", "2026-09-30", rename())


def test_outside_cutoff_keeps_raw_records_but_never_becomes_in_window_events():
    frame = pd.DataFrame([event(), event(NEW, "2026-10-15", registration="2026-10-14")])
    projected, canonical, outside, _ = project_actions(frame, "2023-01-01", "2026-09-30", rename())
    assert len(projected) == 2 and canonical.dividOperateDate.tolist() == ["2023-06-06"]
    assert outside.dividOperateDate.tolist() == ["2026-10-15"] and outside.iloc[0].source_code == NEW


def test_projected_combined_cash_keeps_source_components_identity_and_unknown_tax():
    first = event("sh.601966", "2024-06-14", "0.286", "2024-06-13")
    frame = pd.DataFrame([first, {**first, "dividCashPsBeforeTax": "0.091"}])
    original = frame.copy(deep=True)
    resolution = {**first, "kind": "issuer_combined_cash_event", "expected_provider_cash_amounts": ["0.286", "0.091"],
                  "dividCashPsBeforeTax": "0.377", "dividStocksPs": "0", "dividReserveToStockPs": "0"}
    projected, canonical, _, audit = project_actions(frame, "2023-01-01", "2026-09-30", rename(), [resolution])
    assert frame.equals(original) and projected.dividCashPsBeforeTax.tolist() == ["0.286", "0.091"]
    assert canonical.dividCashPsBeforeTax.tolist() == ["0.377"]
    assert canonical.dividCashPsAfterTax.tolist() == [""]
    assert all(row["dividCashPsAfterTax"] == first["dividCashPsAfterTax"] for row in audit[0]["source_rows"])
    assert canonical.security_id.tolist() == canonical.tradable_code.tolist() == ["sh.601966"]
    assert audit[0]["status"] == "issuer_combined_cash_event" and len(audit[0]["source_rows"]) == 2


def test_missing_source_field_and_nonfinite_terms_fail_instead_of_inventing_values():
    frame = pd.DataFrame([event()])
    with pytest.raises(ValueError, match="source action schema"):
        project_actions(frame.drop(columns="dividRegistDate"), "2023-01-01", "2026-09-30", rename())
    frame.loc[0, "dividCashPsBeforeTax"] = "NaN"
    with pytest.raises(ValueError, match="Nonfinite"):
        project_actions(frame, "2023-01-01", "2026-09-30", rename())


def pinned_inputs(tmp_path, monkeypatch):
    from trading.research import action_events, price_boundaries
    raw = source_views(tmp_path / "raw")
    root, _ = prepare(raw, tmp_path / "root")
    symbol, relative = "300114.SZSE", "actions/300114.SZSE/2023.csv"
    write_frame(root / relative, pd.DataFrame([event()]).drop(columns="_cache_file"))
    receipt = load_receipt(root, symbol)
    receipt["tasks"]["actions_2023"] = {"path": relative, "quality": {"rows": 1},
                                          "sha256": sha256((root / relative).read_bytes()).hexdigest()}
    write_json(root / "receipts" / (symbol + ".json"), receipt)
    view, _ = pin(root, tmp_path / "view")
    levels = tmp_path / "levels"
    levels.mkdir()
    disclosure = levels / "disclosure.pdf"
    disclosure.write_bytes(b"%PDF-test-only-rename")
    digest = sha256(disclosure.read_bytes()).hexdigest()
    monkeypatch.setattr(price_boundaries, "RENAME_SHA256", digest)
    monkeypatch.setattr(action_events, "RENAME_SHA256", digest)
    identity = rename()
    identity["disclosure_sha256"] = digest
    write_json(levels / "result.json", {"status": "derived_research_levels", "raw_view": str(raw), "rename": identity})
    supplements = tmp_path / "supplements"
    supplements.mkdir()
    return view, levels, disclosure, supplements


def test_actual_pinned_consumer_retains_all_pending_and_empty_queries(tmp_path, monkeypatch):
    inputs = pinned_inputs(tmp_path, monkeypatch)
    folder, result = build(*inputs, tmp_path / "output")
    assert (result["expected_codes"], result["expected_queries"], result["queried"], result["unqueried"]) == (3, 12, 1, 11)
    assert result["canonical_events"] == result["dated_alias_source_rows"] == 1
    assert not result["execution_admitted"] and not result["cash_share_actions_verified"]
    assert pd.read_csv(folder / "input_coverage.csv").loc[lambda frame: ~frame.queried, "rows"].isna().all()


@pytest.mark.parametrize("overlap", [False, True])
def test_disjoint_secondary_additions_preserve_both_archives_and_reject_overlaps(tmp_path, monkeypatch, overlap):
    from trading.research import action_terms
    inputs = pinned_inputs(tmp_path, monkeypatch)
    roots = [tmp_path / name for name in ("prior_terms", "new_terms")]
    for root in roots:
        root.mkdir()
        write_json(root / "result.json", {"test_source": str(root)})
    seen = []
    def apply(frame, root):
        seen.append(root)
        return frame, [{"symbol": "600000.SSE", "date": "2024-01-02" if overlap or root == roots[0] else "2025-01-02"}]
    monkeypatch.setattr(action_terms, "apply_reported_terms", apply)
    if overlap:
        with pytest.raises(ValueError, match="Overlapping secondary"):
            build(*inputs, tmp_path / "output", secondary_terms=roots)
        assert not (tmp_path / "output").exists()
    else:
        folder, result = build(*inputs, tmp_path / "output", secondary_terms=roots)
        assert seen == roots and result["secondary_reported_reserve_completions"] == 2
        assert result["secondary_terms_sources"] == [str(root.resolve()) for root in roots]
        assert (folder / "secondary_reported_terms/result.json").exists()
        assert (folder / "secondary_reported_terms_1/result.json").exists()


@pytest.mark.parametrize("tamper", [False, True])
def test_pinned_combined_consumer_publishes_one_cash_event_with_unchanged_issuer_pdf(tmp_path, monkeypatch, tamper):
    from trading.research import action_events
    inputs = pinned_inputs(tmp_path, monkeypatch)
    root = Path(json.loads((inputs[0] / "result.json").read_text())["root"])
    first = event("sh.600000", "2024-06-14", "0.286", "2024-06-13")
    relative = "actions/600000.SSE/2024.csv"
    write_frame(root / relative, pd.DataFrame([first, {**first, "dividCashPsBeforeTax": "0.091"}]).drop(columns="_cache_file"))
    receipt = load_receipt(root, "600000.SSE")
    receipt["tasks"]["actions_2024"] = {"path": relative, "quality": {"rows": 2},
                                         "sha256": sha256((root / relative).read_bytes()).hexdigest()}
    write_json(root / "receipts/600000.SSE.json", receipt)
    view, _ = pin(root, tmp_path / "combined_view")
    pdf = inputs[-1] / "supplements/combined.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-fixture-only-combined")
    row = {**first, "kind": "issuer_combined_cash_event", "expected_provider_cash_amounts": ["0.286", "0.091"],
           "dividCashPsBeforeTax": "0.377", "dividStocksPs": "0", "dividReserveToStockPs": "0",
           "source_date": "2024-06-07", "source_file": "supplements/combined.pdf",
           "source_url": "https://example.test/combined.pdf", "source_sha256": sha256(pdf.read_bytes()).hexdigest()}
    write_json(inputs[-1] / "supplements/provider_action_resolutions.json", [row])
    if tamper:
        copy = action_events.shutil.copy2

        def changed_copy(source, target):
            result = copy(source, target)
            if Path(source) == pdf:
                Path(target).write_bytes(b"%PDF-changed-after-validation")
            return result

        monkeypatch.setattr(action_events.shutil, "copy2", changed_copy)
        with pytest.raises(ValueError, match="source changed during publication"):
            build(view, *inputs[1:], tmp_path / "output")
        assert not (tmp_path / "output/result.json").exists()
        return
    folder, result = build(view, *inputs[1:], tmp_path / "output")
    assert result["source_rows"] == 3 and result["canonical_events"] == 2 and result["issuer_combined_event_groups"] == 1
    canonical = pd.read_parquet(folder / "canonical_actions.parquet").loc[lambda f: f.code.eq("sh.600000")].iloc[0]
    assert canonical.dividCashPsBeforeTax == "0.377" and canonical.dividCashPsAfterTax == ""
    assert (folder / "supplements/combined.pdf").read_bytes() == pdf.read_bytes()
    assert not result["execution_admitted"] and not result["cash_share_actions_verified"]


def test_reviewed_supplement_addition_has_identity_and_separate_reference_cash(tmp_path, monkeypatch):
    inputs = pinned_inputs(tmp_path, monkeypatch)
    supplements = inputs[-1]
    pdf = supplements / "supplements/source.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-test-only-cash-source")
    row = {**event("sh.600000", "2024-06-06", "0.11", "2024-06-05"),
           "source_date": "2024-06-01", "source_file": "supplements/source.pdf",
           "source_url": "https://example.test/source.pdf", "reference_adjustment_cash_ps": "0.109"}
    row["dividReserveToStockPs"] = "0"
    write_frame(supplements / "supplements/corporate_actions.csv", pd.DataFrame([row]).drop(columns="_cache_file"))
    folder, result = build(*inputs, tmp_path / "output")
    assert result["supplements_added"] == 1 and result["canonical_events"] == 2
    actions = pd.read_parquet(folder / "canonical_actions.parquet")
    added = actions.loc[actions.code.eq("sh.600000")].iloc[0]
    assert added.source_code == added.security_id == added.tradable_code == "sh.600000"
    assert added.dividCashPsBeforeTax == "0.11" and added.reference_adjustment_cash_ps == "0.109"
    assert (folder / "supplements/source.pdf").read_bytes() == pdf.read_bytes()


def test_payload_change_fails_before_output(tmp_path, monkeypatch):
    inputs = pinned_inputs(tmp_path, monkeypatch)
    (inputs[0] / "reported_actions.parquet").write_bytes(b"changed source")
    with pytest.raises(ValueError, match="Source hash changed"):
        build(*inputs, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_route_dispatches_only_action_consumer(monkeypatch):
    import run_committee
    from trading.research import action_events
    calls = []
    monkeypatch.setattr(action_events, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["stock-action-events", "--view", "local"]) == 0
    assert calls == [["--view", "local"]]


def test_reference_changes_reuse_observed_prior_close_not_adjusted_price_or_previous_other_stock():
    raw = pd.DataFrame({"code": ["sh.600000", NEW], "date": ["2025-02-18"] * 2,
                        "close": ["10", "7"], "preclose": ["9.95", "7.03"], "tradestatus": ["1", "1"]})
    previous = {"sh.600000": 10.0, NEW: 7.04}
    gaps = reference_changes(raw, previous)
    assert gaps.code.tolist() == ["sh.600000"] and gaps.previous_observed_close.tolist() == [10.0]
    assert previous == {"sh.600000": 10.0, NEW: 7.0}


def test_inactive_missing_close_never_imputes_a_reference_or_hides_it_as_a_zero_return():
    previous = {NEW: 10.0}
    raw = pd.DataFrame({"code": [NEW], "close": [""], "preclose": [""], "tradestatus": ["0"]})
    assert reference_changes(raw, previous).empty and pd.isna(previous[NEW])
    active = pd.DataFrame({"code": [NEW], "close": ["8"], "preclose": ["7"], "tradestatus": ["1"]})
    assert reference_changes(active, previous).empty and previous[NEW] == 8


def test_full_pinned_reference_consumer_and_missing_event_query_classification(tmp_path, monkeypatch):
    from trading.research.tests.test_bulk_adjusted_prices import derived_views
    from trading.research.bulk_adjusted_prices import build as price_build
    raw, levels = derived_views(tmp_path / "prices", monkeypatch)
    price_view, price_result = price_build(raw, levels, tmp_path / "price_output", allow_derived=True)
    assert price_result["status"] == "research_reconstruction_completed"
    root, _ = prepare(raw, tmp_path / "source_root")
    for code in (OLD, NEW):
        symbol = normalize_symbol(code).vt_symbol
        relative = f"actions/{symbol}/2026.csv"
        empty = pd.DataFrame([event(code)]).iloc[:0].drop(columns="_cache_file")
        write_frame(root / relative, empty)
        receipt = load_receipt(root, symbol)
        receipt["tasks"]["actions_2026"] = {"path": relative, "quality": {"rows": 0},
                                             "sha256": sha256((root / relative).read_bytes()).hexdigest()}
        write_json(root / "receipts" / (symbol + ".json"), receipt)
    source_view, source_result = pin(root, tmp_path / "source_view")
    event_view = tmp_path / "events"
    event_view.mkdir()
    actions = pd.DataFrame([event(NEW)]).iloc[:0].drop(columns="_cache_file")
    write_frame(event_view / "canonical_actions.parquet", actions)
    for name in ("coverage.csv", "input_universe.csv", "input_calendar.csv"):
        target = "input_coverage.csv" if name == "coverage.csv" else "source_" + name
        (event_view / target).write_bytes((source_view / name).read_bytes())
    write_json(event_view / "result.json", {"status": "research_action_events_projected", "raw_view": str(raw),
               "start": "2025-02-14", "end": "2026-09-30", "files": {
                   path.name: sha256(path.read_bytes()).hexdigest() for path in event_view.iterdir()}})
    supplements = tmp_path / "no_supplements"
    supplements.mkdir()
    folder, result = check_references(event_view, price_view, supplements, tmp_path / "references")
    assert result["source_rows"] == 5 and result["canonical_rows"] == result["dates"] == 3
    assert result["raw_reference_changes"] == 2 and result["without_event_queried_source_year"] == 1
    assert result["without_event_pending_source_query"] == 1
    assert not result["amount_concordance_verified"] and not result["execution_admitted"]
    assert len(pd.read_parquet(folder / "raw_reference_changes.parquet")) == 2
    assert result["reference_formula_status_counts"] == {"missing_source_event": 2}
    assert (folder / "rules_implementation.py").is_file()


def reference_row(**changes):
    return {"code": "sh.600000", "date": "2024-06-06", "previous_observed_close": 10,
            "preclose": 9.9, "has_reported_distribution": True,
            "dividCashPsBeforeTax": "0.10", "dividStocksPs": "0", "dividReserveToStockPs": "0", **changes}


def test_reference_concordance_separates_known_terms_blank_terms_and_missing_event():
    frame = pd.DataFrame([reference_row(), reference_row(dividReserveToStockPs=""),
                          reference_row(has_reported_distribution=False), reference_row(preclose=9.8)])
    original = frame.copy(deep=True)
    result = reference_concordance(frame, [])
    assert frame.equals(original)
    assert result.reference_formula_status.tolist() == ["reference_match", "unresolved_blank_terms",
                                                        "missing_source_event", "reference_mismatch"]
    assert result.expected_reference_rmb.iloc[1:3].eq("").all()
    assert result.reference_error_rmb.iloc[1:3].isna().all()


def test_reviewed_reference_cash_is_not_holder_cash_and_does_not_fill_source_blanks():
    row = reference_row(dividCashPsBeforeTax="0.100000", dividReserveToStockPs="", preclose=9.95)
    evidence = {"code": row["code"], "dividOperateDate": row["date"], "dividCashPsBeforeTax": "0.10",
                "dividStocksPs": "0", "dividReserveToStockPs": "0", "reference_adjustment_cash_ps": "0.05"}
    result = reference_concordance(pd.DataFrame([row]), [], [evidence]).iloc[0]
    assert result.reference_formula_status == "reference_match"
    assert result.expected_reference_rmb == "9.95" and result.dividCashPsBeforeTax == "0.100000"
    assert result.dividReserveToStockPs == "" and result.reference_formula_basis == "reviewed_cash_reference"
    with pytest.raises(ValueError, match="entitlement"):
        reference_concordance(pd.DataFrame([row]), [], [{**evidence, "dividCashPsBeforeTax": "0.11"}])


def test_literal_issuer_float_formula_does_not_pass_conflicting_stated_and_raw_reference():
    row = reference_row(code="sh.600666", date="2023-02-20", previous_observed_close=2.84,
                        preclose=1.79, has_reported_distribution=False)
    special = {"symbol": "600666.SSE", "date": row["date"], "kind": "restructuring_no_entitlement",
               "shares_before": "1227326240", "debt_shares": "0", "investor_shares": "1536186603",
               "holder_shares": "0", "debt_cash_equivalent": "0", "investor_cash": "989465020",
               "investor_compensation_cash": "483326468", "cash_dividend": "0",
               "reference_share_base": "883566332", "reference_share_basis": "unrestricted_float",
               "reference_threshold_price": "0.96"}
    result = reference_concordance(pd.DataFrame([row]), [special]).iloc[0]
    assert result.reference_formula_status == "reference_mismatch"
    assert round(float(result.expected_reference_rmb), 2) == 1.65
    assert result.preclose == 1.79 and result.reference_error_rmb < -.14


@pytest.mark.parametrize("terms", [{"dividStocksPs": "NaN"}, {"dividCashPsBeforeTax": "-0.1"},
                                   {"dividReserveToStockPs": "Infinity"}])
def test_invalid_reference_terms_are_rejected_not_repaired(terms):
    with pytest.raises(ValueError, match="Invalid reported reference terms"):
        reference_concordance(pd.DataFrame([reference_row(**terms)]), [])


def distribution_fixture(tmp_path):
    source = tmp_path / "supplements/source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-fixture-only")
    row = {**event("sh.688095", "2023-06-21", "0.5", "2023-06-20"), "dividStocksPs": "0",
           "dividReserveToStockPs": "0.4", "dividStockMarketDate": "2023-06-21",
           "source_date": "2023-06-15", "source_url": "https://example.test/source.pdf",
           "source_file": "supplements/source.pdf", "source_sha256": sha256(source.read_bytes()).hexdigest(),
           "reference_adjustment_cash_ps": "0.4775", "reference_adjustment_share_ratio": "0.3820"}
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    return row, pd.DataFrame([row])


def test_differentiated_distribution_keeps_actual_entitlement_and_separate_reference_ratio(tmp_path):
    row, actions = distribution_fixture(tmp_path)
    evidence = distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")
    raw = reference_row(code=row["code"], date=row["dividOperateDate"], previous_observed_close=233.3,
                        preclose=168.47, dividCashPsBeforeTax="0.5", dividReserveToStockPs="0.4")
    result = reference_concordance(pd.DataFrame([raw]), [], evidence).iloc[0]
    assert result.reference_formula_status == "reference_match"
    assert result.reference_formula_basis == "reviewed_distribution_reference"
    assert round(float(result.expected_reference_rmb), 2) == 168.47
    assert result.dividCashPsBeforeTax == "0.5" and result.dividReserveToStockPs == "0.4"
    assert reference_concordance(pd.DataFrame([raw]), []).iloc[0].reference_formula_status == "reference_mismatch"


@pytest.mark.parametrize("change", [{"dividReserveToStockPs": ""}, {"dividReserveToStockPs": "0.3"},
                                   {"dividRegistDate": "2023-06-19"}])
def test_distribution_reference_rejects_missing_or_conflicting_canonical_terms(tmp_path, change):
    row, _ = distribution_fixture(tmp_path)
    with pytest.raises(ValueError, match="canonical"):
        distribution_reference_evidence(tmp_path, pd.DataFrame([{**row, **change}]), "2023-01-01", "2026-09-30")


def test_distribution_reference_requires_unchanged_source_and_finite_virtual_ratio(tmp_path):
    row, actions = distribution_fixture(tmp_path)
    write_json(tmp_path / "supplements/distribution_references.json", [{**row, "reference_adjustment_share_ratio": "NaN"}])
    with pytest.raises(ValueError, match="Invalid reviewed"):
        distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    (tmp_path / row["source_file"]).write_bytes(b"changed source")
    with pytest.raises(ValueError, match="Source hash changed"):
        distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")


def test_cash_only_reference_accepts_no_stock_listing_without_reducing_holder_cash(tmp_path):
    row, _ = distribution_fixture(tmp_path)
    row.update(dividReserveToStockPs="0", dividStockMarketDate="", dividCashPsBeforeTax="0.377",
               reference_adjustment_cash_ps="0.37444", reference_adjustment_share_ratio="0")
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    evidence = distribution_reference_evidence(tmp_path, pd.DataFrame([row]), "2023-01-01", "2026-09-30")
    raw = reference_row(code=row["code"], date=row["dividOperateDate"], previous_observed_close=24.37,
                        preclose=24.0, dividCashPsBeforeTax="0.377")
    result = reference_concordance(pd.DataFrame([raw]), [], evidence).iloc[0]
    assert result.reference_formula_status == "reference_match" and result.dividCashPsBeforeTax == "0.377"
    assert result.reference_formula_basis == "reviewed_cash_reference"
    row["dividReserveToStockPs"] = "0.4"
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    with pytest.raises(ValueError, match="listing"):
        distribution_reference_evidence(tmp_path, pd.DataFrame([row]), "2023-01-01", "2026-09-30")


def test_reserve_only_reference_needs_no_fictitious_cash_payment_date(tmp_path):
    row, _ = distribution_fixture(tmp_path)
    row.update(dividCashPsBeforeTax="0", dividPayDate="", dividReserveToStockPs="0.1",
               reference_adjustment_cash_ps="0", reference_adjustment_share_ratio="0.0993994")
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    evidence = distribution_reference_evidence(tmp_path, pd.DataFrame([row]), "2023-01-01", "2026-09-30")
    raw = reference_row(code=row["code"], date=row["dividOperateDate"], previous_observed_close=33,
                        preclose=30.02, dividCashPsBeforeTax="0", dividReserveToStockPs="0.1")
    result = reference_concordance(pd.DataFrame([raw]), [], evidence).iloc[0]
    assert result.reference_formula_status == "reference_match" and result.dividCashPsBeforeTax == "0"
    assert result.reference_formula_basis == "reviewed_distribution_reference"
    row["dividCashPsBeforeTax"] = "1"
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    with pytest.raises(ValueError, match="payment date"):
        distribution_reference_evidence(tmp_path, pd.DataFrame([row]), "2023-01-01", "2026-09-30")


def test_distribution_correction_notice_can_follow_registration_but_not_ex_date(tmp_path):
    row, _ = distribution_fixture(tmp_path)
    row["source_date"] = row["dividOperateDate"]
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    assert len(distribution_reference_evidence(tmp_path, pd.DataFrame([row]), "2023-01-01", "2026-09-30")) == 1
    for change in ({"source_date": "2023-06-22"}, {"dividPayDate": "2023-06-01"},
                   {"dividRegistDate": row["dividOperateDate"]}):
        write_json(tmp_path / "supplements/distribution_references.json", [{**row, **change}])
        with pytest.raises(ValueError, match="chronology"):
            distribution_reference_evidence(tmp_path, pd.DataFrame([{**row, **change}]), "2023-01-01", "2026-09-30")


def separate_reference_fixture(tmp_path):
    row, actions = distribution_fixture(tmp_path)
    source = tmp_path / "supplements/reference.pdf"
    source.write_bytes(b"%PDF-fixture-only-separate-reference")
    row.update(reference_source_date="2023-06-15", reference_source_url="https://example.test/reference.pdf",
               reference_source_file="supplements/reference.pdf", reference_source_pages=[1, 2],
               reference_source_sha256=sha256(source.read_bytes()).hexdigest())
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    return row, actions


def test_separate_formula_source_is_pinned_without_changing_holder_terms(tmp_path):
    row, actions = separate_reference_fixture(tmp_path)
    original = actions.copy(deep=True)
    evidence = distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")
    assert evidence == [row] and actions.equals(original)
    assert evidence[0]["source_sha256"] != evidence[0]["reference_source_sha256"]
    assert evidence[0]["dividCashPsBeforeTax"] == "0.5"


@pytest.mark.parametrize("change", [{"reference_source_date": "2023-06-22"},
                                   {"reference_source_sha256": "0" * 64},
                                   {"reference_source_file": "supplements/source.pdf"},
                                   {"reference_source_pages": []}])
def test_separate_formula_source_rejects_invalid_or_changed_evidence(tmp_path, change):
    row, actions = separate_reference_fixture(tmp_path)
    write_json(tmp_path / "supplements/distribution_references.json", [{**row, **change}])
    with pytest.raises(ValueError):
        distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")


@pytest.mark.parametrize("change", [None, "missing_link", "wrong_date", "pit", "channel", "changed_page"])
def test_issuer_archive_reference_requires_linked_page_and_preserves_non_pit_status(tmp_path, change):
    from trading.research.action_disclosures import ISSUER_IMPLEMENTATION_NOTICES
    url, context = next(iter(ISSUER_IMPLEMENTATION_NOTICES.items()))
    row, _ = distribution_fixture(tmp_path)
    row.update(context, code="sz.300779", dividRegistDate="2026-06-11", dividOperateDate="2026-06-12",
               dividPayDate="2026-06-12", dividStockMarketDate="2026-06-12", source_url=url,
               source_channel="issuer_linked_archive", point_in_time_availability_verified=False,
               issuer_page_file="supplements/issuer_page.html")
    page = '<time>2026-06-04</time><a href="' + url.replace("https://", "http://") + '">PDF</a>'
    if change == "missing_link":
        page = page.replace("<a href=", "<span data-url=")
    elif change == "wrong_date":
        page = page.replace("2026-06-04", "2026-07-28")
    elif change == "pit":
        row["point_in_time_availability_verified"] = True
    elif change == "channel":
        row.pop("source_channel")
    target = tmp_path / row["issuer_page_file"]
    target.write_text(page, encoding="utf-8")
    row["issuer_page_sha256"] = sha256(target.read_bytes()).hexdigest()
    if change == "changed_page":
        target.write_text(page + "changed", encoding="utf-8")
    write_json(tmp_path / "supplements/distribution_references.json", [row])
    actions = pd.DataFrame([row])
    if change is None:
        evidence = distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")
        assert evidence[0]["point_in_time_availability_verified"] is False
        assert evidence[0]["issuer_archive_upload_date"] == "2026-07-28"
    else:
        with pytest.raises(ValueError):
            distribution_reference_evidence(tmp_path, actions, "2023-01-01", "2026-09-30")
