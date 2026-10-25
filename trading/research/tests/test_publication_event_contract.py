import pytest

from trading.research.publication_event_contract import evaluate_publication_evidence, missing_publication_evidence


def _complete(**changes):
    row = {
        "publication_date": "2026-01-29",
        "retrieved_at": "2026-01-30T14:00:00+08:00",
        "asof_at": "2026-01-30T14:01:00+08:00",
        "revision_id": "rev-1",
        "source_file": "notice.pdf",
        "source_sha256": "a" * 64,
    }
    row.update(changes)
    return row


def test_complete_prior_date_receipt_is_pit_safe():
    result = evaluate_publication_evidence(_complete(), "2026-01-30")
    assert result["pit_asof_safe"] is True
    assert result["publication_evidence_reasons"] == []


def test_missing_sidecar_is_explicit_unknown():
    result = missing_publication_evidence()
    assert result["pit_asof_safe"] is False
    assert result["publication_evidence_reasons"] == ["publication_evidence_missing"]


@pytest.mark.parametrize("changes, reason", [
    ({"source_sha256": "bad"}, "source_file_or_sha256_missing"),
    ({"revision_id": ""}, "revision_id_missing"),
    ({"publication_date": "2026-01-30"}, "publication_not_proven_before_decision"),
])
def test_incomplete_receipt_never_becomes_pit_safe(changes, reason):
    result = evaluate_publication_evidence(_complete(**changes), "2026-01-30")
    assert result["pit_asof_safe"] is False
    assert reason in result["publication_evidence_reasons"]
