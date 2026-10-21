"""Source-history protocol and timing consumers; no historical-return runs."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.research import leader_forward_run as forward, leader_close_publication as publish
from trading.research import leader_recent_source_history as history_module
from trading.research.tests.test_leader_forward_run import bundle, FAMILIES
from trading.research.tests.test_listing_feature_integration import feature_protocol, qualify_bundle
from trading.research.tests.test_baostock_bulk_close import sources, import_package


HISTORY_DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]


def recent_protocol(tmp_path, monkeypatch, *, available="2026-10-15T11:00:00+08:00"):
    feature, _ = feature_protocol(tmp_path)
    old = forward._protocol(feature)[0]
    view_file = tmp_path / "view/recent_source_lineage.json"
    view_file.parent.mkdir()
    view_file.write_text('{"fixture_only": true}', encoding="utf-8")
    calendar_hash = sha256((tmp_path / "calendar.json").read_bytes()).hexdigest()
    def proof(day):
        return {"day": day, "source_folder": "fixture_" + day, "raw_daily_sha256": "raw_" + day,
            "roster_raw_sha256": "roster_" + day, "calendar_sha256": calendar_hash,
            "read_only_verified": True, "source_inventory_complete": True,
            "full_dated_roster_inventory_complete": True,
            "receipt_status": "complete_full_dated_close_inventory_not_admitted"}
    history = {"method_id": forward.RECENT_SOURCE_METHOD, "view_file": str(view_file.resolve()),
        "view_sha256": sha256(view_file.read_bytes()).hexdigest(), "shared_view_id": "fixture_view",
        "base_history_end": "2026-10-09", "available_at": available,
        "original_history_manifest_sha256": "native_fixture", "calendar_sha256": calendar_hash,
        "view_sessions": HISTORY_DAYS, "source_proofs": [proof(day) for day in HISTORY_DAYS]}
    # The helper has its own source-manifest tests. This fixture isolates the
    # wrapper, batch-binding and lifecycle consumers from raw loading.
    monkeypatch.setattr(history_module, "read_recent_source_view", lambda path: deepcopy(history))
    outer = tmp_path / "RECENT_SOURCE_SUPPLEMENT.json"
    payload = {"schema": "leader_next_window_recent_source_supplement_v1", "parent_protocol": feature.name,
        "created_at": "2026-10-15T11:15:00+08:00",
        "parent_protocol_sha256": forward._digest(json.loads(feature.read_text(encoding="utf-8"))),
        "method_id": forward.RECENT_SOURCE_METHOD,
        "source_history_view": "view/recent_source_lineage.json", "source_history_view_sha256": history["view_sha256"],
        **{key: False for key in ("fixed_alpha_thresholds_changed", "costs_changed", "window_length_changed",
                                  "holding_policy_changed", "execution_admitted", "selection_enabled", "strategy_validated")}}
    outer.write_text(json.dumps(payload), encoding="utf-8")
    binding = forward._protocol(outer)[0]
    def source(count=1, *, only14=False):
        result = qualify_bundle(bundle(count), binding)
        if only14:
            result["raw_bars"] = result["raw_bars"].loc[lambda frame: frame.date.le(HISTORY_DAYS[-1])].copy()
            result["source_days_complete"] = [HISTORY_DAYS[-1]]
            result["provider_inventory_by_day"] = {HISTORY_DAYS[-1]: result["provider_inventory_by_day"][HISTORY_DAYS[-1]]}
            for family in FAMILIES:
                result["batches"][family] = {HISTORY_DAYS[-1]: result["batches"][family][HISTORY_DAYS[-1]]}
        result["recent_source_history"] = deepcopy(history)
        proofs = {day: proof(day) for day in set(HISTORY_DAYS + result["source_days_complete"])}
        result["source_packages"] = [{"day": day, "folder": proofs[day]["source_folder"],
            "raw_daily_sha256": proofs[day]["raw_daily_sha256"],
            "terminal_completion_observed_at": day + "T16:00:00+08:00"} for day in result["source_days_complete"]]
        for batches in result["batches"].values():
            for day, batch in batches.items():
                used = sorted(value for value in proofs if value <= day)
                batch["recent_source_history"] = {**deepcopy(history), "source_days_used": used,
                                                    "source_proofs": [proofs[value] for value in used]}
        return result
    return outer, feature, old, binding, source


def test_outer_binds_raw_view_and_preserves_every_old_parent_field_and_cost(tmp_path, monkeypatch):
    outer, feature, old, binding, source = recent_protocol(tmp_path, monkeypatch)
    extra = {"history_source_supplement", "history_source_supplement_sha256", "history_source_view", "history_source_specified_at"}
    assert {key: value for key, value in binding.items() if key not in extra} == old
    assert binding["feature_supplement"] == str(feature.resolve())
    assert binding["history_source_supplement"] == str(outer.resolve())
    forward._check_source_binding(source(), binding)
    assert binding["config"] == old["config"] and binding["risk_exit_policy"] == old["risk_exit_policy"]


@pytest.mark.parametrize("changed", ["parent_hash", "cost_flag", "raw_view_bytes"])
def test_changed_outer_or_source_bytes_are_rejected(tmp_path, monkeypatch, changed):
    outer, _, _, binding, _ = recent_protocol(tmp_path, monkeypatch)
    value = json.loads(outer.read_text(encoding="utf-8"))
    if changed == "parent_hash":
        value["parent_protocol_sha256"] = "changed"
    elif changed == "cost_flag":
        value["costs_changed"] = True
    else:
        (tmp_path / "view/recent_source_lineage.json").write_text("changed", encoding="utf-8")
    outer.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        forward._protocol(outer)


@pytest.mark.parametrize("changed", ["new_under_old", "hidden_under_old", "old_under_new", "future_used", "mixed_prefix"])
def test_source_binding_mismatch_fails_before_run_or_publication_directory(tmp_path, monkeypatch, changed):
    outer, feature, _, binding, make_source = recent_protocol(tmp_path, monkeypatch)
    source, protocol = make_source(), outer
    if changed in {"new_under_old", "hidden_under_old"}:
        protocol = feature
        if changed == "hidden_under_old":
            source.pop("recent_source_history")
    elif changed == "old_under_new":
        source.pop("recent_source_history")
    else:
        first = next(iter(next(iter(source["batches"].values())).values()))["recent_source_history"]
        if changed == "future_used":
            first["source_days_used"].append("2026-10-16")
        else:
            first["source_proofs"][0]["source_folder"] = "another_source"
    for consumer, folder in ((forward.run_bundle, "no_head"), (publish.publish_close_targets, "no_publication")):
        with pytest.raises(ValueError, match="source-history|source history|historical prefix|Recent-source"):
            consumer(source, tmp_path / folder, protocol_path=protocol)
        assert not (tmp_path / folder).exists()
    assert not list(tmp_path.rglob("head.json"))


def test_history_unavailable_at_forward_decision_is_rejected_before_head(tmp_path, monkeypatch):
    outer, _, _, _, source = recent_protocol(tmp_path, monkeypatch, available="2026-10-15T16:00:00+08:00")
    with pytest.raises(ValueError, match="unavailable at the decision cutoff"):
        forward.run_bundle(source(), tmp_path / "no_late_forward", protocol_path=outer)
    assert not (tmp_path / "no_late_forward").exists()


def test_new_14_context_uses_actual_history_availability_without_rolling_execution_or_account(tmp_path, monkeypatch):
    outer, _, _, binding, make_source = recent_protocol(tmp_path, monkeypatch)
    monkeypatch.setattr(publish, "_now", lambda: datetime.fromisoformat("2026-10-15T11:30:00+08:00"))
    result = publish.publish_close_targets(make_source(only14=True), tmp_path / "late_context", protocol_path=outer)
    assert result["status"] == "late_context_only" and result["source_close_day"] == "2026-10-14"
    assert result["proposed_execution_day"] == "2026-10-15" and result["execution_day_rolled_forward"] is False
    assert result["history_input_available_at"] == "2026-10-15T11:00:00+08:00"
    assert result["source_ready_at"] == result["history_source_specified_at"] == "2026-10-15T11:15:00+08:00"
    assert result["recent_source_history"] == binding["history_source_view"]
    assert result["orders_generated"] is result["account_created"] is result["future_independent_window_advanced"] is False
    assert not list(tmp_path.rglob("head.json"))


def test_history_after_actual_publication_or_future_raw_is_rejected_without_output(tmp_path, monkeypatch):
    outer, _, _, _, make_source = recent_protocol(tmp_path, monkeypatch)
    monkeypatch.setattr(publish, "_now", lambda: datetime.fromisoformat("2026-10-15T10:59:59+08:00"))
    with pytest.raises(ValueError, match="precedes source completion"):
        publish.publish_close_targets(make_source(only14=True), tmp_path / "future_history", protocol_path=outer)
    assert not (tmp_path / "future_history").exists()
    monkeypatch.setattr(publish, "_now", lambda: datetime.fromisoformat("2026-10-15T11:30:00+08:00"))
    with pytest.raises(ValueError, match="only D and earlier"):
        publish.publish_close_targets(make_source(), tmp_path / "future_raw", protocol_path=outer, decision_day="2026-10-14")
    assert not (tmp_path / "future_raw").exists()


@pytest.mark.parametrize("consumer", ["forward", "publication"])
def test_clis_take_recent_source_only_from_the_explicit_protocol_binding(tmp_path, monkeypatch, consumer):
    from trading.research import provider_close_batches as provider
    outer, _, _, binding, _ = recent_protocol(tmp_path, monkeypatch)
    calls = []
    def build(packages, history, calendar, **kwargs):
        calls.append(kwargs)
        return {"fixture": True}
    monkeypatch.setattr(provider, "build_provider_close_bundle", build)
    monkeypatch.setattr(publish, "build_provider_close_bundle", build)
    monkeypatch.setattr(forward, "_pending_source_packages", lambda _: [])
    monkeypatch.setattr(publish, "_pending_source_packages", lambda _: [])
    monkeypatch.setattr(forward, "run_bundle", lambda *a, **k: {"cases": [], "execution_admitted": False})
    monkeypatch.setattr(publish, "publish_close_targets", lambda *a, **k: {"status": "fixture",
        "source_close_day": "2026-10-14", "proposed_execution_day": "2026-10-15", "case_count": 0,
        "execution_admitted": False})
    arguments = ["--package", str(tmp_path / "not_loaded"), "--history-snapshot", str(tmp_path / "history"),
                 "--protocol", str(outer)]
    if consumer == "forward":
        assert forward.main(arguments + ["--run-dir", str(tmp_path / "no_head")]) == 0
    else:
        assert publish.main(arguments + ["--out", str(tmp_path / "no_publication")]) == 0
    assert len(calls) == 1 and calls[0]["recent_source_view"] == binding["history_source_view"]["view_file"]
    assert calls[0]["ipo_qualification"] is not None
    assert not list(tmp_path.rglob("head.json"))


def test_status_only_strictly_verifies_typed_bulk_but_never_calls_it_partial(sources):
    import_package(sources)
    target = sources[-1]
    result = forward.source_status_only([target])
    assert result["status"] == "complete_source_closes_read_only_verified"
    assert result["source_packages"][0]["source_schema"] == "baostock_bulk_exact_dated_close_inventory_v1"
    assert result["research_checkpoint_created"] is result["provider_contacted"] is False
    receipt_path = target / "receipt.json"
    changed = json.loads(receipt_path.read_text(encoding="utf-8"))
    changed.update(status="running", full_dated_roster_inventory_complete=False)
    receipt_path.write_text(json.dumps(changed), encoding="utf-8")
    invalid = forward.source_status_only([target])
    assert invalid["status"] == "failed_source_close"
    assert invalid["source_packages"][0]["status"] == "failed_source_status_read"
    assert not list(target.rglob("head.json"))


def test_late_source_specification_cannot_backfill_an_earlier_forward_or_publication(tmp_path, monkeypatch):
    outer, _, _, _, make_source = recent_protocol(tmp_path, monkeypatch)
    payload = json.loads(outer.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-10-15T16:00:00+08:00"
    outer.write_text(json.dumps(payload), encoding="utf-8")
    class LaterRead(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-16T09:00:00+08:00")
    monkeypatch.setattr(forward, "datetime", LaterRead)
    with pytest.raises(ValueError, match="unavailable at the decision cutoff"):
        forward.run_bundle(make_source(), tmp_path / "late_rule_no_head", protocol_path=outer)
    monkeypatch.setattr(publish, "_now", lambda: datetime.fromisoformat("2026-10-15T15:59:59+08:00"))
    with pytest.raises(ValueError, match="precedes source completion"):
        publish.publish_close_targets(make_source(only14=True), tmp_path / "backdated_rule", protocol_path=outer)
    assert not (tmp_path / "late_rule_no_head").exists() and not (tmp_path / "backdated_rule").exists()
