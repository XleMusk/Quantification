from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import operator_recovery_review_view as view


class _HttpError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path]:
    index = tmp_path / "paper_recovery_handoff_index.json"
    handoff = tmp_path / "paper_recovery_handoff_v1.json"
    handoff.write_text(json.dumps({
        "schema": "paper_recovery_handoff_v1",
        "strategy_id": "momentum_12_1_20",
        "rows": [{"scenario_id": "unknown_submission_reconcile_required",
                  "lifecycle_status": "blocked",
                  "recovery_action": "stop_and_reconcile_before_retry",
                  "required_observation": "account_orders_trades"}],
    }), encoding="utf-8")
    index.write_text(json.dumps({"rows": [{"strategy_id": "momentum_12_1_20",
                                           "handoff_json": str(handoff)}]}), encoding="utf-8")
    package = {"execution_day": "2026-10-12", "package_freshness": "fresh"}
    session = tmp_path / "session"
    namespace = {
        "HTTPException": _HttpError,
        "operator_package": lambda: package,
        "_package_artifact": lambda _package, name: index if name == "paper_recovery_handoff_index" else None,
        "_recovery_review_receipt_path": lambda _package: session,
        "_recovery_review_receipt_file": lambda _package, strategy, scenario: session / f"human_recovery_review_{strategy}_{scenario}_v1.json",
    }
    return namespace, session


def test_recovery_review_view_requires_confirmation_then_records_receipt(tmp_path: Path) -> None:
    namespace, session = _fixture(tmp_path)
    body = SimpleNamespace(strategy_id="momentum_12_1_20",
                           scenario_id="unknown_submission_reconcile_required",
                           decision="acknowledge_and_hold", reviewer="operator",
                           reviewed_at="2026-10-12T09:30:00+08:00",
                           notes="wait for observation", confirm_review=False)
    pending = view.submit_review(namespace, body)
    assert pending["status"] == "confirmation_required"
    body.confirm_review = True
    recorded = view.submit_review(namespace, body)
    assert recorded["status"] == "human_recovery_review_recorded"
    assert recorded["retry_permitted"] is False
    assert view.build_view(namespace)["status"] == "human_recovery_review_recorded"
    assert session.is_dir()


def test_recovery_review_view_rejects_unknown_scenario(tmp_path: Path) -> None:
    namespace, _ = _fixture(tmp_path)
    body = SimpleNamespace(strategy_id="momentum_12_1_20", scenario_id="unknown",
                           decision="defer", reviewer="operator",
                           reviewed_at="2026-10-12T09:30:00+08:00", notes="unknown",
                           confirm_review=True)
    with pytest.raises(_HttpError) as error:
        view.submit_review(namespace, body)
    assert error.value.status_code == 400
