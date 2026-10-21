from copy import deepcopy
from datetime import datetime
import json

import pytest

from trading.research import leader_eight_type_residual_paper as m
from trading.research.tests.test_leader_eight_type_forward_run import evidence, CALENDAR


def prepare(e, tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-15T23:00:00+08:00"))
    directory = tmp_path / "residual_registration"
    m.register(e["pubs"][0], calendar, "2026-10-16", directory)
    return directory / "registration.json"


def test_same_candidates_new_publication_and_shared_ledger_recovery(evidence, tmp_path, monkeypatch):
    e = evidence
    reg = prepare(e, tmp_path, monkeypatch)
    original_bytes = {str(p): p.read_bytes() for p in e["pubs"]}
    publications = []
    for day, base in zip(("2026-10-16", "2026-10-19"), e["pubs"][1:]):
        monkeypatch.setattr(m, "_now", lambda day=day: datetime.fromisoformat(day + "T19:00:00+08:00"))
        folder = tmp_path / ("new_publication_" + day)
        value = m.publish(base, e["panels"][day], reg, folder)
        assert value["candidates_recomputed"] is False
        publications.append(folder / "paper_plan.json")
    run = tmp_path / "new_books"
    first = m.forward(publications[:1], e["panels"]["2026-10-19"], None, None, run, registration_file=reg)
    assert first["newly_committed_case_sessions"] == 32
    head = json.loads((run / "head.json").read_text(encoding="utf-8"))
    assert all(case["state"]["day"] == "2026-10-16" for case in head["cases"].values())
    second = m.forward(publications, e["panels"]["2026-10-19"], None, None, run, registration_file=reg)
    final = json.loads((run / "head.json").read_text(encoding="utf-8"))
    assert second["newly_committed_case_sessions"] == 32
    for key, case in final["cases"].items():
        assert case["daily"][:1] == head["cases"][key]["daily"]
        assert case["journal"][:1] == head["cases"][key]["journal"]
        assert case["state"]["replay_risk_exit_policy"]["policy_id"].startswith(m.RISK_POLICY_ID)
    assert all(p.read_bytes() == original_bytes[str(p)] for p in e["pubs"])
    assert not e["run"].exists()


def test_no_next_source_no_account_and_late_publication_rejected(evidence, tmp_path, monkeypatch):
    e = evidence
    reg = prepare(e, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-16T19:00:00+08:00"))
    out = tmp_path / "own_publication"
    m.publish(e["pubs"][1], e["panels"]["2026-10-16"], reg, out)
    run = tmp_path / "waiting_books"
    result = m.forward([out / "paper_plan.json"], e["panels"]["2026-10-16"], None, None, run, registration_file=reg)
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert not run.exists()
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-19T08:00:00+08:00"))
    with pytest.raises(ValueError, match="same-day"):
        m.publish(e["pubs"][1], e["panels"]["2026-10-16"], reg, tmp_path / "late")
    assert not (tmp_path / "late").exists()


def test_registration_cannot_roll_first_day_or_publish_before_method(evidence, tmp_path, monkeypatch):
    e = evidence
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-19T08:00:00+08:00"))
    with pytest.raises(ValueError, match="Register before"):
        m.register(e["pubs"][0], calendar, "2026-10-16", tmp_path / "late_reg")
    with pytest.raises(ValueError, match="Register before"):
        m.register(e["pubs"][0], calendar, "2026-10-19", tmp_path / "rolled_reg")


def test_registered_history_facts_cannot_change_under_same_raw_source(evidence):
    anchor = evidence["loaded"][str(evidence["panels"]["2026-10-15"])]
    latest = evidence["loaded"][str(evidence["panels"]["2026-10-19"])]
    m._anchor_prefix(anchor, latest)
    altered = deepcopy(latest)
    altered["panel_by_day"]["2026-10-15"]["risk_features"]["000001.SZSE"]["sma20"] = 90.
    with pytest.raises(ValueError, match="candidate/risk/identity prefix"):
        m._anchor_prefix(anchor, altered)
