"""Synthetic sources, unchanged identity evaluator and actual shared Engine."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.contracts import IntentKind, RunContext
from trading.ledger import Ledger
from trading.research import leader_eight_type_paper as original
from trading.research import leader_eight_type_forward_run as shared
from trading.research import leader_known_identity_paper as m
from trading.research.tests.test_leader_eight_type_forward_run import evidence, CALENDAR, SYMBOLS


@pytest.fixture
def crossing_evidence(evidence, tmp_path, monkeypatch):
    e = evidence
    previews = {}
    for loaded in e["loaded"].values():
        references = []
        for day in loaded["source_days"]:
            preview = tmp_path / ("preview_" + day) / "result.json"
            preview.parent.mkdir(exist_ok=True)
            envelope = {"day": day, "created_at": day + "T18:00:00+08:00"}
            preview.write_text(json.dumps(envelope), encoding="utf-8")
            reference = {"preview": str(preview)}
            references.append(reference)
            panel = loaded["panel_by_day"][day]
            weak = day >= "2026-10-14"
            panel["environment"].update(breadth=-.5 if weak else .5, median_return_pct=-1. if weak else 1.)
            for item in panel["types"]:
                item.update(status="matched", unresolved_symbols=[], scope_complete=True)
            proof = next(p for p in loaded["provenance"]["source_proofs"] if p["day"] == day)
            proof.update(read_only_verified=True, source_inventory_complete=True,
                full_dated_roster_inventory_complete=True, receipt_status="complete_full_dated_close_inventory_not_admitted",
                roster_raw_sha256="synthetic-roster-" + day, source_completed_at=day + "T17:00:00+08:00")
            panel["source_proof"] = proof
            for symbol, feature in panel["risk_features"].items():
                feature.update(close=10., date=day, amount=100_000_000., high=10.1, low=9.9,
                    pct_chg=2., preclose_for_limits=10., stage=2, limit_up=True,
                    listing_qualification_status="supported")
            frame = pd.DataFrame([{"symbol": s, **f} for s, f in panel["risk_features"].items()])
            previews[str(preview.parent)] = (envelope, frame, reference)
        loaded["provenance"]["input_record"] = {"preview_sources": references,
            "source_days": list(loaded["source_days"])}
        loaded["raw"]["amount"] = "100000000"
    monkeypatch.setattr(m, "read_preview", lambda path, **kwargs: deepcopy(previews[str(path)]))
    monkeypatch.setattr(original, "_source_bindings", lambda value, observed: deepcopy(value["_bindings"]))
    industry = {"membership": {s: "one-fixed-industry" for s in SYMBOLS}, "source_file": "synthetic-industry",
        "source_sha256": "a" * 64, "snapshot_date": "2026-10-09", "posthoc_only": True,
        "revision_safe_PIT_verified": False, "theme_membership": False,
        "actual_available_at": "2026-10-09T19:00:00+08:00"}
    monkeypatch.setattr(m, "_industry_provider", lambda loaded, stamp: lambda day: deepcopy(industry))
    # The saved-opening reader consumes the same exact raw file as the
    # synthetic Engine source; persist it before original publication/anchor.
    for panel_file, loaded in e["loaded"].items():
        raw = tmp_path / (Path(panel_file).stem + "_raw_" + Path(panel_file).parent.name + ".parquet")
        pq.write_table(pa.Table.from_pandas(loaded["raw"], preserve_index=False), raw)
        Path(panel_file).write_text(json.dumps({"synthetic_only": True,
            "source_days": loaded["source_days"], "raw_file": str(raw),
            "raw_sha256": sha256(raw.read_bytes()).hexdigest()}), encoding="utf-8")
        loaded["_bindings"]["input_manifest_sha256"] = sha256(Path(panel_file).read_bytes()).hexdigest()
    for day, path in zip(("2026-10-15", "2026-10-16", "2026-10-19"), e["pubs"]):
        loaded = e["loaded"][str(e["panels"][day])]
        stamp = datetime.fromisoformat(day + "T18:45:00+08:00")
        d, execution, env, rows = original._prepare(loaded, stamp)
        record = original._finalize(d, execution, env, rows, loaded["_bindings"], stamp)
        path.write_text(json.dumps(record), encoding="utf-8")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-15T23:00:00+08:00"))
    registration = tmp_path / "crossing_registration"
    m.register(e["pubs"][0], calendar, "2026-10-16", registration)
    e.update(crossing_registration=registration / "registration.json", calendar=calendar,
        previews=previews, own_publications=[])
    return e


def publish_all(e, tmp_path, monkeypatch):
    for day, base in zip(("2026-10-16", "2026-10-19"), e["pubs"][1:]):
        monkeypatch.setattr(m, "_now", lambda day=day: datetime.fromisoformat(day + "T19:00:00+08:00"))
        folder = tmp_path / ("crossing_publication_" + day)
        value = m.publish(base, e["panels"][day], e["crossing_registration"], None, None, folder,
            previous_publication=e["own_publications"][-1] if e["own_publications"] else None)
        e["own_publications"].append(folder / "paper_plan.json")
    return value


def run_actual(e, tmp_path, monkeypatch):
    publish_all(e, tmp_path, monkeypatch)
    run = tmp_path / "crossing_books"
    result = m.forward(e["own_publications"], e["panels"]["2026-10-19"], None, None, run,
        registration_file=e["crossing_registration"])
    return run, result


def test_exact_episode_three_closes_four_shared_accounts_and_idempotent_recovery(crossing_evidence, tmp_path, monkeypatch):
    e = crossing_evidence
    originals = {str(p): p.read_bytes() for p in e["pubs"]}
    run, result = run_actual(e, tmp_path, monkeypatch)
    assert result["case_count"] == 4 and result["newly_committed_case_sessions"] == 8
    saved = json.loads((run / "head.json").read_text(encoding="utf-8"))
    assert list(saved["cases"]) == list(m.CASE_IDS)
    for key, case in saved["cases"].items():
        assert [r["date"] for r in case["daily"]] == ["2026-10-16", "2026-10-19"]
        assert len(case["journal"]) == 2
        assert case["state"]["replay_risk_exit_policy"]["policy_id"] == m.RISK_POLICY_ID + ":" + m.TYPE_ID
        if "__unguarded__" in key:
            assert case["state"]["fills"]
            book = Ledger(case["state"], m.shared._config(100000, 5), RunContext.from_dict(case["context"]))
            assert all(book.available(s, "2026-10-19") == 0 for s in SYMBOLS if book.quantity(s))
    record = json.loads(e["own_publications"][0].read_text(encoding="utf-8"))
    panels = m._verify(record["derived_panels"])
    candidate = next(t for t in panels["2026-10-16"]["types"] if t["id"] == m.TYPE_ID)["candidates"][0]
    assert candidate["evidence"]["episode_close_days"] == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert candidate["evidence"]["original_proxy_identity"]["identity_source_day"] == "2026-10-13"
    assert len(candidate["evidence"]["legs"]) == 2
    before = (run / "head.json").read_bytes()
    again = m.forward(e["own_publications"], e["panels"]["2026-10-19"], None, None, run,
        registration_file=e["crossing_registration"])
    assert again["newly_committed_case_sessions"] == 0 and (run / "head.json").read_bytes() == before
    assert all(p.read_bytes() == originals[str(p)] for p in e["pubs"])
    assert not e["run"].exists()


def test_missing_exact_feature_stays_unknown_and_no_successor_does_not_create_book(crossing_evidence, tmp_path, monkeypatch):
    e = crossing_evidence
    for envelope, frame, reference in e["previews"].values():
        frame.drop(columns=["preclose_for_limits"], inplace=True)
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-16T19:00:00+08:00"))
    folder = tmp_path / "unknown_publication"
    value = m.publish(e["pubs"][1], e["panels"]["2026-10-16"], e["crossing_registration"], None, None, folder)
    assert all(r["decision"] == "NO_CHANGE" for r in value["cases"] if r["guard"] == "unguarded")
    run = tmp_path / "no_successor_book"
    result = m.forward([folder / "paper_plan.json"], e["panels"]["2026-10-16"], None, None, run,
        registration_file=e["crossing_registration"])
    assert result["status"] == "waiting_for_exact_successor_complete_source_close" and not run.exists()


def test_owned_or_queued_unknown_nc_does_not_suppress_environment_exit(crossing_evidence, tmp_path, monkeypatch):
    e = crossing_evidence
    publish_all(e, tmp_path, monkeypatch)
    pub = m.load_publication(e["own_publications"][0], None, None,
        datetime.fromisoformat("2026-10-19T20:00:00+08:00"), {})
    row = next(t for t in pub["authoritative_panels"][pub["day"]]["types"] if t["id"] == m.TYPE_ID)
    row["unresolved_symbols"] = [SYMBOLS[0]]
    state = {"positions": {}, "pending_targets": None,
        "pending_published_close_intent": {"intent": {"weights": {SYMBOLS[0]: .16}}}}
    unguarded = next(key for key in m.CASE_IDS if "__unguarded__" in key)
    intent, evidence = m.prepare_intent(pub, unguarded, state)
    assert intent.kind == IntentKind.NO_CHANGE
    assert evidence["owned_or_pending_unknown_symbols"] == [SYMBOLS[0]]
    guarded = next(key for key in m.CASE_IDS if "__two_close_envguard__" in key)
    intent, evidence = m.prepare_intent(pub, guarded, state)
    assert intent.kind == IntentKind.FULL_TARGET and not intent.weights
    assert evidence["independent_environment_exit_not_suppressed"] is True


def test_tampered_identity_sidecar_and_late_or_skipped_publication_rejected(crossing_evidence, tmp_path, monkeypatch):
    e = crossing_evidence
    publish_all(e, tmp_path, monkeypatch)
    first = json.loads(e["own_publications"][0].read_text(encoding="utf-8"))
    state_file = Path(first["identity_state"]["file"])
    state_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence changed"):
        m.load_publication(e["own_publications"][0], None, None, datetime.fromisoformat("2026-10-19T20:00:00+08:00"), {})
    with pytest.raises(ValueError, match="First crossing publication"):
        m.publish(e["pubs"][2], e["panels"]["2026-10-19"], e["crossing_registration"], None, None, tmp_path / "skip")
    monkeypatch.setattr(m, "_now", lambda: datetime.fromisoformat("2026-10-19T08:00:00+08:00"))
    with pytest.raises(ValueError, match="Same-day"):
        m.publish(e["pubs"][1], e["panels"]["2026-10-16"], e["crossing_registration"], None, None, tmp_path / "late")
