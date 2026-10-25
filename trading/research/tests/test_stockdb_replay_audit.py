from trading.research.stockdb_replay_audit import BLOCKED, VERIFIED


def test_status_constants_keep_historical_replay_research_only():
    assert VERIFIED == "VERIFIED_STOCKDB_HISTORICAL_REPLAY"
    assert BLOCKED == "STOCKDB_HISTORICAL_REPLAY_AUDIT_BLOCKED"
