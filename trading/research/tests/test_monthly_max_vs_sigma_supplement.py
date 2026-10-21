"""Synthetic one-round source supplement checks; never read actual market data."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from trading.research import monthly_max_vs_sigma_supplement as method


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def pair_fixture(tmp_path, monkeypatch):
    old_event, old_reference, event, reference, reviewed = [tmp_path / name for name in
        ("old_event", "old_reference", "new_event", "new_reference", "reviewed")]
    for directory in (old_event, old_reference, event, reference, reviewed / "supplements"):
        directory.mkdir(parents=True)
    actions = pd.DataFrame([
        dict(symbol=symbol, code=code, dividOperateDate=day, dividRegistDate=record,
            dividPayDate=day, dividStockMarketDate="", dividCashPsBeforeTax=cash,
            dividCashPsAfterTax="original tax text", dividStocksPs="0.000000",
            dividReserveToStockPs="", dividCashStock="original distribution text")
        for symbol, code, day, record, cash in (
            ("600507.SSE", "sh.600507", "2024-05-09", "2024-05-08", "0.1"),
            ("002293.SZSE", "sz.002293", "2024-06-17", "2024-06-14", "0.4"),
            ("600001.SSE", "sh.600001", "2024-04-15", "2024-04-12", "0.2"))])
    terms = actions.copy(deep=True)
    terms.loc[:1, "dividReserveToStockPs"] = "0"
    gaps = pd.DataFrame([dict(code=row.code, date=row.dividOperateDate, preclose="10.00",
        dividCashPsBeforeTax=row.dividCashPsBeforeTax, dividReserveToStockPs="",
        reference_formula_status="unresolved_blank_terms", reference_formula_basis="old",
        expected_reference_rmb="", reference_error_rmb="") for row in actions.itertuples()])
    amended = gaps.copy(deep=True)
    amended.loc[:1, "dividReserveToStockPs"] = "0"
    amended.loc[:1, "reference_formula_status"] = "reference_match"
    amended.loc[:1, "reference_formula_basis"] = "reviewed_terms"
    amended.loc[:1, "expected_reference_rmb"] = "9.90"
    amended.loc[:1, "reference_error_rmb"] = "0.00"
    for directory in (old_event, event, old_reference, reference):
        write(directory / "unchanged_scope.json", {"synthetic_scope": "same_original"})
    actions.to_parquet(old_event / "canonical_actions.parquet", index=False)
    gaps.to_parquet(old_reference / "raw_reference_changes.parquet", index=False)
    prior_event = dict(status="research_action_events_projected", canonical_events=3,
        execution_admitted=False, files={name: method.v1.digest(old_event / name)
        for name in ("canonical_actions.parquet", "unchanged_scope.json")})
    write(old_event / "result.json", prior_event)
    write(old_reference / "input_events.json", prior_event)
    prior_reference = dict(status="research_reference_presence_diagnostic", execution_admitted=False,
        event_view=str(old_event.resolve()), reference_formula_status_counts={"old": 3},
        reference_formula_basis_counts={"old": 3}, files={name: method.v1.digest(old_reference / name)
        for name in ("raw_reference_changes.parquet", "input_events.json", "unchanged_scope.json")})
    write(old_reference / "result.json", prior_reference)
    receipt = dict(parent_event_view=str(old_event.resolve()), parent_reference_view=str(old_reference.resolve()),
        reviewed_root=str(reviewed.resolve()), events=[list(key) for key in method.EVENTS],
        raw_prices_rescanned=False, other_events_changed=False, execution_admitted=False)
    after_event = {**prior_event, "files": dict(prior_event["files"]), "reviewed_cash_amendment": receipt}
    after_reference = {**prior_reference, "files": dict(prior_reference["files"]),
        "event_view": str(event.resolve()), "reviewed_cash_amendment": receipt}

    def refresh():
        terms.to_parquet(event / "canonical_actions.parquet", index=False)
        amended.to_parquet(reference / "raw_reference_changes.parquet", index=False)
        after_event["files"]["canonical_actions.parquet"] = method.v1.digest(event / "canonical_actions.parquet")
        write(event / "result.json", after_event)
        write(reference / "input_events.json", after_event)
        for name in ("raw_reference_changes.parquet", "input_events.json"):
            after_reference["files"][name] = method.v1.digest(reference / name)
        write(reference / "result.json", after_reference)

    refresh()
    amendment_receipt = tmp_path / "amendment_receipt.json"
    write(amendment_receipt, dict(event_view=str(event), reference_view=str(reference), amendment=receipt))
    resolutions = []
    for symbol, day in method.EVENTS:
        source = reviewed / (symbol + ".pdf")
        source.write_bytes(b"%PDF-synthetic-reviewed-" + symbol.encode())
        resolutions.append(dict(symbol=symbol, code="sh.600507" if symbol.startswith("6") else "sz.002293",
            dividOperateDate=day, kind="issuer_confirmed_cash_only_terms" if symbol.startswith("6")
            else "issuer_confirmed_reserve_zero_term", source_file=source.name,
            source_sha256=method.v1.digest(source), source_url="https://example.test/synthetic.pdf",
            source_date="2024-04-27" if symbol.startswith("6") else "2024-06-07",
            actual_primary_retrieved_at="2026-10-21T15:00:00+08:00",
            actual_primary_reviewed_at="2026-10-21T15:01:00+08:00",
            reserve_numeric_zero_explicit_in_notice=False, complete_cash_only_scheme_projection=True,
            pit_verified=False, execution_admitted=False))
    write(reviewed / "supplements/provider_action_resolutions.json", resolutions)
    monkeypatch.setattr(method, "PRIMARY_SHA", {row["symbol"]: row["source_sha256"] for row in resolutions})
    monkeypatch.setattr(method, "load_action_resolutions", lambda *args: deepcopy(resolutions))

    def corrected(frame, *args):
        result = frame.copy(deep=True)
        result.loc[:1, "dividReserveToStockPs"] = "0"
        return result, []

    monkeypatch.setattr(method.action_reviewed_amendment, "corrected_actions", corrected)
    return SimpleNamespace(original=dict(inputs=dict(event_view=str(old_event), reference_view=str(old_reference))),
        original_file=old_event / "canonical_actions.parquet", terms=terms,
        refresh=refresh, receipt=amendment_receipt, event=event, reference=reference)


@pytest.mark.parametrize("fault", [None, "cash", "date", "unselected"])
def test_exact_two_reserve_pair_accepts_only_blank_to_zero_and_rejects_other_fact_changes(
        tmp_path, monkeypatch, fault):
    fixture = pair_fixture(tmp_path, monkeypatch)
    original_bytes = fixture.original_file.read_bytes()
    if fault == "cash":
        fixture.terms.at[0, "dividCashPsBeforeTax"] = "0.11"
    elif fault == "date":
        fixture.terms.at[1, "dividRegistDate"] = "2024-06-13"
    elif fault == "unselected":
        fixture.terms.at[2, "dividReserveToStockPs"] = "0"
    if fault:
        fixture.refresh()  # Keep all saved hashes coherent; semantic validation must reject it.
        with pytest.raises(ValueError, match="Only two blank reserve terms"):
            method._pair(fixture.original, fixture.receipt)
    else:
        pair, refs = method._pair(fixture.original, fixture.receipt)
        assert pair["event_view"] == str(fixture.event.resolve())
        assert pair["reference_view"] == str(fixture.reference.resolve())
        assert len(pair["late_primary_reviews"]) == 2 and refs
    assert fixture.original_file.read_bytes() == original_bytes


@pytest.mark.parametrize("changed", ["batches.json", "scores.parquet"])
def test_original_saved_formation_hash_change_is_rejected_without_any_recomputation(tmp_path, monkeypatch, changed):
    root = tmp_path / "original"
    registration = root / "registration.json"
    write(registration, {"synthetic": True})
    days = pd.date_range("2024-01-31", periods=32, freq="ME").strftime("%Y-%m-%d").tolist()
    original = dict(decision_days=days)
    monkeypatch.setattr(method.v1, "_registration", lambda path: original)
    write(root / "batches.json", {day: {"status": "complete"} for day in days})
    (root / "scores.parquet").write_bytes(b"synthetic saved scores, never parsed")
    hashes = {name: method.v1.digest(root / name) for name in method.FORMATION_SHA}
    monkeypatch.setattr(method, "FORMATION_SHA", hashes)
    write(root / "formation_receipt.json", dict(common_batch_count=32, formation_rows=93188,
        historical_publication=False, files={name: method.pin(root / name) for name in hashes}, **method.v1.FLAGS))
    cases = [dict(arm=arm, status="failed", full_period_metrics_available=False,
        error=f"ValueError: Unresolved held corporate action: {symbol} {day}; missing=dividReserveToStockPs")
        for arm, (symbol, day) in zip(method.v1.ARMS, method.EVENTS)]
    write(root / "result.json", dict(schema=method.v1.METHOD + "_result", registration=method.pin(registration),
        spec=method.v1.SPEC, config=method.v1.config().to_dict(), status="completed_two_arm_attempt",
        full_paired_account_comparison=False, formation_rows=93188, cases=cases, **method.v1.FLAGS))
    for arm in method.v1.ARMS:
        write(root / (arm + "_result.json"), {"synthetic": True})
        write(root / arm / "account.json", {"original_account": True})
        (root / arm / "daily.csv").write_text("date,equity\n2024-01-02,140000\n", encoding="utf8")
    assert len(method._original(root / "result.json")[1]) == 32
    target = root / changed
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="Saved formation/scores changed"):
        method._original(root / "result.json")


def test_fresh_run_reuses_saved_batches_isolates_first_failure_and_never_resumes_old_accounts(tmp_path, monkeypatch):
    registration, original_result = tmp_path / "registered" / "registration.json", tmp_path / "old" / "result.json"
    write(registration, {"synthetic": True})
    write(original_result, {"original": True})
    write(original_result.parent / "formation_receipt.json", {"saved_formation": True})
    old_account = original_result.parent / "low_max" / "account.json"
    write(old_account, {"sealed_old_account": True})
    old_bytes = old_account.read_bytes()
    out = tmp_path / "fresh_accounts"
    batch = dict(status="complete", targets={arm: [{"symbol": "600001.SSE", "score": -.01}]
        for arm in method.v1.ARMS})
    batches = {"2024-01-31": batch}
    saved = dict(run_output=str(out), original_result=str(original_result), original_artifacts=[method.pin(old_account)],
        inputs={key: str(tmp_path / "inputs" / key) for key in
            ("price_view", "event_view", "reference_view", "raw_view", "listing_master")},
        source_clock={"actual_source_clock": "2026-10-21T15:00:00+08:00"},
        source_inventory={"mainboard_identities": 1, "synthetic": True}, amendment={"synthetic": True},
        late_primary_reviews=[{"actual_primary_reviewed_at": "2026-10-21T15:01:00+08:00"}])
    base = SimpleNamespace(calendar=["2024-01-31", "2024-02-01"],
        manifest={"limitations": ["synthetic execution reader"]}, close=Mock())
    monkeypatch.setattr(method, "_registration", lambda path: (saved, {"source_calendar": base.calendar}, batches))
    monkeypatch.setattr(method.v1, "BulkResearchFeed", lambda **options: base)

    def forbidden(*args, **kwargs):
        pytest.fail("Source supplement must not form, score, train, amend or resume original accounts")

    monkeypatch.setattr(method.v1, "formation", forbidden)
    monkeypatch.setattr(method.v1, "score_month", forbidden)
    monkeypatch.setattr(method.action_reviewed_amendment, "amend", forbidden)
    calls = []

    def consume(feed, own_config, dates, destination, **options):
        assert feed.batches is batches and options == {}
        assert destination == out / feed.arm and old_account.read_bytes() == old_bytes
        calls.append(feed.arm)
        if feed.arm == "low_max":
            raise ValueError("synthetic new unresolved action")
        destination.mkdir()
        write(destination / "account.json", {"synthetic_new_account": True})
        return dict(status="completed", completed_sessions=2, full_period_metrics_available=True)

    monkeypatch.setattr(method.v1, "baseline_ledger_result", consume)
    monkeypatch.setattr(method.v1, "_case_summary", lambda case, *args: case)
    result = method.run(registration, out)
    assert calls == list(method.v1.ARMS) and result["full_paired_account_comparison"] is False
    assert result["cases"][0]["status"] == "failed_command"
    assert "synthetic new unresolved action" in result["cases"][0]["error"]
    assert result["cases"][1]["status"] == "completed"
    assert result["saved_formation_reused"] is True and result["formation_recomputed"] is False
    assert result["original_accounts_reused"] is False and result["automatic_retry"] is False
    assert old_account.read_bytes() == old_bytes
    saved_failure = (out / "low_max_result.json").read_bytes()
    with pytest.raises(FileExistsError, match="never retried or resumed"):
        method.run(registration, out)
    assert calls == list(method.v1.ARMS) and (out / "low_max_result.json").read_bytes() == saved_failure
    base.close.assert_called_once()


def test_changed_registered_source_reference_rejects_before_original_or_execution_reader(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    write(source, {"original_source": True})
    source_ref = method.pin(source)
    registration = tmp_path / "registration.json"
    code = {"synthetic_implementation": "same"}
    monkeypatch.setattr(method, "_code", lambda: code)
    write(registration, dict(schema=method.METHOD + "_registration", spec=method.SPEC,
        config=method.v1.config().to_dict(),
        implementations=code, original_artifacts=[], source_refs=[source_ref],
        original_result=str(tmp_path / "old_result.json"), **method.v1.FLAGS))
    write(source, {"changed_source": True})

    def forbidden(*args, **kwargs):
        pytest.fail("Changed source must fail before any original-data or execution reader")

    monkeypatch.setattr(method, "_original", forbidden)
    monkeypatch.setattr(method, "_pair", forbidden)
    monkeypatch.setattr(method.v1, "BulkResearchFeed", forbidden)
    out = tmp_path / "uncreated_accounts"
    with pytest.raises(ValueError, match="source artifact changed"):
        method.run(registration, out)
    assert not out.exists()
