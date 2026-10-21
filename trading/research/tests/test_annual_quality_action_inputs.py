"""Small source-table amendments only; no price history or account replay."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import annual_quality_action_inputs as source


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def fixture(tmp_path):
    root = tmp_path / "original"
    events, refs = root / "events", root / "references"
    events.mkdir(parents=True)
    refs.mkdir()
    actions = pd.DataFrame([dict(code="sh." + code, symbol=code + ".SSE",
        dividRegistDate="2026-10-13", dividOperateDate="2026-10-14", dividPayDate="2026-10-14",
        dividStockMarketDate="", dividCashPsBeforeTax=cash, dividCashPsAfterTax="",
        dividStocksPs="0.000000", dividReserveToStockPs="", reference_adjustment_cash_ps=None,
        source_file="", source_date="", source_url="", source_pages=None)
        for code, cash in (("603995", "0.3"), ("600612", "0.2"))])
    gaps = pd.DataFrame([dict(code="sh." + code, symbol=code + ".SSE", date=day,
        close=close, preclose=preclose, tradestatus=1, previous_observed_close=prior,
        reference_delta=preclose - prior, has_reported_distribution=None,
        **{field: None for field in actions if field not in {"code", "symbol"}},
        reference_formula_status="unresolved_incremental_raw_reference_change",
        reference_formula_basis="new_raw_observation_not_independent_action_evidence",
        expected_reference_rmb=None, reference_error_rmb=float("nan"))
        for code, day, close, preclose, prior in (("603995", "2026-10-14", 25.3, 25.13, 25.43),
            ("600612", "2026-10-14", 32.19, 31.85, 32.05), ("603605", "2026-10-16", 57.79, 55.62, 56.79))])
    actions.to_parquet(events / "canonical_actions.parquet", index=False)
    gaps.to_parquet(refs / "raw_reference_changes.parquet", index=False)
    pd.DataFrame({"symbol": ["603995.SSE", "600612.SSE", "603605.SSE"]}).to_csv(events / "source_input_universe.csv", index=False)
    save(events / "unchanged_original_evidence.json", {"keep": "original"})
    save(events / "result.json", {"status": "research_action_events_projected", "raw_view": str(root / "raw"),
        "start": "2026-01-01", "end": "2026-10-16", "canonical_events": 2, "execution_admitted": False,
        "files": {"canonical_actions.parquet": source._ref(events / "canonical_actions.parquet")["sha256"]}})
    save(refs / "result.json", {"status": "research_reference_presence_diagnostic", "event_view": str(events),
        "price_view": str(root / "prices"), "execution_admitted": False,
        "files": {"raw_reference_changes.parquet": source._ref(refs / "raw_reference_changes.parquet")["sha256"]}})
    inputs = save(root / "inputs.json", {"price_view": str(root / "prices"), "event_view": str(events),
        "reference_view": str(refs), "listing_master": str(root / "listing.csv")})
    document = tmp_path / "review/official.pdf"
    document.parent.mkdir()
    document.write_bytes(b"%PDF-synthetic-source-already-reviewed")
    evidence = [{**source._ref(document), "role": "official_implementation_notice",
        "available_at": "2026-10-18T10:00:00+08:00"}]
    review = {"evidence": evidence, "reviewed_at": "2026-10-18T10:01:00+08:00",
        "terms_basis": "synthetic_already_reviewed_notice"}
    return inputs, actions, gaps, review


def curation(tmp_path, **parts):
    return save(tmp_path / "curation.json", {"schema": source.CURATION_SCHEMA,
        "execution_admitted": False, **parts})


def reserve_patches(actions, review):
    return [{"symbol": row["symbol"], "dividOperateDate": row["dividOperateDate"],
        "expected_provider_terms": row, "updates": {"dividReserveToStockPs": "0"},
        **deepcopy(review), "terms_basis": "linked_official_proposal_and_implementation" if row["symbol"].startswith("603995")
            else "implementation_explicit_zero_reserve"} for row in actions.to_dict("records")]


def test_only_two_unknown_reserves_change_full_audit_keeps_nulls_and_reference_bytes(tmp_path):
    inputs, actions, gaps, review = fixture(tmp_path)
    patches = reserve_patches(actions, review)
    curated = curation(tmp_path, event_patches=patches)
    original = source._read(inputs)
    before_ref = source._ref(Path(original["reference_view"]) / "raw_reference_changes.parquet")
    out = tmp_path / "amended"
    result = source.build(inputs, [curated], out)
    assert result["event_patch_count"] == 2 and result["reference_patch_count"] == 0
    changed = pd.read_parquet(out / "events/canonical_actions.parquet")
    assert changed.drop(columns="dividReserveToStockPs").equals(actions.drop(columns="dividReserveToStockPs"))
    assert changed.dividStocksPs.tolist() == ["0.000000", "0.000000"]
    assert changed.dividReserveToStockPs.tolist() == ["0", "0"]
    assert source._ref(out / "references/raw_reference_changes.parquet")["sha256"] == before_ref["sha256"]
    assert source._read(out / "inputs.json")["price_view"] == original["price_view"]
    audit = source._read(out / "audit.json")
    assert audit["event_changes"][0]["old"]["reference_adjustment_cash_ps"] is None
    assert all(row["changed_fields"] == ["dividReserveToStockPs"] for row in audit["event_changes"])
    assert audit["reference_changes"] == []
    assert not Path(original["price_view"]).exists()
    assert (out / "events/unchanged_original_evidence.json").read_bytes() == (Path(original["event_view"]) / "unchanged_original_evidence.json").read_bytes()
    verified = source.validate_audit(out / "inputs.json", out / "audit.json")
    assert len(verified["event_changes"]) == 2 and verified["reference_changes"] == []


def differential_curation(tmp_path, actions, gaps, review):
    new = {key: "" for key in actions.columns}
    new.update(code="sh.603605", symbol="603605.SSE", dividRegistDate="2026-10-15",
        dividOperateDate="2026-10-16", dividPayDate="2026-10-16", dividCashPsBeforeTax="1.18",
        dividStocksPs="0", dividReserveToStockPs="0", reference_adjustment_cash_ps="1.17",
        source_file=review["evidence"][0]["file"], source_date="2026-10-12")
    independent = {**deepcopy(review), "evidence": [*deepcopy(review["evidence"]),
        {**deepcopy(review["evidence"][0]), "role": "official_reference_formula"}]}
    return curation(tmp_path, event_appends=[{"row": new, **review}], reference_patches=[{
        "code": "sh.603605", "date": "2026-10-16", "formula_id": source.FORMULA_ID,
        "expected": source._clean(gaps.loc[gaps.code.eq("sh.603605")].iloc[0].to_dict()), **independent}])


def test_explicit_new_event_keeps_118_entitlement_separate_from_117_reference(tmp_path):
    inputs, actions, gaps, review = fixture(tmp_path)
    curated = differential_curation(tmp_path, actions, gaps, review)
    out = tmp_path / "amended"
    source.build(inputs, [curated], out)
    new_actions = pd.read_parquet(out / "events/canonical_actions.parquet")
    new_gaps = pd.read_parquet(out / "references/raw_reference_changes.parquet")
    assert new_actions.iloc[:2].equals(actions)
    assert new_actions.iloc[-1].dividCashPsBeforeTax == "1.18"
    assert new_actions.iloc[-1].reference_adjustment_cash_ps == "1.17"
    expected = new_gaps.iloc[-1]
    assert expected.expected_reference_rmb == "55.6200" and expected.reference_formula_status == "reference_match"
    assert expected.reference_formula_basis == "reviewed_differential_cash_reference"
    raw = ["code", "date", "close", "preclose", "previous_observed_close", "reference_delta", "tradestatus"]
    assert new_gaps[raw].equals(gaps[raw]) and new_gaps.dtypes.equals(gaps.dtypes)
    audit = source.validate_audit(out / "inputs.json", out / "audit.json")
    assert audit["event_changes"][0]["old"] is None
    assert audit["reference_changes"][0]["new"]["dividCashPsBeforeTax"] == "1.18"
    assert audit["reference_changes"][0]["cash_inferred_from_reference_difference"] is False


@pytest.mark.parametrize("bad", ["known_cash", "wrong_expected", "late_evidence"])
def test_wrong_terms_or_evidence_reject_before_any_output(tmp_path, bad):
    inputs, actions, _, review = fixture(tmp_path)
    patch = reserve_patches(actions, review)[0]
    if bad == "known_cash":
        patch["updates"]["dividCashPsBeforeTax"] = "0.31"
    elif bad == "wrong_expected":
        patch["expected_provider_terms"]["dividCashPsBeforeTax"] = "0.31"
    else:
        patch["evidence"][0]["available_at"] = "2100-01-01T10:00:00+08:00"
    curated = curation(tmp_path, event_patches=[patch])
    out = tmp_path / "must_not_exist"
    with pytest.raises(ValueError):
        source.build(inputs, [curated], out)
    assert not out.exists()


def test_forged_audit_cannot_hide_unselected_change_or_changed_evidence(tmp_path):
    inputs, actions, _, review = fixture(tmp_path)
    curated = curation(tmp_path, event_patches=reserve_patches(actions, review))
    out = tmp_path / "amended"
    source.build(inputs, [curated], out)
    original = pd.read_parquet(out / "events/canonical_actions.parquet")
    forged = original.copy()
    forged.at[0, "dividCashPsBeforeTax"] = "9.99"
    forged.to_parquet(out / "events/canonical_actions.parquet", index=False)
    audit = source._read(out / "audit.json")
    audit["new_sources"]["events"] = source._ref(out / "events/canonical_actions.parquet")
    save(out / "audit.json", audit)
    with pytest.raises(ValueError, match="complete source-table differences"):
        source.validate_audit(out / "inputs.json", out / "audit.json")
    original.to_parquet(out / "events/canonical_actions.parquet", index=False)
    audit["new_sources"]["events"] = source._ref(out / "events/canonical_actions.parquet")
    save(out / "audit.json", audit)
    Path(review["evidence"][0]["file"]).write_bytes(b"changed source")
    with pytest.raises(ValueError, match="source changed"):
        source.validate_audit(out / "inputs.json", out / "audit.json")


def test_reference_unknown_cannot_be_promoted_without_independent_formula_evidence(tmp_path):
    inputs, actions, gaps, review = fixture(tmp_path)
    curated = differential_curation(tmp_path, actions, gaps, review)
    value = source._read(curated)
    value["reference_patches"][0]["evidence"] = deepcopy(review["evidence"])
    save(curated, value)
    out = tmp_path / "must_not_exist"
    with pytest.raises(ValueError, match="Independent official formula"):
        source.build(inputs, [curated], out)
    assert not out.exists()
