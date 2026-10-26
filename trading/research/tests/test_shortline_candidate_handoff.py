from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from trading.research import shortline_candidate_handoff as handoff


SOURCE_DAY = "2026-10-15"
EXECUTION_DAY = "2026-10-16"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _loaded(plan_dir: Path, *, mismatch: bool = False):
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "plan.json"
    plan.write_text(json.dumps({"policy_id": "qualified", "execution_admitted": False}), encoding="utf-8")
    decisions = {}
    for model in handoff.MODEL_KINDS:
        selected_id = f"{model}-e1"
        rows = [
            {"event_id": selected_id, "symbol": "000001.SZSE", "date": SOURCE_DAY,
             "stage": "1", "model_hit5_probability": "0.8", "paper_candidate_only": "False",
             "candidate_selected": "True"},
            {"event_id": f"{model}-e2", "symbol": "000002.SZSE", "date": SOURCE_DAY,
             "stage": "2", "model_hit5_probability": "0.2", "paper_candidate_only": "False",
             "candidate_selected": "False"},
        ]
        with (plan_dir / f"{model}_ranked_preview.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        decisions[model] = {
            "selected_count": 1,
            "selected_events": [{"event_id": selected_id}],
        }
        if mismatch and model == "lr":
            decisions[model]["selected_events"] = [{"event_id": f"{model}-e2"}]
    return {
        "record": {"policy_id": "qualified", "execution_admitted": False, "paper_allowed": False},
        "source_day": SOURCE_DAY,
        "execution_day": EXECUTION_DAY,
        "decisions": decisions,
    }, plan


def test_selected_plan_rows_are_copied_with_new_candidate_flags(monkeypatch, tmp_path: Path):
    loaded, plan = _loaded(tmp_path / "plan")
    monkeypatch.setattr(handoff, "load_preopen_plan", lambda path: loaded)
    out = tmp_path / "handoff"
    receipt = handoff.build_candidate_handoff(plan, out)

    assert receipt["status"] == handoff.STATUS
    assert receipt["execution_admitted"] is False
    assert [row["selected_count"] for row in receipt["models"]] == [1, 1]
    with (out / "lr_candidates.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["paper_candidate_only"] == "True"
    assert rows[0]["candidate_selected"] == "True"
    assert rows[0]["event_id"] == "lr-e1"
    assert (out / "receipt.json").is_file()


def test_selected_identity_mismatch_is_rejected_without_output(monkeypatch, tmp_path: Path):
    loaded, plan = _loaded(tmp_path / "plan", mismatch=True)
    monkeypatch.setattr(handoff, "load_preopen_plan", lambda path: loaded)
    out = tmp_path / "handoff"
    with pytest.raises(ValueError, match="selected identities differ"):
        handoff.build_candidate_handoff(plan, out)
    assert not out.exists()

