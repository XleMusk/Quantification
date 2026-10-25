import pytest

from trading.research.run_identity import attempt_key, canonical_identity, run_key


IDENTITY = {
    "study_id": "f01-v1", "code_version": "abc123", "snapshot_id": "snap-1",
    "calendar_id": "sse-2026-v1", "rules_id": "rules-v1", "benchmark_config_id": "bm-v1",
    "initial_account_ref": "research-account-1", "study_config": {"delay": 1, "threshold": "0.01"},
}


def test_run_key_is_stable_under_mapping_order_and_attempt_is_separate():
    reordered = {key: IDENTITY[key] for key in reversed(list(IDENTITY))}
    assert run_key(IDENTITY) == run_key(reordered)
    assert attempt_key(IDENTITY, 0) != attempt_key(IDENTITY, 1)
    assert attempt_key(IDENTITY, 0).startswith(run_key(IDENTITY) + ":")


def test_changed_code_or_inputs_create_a_new_run_key():
    changed = dict(IDENTITY, code_version="def456")
    assert run_key(changed) != run_key(IDENTITY)
    changed_config = dict(IDENTITY, study_config={"delay": 2, "threshold": "0.01"})
    assert run_key(changed_config) != run_key(IDENTITY)


@pytest.mark.parametrize("bad", ["study_id", "snapshot_id", "calendar_id"])
def test_identity_requires_all_stable_components(bad):
    incomplete = dict(IDENTITY)
    del incomplete[bad]
    with pytest.raises(ValueError, match="missing"):
        canonical_identity(incomplete)


def test_attempt_rejects_negative_or_boolean_index():
    with pytest.raises(ValueError):
        attempt_key(IDENTITY, -1)
    with pytest.raises(ValueError):
        attempt_key(IDENTITY, True)
