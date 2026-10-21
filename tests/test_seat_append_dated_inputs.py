"""Append maturity and exact D publishing; synthetic data, no Engine/network.

Catalog validation has its own suite.  The local adapter below deliberately
supplies a validated catalog shape so these checks isolate dated semantics.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import seat_append_dated_inputs as module
from trading.research import seat_hypothesis_dated_inputs as old
from test_seat_hypothesis_dated_inputs import evidence as input_evidence, save, read, A, B, D, F


SESSIONS = ["2026-10-14", "2026-10-15", D, F, "2026-10-20", "2026-10-21"]


def batch(event, clock, rows, *, name=None, unknown=(), status="completed_nonempty"):
    candidates = [{"symbol": symbol, "score": score, "event_date": event} for symbol, score in rows]
    facts = {"candidates": candidates, "supported": deepcopy(candidates),
        "clocks": {(event, symbol): {"retrieved_at": None if symbol in unknown else clock,
            "published_at": None, "available_at": None if symbol in unknown else clock}
            for symbol, _ in rows}, "panel_observations": [], "input_refs": []}
    return {"batch_id": name or event, "event_day": event, "status": status,
        "source_available_at": clock, "recipes": {recipe: deepcopy(facts) for recipe in
            ("legacy_repeated_sum", "stockday_unique")}}


def test_first_maturity_is_not_rolled_to_new_account_first_day_and_lags_differ():
    catalog = {"batches": [batch("2026-10-15", D + "T05:36:00+08:00", [(A, .2)])]}
    arms = module.mature_arms(catalog, SESSIONS, F)
    for arm in arms:
        event = arm["event_schedule"][0]
        if arm["lag_sessions"] == 1:
            assert event["status"] == "matured_before_D" and event["first_eligible_decision_day"] == D
            assert arm["decision_kind"] == "NO_CHANGE" and arm["ranked_candidates"] == []
        else:
            assert event["status"] == "matured_on_D" and event["first_eligible_decision_day"] == F
            assert arm["ranked_candidates"][0]["symbol"] == A
        assert arm["weights"] is None


def test_after_close_raw_clock_delays_maturity_unknown_does_not_veto_known():
    catalog = {"batches": [batch("2026-10-15", D + "T16:00:00+08:00", [(A, .2), (B, .3)], unknown=(B,))]}
    assert all(arm["decision_kind"] == "NO_CHANGE" for arm in module.mature_arms(catalog, SESSIONS, D))
    for arm in module.mature_arms(catalog, SESSIONS, F):
        assert [row["symbol"] for row in arm["ranked_candidates"]] == [A]
        assert next(row for row in arm["event_schedule"] if row["symbol"] == B)["status"] == "unknown"
        assert next(row for row in arm["event_schedule"] if row["symbol"] == A)["source_available_by_D_1500"] is True


def test_cross_batch_same_D_latest_event_date_wins_without_score_selection_or_sum():
    # Late acquisition makes two different event dates mature together.  The
    # newer event has the LOWER score, so max(score) would visibly be wrong.
    catalog = {"batches": [batch("2026-10-14", D + "T16:00:00+08:00", [(A, .9)], name="old"),
        batch("2026-10-15", D + "T16:00:00+08:00", [(A, .1), (B, .3)], name="new")]}
    for arm in module.mature_arms(catalog, SESSIONS, F):
        assert [(row["symbol"], row["score"]) for row in arm["ranked_candidates"]] == [(B, .3), (A, .1)]
        assert len(arm["event_schedule"]) == 3
        suppressed = [row for row in arm["event_schedule"] if row["suppressed_by_newer_matured_event"]]
        assert len(suppressed) == 1 and suppressed[0]["batch_id"] == "old"
        assert suppressed[0]["status"] == "matured_on_D" and suppressed[0]["selected_event_date"] == "2026-10-15"


def test_known_empty_is_NO_CHANGE_and_pending_never_becomes_empty_target():
    empty = {"batches": [batch("2026-10-15", D + "T05:00:00+08:00", [], status="completed_empty")]}
    arms = module.mature_arms(empty, SESSIONS, D)
    assert module.required_audit_symbols(arms) == []
    assert all(arm["decision_kind"] == "NO_CHANGE" and arm["weights"] is None for arm in arms)
    empty["batches"][0]["status"] = "pending"
    with pytest.raises(ValueError, match="Pending or failed"):
        module.mature_arms(empty, SESSIONS, D)


@pytest.fixture
def evidence(input_evidence, monkeypatch):
    e = input_evidence
    _, recipes, refs = old._registered(e.registration, e.clock["value"])
    catalog_folder = e.inputs / "catalog"
    catalog_folder.mkdir()
    catalog_file = catalog_folder / "catalog.json"
    save(catalog_file, {"synthetic_validated_catalog_fixture": True})
    loaded = {"reference": old._ref(catalog_file), "record": {"batches": ["source15"]},
        "batches": [{"batch_id": "source15", "event_day": "2026-10-15", "status": "completed_nonempty",
            "source_available_at": D + "T05:36:00+08:00", "recipes": recipes}],
        "references": [old._ref(catalog_file), *refs], "files": [catalog_file],
        "packages": [catalog_folder], "created_at": D + "T06:00:00+08:00"}
    def load_catalog(path, now=None):
        assert Path(path).resolve() == catalog_file.resolve()
        old._verify(loaded["reference"])
        return deepcopy(loaded)
    def prefix(current, initial):
        if current["reference"] != initial["reference"]:
            raise ValueError("Synthetic source prefix changed")
        return True
    catalog_adapter = SimpleNamespace(__file__=__file__, load_catalog=load_catalog, assert_catalog_prefix=prefix)
    monkeypatch.setattr(module, "_catalog_module", lambda: catalog_adapter)
    monkeypatch.setattr(module, "_now", lambda: e.clock["value"])
    registration_dir = e.inputs / "new_method"
    registration_dir.mkdir()
    registration = registration_dir / "registration.json"
    save(registration, {"schema": module.REGISTRATION_SCHEMA, "method_id": module.METHOD_ID,
        "registered_at": D + "T07:00:00+08:00", "rule_spec": module.RULE_SPEC,
        "arm_ids": list(module.ARMS), "implementation": old._ref(module.__file__),
        "catalog_implementation": old._ref(__file__), "original_registration": old._ref(e.registration),
        "calendar": old._ref(e.calendar), "initial_catalog": old._ref(catalog_file),
        "first_decision_day": D, "config": read(e.registration)["config"], **module.FLAGS})
    e.append_registration, e.catalog, e.loaded = registration, catalog_file, loaded
    e.out = e.root / "append_publication"
    return e


def build(e, *, close=None, audit=None, out=None):
    return module.build_dated_inputs(e.append_registration, e.catalog, e.calendar,
        e.closes[D] if close is None else close, e.audit if audit is None else audit,
        e.out if out is None else out, decision_day=D)


def test_exact_D_publication_roundtrip_preserves_old_files_and_does_not_read_E(evidence, monkeypatch):
    e = evidence
    before = {file: file.read_bytes() for file in (e.registration, e.snapshot / "result.json", e.closes[F] / "raw_daily.csv")}
    real = old.load_complete_close
    read_days = []
    def only_D(folder):
        assert Path(folder).resolve() == e.closes[D].resolve()
        read_days.append(D)
        return real(folder)
    monkeypatch.setattr(old, "load_complete_close", only_D)
    result = build(e)
    registered = module.load_registration(e.append_registration)
    loaded = module.load_publication(e.out / "publication.json", registered)
    assert result["publication"]["published_at"] == D + "T20:00:00+08:00"
    assert loaded["bundle"]["arms"] == result["arms"]
    assert loaded["reference"]["execution_day"] == F and read_days
    assert result["required_audit_symbols"] == [A, B]
    assert result["metrics"] is None and result["target_weights_generated"] is False
    assert not result["next_execution_raw_supplied"] and not result["account_created"]
    assert all(file.read_bytes() == data for file, data in before.items())


@pytest.mark.parametrize("missing", ["close", "audit"])
def test_missing_real_D_or_audit_waits_without_directory(evidence, missing):
    e = evidence
    result = build(e, **{missing: e.inputs / ("missing_" + missing)})
    assert result["status"].startswith("waiting_") and result["publication"] is None
    assert not e.out.exists() and not result["account_created"]


def test_cross_day_publication_rejected_without_directory(evidence):
    e = evidence
    e.clock["value"] = datetime.fromisoformat(F + "T20:00:00+08:00")
    with pytest.raises(ValueError, match="actually occur on D"):
        build(e)
    assert not e.out.exists()


def test_rehashed_false_candidate_claim_is_reconstructed_and_rejected(evidence):
    e = evidence
    build(e)
    bundle_path = e.out / "bundle.json"
    bundle = read(bundle_path)
    bundle["arms"][0]["ranked_candidates"][0]["score"] += 10
    save(bundle_path, bundle)
    pub_path = e.out / "publication.json"
    pub = read(pub_path)
    pub["bundle"] = old._ref(bundle_path)
    save(pub_path, pub)
    with pytest.raises(ValueError, match="schedule, ranked candidates"):
        module.load_publication(pub_path, module.load_registration(e.append_registration))


def test_indirect_source_package_cannot_contain_output(evidence):
    e = evidence
    out = e.comparison.parent / "bad_child"
    with pytest.raises(ValueError, match="input package"):
        build(e, out=out)
    assert not out.exists()


def test_registration_keeps_original_config_and_firstD_prospective(evidence):
    e = evidence
    record = read(e.append_registration)
    record["config"]["retain_rank"] = 5
    save(e.append_registration, record)
    with pytest.raises(ValueError, match="thresholds, costs"):
        module.load_registration(e.append_registration)


def test_real_catalog_adapter_binds_saved_snapshot_and_publishes_without_account(input_evidence, monkeypatch):
    from trading.research import seat_disclosure_catalog as catalog

    e = input_evidence
    monkeypatch.setattr(module, "_now", lambda: e.clock["value"])
    source = Path(read(e.snapshot / "result.json")["source_manifest"]["file"]).parent
    batch_dir = e.root / "actual_catalog_batch"
    catalog.build_batch(source, batch_dir, snapshot_result=e.snapshot / "result.json",
        now=datetime.fromisoformat(D + "T06:00:00+08:00"))
    catalog_folder = e.root / "real_catalog_package"
    catalog_folder.mkdir()
    catalog_file = catalog_folder / "catalog.json"
    catalog.append_catalog(batch_dir / "batch.json", catalog_file,
        now=datetime.fromisoformat(D + "T06:30:00+08:00"))
    method_dir = e.root / "input_method"
    method_dir.mkdir()
    registration = method_dir / "registration.json"
    save(registration, {"schema": module.REGISTRATION_SCHEMA, "method_id": module.METHOD_ID,
        "registered_at": D + "T07:00:00+08:00", "rule_spec": module.RULE_SPEC,
        "arm_ids": list(module.ARMS), "implementation": old._ref(module.__file__),
        "catalog_implementation": old._ref(catalog.__file__), "original_registration": old._ref(e.registration),
        "calendar": old._ref(e.calendar), "initial_catalog": old._ref(catalog_file),
        "first_decision_day": D, "config": read(e.registration)["config"], **module.FLAGS})
    out = e.root / "actual_append_publication"
    result = module.build_dated_inputs(registration, catalog_file, e.calendar, e.closes[D], e.audit, out,
        decision_day=D)
    checked = module.load_publication(out / "publication.json", module.load_registration(registration))
    assert checked["bundle"]["catalog"] == old._ref(catalog_file)
    assert [row["symbol"] for row in result["arms"][0]["ranked_candidates"]] == [B, A]
    assert result["arms"][1]["decision_kind"] == "NO_CHANGE"
    assert result["target_weights_generated"] is False and result["engine_run"] is False


@pytest.mark.parametrize("field,value", [("source_availability_cutoff", D + "T16:00:00+08:00"),
    ("next_execution_quality", "observed_and_admitted")])
def test_rehashed_false_cutoff_or_future_quality_declaration_is_rejected(evidence, field, value):
    e = evidence
    build(e)
    bundle_path = e.out / "bundle.json"
    bundle = read(bundle_path)
    bundle[field] = value
    save(bundle_path, bundle)
    pub_path = e.out / "publication.json"
    pub = read(pub_path)
    pub["bundle"] = old._ref(bundle_path)
    save(pub_path, pub)
    with pytest.raises(ValueError, match="schedule, ranked candidates"):
        module.load_publication(pub_path, module.load_registration(e.append_registration))
