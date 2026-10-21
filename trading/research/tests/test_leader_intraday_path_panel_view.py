"""Synthetic saved-summary reader cases; no source requests or forward-panel runs."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.research import leader_intraday_path_panel_view as m

NOW = datetime.fromisoformat("2026-10-15T14:35:00+08:00")


def summary(n, unknown=0, *, outcome=False):
    known = n - unknown
    return {"member_count": n, "unknown_count": unknown,
            ("paired_count" if outcome else "known_count"): known,
            ("up_count" if outcome else "advance_count"): known,
            ("down_count" if outcome else "decline_count"): 0, "flat_count": 0,
            ("conditional_median_window_return_percent" if outcome else "conditional_median_change_percent"): (0.1 if outcome else 1.5) if known else None}


def groups(*, outcome=False):
    def group(identity, name, n, unknown=0):
        result = {"id": identity, "name": name, "member_count": n, "summary": summary(n, unknown, outcome=outcome)}
        if outcome:
            result["conditional_median_difference_vs_rest_pct"] = 0.0 if n > unknown else None
        return result
    background = [group(identity, identity, n, unknown) for identity, n, unknown in zip(m.GROUP_IDS, (10, 4, 6, 8, 2, 7), (1, 1, 0, 1, 0, 1))]
    types = [group(identity, name, 2 if i < 2 else 0, 1 if i == 0 else 0) for i, (identity, name) in enumerate(m.ARCHETYPES)]
    return {"groups": background, "archetype_groups": types}


def save(tmp_path):
    folder = tmp_path / "research/leader_intraday_path_panel" / m.RUN
    folder.mkdir(parents=True)
    protocol = {"schema": m.PROTOCOL_SCHEMA, "registered_at": "2026-10-15T14:24:00+08:00",
                "data_root": str(tmp_path.resolve()), "job_id": "fixed-synthetic-job", "source_day": "2026-10-14",
                "source_available_at": "2026-10-15T09:50:00+08:00", "target_count": 10,
                "archetypes": [{"id": identity, "name": name} for identity, name in m.ARCHETYPES],
                "session": {"session_day": "2026-10-15"}, "rule": deepcopy(m.RULE), **m.FLAGS}
    features = {"summary": summary(10, 1), **groups(), "source_quote_clocks": {"current_at_anchor": {"count": 9, "minimum": "2026-10-15T14:24:00+08:00", "maximum": "2026-10-15T14:24:58+08:00", "span_seconds": 58}}}
    first = {"schema": m.ANCHOR_SCHEMA, "anchor_id": "round001", "created_at": "2026-10-15T14:25:00+08:00",
             "persisted_at": "2026-10-15T14:25:00.100000+08:00", "response_received_at": "2026-10-15T14:24:58+08:00",
             "source_file": "unopened-original-quotes.json", "source_sha256": "a" * 64, "target_count": 10,
             "features": features, "current_mask": ["not-exposed"], **m.FLAGS}
    second = {**deepcopy(first), "anchor_id": "round002", "created_at": "2026-10-15T14:32:00+08:00",
              "persisted_at": "2026-10-15T14:32:00.100000+08:00", "response_received_at": "2026-10-15T14:31:58+08:00"}
    outcome = {"schema": m.OUTCOME_SCHEMA, "anchor_id": first["anchor_id"], "derived_at": "2026-10-15T14:31:00+08:00",
               "status": "observed", "anchor_created_at": first["created_at"], "anchor_persisted_at": first["persisted_at"],
               "anchor_response_received_at": first["response_received_at"],
               "selected_successor_receipt": "unopened-successor-quotes.json", "successor_response_received_at": "2026-10-15T14:30:58+08:00",
               "response_span_seconds": 360, "prospective_claim": True, "summary": summary(10, 1, outcome=True),
               **groups(outcome=True), "future_outcome_is_prediction_accuracy": False, "members": ["not-exposed"], **m.FLAGS}
    report = {"schema": m.REPORT_SCHEMA, "status": "observing_prospective_research_panel", "registered_at": protocol["registered_at"],
              "derived_at": "2026-10-15T14:33:00+08:00", "job_id": protocol["job_id"], "source_day": protocol["source_day"],
              "target_count": 10, "protocol_file": str(folder / "protocol.json"), "anchor_count": 2, "outcome_count": 1,
              "pending_count": 1, "anchors": [second, first], "outcomes": [outcome],
              "pending": [{"anchor_id": second["anchor_id"], "status": "pending"}],
              "overlapping_windows_not_independent": True, "group_labels_overlap": True, "rule": deepcopy(m.RULE), **m.FLAGS}
    write(folder, protocol, report)
    return folder, protocol, report


def write(folder, protocol, report):
    raw = json.dumps(protocol).encode()
    (folder / "protocol.json").write_bytes(raw)
    report["protocol_sha256"] = sha256(raw).hexdigest()
    (folder / "report.json").write_text(json.dumps(report), encoding="utf8")


def test_missing_is_fixed_waiting_without_creating_or_discovering_other_run(tmp_path):
    old = tmp_path / "research/leader_intraday_path_panel/older/report.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"status":"observed"}')
    before = list(tmp_path.rglob("*"))
    value = m.leader_intraday_path_panel_view(tmp_path, now=NOW)
    assert value["status"] == "waiting_for_fixed_panel_registration"
    assert value["latest_anchor"] is value["latest_outcome"] is None
    assert value["panel_advanced"] is value["raw_quotes_loaded"] is value["execution_admitted"] is False
    assert before == list(tmp_path.rglob("*"))


def test_registered_missing_report_remains_explicitly_waiting(tmp_path):
    folder, _, _ = save(tmp_path)
    (folder / "report.json").unlink()
    assert m.leader_intraday_path_panel_view(tmp_path, now=NOW)["status"] == "registered_waiting_for_saved_report"


def test_latest_actual_anchor_and_latest_terminal_are_distinct_preserve_unknown_and_order(tmp_path):
    folder, _, _ = save(tmp_path)
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    value = m.leader_intraday_path_panel_view(tmp_path, now=NOW)
    assert (value["anchor_count"], value["outcome_count"], value["pending_count"]) == (2, 1, 1)
    assert value["latest_anchor"]["anchor_id"] == "round002"
    assert value["latest_outcome"]["anchor_id"] == "round001"
    assert value["latest_outcome"]["summary"]["unknown_count"] == 1
    assert [row["id"] for row in value["latest_outcome"]["archetype_groups"]] == [row[0] for row in m.ARCHETYPES]
    assert value["latest_outcome"]["archetype_groups"][2]["conditional_median_difference_vs_rest_pct"] is None
    assert "current_mask" not in value["latest_anchor"] and "members" not in value["latest_outcome"]
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before


@pytest.mark.parametrize("fault", ["protocol_flag", "report_flag", "future_report", "future_anchor", "naive_clock", "pending_loss", "wrong_order", "empty_median", "outcome_flag", "bad_span", "false_observed"])
def test_changed_boundaries_clocks_or_full_inventory_are_rejected(tmp_path, fault):
    folder, protocol, report = save(tmp_path)
    if fault == "protocol_flag": protocol["execution_admitted"] = True
    if fault == "report_flag": report["selection_enabled"] = True
    if fault == "future_report": report["derived_at"] = "2026-10-15T14:36:00+08:00"
    if fault == "future_anchor": report["anchors"][0]["persisted_at"] = "2026-10-15T14:36:00+08:00"
    if fault == "naive_clock": report["derived_at"] = "2026-10-15T14:33:00"
    if fault == "pending_loss": report.update(pending=[], pending_count=0)
    if fault == "wrong_order": report["outcomes"][0]["archetype_groups"].reverse()
    if fault == "empty_median": report["outcomes"][0]["archetype_groups"][2]["summary"]["conditional_median_window_return_percent"] = 0
    if fault == "outcome_flag": report["outcomes"][0]["economic_return_or_PnL"] = True
    if fault == "bad_span": report["outcomes"][0]["response_span_seconds"] = 359
    if fault == "false_observed": report["outcomes"][0]["status"] = "observed_unknown"
    write(folder, protocol, report)
    with pytest.raises(ValueError):
        m.leader_intraday_path_panel_view(tmp_path, now=NOW)


def test_optional_get_missing_and_bad_report409(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from trading import api
    monkeypatch.setattr(api.settings, "DATA_ROOT", tmp_path)
    assert api.leader_intraday_panel()["status"] == "waiting_for_fixed_panel_registration"
    folder, protocol, report = save(tmp_path)
    report["execution_admitted"] = True
    write(folder, protocol, report)
    with pytest.raises(HTTPException) as error:
        api.leader_intraday_panel()
    assert error.value.status_code == 409
