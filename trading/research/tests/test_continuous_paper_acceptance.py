import json
from trading.research.continuous_paper_acceptance import run_acceptance


def test_two_session_paper_continuity_carries_partial_fill_and_is_idempotent(tmp_path):
    receipt = run_acceptance(tmp_path)
    assert receipt["status"] == "VERIFIED_LOCAL_RESEARCH_ONLY"
    assert receipt["execution_admitted"] is False
    assert receipt["sessions"] == ["2026-10-27", "2026-10-28"]
    assert all(receipt["checks"].values())
    assert json.loads((tmp_path / "day_one_state.json").read_text())["target_day"] == "2026-10-27"
    assert json.loads((tmp_path / "day_two_state.json").read_text())["target_day"] == "2026-10-28"
