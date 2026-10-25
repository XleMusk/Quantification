import hashlib
import json
from pathlib import Path

from trading.research.paper_account_recovery import rehearse_paper_account_recovery


def _write_ref(path: Path, value: object) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _fixture(tmp_path: Path) -> Path:
    external = tmp_path / "external"
    run = tmp_path / "checkpoint"
    run.mkdir()
    run_ref = _write_ref(external / "run.json", {"run": "r1"})
    protocol_ref = _write_ref(external / "protocol.json", {"protocol": "p1"})
    account_ref = _write_ref(external / "account.json", {
        "cash": 4341307,
        "positions": {"600000.SSE": [{"qty": 100, "available": "2026-10-23"}]},
    })
    replay_ref = _write_ref(external / "replay.json", {"sequence": 148})
    daily_ref = _write_ref(external / "daily.csv", b"day,nav\n2026-10-23,13963807\n")
    supplement_ref = _write_ref(external / "raw.parquet", b"raw-input")
    snapshot_ref = _write_ref(external / "manifest.json", {"snapshot": "s1"})
    calendar_ref = _write_ref(external / "calendar.json", {"sessions": ["2026-10-23"]})
    head = {
        "schema": "stockdb_fixed_persistent_daily_research_v1",
        "method_id": "test_method",
        "generation": 2,
        "last_completed_day": "2026-10-23",
        "source": {"run": run_ref, "protocol": protocol_ref, "account": account_ref,
                    "replay": replay_ref, "daily": daily_ref},
        "supplement": supplement_ref,
        "candidate_panel_only": True,
        "research_only": True,
        "execution_admitted": False,
    }
    registration = {
        "schema": head["schema"],
        "method_id": head["method_id"],
        "snapshot_manifest": snapshot_ref,
        "initial_supplement": supplement_ref,
        "month_end_schedule": {"calendar_files": [calendar_ref]},
        "research_only": True,
        "execution_admitted": False,
    }
    (run / "head.json").write_text(json.dumps(head), encoding="utf-8")
    (run / "registration.json").write_text(json.dumps(registration), encoding="utf-8")
    return run


def test_rehearsal_restores_real_account_state_and_tracks_external_inputs(tmp_path):
    run = _fixture(tmp_path)
    receipt = rehearse_paper_account_recovery(run, tmp_path / "bundle", tmp_path / "restored")
    assert receipt["status"] == "RECOVERY_REHEARSAL_VERIFIED"
    assert receipt["execution_admitted"] is False
    assert receipt["cash_equal"] is True
    assert receipt["positions_equal"] is True
    assert receipt["source_cash"] == 4341307
    assert receipt["position_symbols"] == 1
    assert {row["label"] for row in receipt["external_dependencies"]} == {
        "head.supplement", "registration.initial_supplement"
    }
    restored = Path(receipt["restore_dir"])
    assert (restored / "account" / "account.json").is_file()
    assert json.loads((restored / "restore_receipt.json").read_text(encoding="utf-8"))["execution_admitted"] is False


def test_rehearsal_rejects_admitted_checkpoint(tmp_path):
    run = _fixture(tmp_path)
    head_path = run / "head.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["execution_admitted"] = True
    head_path.write_text(json.dumps(head), encoding="utf-8")
    try:
        rehearse_paper_account_recovery(run, tmp_path / "bundle", tmp_path / "restored")
    except ValueError as error:
        assert "unadmitted" in str(error)
    else:
        raise AssertionError("admitted checkpoint must be rejected")
