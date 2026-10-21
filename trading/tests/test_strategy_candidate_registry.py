import json
from pathlib import Path


REGISTRY = Path(r"D:\Quantification\trading\research\recent_candidates.json")
REQUIRED = {
    "strategy_id", "source", "market", "universe", "signal",
    "rebalance_frequency", "entry_rule", "exit_rule", "position_rule",
    "required_data", "original_period", "A_share_reproduction_status",
    "result", "known_gaps", "next_test",
}


def test_strategy_candidate_registry_has_complete_typed_records():
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    candidates = payload["candidates"]
    assert candidates
    ids = [row["strategy_id"] for row in candidates]
    assert len(ids) == len(set(ids))
    for row in candidates:
        if row["strategy_id"] == "emotion_core_replenishment_v2":
            assert REQUIRED <= set(row)
            assert isinstance(row["source"], list) and row["source"]
            assert isinstance(row["required_data"], list)
            assert isinstance(row["known_gaps"], list)
            assert isinstance(row["next_test"], str) and row["next_test"]
        else:
            # Older records predate the normalized fields; keep them readable
            # without silently claiming that they have the new schema.
            assert {"strategy_id", "source", "A_share_reproduction_status", "result", "next_test"} <= set(row)
