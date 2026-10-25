import json
from pathlib import Path

from trading.research.copied_account_replay_proof import prove_copied_account_replay


def _fixture(tmp_path: Path) -> Path:
    external = tmp_path / "external"
    run = tmp_path / "checkpoint"
    run.mkdir()
    def write(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        import hashlib
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    run_ref = write(external / "run.json", {"execution_admitted": False})
    protocol_ref = write(external / "protocol.json", {"execution_admitted": False})
    account = {"cash": 1000, "positions": {"600000.SSE": {"qty": 100}},
               "orders": {"o1": {"qty": 100}}, "fills": {"f1": {"qty": 100}},
               "events": [{"sequence": 1, "kind": "targets_generated"}, {"sequence": 2, "kind": "submitted"}, {"sequence": 3, "kind": "filled"}], "sequence": 3, "day": "2026-10-23",
               "targets": {"600000.SSE": 100}, "candidates": {"600000.SSE": 1}}
    account_ref = write(external / "account.json", account)
    replay_ref = write(external / "replay.json", {"status": "completed"})
    daily_ref = write(external / "daily.csv", b"date,nav\n2026-10-23,1000\n")
    raw_ref = write(external / "raw.parquet", b"raw")
    head = {"schema": "stockdb_fixed_persistent_daily_research_v1", "last_completed_day": "2026-10-23",
            "source": {"run": run_ref, "protocol": protocol_ref, "account": account_ref, "replay": replay_ref, "daily": daily_ref},
            "supplement": raw_ref, "registration": {"signal": "test"}, "research_only": True, "execution_admitted": False}
    registration = {"schema": head["schema"], "research_only": True, "execution_admitted": False,
                    "snapshot_manifest": write(external / "manifest.json", {"snapshot": "s"})}
    (run / "head.json").write_text(json.dumps(head), encoding="utf-8")
    (run / "registration.json").write_text(json.dumps(registration), encoding="utf-8")
    return run


def test_copied_account_preserves_event_and_cash_state(tmp_path):
    result = prove_copied_account_replay(_fixture(tmp_path), tmp_path / "proof")
    assert result["status"] == "VERIFIED_LOCAL_COPIED_ACCOUNT_REPLAY"
    assert result["source_state"]["event_count"] == 3
    assert all(result["compatibility"].values())
    assert result["execution_admitted"] is False


def test_proof_does_not_mutate_source_account(tmp_path):
    run = _fixture(tmp_path)
    before = (run / "head.json").read_bytes()
    prove_copied_account_replay(run, tmp_path / "proof")
    assert (run / "head.json").read_bytes() == before
