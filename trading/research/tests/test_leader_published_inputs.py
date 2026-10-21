"""Synthetic fixture inputs; no historical PnL or external account writes."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import leader_close_publication as m
from trading.research import leader_published_inputs as inputs
from trading.research.leader_daily_replay import EXIT_POLICIES, freeze_cohorts
from trading.research.tests.test_leader_forward_run import A, B, DAYS, bundle, protocol
from trading.research.tests.test_leader_close_publication import qualification


def clock(monkeypatch, stamp):
    value = datetime.fromisoformat(stamp)
    monkeypatch.setattr(m, "_now", lambda: value)
    monkeypatch.setattr(inputs, "_now", lambda: value)


def completed(count):
    source = bundle(count)
    hit = source["raw_bars"].date.eq(DAYS[0]) & source["raw_bars"].symbol.eq(A)
    source["raw_bars"].loc[hit, "amount"] = 10_000_000.
    for day in DAYS[:count]:
        source["source_packages"] = source.get("source_packages", []) + [{
            "day": day, "folder": "synthetic:" + day,
            "terminal_completion_observed_at": day + "T16:00:00+08:00"}]
        for family in source["batches"]:
            source["batches"][family][day].update(status="complete", candidates=[{"symbol": A, "score": 1.}])
    return source


@pytest.fixture
def registration(tmp_path, monkeypatch):
    p = protocol(tmp_path)
    source = completed(1)
    clock(monkeypatch, DAYS[0] + "T16:30:00+08:00")
    record = m.publish_close_targets(source, tmp_path / "first", protocol_path=p,
        ipo_qualification=qualification(source))
    first = tmp_path / "first" / "publication.json"
    value = {"schema": "leader_published_close_forward_registration_v1",
        "registered_at": DAYS[0] + "T17:00:00+08:00",
        "first_decision_day": DAYS[0], "first_execution_day": DAYS[1],
        "first_execution_deadline": DAYS[1] + "T09:30:00+08:00",
        "original_publication": {"file": str(first), "sha256": sha256(first.read_bytes()).hexdigest(),
            "recorded_at": record["recorded_at"]},
        "protocol_binding": record["protocol_binding"], "config": record["protocol_binding"]["config"],
        "cohort": freeze_cohorts(source["raw_bars"], DAYS[0]),
        "case_ids": [c["case_id"] for c in record["cases"]],
        "window": {"length_open_sessions": 40, "execution_tail_sessions": 6},
        "registration_is_original_publication": False, "candidate_panel_only": True,
        "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
        "model_refit": False, "future_labels_used_for_score": False}
    path = tmp_path / "registration.json"
    path.write_text(json.dumps(value), encoding="utf8")
    return p, path, value


def test_saved_bundle_roundtrip_keeps_nullable_fields_and_does_not_overwrite(tmp_path):
    source = bundle(2, nullable_owned_day=DAYS[1])
    source["raw_bars"]["pre_close"] = ["10.0000" if i % 2 else 10.0 for i in range(len(source["raw_bars"]))]
    saved = inputs.save_bundle(source, tmp_path / "inputs", origin="synthetic_fixture_only")
    loaded = inputs.load_bundle(saved)
    pd.testing.assert_frame_equal(source["raw_bars"], loaded["raw_bars"])
    assert type(loaded["raw_bars"].pre_close.iloc[0]) is float
    assert type(loaded["raw_bars"].pre_close.iloc[1]) is str
    assert source["batches"] == loaded["batches"]
    original = saved.read_bytes()
    with pytest.raises(FileExistsError):
        inputs.save_bundle(source, saved.parent, origin="synthetic_fixture_only")
    assert saved.read_bytes() == original
    raw_path = saved.parent / "raw_bars.parquet"
    raw_path.write_bytes(raw_path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="bytes"):
        inputs.load_bundle(saved)


def test_fixed_cohort_publication_does_not_roll_pool_and_saves_input_before_actual_stamp(tmp_path, monkeypatch, registration):
    protocol_path, reg_path, value = registration
    source = completed(2)
    clock(monkeypatch, DAYS[1] + "T18:30:00+08:00")
    ordinary = m.publish_close_targets(source, tmp_path / "ordinary", protocol_path=protocol_path,
        ipo_qualification=qualification(source))
    fixed = m.publish_close_targets(source, tmp_path / "fixed", protocol_path=protocol_path,
        ipo_qualification=qualification(source), cohort_registration=reg_path)
    assert ordinary["cohort"]["counts"]["common_pre_window_symbol20"] == 1
    assert fixed["cohort"]["counts"]["common_pre_window_symbol20"] == 2
    base = [c for c in fixed["cases"] if c["exit_policy"] == EXIT_POLICIES[0]]
    assert all(c["target_weights"] == {A: .16} for c in base)
    proof = inputs.input_proof(fixed["publication_input_bundle"]["file"])
    assert fixed["publication_input_bundle"] == proof
    assert datetime.fromisoformat(proof["recorded_at"]) <= datetime.fromisoformat(fixed["recorded_at"])
    assert inputs.load_bundle(proof["file"])["source_days_complete"] == source["source_days_complete"]


@pytest.mark.parametrize("fault", ["cohort", "clock"])
def test_changed_registered_cohort_or_late_method_rejected_before_output(tmp_path, monkeypatch, registration, fault):
    protocol_path, reg_path, value = registration
    if fault == "cohort":
        value["cohort"]["pools"]["common_pre_window_symbol20"] = [B]
    else:
        value["registered_at"] = DAYS[1] + "T09:30:00+08:00"
    reg_path.write_text(json.dumps(value), encoding="utf8")
    clock(monkeypatch, DAYS[1] + "T18:30:00+08:00")
    with pytest.raises(ValueError, match="registration"):
        m.publish_close_targets(completed(2), tmp_path / "rejected", protocol_path=protocol_path,
            ipo_qualification=qualification(completed(2)), cohort_registration=reg_path)
    assert not (tmp_path / "rejected").exists()


def test_tail_preparation_is_explicit_no_change_despite_valid_new_candidates(tmp_path):
    source = completed(1)
    binding, calendar_payload, config = m._protocol(protocol(tmp_path))
    calendar, _, _ = m._calendar_view(calendar_payload, "2026-01-01", "2026-12-31", [])
    prepared = m.prepare_close_case_targets(source, cohort=freeze_cohorts(source["raw_bars"], DAYS[0]),
        head=None, day=DAYS[0], calendar=calendar, config=config,
        ipo_qualification=qualification(source), decision_tail=True)
    assert len(prepared) == 20
    assert all(c["weights"] is None and c["raw_rule_weights_before_qualification"] is None
        and c["no_change_reason"] == "registered_forward_tail_no_new_decision" for c in prepared)
