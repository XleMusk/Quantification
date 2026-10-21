"""Synthetic source/publication contracts; no replay, source request or old PnL."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_close_publication as old
from trading.research import leader_entry_comparison_publication as m
from trading.research import leader_entry_confirmation_inputs as confirmations
from trading.research import leader_published_inputs as inputs
from trading.research.baostock_daily_slice import digest
from trading.research.leader_daily_replay import freeze_cohorts
from trading.research.leader_published_forward_run import CASE_IDS, FLAGS, RISK_POLICY_ID, RISK_RULE_SPEC, TARGET_POLICY_ID
from trading.research.tests.test_leader_forward_run import A, B, DAYS, bundle, protocol


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    frozen = protocol(tmp_path)
    binding, calendar, config = m._protocol(frozen)
    clock = {"value": datetime.fromisoformat("2026-10-15T18:45:00+08:00")}
    for module in (m, old, inputs):
        monkeypatch.setattr(module, "_now", lambda: clock["value"])
    symbols = [f"600{i:03d}.SSE" for i in range(1, 21)]

    def source(count):
        value = bundle(count)
        exemplar = value["raw_bars"][value["raw_bars"].symbol.eq(A)]
        value["raw_bars"] = pd.concat([value["raw_bars"], *[exemplar.assign(symbol=s) for s in symbols[2:]]], ignore_index=True)
        for day, inventory in value["provider_inventory_by_day"].items():
            inventory.update(status_by_symbol={s: "1" for s in symbols}, active_valid_raw_symbols=symbols,
                raw_valuation_usable_symbols=symbols, nullable_status0_symbols=[])
            for batches in value["batches"].values():
                batches[day].update(status="complete", candidates=[{"symbol": A, "score": 1.}])
        value["source_packages"] = [{"day": day, "folder": "synthetic/" + day,
            "raw_daily_sha256": "raw-" + day, "terminal_completion_observed_at": day + "T18:30:00+08:00"}
            for day in value["source_days_complete"]]
        return value

    first = source(1)
    old.publish_close_targets(first, tmp_path / "oldpub15", protocol_path=frozen)
    pub15 = tmp_path / "oldpub15/publication.json"
    clock["value"] = datetime.fromisoformat("2026-10-15T22:00:00+08:00")
    own15 = inputs.save_bundle(first, tmp_path / "oldinput15", origin="synthetic_old_saved_input")
    source_registration = tmp_path / "oldregistration.json"
    source_registration.write_text(json.dumps({"schema": "leader_published_close_forward_registration_v1",
        "registered_at": "2026-10-15T22:10:00+08:00", "first_decision_day": DAYS[0],
        "first_execution_day": DAYS[1], "first_execution_deadline": DAYS[1] + "T09:30:00+08:00",
        "original_publication": {"file": str(pub15), "sha256": digest(pub15), "recorded_at": read(pub15)["recorded_at"]},
        "initial_publication_input_bundle": inputs.input_proof(own15), "protocol_binding": binding,
        "cohort": freeze_cohorts(first["raw_bars"], DAYS[0]), "case_ids": list(CASE_IDS),
        "config": config.to_dict(), "window": {"length_open_sessions": 40, "execution_tail_sessions": 6},
        "published_target_policy_id": TARGET_POLICY_ID, "risk_policy_id": RISK_POLICY_ID,
        "risk_rule_spec": RISK_RULE_SPEC, "registration_is_original_publication": False, **FLAGS}), encoding="utf-8")
    before = {p: p.read_bytes() for p in (source_registration, pub15, own15)}
    clock["value"] = datetime.fromisoformat("2026-10-16T00:45:00+08:00")
    registration, registered = m.register_method(source_registration, frozen, tmp_path / "newmethod")
    clock["value"] = datetime.fromisoformat("2026-10-16T18:35:00+08:00")
    own16 = inputs.save_bundle(source(2), tmp_path / "newinput16", origin="synthetic_own_input_before_new_publication")
    clock["value"] = datetime.fromisoformat("2026-10-16T18:45:00+08:00")
    return {"tmp": tmp_path, "clock": clock, "protocol": frozen, "source_registration": source_registration,
        "registration": registration, "registered": registered, "own16": own16, "source": source,
        "before": before, "symbols": symbols}


def loaded(e):
    reg = m.load_registration(e["registration"])
    return m._load(e["own16"], reg["binding"], reg["calendar"], e["clock"]["value"])


def test_preregister_inherits_old_anchor_exact_scope_and_new_40_plus_6(evidence):
    e = evidence
    value = m.load_registration(e["registration"])["record"]
    assert value["original_cohort"]["anchor"] == "2026-10-14" and value["common20"] == e["symbols"]
    assert value["first_decision_day"] == "2026-10-16" and value["first_execution_day"] == "2026-10-19"
    assert len(value["decision_sessions"]) == 40 and len(value["execution_tail_sessions"]) == 6
    assert len(value["window"]) == 47 and len(value["case_ids"]) == 15
    assert all(path.read_bytes() == data for path, data in e["before"].items())


def test_registration_at_close_or_reusing15_rejected_before_output(evidence):
    e = evidence
    for stamp, first in (("2026-10-16T15:00:00+08:00", "2026-10-16"), ("2026-10-15T23:00:00+08:00", "2026-10-15")):
        e["clock"]["value"] = datetime.fromisoformat(stamp)
        target = e["tmp"] / (first + stamp[11:13])
        with pytest.raises(ValueError, match="first unseen|15 cannot"):
            m.register_method(e["source_registration"], e["protocol"], target, first_day=first)
        assert not target.exists()


def test_legal_pub_and_loader_unknown_confirmation_does_not_collapse_targets(evidence):
    e = evidence
    path, value = m.publish(e["own16"], None, e["registration"], e["tmp"] / "pub16")
    result = m.load_publication(path, loaded(e), e["registration"], e["clock"]["value"])
    assert result["day"] == "2026-10-16" and result["execution"] == "2026-10-19"
    assert list(result["intents"]) == list(m.COMMON5KEYS)
    assert all(intent.context is None and dict(intent.weights) == {A: .16} for intent in result["intents"].values())
    assert len(result["confirmation"]) == 20 and {r["status"] for r in result["confirmation"].values()} == {"unknown"}
    assert value["source_close_day"] != "2026-10-15" and value["successor_raw_used"] is False
    assert all(row["original_candidates"] == [{"symbol": A, "score": 1.}] for row in value["base_cases"])
    assert not value["account_created"] and not value["orders_generated"] and not value["engine_advanced"]
    assert all(path.read_bytes() == data for path, data in e["before"].items())


def test_confirmation_false_remains_evidence_not_pre_filtered_weights(evidence, monkeypatch):
    e = evidence
    manifest = e["tmp"] / "confirmation.json"
    manifest.write_text('{"synthetic":true}', encoding="utf-8")
    def load(path, *, day, calendar, source_day_proofs, observed_at):
        assert Path(path).resolve() == manifest.resolve() and day == "2026-10-16"
        assert str(calendar).endswith("calendar.json")
        assert source_day_proofs[-1]["day"] == day and observed_at == e["clock"]["value"].isoformat()
        return {"day": day, "previous_day": "2026-10-15", "available_at": "2026-10-16T18:40:00+08:00",
            "by_symbol": {A: {"symbol": A, "status": "known_false", "reason": "not_expanding"}}, "rows": [1],
            "sourceproof": {"manifest": {"file": str(manifest), "sha256": digest(manifest), "available_at": "2026-10-16T18:40:00+08:00"}}}
    from pathlib import Path
    monkeypatch.setattr(confirmations, "load_confirmation", load)
    path, value = m.publish(e["own16"], manifest, e["registration"], e["tmp"] / "with_confirmation")
    result = m.load_publication(path, loaded(e), e["registration"])
    assert result["confirmation"][A]["status"] == "known_false"
    assert all(dict(intent.weights) == {A: .16} for intent in result["intents"].values())
    assert "rows" not in value["confirmation"] and len(value["confirmation"]["by_symbol"]) == 20


@pytest.mark.parametrize("tamper", ["intent", "source", "flag", "clock"])
def test_saved_publication_changed_contract_rejected(evidence, tamper):
    e = evidence
    path, value = m.publish(e["own16"], None, e["registration"], e["tmp"] / ("tamper_" + tamper))
    if tamper == "intent": value["base_cases"][0]["target_weights"][A] = .17
    if tamper == "source": value["input_bundle"]["sha256"] = "changed"
    if tamper == "flag": value["execution_admitted"] = True
    if tamper == "clock": value["recorded_at"] = "2026-10-16T19:45:00+08:00"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        m.load_publication(path, loaded(e), e["registration"])


def test_missing_current_or_weekend_backfill_never_creates_pub(evidence):
    e = evidence
    for stamp in ("2026-10-15T23:45:00+08:00", "2026-10-17T08:00:00+08:00"):
        e["clock"]["value"] = datetime.fromisoformat(stamp)
        target = e["tmp"] / ("invalid" + stamp[:10])
        with pytest.raises(ValueError):
            m.publish(e["own16"], None, e["registration"], target)
        assert not target.exists()


def test_confirmation_future_availability_rejected_before_output(evidence, monkeypatch):
    e = evidence
    monkeypatch.setattr(m, "_confirmation", lambda *a, **k: {"available_at": "2026-10-16T19:00:00+08:00"})
    target = e["tmp"] / "future_confirmation"
    with pytest.raises(ValueError, match="Confirmation became"):
        m.publish(e["own16"], e["tmp"] / "not_read", e["registration"], target)
    assert not target.exists()


def test_tail_is_no_change_without_reading_confirmation(evidence, monkeypatch):
    e = evidence
    reg = m.load_registration(e["registration"])
    day = reg["window"][40]
    own = deepcopy(loaded(e))
    own["raw"].loc[own["raw"].date.eq("2026-10-16"), "date"] = day
    own["days"][-1] = day
    own["bundle"]["raw_bars"] = own["raw"]
    own["bundle"]["provider_inventory_by_day"][day] = own["bundle"]["provider_inventory_by_day"].pop("2026-10-16")
    own["bundle"]["provider_inventory_by_day"][day]["day"] = day
    for batches in own["bundle"]["batches"].values():
        batches[day] = batches.pop("2026-10-16")
        batches[day]["day"] = day
    own["bundle"]["source_packages"][-1].update(day=day, terminal_completion_observed_at=day + "T18:30:00+08:00")
    own["proof"]["recorded_at"] = day + "T18:35:00+08:00"
    own["previous"][day] = reg["window"][39]
    e["clock"]["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
    monkeypatch.setattr(confirmations, "load_confirmation", lambda *a, **k: pytest.fail("Tail has no turn requirement"))
    value = m._prepare(own, reg, e["clock"]["value"], e["tmp"] / "absent")
    assert value["registered_window_tail"] is True and value["confirmation"]["by_symbol"] == {}
    assert all(row["portfolio_intent"]["kind"] == "NO_CHANGE" and row["target_weights"] is None for row in value["base_cases"])


def test_finished_actual_clock_replaces_preparation_clock_and_rejects_midnight(evidence, monkeypatch):
    e = evidence
    prepare = m._prepare
    def delayed(*args, **kwargs):
        value = prepare(*args, **kwargs)
        e["clock"]["value"] = datetime.fromisoformat("2026-10-16T18:47:00+08:00")
        return value
    monkeypatch.setattr(m, "_prepare", delayed)
    path, value = m.publish(e["own16"], None, e["registration"], e["tmp"] / "finished")
    assert value["recorded_at"] == "2026-10-16T18:47:00+08:00"
    assert all(r["portfolio_intent"]["decided_at"] == value["recorded_at"] for r in value["base_cases"])
    monkeypatch.setattr(m, "_prepare", prepare)
    m.load_publication(path, loaded(e), e["registration"])
    def midnight(*args, **kwargs):
        value = prepare(*args, **kwargs)
        e["clock"]["value"] = datetime.fromisoformat("2026-10-17T00:00:00+08:00")
        return value
    monkeypatch.setattr(m, "_prepare", midnight)
    target = e["tmp"] / "after_midnight"
    with pytest.raises(ValueError, match="preparation crossed"):
        m.publish(e["own16"], None, e["registration"], target)
    assert not target.exists()
