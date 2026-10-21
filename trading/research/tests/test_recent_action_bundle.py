"""Builder-only boundaries with small pinned action evidence, no market run."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from trading.research.recent_action_bundle import build
from trading.research.tests.test_recent_action_extension import fixture_extension, pin, write, END


def execute(parts, out, **options):
    bundle = parts[3]
    return build(bundle["provider_root"], parts[0], parts[1], bundle["calendar"]["path"],
        out, day=END, **options)


def reseal(parts, rows):
    query = parts[3]["queries"][0]
    payload = Path(query["payload"]["path"])
    rows.to_csv(payload, index=False)
    receipt_file = Path(query["receipt"]["path"])
    import json
    receipt = json.loads(receipt_file.read_text())
    receipt["tasks"]["actions_2026"]["sha256"] = pin(payload)["sha256"]
    write(receipt_file, receipt)


def test_unfinished_provider_is_pending_source_without_a_usable_bundle(tmp_path):
    parts = fixture_extension(tmp_path / "inputs")
    root = Path(parts[3]["provider_root"])
    status = root / "status.json"
    import json
    value = json.loads(status.read_text())
    value["status"] = "partial"
    write(status, value)
    result = execute(parts, tmp_path / "pending_source")
    assert result["status"] == "pending_source" and result["bundle"] is None
    assert result["pending_source"]["provider_status"] == "partial"
    assert not (tmp_path / "pending_source" / "bundle.json").exists()


def test_relevant_event_with_unknown_ex_date_remains_pending_and_raw_blank_is_not_changed(tmp_path):
    parts = fixture_extension(tmp_path / "inputs")
    payload = Path(parts[3]["queries"][0]["payload"]["path"])
    rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
    rows.loc[1, "dividOperateDate"] = ""
    reseal(parts, rows)
    before = payload.read_bytes()
    result = execute(parts, tmp_path / "pending_ex")
    assert result["status"] == "pending_review" and result["bundle"] is None
    assert result["pending_review"][0]["reason"] == "relevant_provider_event_has_unknown_ex_date"
    assert result["pending_review"][0]["provider_row"]["dividOperateDate"] == ""
    assert payload.read_bytes() == before and not (tmp_path / "pending_ex" / "bundle.json").exists()


def test_missing_independent_reference_does_not_turn_an_official_action_into_a_usable_bundle(tmp_path):
    parts = fixture_extension(tmp_path / "inputs")
    result = execute(parts, tmp_path / "pending_reference", reviews=[{"action": parts[3]["actions"][0]}])
    assert result["status"] == "pending_review" and result["bundle"] is None
    assert result["pending_review"][0]["reason"] == "official_complete_terms_or_independent_reference_review_incomplete"
    assert not (tmp_path / "pending_reference" / "bundle.json").exists()
    assert result["targets_generated"] is result["engine_called"] is result["network_accessed"] is False


@pytest.mark.parametrize("fault", ["frozen_provider_terms", "review_conflicts_with_source"])
def test_old_economic_or_explicit_review_conflicts_are_saved_failures(tmp_path, fault):
    parts = fixture_extension(tmp_path / "inputs")
    action, reference = deepcopy(parts[3]["actions"][0]), deepcopy(parts[3]["reference_evidence"][0])
    if fault == "frozen_provider_terms":
        payload = Path(parts[3]["queries"][0]["payload"]["path"])
        rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
        rows.loc[0, "dividCashPsBeforeTax"] = "0.21"
        reseal(parts, rows)
    else:
        action["dividCashPsBeforeTax"] = reference["dividCashPsBeforeTax"] = "0.06"
        reference["reference_adjustment_cash_ps"] = "0.06"
    out = tmp_path / fault
    result = execute(parts, out, reviews=[{"action": action, "reference": reference}])
    assert result["status"] == "failed" and result["bundle"] is None
    expected = "frozen economic prefix" if fault == "frozen_provider_terms" else "conflict with original provider facts"
    assert expected in result["failures"][0]
    assert (out / "result.json").exists() and not (out / "bundle.json").exists()
    assert result["execution_admitted"] is result["returns_reportable"] is False


def test_explicit_reference_revision_cannot_silently_replace_a_prior_review(tmp_path):
    parts = fixture_extension(tmp_path / "inputs")
    action, reference = deepcopy(parts[3]["actions"][0]), deepcopy(parts[3]["reference_evidence"][0])
    reference["reference_adjustment_cash_ps"] = "0.06"
    result = execute(parts, tmp_path / "prior_reference_revision", prior_bundle=parts[2],
        reviews=[{"action": action, "reference": reference}])
    assert result["status"] == "failed" and result["bundle"] is None
    assert "revises an already saved economic item" in result["failures"][0]
