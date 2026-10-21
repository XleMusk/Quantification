"""New daily producer/durability boundaries with synthetic source, real Engine."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading import engine
from trading.research import leader_type_router_forward as forward
from trading.research import leader_type_router_paper as paper
from trading.research.ipo_qualification import IpoQualification
from trading.research.leader_eight_type_panel_replay import PROTOCOL, INPUT_SCHEMA


A, B = "000001.SZSE", "000002.SZSE"
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
    "2026-10-19", "2026-10-20", "2026-10-21"]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")
    return path


def test_metadata_registration_before_first_close_no_source_or_account(tmp_path, monkeypatch):
    source = tmp_path / "source"
    calendar = save(source / "calendar.json", CALENDAR)
    protocol = save(source / "protocol.json", PROTOCOL)
    manifest = save(source / "manifest.json", {"schema": INPUT_SCHEMA,
        "calendar": str(calendar), "calendar_sha256": paper.digest(calendar),
        "protocol_file": str(protocol), "protocol_sha256": paper.digest(protocol),
        "source_days": DAYS[:5], "source_proofs": [{"day": d} for d in DAYS[:5]],
        "scope_members": [A, B], "scope_frozen_at": "2026-10-15T18:00:00+08:00",
        "candidate_panel_only": True, "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False})
    base = save(source / "publication.json", {"schema": "leader_eight_type_unbound_close_paper_plan_v1",
        "source_close_day": DAYS[4], "protocol": PROTOCOL, "case_count": 32,
        "execution_admitted": False, "recorded_at": DAYS[4] + "T18:45:00+08:00",
        "source": {"input_manifest": str(manifest), "input_manifest_sha256": paper.digest(manifest)}})
    ipo, master = save(source / "ipo.json", {}), save(source / "master.json", {})
    code = save(source / "synthetic_code.json", {"synthetic_only": True})
    monkeypatch.setattr(paper, "_runtime", lambda: [paper._pin(code)])
    monkeypatch.setattr(paper, "_now", lambda: datetime.fromisoformat("2026-10-19T01:00:00+08:00"))
    monkeypatch.setattr(paper, "load_inputs", lambda *a, **k: pytest.fail("register must not load prices"))
    record = paper.register(base, manifest, ipo, master, tmp_path / "reg", first_day=DAYS[5])
    assert record["first_execution_day"] == DAYS[6]
    assert record["accounts_created"] is record["engine_advanced"] is record["raw_loaded"] is False
    assert len(record["case_ids"]) == 4 and paper.registration(tmp_path / "reg/registration.json") == record
    monkeypatch.setattr(paper, "_now", lambda: datetime.fromisoformat("2026-10-19T15:01:00+08:00"))
    with pytest.raises(ValueError, match="before first D close"):
        paper.register(base, manifest, ipo, master, tmp_path / "late", first_day=DAYS[5])
    assert not (tmp_path / "late").exists()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    sessions, _, _ = paper._calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
    qualification = IpoQualification({s: {"ipo_date": "2000-01-01", "available_at": "2000-01-01T00:00:00+08:00",
        "board": "main", "source_basis": "synthetic_saved_evidence"} for s in (A, B)}, tuple(sessions), {})
    reg = {"first_decision_day": DAYS[5], "first_execution_day": DAYS[6],
        "registered_at": "2026-10-19T01:00:00+08:00", "scope_members": [A, B],
        "anchor_source_prefix": [{"day": day} for day in DAYS[:5]],
        "spec": paper._spec(), "references": {"ipo_evidence": {"path": "synthetic_ipo"},
            "research_master_run": {"path": "synthetic_master"}}}
    reg_path = save(tmp_path / "registration/registration.json", reg)
    panels, loaded, sources, pubs = {}, {}, {}, []
    source = tmp_path / "sources"
    calendar = save(source / "calendar.json", CALENDAR)
    protocol = save(source / "protocol.json", PROTOCOL)
    history = source / "history"
    history.mkdir()
    identity = save(source / "identity.json", {"synthetic_only": True})
    for day in DAYS:
        candidates = [{"symbol": A, "score": 1., "date": day, "source_day": day, "board": "main", "evidence": {}}]
        panels[day] = {"day": day, "available_at": day + "T17:00:00+08:00",
            "environment": {"source_complete": True, "breadth": .5, "median_return_pct": 1.},
            "types": [{"id": t, "status": "unknown" if day == DAYS[7] else "matched",
                "candidates": deepcopy(candidates), "unresolved_symbols": [], "missing_evidence": []}
                for t in paper.router.TYPE_IDS],
            "risk_features": {s: {"date": day, "listing_feature_policy_verified": True,
                "ipo_feature_support": True, "qualified_limit_up": day != DAYS[7], "qualified_stage": 2,
                "board": "main", "sma20": 9.5, "sma60": 9., "amount_rank_pct": .995} for s in (A, B)},
            "risk_feature_scope_complete": True}
    for day in DAYS[5:]:
        days = DAYS[:DAYS.index(day)+1]
        folder = source / day
        folder.mkdir()
        raw_file = folder / "raw.parquet"
        raw_file.write_bytes(b"synthetic raw bytes, actual Engine uses the declared fixture below")
        manifest = save(folder / "manifest.json", {"raw_file": str(raw_file), "raw_sha256": paper.digest(raw_file),
            "calendar": str(calendar), "protocol_file": str(protocol), "history_snapshot": str(history),
            "identity_report": {"file": str(identity), "sha256": paper.digest(identity)},
            "source_days": days, "created_at": day + "T18:00:00+08:00"})
        raw = pd.DataFrame([{"date": d, "code": "sz." + s[:6], "open": "10", "high": "10.1", "low": "9.9",
            "close": "10", "preclose": "10", "volume": "10000000", "amount": "100000000",
            "tradestatus": "1", "isST": "0", "pctChg": "0", "adjustflag": "3"} for d in days for s in (A, B)])
        value = {"raw": raw, "panel_by_day": {d: deepcopy(panels[d]) for d in days},
            "source_days": days, "calendar_payload": CALENDAR, "scope_members": [A, B],
            "qualification": qualification, "context_anchor": {"day": "2026-10-09", "closes": {A: 10., B: 10.}},
            "provenance": {"panel": str(manifest), "panel_sha256": paper.digest(manifest),
                "source_proofs": [{"day": d} for d in days], "context_anchor": {"day": "2026-10-09"},
                "external_fixed_protocol": {"synthetic_original_protocol": True}}}
        loaded[day] = value
        sources[day] = manifest
        path = save(tmp_path / "publications" / (day + ".json"), {"fixture": day})
        record = {"recorded_at": day + "T18:45:00+08:00"}
        pubs.append({"day": day, "execution": sessions[sessions.index(day)+1], "loaded": value,
            "record": record, "reference": {"day": day, "file": str(path), "sha256": paper.digest(path),
                "recorded_at": record["recorded_at"], "execution_day": sessions[sessions.index(day)+1]}})
    monkeypatch.setattr(paper, "registration", lambda *a, **k: deepcopy(reg))
    def load_inputs(path, record):
        manifest = paper._read(path)
        value = deepcopy(loaded[manifest["source_days"][-1]])
        value["provenance"].update(panel=str(Path(path).resolve()), panel_sha256=paper.digest(path))
        return value
    monkeypatch.setattr(paper, "_load_inputs", load_inputs)
    monkeypatch.setattr(paper, "load_publication", lambda path, *a, **k:
        deepcopy(next(p for p in pubs if p["reference"]["file"] == str(Path(path).resolve()))))
    clock = datetime.fromisoformat("2026-10-21T20:00:00+08:00")
    monkeypatch.setattr(paper, "_now", lambda: clock)
    monkeypatch.setattr(engine, "_published_now", lambda: clock)
    return {"reg": reg, "registration": reg_path, "loaded": loaded, "sources": sources,
        "pubs": pubs, "run": tmp_path / "run", "tmp": tmp_path}


def execute(evidence, count):
    selected = evidence["pubs"][:count]
    return forward.run([p["reference"]["file"] for p in selected], evidence["sources"][selected[-1]["day"]],
        evidence["run"], registration=evidence["registration"])


def test_D_only_publication_saves_own_bytes_before_real_clock_no_successor(evidence, monkeypatch):
    e = evidence
    day = DAYS[5]
    base = deepcopy(e["pubs"][0])
    monkeypatch.setattr(paper, "_base_publication", lambda *a, **k: deepcopy(base))
    def bindings(loaded, observed):
        return {"input_manifest": loaded["provenance"]["panel"],
            "input_manifest_sha256": loaded["provenance"]["panel_sha256"],
            "input_recorded_at": day + "T18:00:00+08:00"}
    monkeypatch.setattr(paper, "_source_bindings", bindings)
    out = e["tmp"] / "new_publication"
    calls = []
    def now():
        calls.append((out / "own_inputs/manifest.json").exists())
        return datetime.fromisoformat(day + "T19:00:00+08:00")
    monkeypatch.setattr(paper, "_now", now)
    value = paper.publish(e["registration"], e["sources"][day], base["reference"]["file"], out)
    assert calls[0] is False and calls[-1] is True
    assert value["recorded_at"] == day + "T19:00:00+08:00"
    assert value["proposed_execution_day"] == DAYS[6] and value["successor_raw_used"] is False
    assert paper._read(out / "own_inputs/manifest.json")["source_days"][-1] == day
    assert all(row["target_weights"] is None and row["active_type"] is None for row in value["cases"])
    assert value["accounts_created"] is value["engine_advanced"] is False
    with pytest.raises(FileExistsError):
        paper.publish(e["registration"], e["sources"][day], base["reference"]["file"], out)


def test_real_engine_D_only_queue_then_actual_fill_NC_known_exit_and_exact_prefix(evidence):
    e = evidence
    result = execute(e, 1)
    assert result["newly_committed_case_sessions"] == 4
    first = forward.load_saved_head(e["run"])
    assert all(case["state"]["pending_targets"] == {A: .16} and not case["state"]["orders"]
               for case in first["cases"].values())
    assert all(case["journal"][0]["envelope"]["published_at"] == DAYS[5] + "T18:45:00+08:00"
               for case in first["cases"].values())
    result = execute(e, 2)
    second = forward.load_saved_head(e["run"])
    for case in second["cases"].values():
        assert len(case["state"]["orders"]) == 1 and len(case["state"]["fills"]) == 2
        assert case["state"]["pending_targets"] is None
        assert sum(lot["qty"] for lot in case["state"]["positions"][A]) > 0
        assert case["decisions"][-1]["close_rule"] == "old_pending_actual_fill_preserve_exact_shares"
    execute(e, 3)
    final = forward.load_saved_head(e["run"])
    for case in final["cases"].values():
        assert case["state"]["pending_targets"] == {} and case["state"][paper.router.STATE_KEY]["exit_sticky"]
        assert case["journal"][-1]["envelope"]["original_intent"]["kind"] == "NO_CHANGE"
        assert case["daily"][-1]["published_close_router"]["intent"]["kind"] == "FULL_TARGET"
    before = (e["run"] / "head.json").read_bytes()
    assert execute(e, 3)["newly_committed_case_sessions"] == 0
    assert (e["run"] / "head.json").read_bytes() == before
    with pytest.raises(ValueError, match="first-D continuous"):
        forward.run([p["reference"]["file"] for p in e["pubs"][1:]], e["sources"][DAYS[7]],
            e["run"], registration=e["registration"])
    e["loaded"][DAYS[7]]["raw"].loc[e["loaded"][DAYS[7]]["raw"].date.eq(DAYS[5]), "close"] = "9"
    with pytest.raises(ValueError, match="prefix"):
        execute(e, 3)
    assert (e["run"] / "head.json").read_bytes() == before


def test_prepared_commit_after_head_write_failure_restores_without_engine_repeat(evidence, monkeypatch):
    e = evidence
    execute(e, 1)
    original_write = forward.write_json
    def fail_head(path, value):
        if Path(path) == e["run"] / "head.json":
            raise OSError("synthetic lost head replacement after durable prepared commit")
        return original_write(path, value)
    monkeypatch.setattr(forward, "write_json", fail_head)
    with pytest.raises(OSError, match="lost head"):
        execute(e, 2)
    assert forward.load_saved_head(e["run"])["cases"][paper.CASE_IDS[0]]["state"]["day"] == DAYS[5]
    monkeypatch.setattr(forward, "write_json", original_write)
    monkeypatch.setattr(paper.router.LeaderTypeRouter, "step_published",
        lambda *a, **k: pytest.fail("a sealed trial must be restored, not re-executed"))
    result = execute(e, 2)
    assert result["newly_committed_case_sessions"] == 0 and result["engine_advanced"] is False
    restored = forward.load_saved_head(e["run"])
    assert all(case["state"]["day"] == DAYS[6] and len(case["state"]["orders"]) == 1
               and len(case["journal"]) == 2 for case in restored["cases"].values())
