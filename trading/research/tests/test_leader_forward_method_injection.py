"""Four-case injection reuses the real shared runner/Engine on synthetic evidence."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import leader_eight_type_forward_run as m
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_leader_eight_type_forward_run import (
    DAYS, RELAY, SYMBOLS, evidence,
)


@pytest.fixture
def method(evidence):
    keys = tuple(key for key in m.CASE_IDS if key.startswith("relay_continuation_leader__"))
    registration = evidence["run"].parent / "synthetic_four_case_registration.json"
    registration.write_text(json.dumps({"synthetic_only": True, "case_ids": keys}), encoding="utf-8")
    calls = []
    def load_publication(path, ipo, master, now, cache):
        pub = m._publication(path, ipo, master, now, cache)
        if pub.get("unbindable"):
            return pub
        pub["record"]["cases"] = [r for r in pub["record"]["cases"] if r["case_id"] in keys]
        pub["record"]["protocol"] = {"id": "synthetic_four_case_method_v1", "case_ids": list(keys)}
        pub["intents"] = {key: pub["intents"][key] for key in keys}
        own = deepcopy(pub["loaded"]["panel_by_day"][pub["day"]])
        own["types"] = [t for t in own["types"] if t["id"] == "relay_continuation_leader"]
        pub["authoritative_panels"] = {pub["day"]: own}
        return pub
    def registered(path, first, config, now):
        assert Path(path).resolve() == registration.resolve()
        assert first["day"] == "2026-10-15"
        return {"file": str(registration), "sha256": digest(registration),
                "registered_at": "2026-10-15T22:00:00+08:00",
                "registration_is_original_publication": False}
    def risk_policy(identity, *, state, **kwargs):
        calls.append({"day": max(kwargs["candidate_by_day"]), "state_before_open": deepcopy(state),
                      "candidates": deepcopy(kwargs["candidate_by_day"]),
                      "features": deepcopy(kwargs["feature_by_day"])})
        return m.PublishedCloseLeaderTypeRiskExitPolicy(identity, **kwargs)
    value = SimpleNamespace(CASE_IDS=keys, CHECKPOINT_SCHEMA="synthetic_four_case_checkpoint_v1",
        RESULT_SCHEMA="synthetic_four_case_result_v1", ACCOUNT_PREFIX="synthetic-four-case-forward:",
        TARGET_POLICY_ID=m.TARGET_POLICY_ID, RISK_POLICY_ID=m.RISK_POLICY_ID,
        load_publication=load_publication, registration=registered, risk_policy=risk_policy,
        target_policy=m.PublishedFixedExpiryReduction)
    return value, registration, calls


def execute(e, adapter, registration, *, publications=None, end="2026-10-16"):
    return m.run(e["pubs"][:1] if publications is None else publications,
        e["panels"][end], "synthetic_ipo", "synthetic_master", e["run"],
        registration=registration, method=adapter)


def read_head(e):
    return json.loads((e["run"] / "head.json").read_text(encoding="utf-8"))


def test_injected_empty_or_missing_successor_never_creates_accounts(evidence, method, monkeypatch):
    adapter, registration, _ = method
    def forbidden(*args, **kwargs):
        pytest.fail("Publication and successor checks must precede account/lock/Engine creation")
    monkeypatch.setattr(m, "new_state", forbidden)
    monkeypatch.setattr(m, "lock", forbidden)
    monkeypatch.setattr(m, "advance", forbidden)
    result = execute(evidence, adapter, registration, publications=[])
    assert result["schema"] == adapter.RESULT_SCHEMA
    assert result["status"] == "waiting_for_real_close_publication"
    assert not evidence["run"].exists()
    result = execute(evidence, adapter, registration, end="2026-10-15")
    assert result["schema"] == adapter.RESULT_SCHEMA
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert not evidence["run"].exists()
    with pytest.raises(ValueError, match="explicit registration"):
        execute(evidence, adapter, None, publications=[])
    assert not evidence["run"].exists()


def test_four_cases_keep_exact_DE_and_recover_original_books_after_head_failure(evidence, method, monkeypatch):
    adapter, registration, calls = method
    result = execute(evidence, adapter, registration)
    assert result["case_count"] == result["research_accounts_created"] == 4
    assert result["newly_committed_case_sessions"] == 4
    before = read_head(evidence)
    assert before["schema"] == adapter.CHECKPOINT_SCHEMA
    assert set(before["cases"]) == set(adapter.CASE_IDS)
    assert before["binding"]["risk_policy"] == {"id": adapter.RISK_POLICY_ID}
    assert before["binding"]["forward_method_registration"]["file"] == str(registration)
    for case in before["cases"].values():
        assert case["context"]["account_id"].startswith(adapter.ACCOUNT_PREFIX)
        assert [d["date"] for d in case["daily"]] == ["2026-10-15"]
        assert not case["state"]["fills"]
        assert case["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-16"
    saved_bytes = (evidence["run"] / "head.json").read_bytes()
    original_writer = m.write_json
    def interrupted(path, value):
        if Path(path).name == "head.json":
            raise OSError("synthetic four-case interruption after journals")
        return original_writer(path, value)
    monkeypatch.setattr(m, "write_json", interrupted)
    monkeypatch.setattr(m, "new_state", lambda *args: pytest.fail("Recovery cannot reset existing four accounts"))
    with pytest.raises(OSError, match="four-case interruption"):
        execute(evidence, adapter, registration, publications=evidence["pubs"][:2])
    assert (evidence["run"] / "head.json").read_bytes() == saved_bytes
    orphaned = {p: p.read_bytes() for p in (evidence["run"] / "journal").glob("*/2026-10-16_*.json")}
    assert len(orphaned) == 4
    monkeypatch.setattr(m, "write_json", original_writer)
    later = m._now() + timedelta(seconds=1)
    monkeypatch.setattr(m, "_now", lambda: later)
    result = execute(evidence, adapter, registration, publications=evidence["pubs"][:2])
    after = read_head(evidence)
    assert result["schema"] == adapter.RESULT_SCHEMA
    assert result["research_accounts_created"] == 0 and result["account_reset"] is False
    assert result["newly_committed_case_sessions"] == 4
    assert all(path.read_bytes() == original for path, original in orphaned.items())
    for key, case in after["cases"].items():
        assert case["context"] == before["cases"][key]["context"]
        assert case["daily"][:1] == before["cases"][key]["daily"]
        assert [d["date"] for d in case["daily"]] == ["2026-10-15", "2026-10-16"]
        assert {f["day"] for f in case["state"]["fills"].values()} == {"2026-10-16"}
        assert len(case["state"]["orders"]) == 5
        assert len(case["journal_files"]) == 2
    opening = [c for c in calls if c["day"] == "2026-10-16"]
    assert opening and all(c["state_before_open"]["day"] == "2026-10-15" for c in opening)
    assert all(c["state_before_open"]["pending_targets"] for c in opening)
    settled = (evidence["run"] / "head.json").read_bytes()
    assert execute(evidence, adapter, registration, publications=evidence["pubs"][:2])["newly_committed_case_sessions"] == 0
    assert (evidence["run"] / "head.json").read_bytes() == settled


def test_only_own_publication_projection_supplies_authoritative_risk_facts(evidence, method):
    adapter, registration, calls = method
    # A later catalog may project the old day differently. Its old projection
    # must not replace the first actual publication's frozen own-day facts.
    latest = evidence["loaded"][str(evidence["panels"]["2026-10-16"])]
    latest["panel_by_day"]["2026-10-15"]["risk_features"][SYMBOLS[0]]["sma20"] = 0.
    result = execute(evidence, adapter, registration, publications=evidence["pubs"][:2])
    assert result["newly_committed_case_sessions"] == 8
    for call in calls:
        expected = ["2026-10-15"] if call["day"] == "2026-10-15" else ["2026-10-15", "2026-10-16"]
        assert sorted(call["candidates"]) == expected
        assert sorted(call["features"]) == expected
        assert call["features"]["2026-10-15"][SYMBOLS[0]]["sma20"] == 9.


def test_injected_checkpoint_cannot_be_opened_by_default_method(evidence, method):
    adapter, registration, _ = method
    execute(evidence, adapter, registration)
    original = (evidence["run"] / "head.json").read_bytes()
    with pytest.raises(ValueError, match="binding"):
        m.run(evidence["pubs"][:1], evidence["panels"]["2026-10-16"],
              "synthetic_ipo", "synthetic_master", evidence["run"], registration=evidence["registration"])
    assert (evidence["run"] / "head.json").read_bytes() == original
