"""Synthetic local snapshots and strict dated packages; no Engine or network."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_all_stock_probe as roster
from trading.research import baostock_bulk_close as bulk
from trading.research import seat_hypothesis_dated_inputs as dated
from trading.research import seat_hypothesis_snapshot as producer
from trading.research.tests import test_seat_hypothesis_snapshot as source_fixture
from trading.research.tests import test_shortline_native_preopen_forward as close_fixture


EVENT, D, F = "2026-10-15", "2026-10-16", "2026-10-19"
A, B = "600000.SSE", "600001.SSE"


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def registration_from_snapshot(folder, result, snapshot_dir):
    arms = []
    for arm_id, (recipe, lag) in dated.ARMS.items():
        method = result["methods"][recipe]
        frame = pd.read_csv(method["candidates_file"])
        ranked = [{"symbol": row.symbol, "score": float(row.net_buy_to_reported_turnover), "event_date": EVENT}
            for row in frame.itertuples()]
        ranked.sort(key=lambda row: (-row["score"], row["symbol"]))
        arms.append({"arm_id": arm_id, "base_hypothesis_id": method["hypothesis_id"],
            "consumer_method_id": dated.original.AVAILABILITY_HYPOTHESIS_ID if recipe == "legacy_repeated_sum"
                else dated.original.STOCKDAY_AVAILABILITY_ID,
            "lag_sessions": lag, "source_files": {key: method[key] for key in
                ("candidates_file", "candidates_sha256", "panel_file", "panel_sha256", "availability_evidence")},
            "availability_evidence_ref": dated._ref(method["availability_evidence"]),
            "all_source_candidate_rows": len(ranked), "supported_candidate_rows": len(ranked),
            "ranked_research_candidates": ranked})
    record = {"schema": dated.REGISTRATION_SCHEMA, "registered_at": D + "T05:51:00+08:00",
        "source_event_date": EVENT, "source_available_at": result["source_available_at"],
        "source_snapshot": dated._ref(snapshot_dir / "result.json"), "source_archive": result["source_archive"],
        "code_refs": [dated._ref(module.__file__) for module in (producer, dated.original)],
        "rule_spec": result["rule_spec"], "config": dated.original._config(100000, 5).to_dict(),
        "selection_function": "trading.engine.choose_targets", "arms": arms,
        "candidate_panel_only": True, "execution_admitted": False, "selection_enabled": False,
        "strategy_validated": False, "historical_publication_verified": False}
    path = folder / "registration.json"
    save(path, record)
    return path


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dated input assembly must not access network, account or Engine")
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("trading.engine.advance", forbidden)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    clock = {"value": datetime.fromisoformat(D + "T20:00:00+08:00")}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["value"].astimezone(tz) if tz is not None else clock["value"]
    monkeypatch.setattr(bulk, "datetime", Clock)
    monkeypatch.setattr(dated, "_now", lambda: clock["value"])
    monkeypatch.setattr(producer, "_now", lambda: datetime.fromisoformat(D + "T05:48:00+08:00"))
    monkeypatch.setattr(source_fixture, "DAY", EVENT)
    page_clock = D + "T05:35:00+08:00"
    monkeypatch.setattr(source_fixture, "PAGE_CLOCKS", {kind: page_clock for kind in ("summary", "buy", "sell")})
    complete = D + "T05:36:00+08:00"
    factory = source_fixture.cache.__wrapped__(inputs)
    item = factory("source", completed=complete,
        extra_buy=[source_fixture._seat("named-b1", 250., 10., symbol="600001.SH"),
            source_fixture._seat("named-b2", 200., 10., symbol="600001.SH")],
        extra_summaries=[{"TRADE_DATE": EVENT, "SECUCODE": "600001.SH", "SECURITY_CODE": "600001",
            "EXPLANATION": source_fixture.SINGLE, "ACCUM_AMOUNT": 1000.}])
    result = producer.build(item.source, item.out)
    registration = registration_from_snapshot(inputs, result, item.out)
    calendar = inputs / "calendar.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    wheel = inputs / "synthetic.whl"
    wheel.write_bytes(b"synthetic SDK source receipt, never executed")
    sdk_hash = dated._ref(wheel)["sha256"]
    monkeypatch.setattr(bulk, "WHEEL_SHA256", sdk_hash)
    monkeypatch.setattr(roster, "WHEEL_SHA256", sdk_hash)
    monkeypatch.setattr(close_fixture, "SYMBOLS", [A, B])
    sdk = {"wheel": str(wheel), "sha256": sdk_hash, "distribution_version": "0.9.4"}
    closes = {day: close_fixture.strict_close(inputs / ("close_" + day), day, calendar, sdk) for day in (D, F)}
    comparison_dir = inputs / "independent"
    comparison_dir.mkdir()
    comparison = comparison_dir / "D.csv"
    raw = pd.read_csv(closes[D] / "raw_daily.csv", dtype=str, keep_default_na=False)
    values = raw.rename(columns={"preclose": "pre_close"}).copy()
    values["symbol"] = values.code.str.split(".").str[1] + ".SSE"
    values.loc[values.symbol.isin([A, B]), ["date", "symbol", *dated.FIELDS]].to_csv(comparison, index=False)
    metadata = comparison_dir / "metadata.json"
    save(metadata, {"schema": dated.COMPARISON_SCHEMA, "day": D,
        "source_id": "synthetic-second-price-vendor", "source_family": "SyntheticIndependent",
        "source_available_at": D + "T18:45:00+08:00", "basis": "raw_unadjusted",
        "price_unit": "CNY_per_share", "amount_unit": "CNY", "independence_declared": True})
    audit = inputs / "audit.json"
    dated.build_exact_source_audit(closes[D], comparison, metadata, audit, decision_day=D, symbols=[A, B])
    return SimpleNamespace(root=tmp_path, inputs=inputs, clock=clock, registration=registration, calendar=calendar,
        closes=closes, comparison=comparison, metadata=metadata, audit=audit, snapshot=item.out,
        out=tmp_path / "new_dated_inputs")


def build(e, **kwargs):
    return dated.build_dated_inputs(e.registration, e.calendar, e.closes[D], e.audit, e.out,
        decision_day=D, **kwargs)


def rebind_sidecars(e, change):
    registration = read(e.registration)
    done = {}
    for arm in registration["arms"]:
        file = arm["source_files"]["availability_evidence"]
        if file not in done:
            sidecar = read(file)
            change(sidecar)
            save(file, sidecar)
            done[file] = dated._ref(file)
        arm["availability_evidence_ref"] = done[file]
    save(e.registration, registration)


def test_complete_D_publishes_two_lags_with_all_ranked_candidates_and_no_future_raw(evidence, monkeypatch):
    e = evidence
    future_before = (e.closes[F] / "raw_daily.csv").read_bytes()
    original_loader = dated.load_complete_close
    loaded = []
    def D_only(folder):
        assert Path(folder).resolve() == e.closes[D].resolve()
        loaded.append(Path(folder).resolve())
        return original_loader(folder)
    monkeypatch.setattr(dated, "load_complete_close", D_only)
    result = build(e)
    publication = read(e.out / "publication.json")
    assert publication["published_at"] == D + "T20:00:00+08:00"
    assert publication["bundle"] == dated._ref(e.out / "bundle.json")
    assert publication["execution_day"] == F
    assert result["D_primary"]["source_available_at"] == D + "T18:30:00+08:00"
    assert result["D_inventory"]["ordinary_inventory_rows"] == 3
    assert len(result["arms"]) == 4 and result["next_execution_raw_supplied"] is False
    for arm in result["arms"]:
        assert len(arm["source_ranked_candidates"]) == 2
        assert arm["weights"] is None
        if arm["lag_sessions"] == 1:
            assert arm["decision_kind"] == "RANKED_CANDIDATES" and len(arm["ranked_candidates"]) == 2
            assert {row["status"] for row in arm["event_schedule"]} == {"matured_on_D"}
        else:
            assert arm["decision_kind"] == "NO_CHANGE" and arm["ranked_candidates"] == []
            assert {row["first_eligible_decision_day"] for row in arm["event_schedule"]} == {F}
    assert (e.closes[F] / "raw_daily.csv").read_bytes() == future_before
    assert loaded and all(folder == e.closes[D].resolve() for folder in loaded)
    assert not result["engine_run"] and not result["account_created"] and not result["orders_generated"]
    assert result["source_audit"]["verification"]["comparison"]["independence_verified"] is False


def test_unknown_event_does_not_veto_known_event(evidence):
    e = evidence
    rebind_sidecars(e, lambda sidecar: [row.update(retrieved_at=None) for row in sidecar["records"] if row["symbol"] == B])
    result = build(e)
    for arm in result["arms"]:
        if arm["lag_sessions"] == 1:
            assert [row["symbol"] for row in arm["ranked_candidates"]] == [A]
            assert next(row for row in arm["event_schedule"] if row["symbol"] == B)["status"] == "unknown"
            assert arm["decision_kind"] == "RANKED_CANDIDATES"


def test_late_registered_source_waits_for_later_exact_close_without_backfilling_D(evidence):
    e = evidence
    new_clock = D + "T16:00:00+08:00"
    registration = read(e.registration)
    source = read(e.snapshot / "result.json")
    source.update(source_available_at=new_clock, created_at=D + "T16:10:00+08:00")
    save(e.snapshot / "result.json", source)
    registration.update(source_available_at=new_clock, registered_at=D + "T16:20:00+08:00",
        source_snapshot=dated._ref(e.snapshot / "result.json"))
    save(e.registration, registration)
    rebind_sidecars(e, lambda sidecar: (sidecar.update(source_available_at=new_clock),
        [row.update(retrieved_at=new_clock) for row in sidecar["records"]]))
    result = build(e)
    assert all(arm["decision_kind"] == "NO_CHANGE" for arm in result["arms"])
    assert all(row["source_available_by_D_1500"] is False for arm in result["arms"] for row in arm["event_schedule"])
    assert all(row["first_eligible_decision_day"] == F for arm in result["arms"] for row in arm["event_schedule"])


@pytest.mark.parametrize("missing", ["close", "audit", "pending"])
def test_real_missing_source_or_audit_only_waits_without_publication_or_directory(evidence, missing):
    e = evidence
    folder, audit = e.closes[D], e.audit
    if missing == "close":
        folder = e.inputs / "absent_D"
    elif missing == "audit":
        audit = e.inputs / "absent_audit.json"
    else:
        receipt = read(folder / "receipt.json")
        receipt["status"] = "running"
        save(folder / "receipt.json", receipt)
    result = dated.build_dated_inputs(e.registration, e.calendar, folder, audit, e.out, decision_day=D)
    assert result["status"].startswith("waiting_") and result["publication"] is None
    assert not e.out.exists() and not result["account_created"]


@pytest.mark.parametrize("target", ["raw", "comparison", "audit_scope", "registration_config", "old_summary", "source_code", "registration_clock"])
def test_changed_or_inexact_evidence_is_rejected_before_output(evidence, target):
    e = evidence
    if target == "raw":
        file = e.closes[D] / "raw_daily.csv"
        file.write_bytes(file.read_bytes() + b"\n")
    elif target == "comparison":
        frame = pd.read_csv(e.comparison)
        frame.loc[0, "close"] += .1
        frame.to_csv(e.comparison, index=False)
        # Updating claimed input hashes cannot conceal a real row mismatch.
        audit = read(e.audit)
        audit["comparison"]["raw"] = dated._ref(e.comparison)
        save(e.audit, audit)
    elif target == "audit_scope":
        audit = read(e.audit)
        audit["scope_keys"] = audit["scope_keys"][:1]
        save(e.audit, audit)
    elif target == "registration_config":
        registration = read(e.registration)
        registration["config"]["holdings"] = 1
        save(e.registration, registration)
    elif target == "source_code":
        registration = read(e.registration)
        registration["code_refs"][0]["sha256"] = "0" * 64
        save(e.registration, registration)
    elif target == "registration_clock":
        registration = read(e.registration)
        registration["registered_at"] = F + "T05:51:00+08:00"
        save(e.registration, registration)
    else:
        save(e.audit, {"schema": "seat_candidate_source_bar_pair_audit_v1", "summary": {"paired_rows": 9999}})
    with pytest.raises(ValueError):
        build(e)
    assert not e.out.exists()


def test_audit_recomputes_raw_fields_and_rejects_future_or_extra_keys(evidence):
    e = evidence
    for mode in ("future", "extra"):
        frame = pd.read_csv(e.comparison)
        extra = frame.iloc[0].copy()
        extra["date"] = F if mode == "future" else D
        extra["symbol"] = A if mode == "future" else "600099.SSE"
        path = e.comparison.parent / (mode + ".csv")
        pd.concat([frame, pd.DataFrame([extra])], ignore_index=True).to_csv(path, index=False)
        out = e.inputs / (mode + "_audit.json")
        with pytest.raises(ValueError):
            dated.build_exact_source_audit(e.closes[D], path, e.metadata, out, decision_day=D, symbols=[A, B])
        assert not out.exists()


def test_cross_day_and_input_package_output_are_rejected(evidence):
    e = evidence
    e.clock["value"] = datetime.fromisoformat(F + "T20:00:00+08:00")
    with pytest.raises(ValueError, match="cross-day"):
        build(e)
    assert not e.out.exists()
    e.clock["value"] = datetime.fromisoformat(D + "T20:00:00+08:00")
    with pytest.raises(ValueError, match="input package"):
        dated.build_dated_inputs(e.registration, e.calendar, e.closes[D], e.audit,
            e.closes[D] / "not_allowed", decision_day=D)


def test_complete_empty_snapshot_is_NO_CHANGE_not_empty_FULL_TARGET(evidence):
    e = evidence
    # A complete saved producer panel with no matching events retains explicit
    # nonmatches; it is not a missing snapshot or an empty target portfolio.
    registration = read(e.registration)
    result = read(e.snapshot / "result.json")
    for recipe, method in result["methods"].items():
        panel = pd.read_csv(method["panel_file"])
        panel["hypothesis_h1"] = False
        panel["hypothesis_status"] = "not_matched"
        panel.to_csv(method["panel_file"], index=False)
        panel.iloc[:0].to_csv(method["candidates_file"], index=False)
        method.update(panel_sha256=dated._ref(method["panel_file"])["sha256"],
            candidates_sha256=dated._ref(method["candidates_file"])["sha256"])
        sidecar = read(method["availability_evidence"])
        sidecar.update(panel_sha256=method["panel_sha256"], candidates_sha256=method["candidates_sha256"], records=[])
        save(method["availability_evidence"], sidecar)
    save(e.snapshot / "result.json", result)
    registration_from_snapshot(e.inputs, result, e.snapshot)
    empty = e.comparison.parent / "empty.csv"
    pd.DataFrame(columns=["date", "symbol", *dated.FIELDS]).to_csv(empty, index=False)
    audit = e.inputs / "empty_audit.json"
    dated.build_exact_source_audit(e.closes[D], empty, e.metadata, audit, decision_day=D, symbols=[])
    result = dated.build_dated_inputs(e.registration, e.calendar, e.closes[D], audit, e.out, decision_day=D)
    assert len(result["arms"]) == 4
    assert all(arm["decision_kind"] == "NO_CHANGE" and arm["weights"] is None for arm in result["arms"])
    assert all(arm["panel_observations"] for arm in result["arms"])
    assert result["source_audit"]["verification"]["scope_empty_is_not_market_validation"] is True


def test_permuted_raw_after_strict_load_cannot_pair_prices_with_wrong_roster_symbols(evidence, monkeypatch):
    e = evidence
    original_loader = dated.load_complete_close
    def permuted(folder):
        metadata, raw, ordinary = original_loader(folder)
        return metadata, raw.iloc[::-1].reset_index(drop=True), ordinary
    monkeypatch.setattr(dated, "load_complete_close", permuted)
    with pytest.raises(ValueError, match="identities/statuses"):
        build(e)
    assert not e.out.exists()


def test_two_cli_entrypoints_publish_only_dated_inputs_and_future_comparison_clock_is_rejected(evidence, capsys):
    e = evidence
    capsys.readouterr()
    audit = e.inputs / "cli_audit.json"
    assert dated.main(["exact-source-audit", "--close", str(e.closes[D]),
        "--comparison-csv", str(e.comparison), "--comparison-metadata", str(e.metadata),
        "--decision-day", D, "--symbol", A, "--symbol", B, "--out", str(audit)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["matched_rows"] == 2 and output["scope_rows"] == 2
    assert output["audit"] == dated._ref(audit) and output["engine_run"] is False
    assert dated.main(["dated-inputs", "--registration", str(e.registration), "--calendar", str(e.calendar),
        "--close", str(e.closes[D]), "--source-audit", str(audit), "--decision-day", D,
        "--out", str(e.out)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["publication"]["published_at"] == D + "T20:00:00+08:00"
    assert output["output_created"] is True and output["account_created"] is False
    metadata = read(e.metadata)
    metadata["source_available_at"] = F + "T20:00:00+08:00"
    save(e.metadata, metadata)
    future = e.inputs / "future_clock_audit.json"
    with pytest.raises(ValueError, match="future"):
        dated.main(["exact-source-audit", "--close", str(e.closes[D]),
            "--comparison-csv", str(e.comparison), "--comparison-metadata", str(e.metadata),
            "--decision-day", D, "--symbol", A, "--symbol", B, "--out", str(future)])
    assert not future.exists()
