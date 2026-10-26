import json

import pytest

from trading.research.shortline_adoption_gate import build_gate


def _write(tmp_path, *, execution_admitted=False, orders=1, fills=2):
    replay = {
        "schema": "shortline_baostock_candidate_replay_binding_v1",
        "status": "COMPLETED_RESEARCH_ONLY_BOUNDED_REPLAY",
        "execution_admitted": execution_admitted,
        "paper_allowed": False,
        "promotion_allowed": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "pit_verified": False,
        "corporate_actions_verified": False,
        "models": [{"model_kind": "lr", "replay_sessions": 7, "orders": orders, "fills": fills,
                    "net_return": -0.01, "max_drawdown": -0.02, "blocked_order_event_count": 0}],
    }
    handoff = {
        "schema": "native_shortline_candidate_handoff_v1",
        "execution_admitted": False,
        "candidate_panel_only": True,
        "models": [{"model_kind": "lr", "selected_count": 3}],
    }
    replay_path = tmp_path / "replay.json"
    handoff_path = tmp_path / "handoff.json"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    return replay_path, handoff_path


def test_bounded_replay_is_joined_without_promotion(tmp_path):
    replay, handoff = _write(tmp_path)
    result = build_gate(replay, handoff)
    assert result["status"] == "COMPLETED_RESEARCH_ONLY_SHORTLINE_ADOPTION_GATE"
    assert result["models"][0]["research_action"] == "CONTINUE_RESEARCH_ONLY"
    assert result["summary"]["orders"] == 1
    assert result["paper_allowed"] is False


def test_zero_fills_stays_blocked(tmp_path):
    replay, handoff = _write(tmp_path, orders=0, fills=0)
    result = build_gate(replay, handoff)
    assert result["models"][0]["research_action"] == "BLOCKED_EXECUTION_REFERENCE"
    assert result["summary"]["blocked"] == 1


def test_execution_boundary_is_required(tmp_path):
    replay, handoff = _write(tmp_path, execution_admitted=True)
    with pytest.raises(ValueError, match="execution boundary"):
        build_gate(replay, handoff)


def test_missing_handoff_model_is_rejected(tmp_path):
    replay, handoff = _write(tmp_path)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    payload["models"] = []
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="model missing"):
        build_gate(replay, handoff)
