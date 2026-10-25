import json

import pytest

from trading.research.recovery_bundle import (backup_sqlite_database, load_recovery_bundle,
                                              publish_recovery_bundle, restore_recovery_bundle)


def test_recovery_bundle_restores_versioned_paper_inputs_into_empty_directory(tmp_path):
    source = tmp_path / "source"
    for relative, value in {
        "config/config.json": {"config_version": "c1", "initial_cash": 1000000},
        "strategy/strategy.json": {"strategy_id": "f01-v1", "code_version": "s1"},
        "inputs/snapshot.json": {"snapshot_id": "snap-1", "asof": "2026-10-23"},
        "account/ledger.json": {"cash": 1000000, "positions": {}},
        "daily/cursor.json": {"source_day": "2026-10-23", "generation": 2},
    }.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    entries = {
        "config": "config/config.json",
        "strategy": "strategy/strategy.json",
        "input_snapshot": "inputs/snapshot.json",
        "ledger": "account/ledger.json",
        "daily_cursor": "daily/cursor.json",
    }
    bundle = publish_recovery_bundle(source, entries, tmp_path / "bundle",
                                     versions={"schema": "recovery-v1", "strategy": "f01-v1"})
    manifest = load_recovery_bundle(bundle)["manifest"]
    assert manifest["execution_admitted"] is False
    assert sorted(manifest["files"]) == sorted(entries)
    destination = tmp_path / "restored"
    receipt = restore_recovery_bundle(bundle, destination)
    assert receipt["status"] == "RESTORED"
    assert receipt["versions"]["strategy"] == "f01-v1"
    for relative in entries.values():
        assert (destination / relative).read_text(encoding="utf-8") == (source / relative).read_text(encoding="utf-8")
    assert json.loads((destination / "restore_receipt.json").read_text(encoding="utf-8"))["execution_admitted"] is False


def test_recovery_bundle_rejects_tampering_and_nonempty_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "ledger.json").write_text("{}", encoding="utf-8")
    bundle = publish_recovery_bundle(source, {"ledger": "ledger.json"}, tmp_path / "bundle")
    (bundle / "files" / "ledger").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_recovery_bundle(bundle)

    clean_bundle = publish_recovery_bundle(source, {"ledger": "ledger.json"}, tmp_path / "bundle2")
    destination = tmp_path / "nonempty"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="empty"):
        restore_recovery_bundle(clean_bundle, destination)


def test_sqlite_backup_api_produces_verified_paper_backup(tmp_path):
    source = tmp_path / "state.sqlite3"
    import sqlite3
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE daily(day TEXT PRIMARY KEY, cursor INTEGER NOT NULL)")
        db.execute("INSERT INTO daily VALUES('2026-10-23', 2)")
    receipt = backup_sqlite_database(source, tmp_path / "backup.sqlite3")
    assert receipt["status"] == "BACKUP_VERIFIED"
    assert receipt["execution_admitted"] is False
    with sqlite3.connect(tmp_path / "backup.sqlite3") as db:
        assert db.execute("SELECT cursor FROM daily WHERE day='2026-10-23'").fetchone()[0] == 2
