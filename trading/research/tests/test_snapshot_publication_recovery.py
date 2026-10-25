import pytest

from trading.research.snapshot_publication_recovery import (
    IncompleteSnapshotError, build_receipt, load_snapshot, publish_snapshot,
    revision_impact, run_recovery_scenario,
)


def test_partial_snapshot_is_not_consumable(tmp_path):
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "data.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(IncompleteSnapshotError):
        load_snapshot(partial)


def test_publication_is_idempotent_and_rejects_changed_identity(tmp_path):
    root = tmp_path / "snapshots"
    first = publish_snapshot(root, "unit-1", [{"symbol": "000001.SZSE", "value": 1}],
                             received_at="2026-10-23T18:00:00+08:00", revision_id="r1")
    again = publish_snapshot(root, "unit-1", [{"symbol": "000001.SZSE", "value": 1}],
                             received_at="2026-10-23T18:00:00+08:00", revision_id="r1")
    assert first["publication"] == "CREATED"
    assert again["publication"] == "IDEMPOTENT_EXISTING"
    with pytest.raises(ValueError, match="identity changed"):
        publish_snapshot(root, "unit-1", [{"symbol": "000001.SZSE", "value": 2}],
                         received_at="2026-10-24T09:00:00+08:00", revision_id="r2")


def test_revision_impact_is_explicit():
    result = revision_impact([{"symbol": "000001.SZSE", "value": 1}],
                             [{"symbol": "000001.SZSE", "value": 2}])
    assert result["changed_rows"] == 1
    assert result["changed"][0]["kind"] == "REVISED"


def test_checkpoint_resume_acceptance():
    result = run_recovery_scenario()
    assert result["partial_snapshot_refused"] is True
    assert result["resumed_created_count"] == 1
    assert result["resumed_idempotent_count"] == 1
    receipt = build_receipt()
    assert receipt["status"] == "VERIFIED_LOCAL_SNAPSHOT_RECOVERY"
    assert all(receipt["acceptance"].values())
