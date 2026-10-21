"""New saved-reader paths only; tiny original Engine fixture, no market I/O."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import registered_research_saved_opening as m
from trading.research.tests.test_leader_entry_comparison_forward import (
    evidence, real_publications, consume, read)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return {"file": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def seat_files(tmp_path, kind, *, publish=True):
    from trading.config import StrategyConfig
    from trading.research import seat_hypothesis_dated_inputs as inputs
    core, reg_schema, pub_schema, arms, method = m._definition(kind)
    root = tmp_path / kind
    calendar = save(root / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    original = save(root / "original.json", {"saved_fixture": True})
    dated = save(root / "dated.json", {"saved_fixture": True})
    first = "2026-10-16" if kind == "public_seat_static" else "2026-10-19"
    reg = {"schema": reg_schema, "method_id": method, "registered_at": "2026-10-16T10:00:00+08:00",
        "arm_ids": arms, "config": StrategyConfig(holdings=5).to_dict(), "calendar": calendar,
        "original_registration": original, "dated_input_method_registration": dated,
        "first_decision_day": first, **m.FLAGS}
    reg["implementation"] = {"file": str(Path(core.__file__).resolve()),
        "sha256": sha256(Path(core.__file__).read_bytes()).hexdigest()}
    regref = save(root / "registration.json", reg)
    paths = []
    if publish:
        e = "2026-10-19" if first == "2026-10-16" else "2026-10-20"
        bundle = save(root / "input" / "bundle.json", {"decision_day": first, "next_execution_day": e,
            "calendar": calendar, "config": reg["config"], "arms": [
                {"arm_id": arm, "decision_kind": "RANKED_CANDIDATES" if i % 2 == 0 else "NO_CHANGE",
                 "weights": None, "reason": "original_saved_fixture", "ranked_candidates":
                 [{"symbol": "600001.SSE", "score": 1}] if i % 2 == 0 else []}
                for i,arm in enumerate(arms)]})
        path = root / "publication" / "publication.json"
        save(path, {"schema": pub_schema, "published_at": first+"T18:30:00+08:00",
            "decision_day": first, "execution_day": e, "registration":
                original if kind == "public_seat_static" else dated,
            "bundle": bundle, **m.FLAGS})
        paths = [path]
    return {"registration": Path(regref["file"]), "publications": paths,
        "run": root / "account", "source": tmp_path / (kind+"-source"), "arms": arms}


def test_no_head_preserves_15_unbound_rules_four_seat_decisions_and_no_future_source(real_publications, tmp_path):
    e = real_publications
    pub, _ = e["make"]("2026-10-16")
    now = e["clock"]["value"]
    run, source = tmp_path / "entry-account", tmp_path / "entry-source"
    m.build_source("entry_comparison", e["registration"], [pub], run, source, current=now)
    rows = m.export_requests(source, current=now)
    assert len(rows) == 15 and not run.exists()
    assert all(r["status"] == "not_ready" and r["weights"] is None and r["cash_cents"] is None
        and r["published_input"]["decision_kind"] == "UNBOUND_FAMILY_RULE"
        and r["saved_decision"]["selected_decision"] is None for r in rows.values())
    _, product = m.build_opening(source, tmp_path.parent/(tmp_path.name+"-entry-opening"), current=now)
    assert product["account_count"] == 0 and product["preview_count"] == 0
    assert product["opening_pack"] is None and product["candidate_order_count"] is None
    b = seat_files(tmp_path, "public_seat_static")
    m.build_source("public_seat_static", b["registration"], b["publications"], b["run"], b["source"], current=now)
    seats = m.export_requests(b["source"], current=now)
    assert len(seats) == 4 and not b["run"].exists()
    assert [r["published_input"]["decision_kind"] for r in seats.values()] == [
        "RANKED_CANDIDATES", "NO_CHANGE", "RANKED_CANDIDATES", "NO_CHANGE"]
    assert all(r["kind"] is None and r["weights"] is None and r["equity_cents"] is None for r in seats.values())
    c = seat_files(tmp_path, "public_seat_append", publish=False)
    saved = m.build_source("public_seat_append", c["registration"], [], c["run"], c["source"], current=now)
    assert saved["source_day"] is None and saved["first_decision_day"] == "2026-10-19"
    assert saved["execution_day"] == "2026-10-20" and saved["refs"]["publication"] is None
    assert all(r["published_input"] is None and r["status"] == "not_ready"
        for r in m.export_requests(c["source"], current=now).values())


def test_original_15_arm_engine_seals_to_saved_intents_opening_and_ledger_without_mutation(real_publications, tmp_path, monkeypatch):
    from trading.research import opening_pack
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path/"report-root")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path/"data-root")
    e = real_publications
    e["make"]("2026-10-16")
    _, own19 = e["make"]("2026-10-19")
    run = tmp_path / "real15"
    result = consume(e, ["2026-10-16", "2026-10-19"], own19, run)
    assert result["newly_committed_case_sessions"] == 30
    observation = tmp_path/"observed"/"result.json"
    save(observation, {**result, "execution_progress_unknown": False})
    before = (run/"head.json").read_bytes()
    source = tmp_path/"source"
    pubs = [e["saved"][d][0] for d in ("2026-10-16", "2026-10-19")]
    now = e["clock"]["value"]
    m.build_source("entry_comparison", e["registration"], pubs, run, source,
        consumer_observation=observation, current=now)
    rows = m.export_requests(source, current=now)
    assert len(rows) == 15 and all(r.get("status") != "not_ready" for r in rows.values())
    assert all(r["weights"] == {} for key,r in rows.items() if key.endswith(":daily_confirmation"))
    assert all(r["weights"] for key,r in rows.items() if key.endswith(":entry_only_confirmation"))
    for key,intent in rows.items():
        assert m.read_request(intent, current=now) == intent
        saved = m.load_saved_case(source, key, current=now)
        assert saved["state"]["day"] == "2026-10-19" and saved["daily"]["date"] == "2026-10-19"
        assert saved["state"]["fills"] and saved["context"] == intent["context"]
    _, product = m.build_opening(source, tmp_path.parent/(tmp_path.name+"-opening"), current=now)
    assert product["preview_count"] == 15 and product["account_count"] == 15
    assert all(row["strategy_id"].startswith("entry_comparison:") for row in product["cases"])
    assert (run/"head.json").read_bytes() == before
    assert product["engine_advanced"] is False and product["ledger_mutated"] is False
    # A new failed external observation cannot silently keep a success product.
    failed = deepcopy(result); failed.update(status="failed", execution_progress_unknown=True)
    save(observation, failed)
    assert not m.source_dependencies_current(source)
    m.build_source("entry_comparison", e["registration"], pubs, run, source,
        consumer_observation=observation, current=now)
    assert all(r["status"] == "not_ready" for r in m.export_requests(source, current=now).values())
    assert (run/"head.json").read_bytes() == before


def test_source_noop_account_creation_freshness_and_exact_prefix_rejection(tmp_path):
    b = seat_files(tmp_path, "public_seat_static")
    now = datetime.fromisoformat("2026-10-18T10:00:00+08:00")
    args = ("public_seat_static", b["registration"], b["publications"], b["run"], b["source"])
    first = m.build_source(*args, current=now)
    file = b["source"] / "head.json"
    before, stamp = file.read_bytes(), file.stat().st_mtime_ns
    assert m.build_source(*args, current=datetime.fromisoformat("2026-10-18T11:00:00+08:00")) == first
    assert file.read_bytes() == before and file.stat().st_mtime_ns == stamp
    assert m.source_dependencies_current(file)
    b["run"].mkdir(); save(b["run"]/"head.json", {"not_a_valid_head": True})
    assert not m.source_dependencies_current(file)
    with pytest.raises(ValueError, match="stale"):
        m.export_requests(file, current=now)
    with pytest.raises(ValueError, match="prefix"):
        m.build_source("public_seat_static", b["registration"], b["publications"]*2,
            tmp_path/"other-run", tmp_path/"other-source", current=now)
