from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import operator_promotion_review_view as view


class _HttpError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path]:
    template = tmp_path / "strategy_promotion_review_v1.json"
    template.write_text(json.dumps({
        "schema": "strategy_promotion_review_record_v1",
        "status": "manual_promotion_review_template",
        "candidate_count": 1,
        "eligible_candidate_count": 0,
        "required_evidence": ["fresh_holdout_required"],
        "rows": [{"strategy_id": "candidate_one", "promotion_status": "blocked"}],
    }), encoding="utf-8")
    package = {"package_freshness": "current_or_future_execution_day", "execution_day": "2026-10-13"}
    session = tmp_path / "session"
    namespace = {
        "HTTPException": _HttpError,
        "operator_package": lambda: package,
        "_package_artifact": lambda _package, name: template if name == "strategy_promotion_review" else None,
        "_promotion_review_receipt_path": lambda _package: session / "human_promotion_review_receipt_v1.json",
    }
    return namespace, session


def test_promotion_review_view_requires_confirmation_then_records_receipt(tmp_path: Path) -> None:
    namespace, session = _fixture(tmp_path)
    body = SimpleNamespace(strategy_id="candidate_one", decision="accept_for_paper",
                           reviewer="operator", reviewed_at="2026-10-12T09:00:00+08:00",
                           notes="paper only", confirm_review=False)
    pending = view.submit_review(namespace, body)
    assert pending["status"] == "confirmation_required"
    assert not session.exists()

    body.confirm_review = True
    recorded = view.submit_review(namespace, body)
    assert recorded["status"] == "human_review_recorded"
    assert recorded["execution_admitted"] is False
    assert recorded["submission_enabled"] is False
    current = view.build_view(namespace)
    assert current["status"] == "human_review_recorded"
    assert current["receipt"]["strategy_id"] == "candidate_one"


def test_promotion_review_view_rejects_unknown_strategy(tmp_path: Path) -> None:
    namespace, _ = _fixture(tmp_path)
    body = SimpleNamespace(strategy_id="unknown", decision="defer", reviewer="operator",
                           reviewed_at="2026-10-12T09:00:00+08:00", notes="unknown",
                           confirm_review=True)
    with pytest.raises(_HttpError) as error:
        view.submit_review(namespace, body)
    assert error.value.status_code == 400
