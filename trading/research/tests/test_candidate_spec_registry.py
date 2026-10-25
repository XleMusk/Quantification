from copy import deepcopy

from trading.research.candidate_spec_registry import BLOCKED, VERIFIED, validate_registry


def _payloads():
    catalog = {
        "execution_admitted": False,
        "entries": [
            {"id": "candidate_a", "family": "fixed", "comparability_group": "snapshot_a"},
            {"id": "candidate_b", "family": "blocked", "comparability_group": "snapshot_b"},
        ],
    }
    base = {
        "data_snapshot": {"id": "snapshot_a", "source_bound_by_catalog": True},
        "primary_window": {"kind": "bounded", "start": "2026-01-01", "end": "2026-01-31", "sessions_expected": 20},
        "portfolio": {"initial_cash_cny": 140000, "holdings": 20, "retain_rank": 30,
                       "gross_limit": .8, "single_limit": .2, "lot_size_shares": 100},
        "cost_profile": {"commission": .0003, "minimum_commission_cny": 5,
                          "sell_tax": .0005, "transfer_fee": .00001,
                          "slippage_bps": 10, "participation": .01},
        "acceptance_gate": {"execution_admitted": False, "strategy_validated": False,
                             "pit_required_before_promotion": True, "fresh_holdout_required": True,
                             "source_hashes_required": True, "shared_engine_ledger_required": True,
                             "replay_allowed": True, "paper_allowed": True, "stop_reason": None},
        "promotion_rule": {"promotion_allowed": False, "decision": "diagnostic", "reason": "research only"},
        "execution_admitted": False, "live_channel_allowed": False,
    }
    spec_a = {"id": "candidate_a", "family": "fixed", "comparability_group": "snapshot_a", **base}
    spec_b = deepcopy(spec_a)
    spec_b.update(id="candidate_b", family="blocked", comparability_group="snapshot_b")
    spec_b["data_snapshot"] = {"id": "snapshot_b", "source_bound_by_catalog": True}
    spec_b["acceptance_gate"] = {**spec_b["acceptance_gate"], "replay_allowed": False,
                                  "paper_allowed": False, "stop_reason": "input contract blocked"}
    return catalog, {"schema": "quantification_candidate_strategy_spec_registry_v1",
                     "execution_admitted": False, "promotion_allowed": False,
                     "entries": [spec_a, spec_b]}


def test_registry_binds_every_catalog_id_and_keeps_replay_promotion_separate():
    result = validate_registry(*_payloads())
    assert result["status"] == VERIFIED
    assert result["catalog_entries"] == result["spec_entries"] == 2
    assert result["replay_allowed_count"] == 1
    assert result["paper_allowed_count"] == 1
    assert result["execution_admitted"] is False
    assert result["promotion_allowed"] is False


def test_registry_blocks_live_flags_and_orphan_specs():
    catalog, specs = _payloads()
    specs["entries"][0]["execution_admitted"] = True
    specs["entries"].append(deepcopy(specs["entries"][0]) | {"id": "orphan"})
    result = validate_registry(catalog, specs)
    assert result["status"] == BLOCKED
    assert "spec.candidate_a.live_channel" in result["issues"]
    assert "orphan_spec.orphan" in result["issues"]
