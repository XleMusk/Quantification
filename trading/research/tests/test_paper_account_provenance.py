from trading.research.paper_account_provenance import VERIFIED, validate_payloads


def _payloads():
    head = {
        "execution_admitted": False, "last_completed_day": "2026-10-23",
        "registration": {"signal": "low_volatility_252_20"},
        "supplement": {"sha256": "raw"},
        "source": {"account": {"sha256": "account"}, "run": {"sha256": "run"},
                    "daily": {"sha256": "daily"}},
    }
    run = {"execution_admitted": False}
    replay = {"status": "completed"}
    account = {
        "day": "2026-10-23", "cash": 1000, "positions": {"000001.SZSE": []}, "targets": {"000001.SZSE": .4},
        "orders": {"o": {}}, "fills": {"f": {}}, "candidates": [],
        "events": [{"sequence": 1, "kind": "targets_generated"},
                   {"sequence": 2, "kind": "submitted"},
                   {"sequence": 3, "kind": "filled"}],
    }
    daily = [{"date": "2026-10-23"}]
    refs = {role: {"exists": True, "sha256": value} for role, value in {
        "raw_snapshot": "raw", "paper_account": "account", "source_run": "run", "daily": "daily"}.items()}
    return head, run, replay, account, daily, refs


def test_paper_checkpoint_proves_all_local_pipeline_stages_without_live_access():
    head, run, replay, account, daily, refs = _payloads()
    result = validate_payloads(head, run, replay, account, daily, references=refs)
    assert result["status"] == VERIFIED
    assert all(result["stages"].values())
    assert result["execution_admitted"] is False
    assert result["broker_or_ths_interacted"] is False


def test_paper_checkpoint_rejects_sequence_or_source_hash_drift():
    head, run, replay, account, daily, refs = _payloads()
    account["events"][2]["sequence"] = 2
    refs["raw_snapshot"]["sha256"] = "changed"
    result = validate_payloads(head, run, replay, account, daily, references=refs)
    assert result["status"] != VERIFIED
    assert "event_sequence_not_strict" in result["issues"]
    assert "hash_mismatch:raw_snapshot" in result["issues"]
