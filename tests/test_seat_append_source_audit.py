"""Synthetic exact-D close fixtures and injected one-shot transport only.

The existing validated-catalog adapter fixture isolates this helper from the
separate catalog/registration suites. No real quote or Engine is run.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import seat_append_source_audit as module
from test_seat_hypothesis_dated_inputs import evidence as input_evidence, save, read, A, B, D, F
from test_seat_append_dated_inputs import evidence as append_evidence


@pytest.fixture
def evidence(append_evidence, monkeypatch):
    e = append_evidence
    folder = e.inputs / "synthetic_contract"
    folder.mkdir()
    probe = folder / "probe_receipt.json"
    save(probe, {"synthetic_contract_evidence_only": True})
    official = []
    for name in ("stock_page", "stock_A_formatter", "quote_grammar"):
        raw = folder / (name + ".raw")
        raw.write_bytes(("synthetic official evidence fixture " + name).encode())
        official.append({"raw_file": str(raw), "sha256": module.original._ref(raw)["sha256"],
            "retrieved_at": "2026-10-15T12:00:00+08:00"})
    contract = folder / "contract.json"
    save(contract, {"schema": "sina_ordinary_SH_SZ_A_price_time_contract_assessment_v1",
        "created_at": "2026-10-15T12:01:00+08:00", "probe_receipt": str(probe),
        "probe_receipt_sha256": module.original._ref(probe)["sha256"], "official_sources": official,
        "price_currency": {"a_currency_value": "CNY", "normal_SH_SZ_A_only": True},
        "volume_amount_orderbook": {"normalization_performed": False,
            "amount_display_evidence": "official stock_A formatter labels amount yuan"}})
    monkeypatch.setattr(module, "CONTRACT_FILE", contract)
    e.source_audit_out, e.contract = e.root / "new_source_comparison", contract
    return e


def transport(e, *, case=None):
    raw = pd.read_csv(e.closes[D] / "raw_daily.csv", dtype=str, keep_default_na=False).set_index("code")
    calls = []
    def get(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 12)
        if case == "transport_error":
            raise OSError("synthetic single transport failure")
        codes = endpoint.removeprefix(module.parser.ENDPOINT).split(",")
        lines = []
        for index, code in enumerate(codes):
            if case == "missing_assignment" and index == 0:
                continue
            values = raw.loc[code[:2] + "." + code[2:]]
            fields = ["0"] * 34
            for position, value in {0: "合成证券", 1: values.open, 2: values.preclose, 3: values.close,
                    4: values.high, 5: values.low, 8: values.volume, 9: values.amount,
                    30: F if case == "wrong_date" else D, 31: "14:59:59" if case == "pre_close_clock" else "15:00:00",
                    32: "03" if case == "nonregular" else "00", 33: ""}.items():
                fields[position] = str(value)
            if case == "amount_mismatch":
                fields[9] = str(float(values.amount) * 10)
            lines.append(f'var hq_str_{code}="' + ",".join(fields) + '";')
        body = "\n".join(lines) + (";alert(1)" if case == "bad_grammar" else "")
        return SimpleNamespace(content=body.encode("gb18030"), status_code=302 if case == "http_error" else 200,
            headers={"Content-Type": "application/javascript; charset=GB18030", "Location": None})
    return get, calls


def run(e, *, execute=False, get=None, close=None, day=D, out=None):
    return module.run(e.append_registration, e.catalog, e.closes[day] if close is None else close,
        day, e.source_audit_out if out is None else out, execute=execute, _get=get,
        _clock=lambda: e.clock["value"])


def test_default_plan_dynamic_four_arm_union_does_not_create_output_or_request(evidence):
    e = evidence
    get, calls = transport(e)
    plan = run(e, get=get)
    assert plan["symbols"] == [A, B]  # Deliberately two names, never the old fixed eight.
    assert list(plan["arm_candidate_counts"].values()) == [2, 0, 2, 0]
    assert plan["ready_for_execute"] and plan["request_count"] == 0
    assert plan["fetch_succeeded"] is None and not plan["network_accessed"]
    assert calls == [] and not e.source_audit_out.exists()


def test_one_get_preserves_original_fields_and_calls_exact_old_audit_without_E(evidence, monkeypatch):
    e = evidence
    get, calls = transport(e)
    real = module.original.load_complete_close
    loaded = []
    def D_only(folder):
        assert Path(folder).resolve() == e.closes[D].resolve()
        loaded.append(Path(folder).resolve())
        return real(folder)
    monkeypatch.setattr(module.original, "load_complete_close", D_only)
    result = run(e, execute=True, get=get)
    assert result["status"] == "completed_exact_D_comparison_only"
    assert len(calls) == result["request_count"] == 1 and result["fetch_succeeded"] is True
    assert calls[0][0] == module.parser.ENDPOINT + "sh600000,sh600001"
    assert result["all_rows_match"] is True and result["quote_response_observed"] is True
    assert loaded and all(path == e.closes[D].resolve() for path in loaded)
    audit = read(e.source_audit_out / "audit.json")
    assert len(audit["records"]) == 2 and audit["comparison"]["independence_verified"] is False
    assert set(audit["records"][0]["fields"]) == set(module.FIELDS)
    source = e.source_audit_out / "comparison_source"
    metadata = read(source / "metadata.json")
    assert metadata["amount_unit"] == "CNY" and metadata["price_unit"] == "CNY_per_share"
    assert metadata["is_BrokerQuote"] is False and metadata["independent_source_identity_verified"] is False
    assert (source / "source_response.raw").is_file() and (source / "request.json").is_file()
    parsed = read(source / "parsed_source.json")
    assert all(q["frozen_labels"] == [] and q["current_quote"] is False for q in parsed["quotes"])
    assert not result["engine_run"] and not result["publication_created"]


@pytest.mark.parametrize("case", ["transport_error", "http_error", "wrong_date", "pre_close_clock",
    "nonregular", "missing_assignment", "bad_grammar"])
def test_failed_attempt_is_retained_once_without_retry_or_successful_audit(evidence, case):
    e = evidence
    get, calls = transport(e, case=case)
    result = run(e, execute=True, get=get)
    assert len(calls) == result["request_count"] == 1
    assert result["status"] == "failed_stopped_no_retry" and result["fetch_succeeded"] is False
    assert result["audit"] is None and not (e.source_audit_out / "audit.json").exists()
    assert (e.source_audit_out / "outcome.json").is_file()
    if case != "transport_error":
        assert (e.source_audit_out / "comparison_source/source_response.raw").is_file()
        assert result["quote_response_observed"] is True


def test_exact_field_disagreement_keeps_real_audit_but_blocks_admission(evidence):
    e = evidence
    get, calls = transport(e, case="amount_mismatch")
    result = run(e, execute=True, get=get)
    assert len(calls) == 1 and result["fetch_succeeded"] is True
    assert result["status"] == "blocked_exact_source_mismatch" and result["all_rows_match"] is False
    assert (e.source_audit_out / "audit.json").is_file() and result["execution_admitted"] is False


def test_empty_union_has_readable_empty_audit_without_observed_or_fabricated_quote(evidence):
    e = evidence
    for facts in e.loaded["batches"][0]["recipes"].values():
        facts.update(candidates=[], supported=[], clocks={})
    get, calls = transport(e)
    result = run(e, execute=True, get=get)
    assert result["status"] == "completed_empty_scope_no_quote_no_market_validation"
    assert result["request_count"] == 0 and result["quote_response_observed"] is False
    assert result["fetch_succeeded"] is None and result["network_accessed"] is False and calls == []
    audit = read(e.source_audit_out / "audit.json")
    assert audit["records"] == [] and audit["scope_empty_is_not_market_validation"] is True
    assert audit["comparison"]["independence_verified"] is False
    metadata = read(e.source_audit_out / "comparison_source/metadata.json")
    assert metadata["source_family"] == "ExplicitNoQuoteEmptyScope" and metadata["raw_response"] is None
    assert metadata["fetch_succeeded"] is None and metadata["request_count"] == 0
    assert pd.read_csv(e.source_audit_out / "comparison_source/comparison.csv").empty
    assert not (e.source_audit_out / "comparison_source/source_response.raw").exists()


def test_future_D_and_wrong_D_close_rejected_without_transport_or_future_reads(evidence, monkeypatch):
    e = evidence
    get, calls = transport(e)
    real = module.original.load_complete_close
    def D_only(folder):
        assert Path(folder).resolve() != e.closes[F].resolve()
        return real(folder)
    monkeypatch.setattr(module.original, "load_complete_close", D_only)
    plan = run(e, day=F, get=get)
    assert plan["status"] == "waiting_for_same_D_post_close" and calls == []
    with pytest.raises(ValueError, match="Execute requires"):
        run(e, day=F, execute=True, get=get)
    monkeypatch.setattr(module.original, "load_complete_close", real)
    with pytest.raises(ValueError):
        run(e, close=e.closes[F], execute=True, get=get)
    assert not e.source_audit_out.exists() and calls == []


def test_output_protection_and_100_symbol_bound_precede_any_request(evidence, monkeypatch):
    e = evidence
    get, calls = transport(e)
    with pytest.raises(ValueError, match="inside"):
        run(e, execute=True, get=get, out=e.closes[D] / "nested_output")
    existing = e.root / "existing_output"
    existing.mkdir()
    marker = existing / "user.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run(e, execute=True, get=get, out=existing)
    assert marker.read_text(encoding="utf-8") == "keep"
    monkeypatch.setattr(module.dated, "required_audit_symbols", lambda arms: [f"{i:06d}.SZSE" for i in range(1, 102)])
    with pytest.raises(ValueError, match="at most 100"):
        run(e, execute=True, get=get)
    assert calls == [] and not e.source_audit_out.exists()


def test_changed_contract_evidence_or_non_yuan_unit_rejected_before_request(evidence):
    e = evidence
    get, calls = transport(e)
    contract = read(e.contract)
    official = Path(contract["official_sources"][0]["raw_file"])
    original_bytes = official.read_bytes()
    official.write_bytes(original_bytes + b"changed")
    with pytest.raises(ValueError, match="changed"):
        run(e, execute=True, get=get)
    official.write_bytes(original_bytes)
    contract["volume_amount_orderbook"]["amount_display_evidence"] = "unverified ten thousand yuan"
    save(e.contract, contract)
    with pytest.raises(ValueError, match="formatter-yuan"):
        run(e, execute=True, get=get)
    assert calls == [] and not e.source_audit_out.exists()


@pytest.mark.parametrize("empty", [False, True])
def test_cross_midnight_after_preflight_cannot_request_or_build_even_empty_audit(evidence, empty):
    e = evidence
    if empty:
        for facts in e.loaded["batches"][0]["recipes"].values():
            facts.update(candidates=[], supported=[], clocks={})
    get, calls = transport(e)
    moments = iter([e.clock["value"], datetime.fromisoformat("2026-10-17T00:00:01+08:00"),
        datetime.fromisoformat("2026-10-17T00:00:02+08:00")])
    result = module.run(e.append_registration, e.catalog, e.closes[D], D, e.source_audit_out,
        execute=True, _get=get, _clock=lambda: next(moments))
    assert result["status"] == "failed_stopped_no_retry" and result["request_count"] == 0
    assert result["fetch_succeeded"] is None and result["quote_response_observed"] is False
    assert calls == [] and not (e.source_audit_out / "audit.json").exists()
