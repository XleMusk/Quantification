"""Protocol/source contracts only; no market-return experiment."""
from copy import deepcopy
import json

import pytest

from trading.research import leader_forward_run as forward
from trading.research import leader_close_publication as publish
from trading.research import provider_close_batches as provider
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_daily_signals import _calendar_view
from trading.research.tests.test_leader_forward_run import protocol, bundle
from trading.research.tests.test_leader_close_publication import close_bundle
from trading.research.tests.test_provider_close_batches import assets, A, HALT, DAY


def feature_protocol(tmp_path):
    parent = protocol(tmp_path)
    payload = {"schema": "leader_next_window_listing_feature_supplement_v1",
        "parent_protocol": parent.name,
        "parent_protocol_sha256": forward._digest(json.loads(parent.read_text(encoding="utf-8"))),
        "method_id": forward.IPO_FEATURE_METHOD,
        "decision_cutoff_policy": "each_decision_own_15:05_local_time_for_current_and_historical_qualification",
        **{key: False for key in ("fixed_alpha_thresholds_changed", "costs_changed", "window_length_changed",
            "holding_policy_changed", "execution_admitted", "selection_enabled", "strategy_validated")}}
    path = tmp_path / "FEATURE_SUPPLEMENT.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, parent


def qualify_bundle(source, binding):
    source = deepcopy(source)
    source["feature_qualification"] = {"method_id": forward.IPO_FEATURE_METHOD,
        "verification": deepcopy(binding["ipo_qualification"]),
        "decision_cutoff_policy": "each_decision_own_15:05_local_time_for_current_and_historical_qualification"}
    for batches in source["batches"].values():
        for batch in batches.values():
            batch["feature_qualification"] = {"method_id": forward.IPO_FEATURE_METHOD,
                "unknown_symbols": [], "known_excluded_symbols": [], "qualification_complete": True,
                "source_coverage_status_before_qualification": batch["status"]}
    return source


def test_explicit_feature_wrapper_pins_same_costs_and_parent_and_rejects_unqualified_source(tmp_path):
    path, parent = feature_protocol(tmp_path)
    old, _, old_config = forward._protocol(parent)
    bound, _, config = forward._protocol(path)
    assert bound["feature_qualification_method_id"] == forward.IPO_FEATURE_METHOD
    assert bound["ipo_supplement"] == str(parent.resolve())
    assert config.to_dict() == old_config.to_dict() and bound["risk_exit_policy"] == old["risk_exit_policy"]
    with pytest.raises(ValueError, match="method or qualification proof"):
        forward.run_bundle(bundle(2), tmp_path / "not_created", protocol_path=path)
    assert not (tmp_path / "not_created").exists()
    changed = json.loads(parent.read_text(encoding="utf-8"))
    changed["unexpected_change"] = True
    parent.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="unchanged explicit IPO parent"):
        forward._protocol(path)


def test_qualified_features_cannot_enter_old_protocol_or_hide_unknown_membership(tmp_path):
    path, parent = feature_protocol(tmp_path)
    binding = forward._protocol(path)[0]
    source = qualify_bundle(bundle(2), binding)
    with pytest.raises(ValueError, match="explicit feature supplement"):
        forward.run_bundle(source, tmp_path / "old_account", protocol_path=parent)
    assert not (tmp_path / "old_account").exists()
    first = next(iter(next(iter(source["batches"].values())).values()))
    first["feature_qualification"]["unknown_symbols"] = [A]
    with pytest.raises(ValueError, match="lost unknown membership"):
        forward.run_bundle(source, tmp_path / "wrongly_complete", protocol_path=path)
    assert not (tmp_path / "wrongly_complete").exists()


def test_close_publication_rejects_feature_mismatch_before_creating_targets(tmp_path):
    path, _ = feature_protocol(tmp_path)
    with pytest.raises(ValueError, match="method or qualification proof"):
        publish.publish_close_targets(close_bundle(), tmp_path / "no_targets", protocol_path=path)
    assert not (tmp_path / "no_targets").exists()


def proof(calendar_path, *, record=None):
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    sessions = _calendar_view(payload, "2026-01-01", "2026-12-31", [])[0]
    return IpoQualification({A: record} if record else {}, tuple(sessions),
        {"synthetic": True, "pit_verified": False, "execution_admitted": False})


def test_provider_first_five_exclusion_keeps_same_inventory_and_default_candidates(assets):
    calendar, _, _, package = assets
    source = package()
    old = provider.build_provider_close_bundle(source, "unused", calendar)
    new = provider.build_provider_close_bundle(source, "unused", calendar, ipo_qualification=proof(calendar,
        record={"ipo_date": "2026-10-13", "available_at": "2026-10-08T08:00:00+08:00",
                "board": "main", "source_basis": "synthetic_known_date_guard_only"}))
    assert old["batches"]["trend_leader_20_60"][DAY]["candidates"][0]["symbol"] == A
    assert new["batches"]["trend_leader_20_60"][DAY]["candidates"] == []
    assert new["provider_inventory_by_day"] == old["provider_inventory_by_day"]
    assert A in new["batches"]["trend_leader_20_60"][DAY]["feature_qualification"]["known_excluded_symbols"]
    assert "feature_qualification" not in old


def test_fixed_cohort_scopes_unknown_qualification_without_making_unknown_cash(assets):
    calendar, _, _, package = assets
    source = provider.build_provider_close_bundle(package(), "unused", calendar, ipo_qualification=proof(calendar))
    batch = source["batches"]["trend_leader_20_60"][DAY]
    assert batch["status"] == "incomplete" and A in batch["feature_qualification"]["unknown_symbols"]
    held, _ = provider.scope_provider_batches(source, [A])
    assert held["trend_leader_20_60"][DAY]["status"] == "incomplete"
    assert held["trend_leader_20_60"][DAY]["incomplete_reason"] == "fixed_cohort_listing_feature_qualification_unknown"
    unrelated, _ = provider.scope_provider_batches(source, [HALT])
    assert unrelated["trend_leader_20_60"][DAY]["status"] == "complete"
    assert unrelated["trend_leader_20_60"][DAY]["feature_qualification"]["unknown_symbols"] == []
    assert unrelated["trend_leader_20_60"][DAY]["candidates"] == []
