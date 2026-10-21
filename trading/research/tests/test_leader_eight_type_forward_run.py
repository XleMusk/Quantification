"""Explicit synthetic saved evidence; the real Engine and Ledger are not mocked."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading import engine
from trading.contracts import RunContext, TradingMode
from trading.ledger import Ledger
from trading.research import leader_eight_type_forward_run as m
from trading.research import leader_eight_type_paper as paper
from trading.research.baostock_daily_slice import digest
from trading.research.ipo_qualification import IpoQualification

CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"]
SYMBOLS = [f"{i:06d}.SZSE" for i in range(1, 7)]
RELAY = "relay_continuation_leader__unguarded__uncapped"
GUARDED = "relay_continuation_leader__two_close_envguard__uncapped"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    sessions, _, _ = paper._calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
    qualification = IpoQualification({s: {"ipo_date": "1999-01-01", "available_at": "2026-10-08T10:00:00+08:00",
        "board": "main", "source_basis": "synthetic_only"} for s in SYMBOLS}, tuple(sessions), {"synthetic_only": True})
    external = {"file": str(tmp_path / "synthetic_protocol.json"), "sha256": "synthetic-fixed-protocol",
        "record": {"schema": "leader_eight_type_fixed_comparison_protocol_v1", "case_count": 32,
            "cases": [{"id": key} for key in m.CASE_IDS],
            "environment_parameters": {"strong_breadth_at_least": .2, "strong_median_above": 0,
                "weak_breadth_at_most": -.2, "weak_median_below": 0, "entry_confirmation_closes": 2}}}
    panels = {}
    for day in DAYS:
        weak = day >= "2026-10-16"
        types = [{"id": t, "status": "unknown" if weak and t == "relay_continuation_leader" else "matched",
            "candidates": [{"symbol": s, "score": 100-i, "date": day, "source_day": day,
                "evidence": {"previous_open": 10., "structure_close": 9.}}
                for i, s in enumerate(SYMBOLS)],
            "unresolved_symbols": SYMBOLS[-1:] if weak and t == "relay_continuation_leader" else [],
            "missing_evidence": []} for t in m.TYPE_IDS]
        panels[day] = {"day": day, "available_at": day + "T17:00:00+08:00",
            "environment": {"source_complete": True, "breadth": -.5 if weak else .5,
                "median_return_pct": -1. if weak else 1.}, "types": types,
            "risk_features": {s: {"date": day, "listing_feature_policy_verified": True,
                "ipo_feature_support": True, "qualified_limit_up": not weak, "qualified_stage": 0 if weak else 2,
                "board": "main", "sma20": 9., "sma60": 8., "amount_rank_pct": 1.} for s in SYMBOLS},
            "risk_feature_scope_complete": True}
    loaded, paths = {}, {}
    for end in (3, 4, 5):
        days, day = DAYS[:end+1], DAYS[end]
        path = tmp_path / ("synthetic_input_" + day + ".json")
        path.write_text(json.dumps({"synthetic_only": True, "source_days": days}), encoding="utf8")
        proofs = [{"day": d, "raw_daily_sha256": "synthetic-raw-" + d, "ordinary_inventory_rows": len(SYMBOLS)} for d in days]
        bindings = {"input_manifest": str(path), "input_manifest_sha256": digest(path),
            "input_recorded_at": day + "T18:00:00+08:00", "scope_frozen_at": day + "T17:59:00+08:00",
            "external_fixed_protocol": deepcopy(external), "context_anchor": {"synthetic_fixed_anchor": "2026-10-09"},
            "source_proofs": proofs, "IPO_qualification": qualification.verification}
        raw = pd.DataFrame([{"date": d, "code": "sz." + s[:6], "open": "10", "high": "10.1", "low": "9.9",
            "close": "10", "preclose": "10", "volume": "1000000", "amount": "10000000",
            "tradestatus": "1", "isST": "0", "pctChg": "0", "adjustflag": "3"} for d in days for s in SYMBOLS])
        value = {"raw": raw, "panel_by_day": {d: deepcopy(panels[d]) for d in days}, "source_days": days,
            "calendar_payload": CALENDAR, "scope_members": SYMBOLS, "qualification": qualification,
            "context_anchor": {"day": "2026-10-09", "closes": dict.fromkeys(SYMBOLS, 10.)},
            "provenance": {"panel": str(path), "panel_sha256": digest(path), "source_proofs": proofs,
                "context_anchor": bindings["context_anchor"], "external_fixed_protocol": deepcopy(external)},
            "_bindings": bindings}
        loaded[str(path)] = value
        paths[day] = path
    monkeypatch.setattr(m, "load_inputs", lambda path, *a: deepcopy(loaded[str(Path(path).resolve())]))
    monkeypatch.setattr(m, "_source_bindings", lambda value, observed: deepcopy(value["_bindings"]))
    clock = datetime.fromisoformat("2026-10-19T20:00:00+08:00")
    monkeypatch.setattr(m, "_now", lambda: clock)
    monkeypatch.setattr(engine, "_published_now", lambda: clock)
    pubs = []
    for day in DAYS[3:]:
        value = loaded[str(paths[day])]
        observed = datetime.fromisoformat(day + "T18:30:00+08:00")
        d, execution, env, prepared = paper._prepare(value, observed)
        record = paper._finalize(d, execution, env, prepared, value["_bindings"], datetime.fromisoformat(day + "T18:45:00+08:00"))
        path = tmp_path / ("publication_" + day + ".json")
        path.write_text(json.dumps(record), encoding="utf8")
        pubs.append(path)
    registered = tmp_path / "synthetic_forward_registration.json"
    registered.write_text(json.dumps({"schema": "leader_eight_type_published_close_forward_registration_v1",
        "registered_at": "2026-10-15T22:00:00+08:00", "first_decision_day": "2026-10-15",
        "first_execution_day": "2026-10-16", "first_execution_deadline": "2026-10-16T09:30:00+08:00",
        "original_publication": {"file": str(pubs[0]), "sha256": digest(pubs[0]), "recorded_at": "2026-10-15T18:45:00+08:00"},
        "published_target_policy_id": m.TARGET_POLICY_ID, "risk_policy": m.FORWARD_RISK_SPEC,
        "case_ids": list(m.CASE_IDS), "config": m._config(100000, 5).to_dict(),
        "registration_is_original_publication": False, "original_publication_unchanged": True,
        "synthetic_only": True, **m.FLAGS}), encoding="utf8")
    return {"loaded": loaded, "panels": paths, "pubs": pubs, "run": tmp_path / "research_books", "registration": registered}


def execute(e, *, pubs=None, end="2026-10-16", through=None):
    return m.run(e["pubs"][:1] if pubs is None else pubs, e["panels"][end], "synthetic_ipo", "synthetic_master", e["run"], through=through, registration=e["registration"])


def head(e):
    return json.loads((e["run"] / "head.json").read_text(encoding="utf8"))


def test_only_real_15_source_waits_for_exact16_without_directory_or_state(evidence, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Exact E witness must precede directory/lock/new_state/Engine")
    monkeypatch.setattr(m, "new_state", forbidden)
    monkeypatch.setattr(m, "lock", forbidden)
    monkeypatch.setattr(m, "advance", forbidden)
    result = execute(evidence, end="2026-10-15")
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert result["required_successor_day"] == "2026-10-16"
    assert not evidence["run"].exists()


def test_raw16_without_own16_publication_never_advances_whole16_or_rolls_E(evidence):
    result = execute(evidence, through="2026-10-19")
    assert result["newly_committed_case_sessions"] == 32
    assert result["next_real_close_publication_days_required"] == ["2026-10-16"]
    for case in head(evidence)["cases"].values():
        assert [d["date"] for d in case["daily"]] == ["2026-10-15"]
        assert case["state"]["day"] == "2026-10-15" and not case["state"]["fills"]
        pending = case["state"].get("pending_published_close_intent")
        if pending:
            assert pending["execution_not_before"] == "2026-10-16"


def test_two_actual_publications_consume15_on16_and_queue19_without19raw(evidence, monkeypatch):
    execute(evidence)
    first = head(evidence)
    monkeypatch.setattr(m, "new_state", lambda *a: pytest.fail("No account reset on continuation"))
    result = execute(evidence, pubs=evidence["pubs"][:2])
    assert result["newly_committed_case_sessions"] == 32 and result["metrics"] is None
    second = head(evidence)
    for key, case in second["cases"].items():
        assert case["context"] == first["cases"][key]["context"]
        assert case["daily"][:1] == first["cases"][key]["daily"]
        assert [r["date"] for r in case["daily"]] == DAYS[3:5]
        assert case["journal"][0] == first["cases"][key]["journal"][0]
        assert case["state"]["published_close_target_policy"]["policy_id"] == m.TARGET_POLICY_ID
    held = second["cases"][RELAY]
    assert len(held["state"]["orders"]) == 5
    assert {fill["side"] for fill in held["state"]["fills"].values()} == {"buy"}
    assert {fill["day"] for fill in held["state"]["fills"].values()} == {"2026-10-16"}
    assert held["journal"][-1]["bound_original_intent"]["kind"] == "NO_CHANGE"
    assert held["journal"][-1]["published_close_reduction"] is None
    assert held["state"]["pending_targets"] is None
    assert held["state"]["pending_risk_exits"]["execution_on"] == "2026-10-19"
    book = Ledger(held["state"], m._config(100000, 5), RunContext.from_dict(held["context"]))
    assert all(book.quantity(s) > 0 and book.available(s, "2026-10-16") == 0 for s in SYMBOLS[:5])
    exited = second["cases"][GUARDED]
    assert len(exited["state"]["orders"]) == 5
    assert {fill["side"] for fill in exited["state"]["fills"].values()} == {"buy"}  # No immediate T+1 sale.
    receipt = exited["journal"][-1]["published_close_reduction"]
    assert receipt["original_intent"]["weights"] == receipt["reduced_intent"]["weights"] == {}
    assert exited["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-19"
    original = (evidence["run"] / "head.json").read_bytes()
    assert execute(evidence, pubs=evidence["pubs"][:2])["newly_committed_case_sessions"] == 0
    assert (evidence["run"] / "head.json").read_bytes() == original


@pytest.mark.parametrize("fault", ["weights", "context", "source_hash"])
def test_changed_publication_or_input_hash_rejected_before_creation(evidence, fault):
    path = evidence["pubs"][0]
    record = json.loads(path.read_text())
    if fault == "weights":
        record["cases"][0]["portfolio_intent"]["weights"][SYMBOLS[0]] = .01
    elif fault == "context":
        record["cases"][0]["portfolio_intent"]["context"] = RunContext("wrong", TradingMode.REPLAY).to_dict()
    else:
        record["source"]["input_manifest_sha256"] = "changed"
    path.write_text(json.dumps(record), encoding="utf8")
    with pytest.raises(ValueError):
        execute(evidence)
    assert not evidence["run"].exists()


def test_processed_prefix_and_case_context_cannot_be_rewritten(evidence):
    execute(evidence)
    before = (evidence["run"] / "head.json").read_bytes()
    with pytest.raises(ValueError, match="prefix|registration"):
        execute(evidence, pubs=evidence["pubs"][1:2])
    assert (evidence["run"] / "head.json").read_bytes() == before
    with pytest.raises(ValueError, match="backwards|Through"):
        execute(evidence, through="2026-10-14")
    value = head(evidence)
    value["cases"][RELAY]["context"]["account_id"] = "changed"
    (evidence["run"] / "head.json").write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="context"):
        execute(evidence)


def test_old_unknown_cannot_be_retroactively_replaced_by_new_prefix(evidence):
    value = evidence["loaded"][str(evidence["panels"]["2026-10-16"])]["panel_by_day"]["2026-10-15"]
    value["risk_features"][SYMBOLS[0]]["sma20"] = 0.
    with pytest.raises(ValueError, match="prefix facts"):
        execute(evidence, pubs=evidence["pubs"][:2])
    assert not evidence["run"].exists()


def test_bad_held_raw_reference_blocks19_without_changing_actual_book(evidence):
    execute(evidence, pubs=evidence["pubs"][:2])
    before = deepcopy(head(evidence)["cases"][RELAY])
    loaded = evidence["loaded"][str(evidence["panels"]["2026-10-19"]) ]
    loaded["raw"].loc[loaded["raw"].date.eq("2026-10-19") & loaded["raw"].code.eq("sz." + SYMBOLS[0][:6]), "preclose"] = "9"
    # Rebuild only the explicitly synthetic saved 19 publication from this
    # fixture's bad current evidence; no real producer/source is contacted.
    observed = datetime.fromisoformat("2026-10-19T18:30:00+08:00")
    d, execution, env, prepared = paper._prepare(loaded, observed)
    value = paper._finalize(d, execution, env, prepared, loaded["_bindings"], datetime.fromisoformat("2026-10-19T18:45:00+08:00"))
    evidence["pubs"][2].write_text(json.dumps(value), encoding="utf8")
    result = execute(evidence, pubs=evidence["pubs"], end="2026-10-19")
    after = head(evidence)["cases"][RELAY]
    assert result["status"] == "advanced_research_only_with_blocked_cases"
    assert next(c for c in result["cases"] if c["case_id"] == RELAY)["blocking_evidence"] is not None
    assert after["state"] == before["state"] and after["daily"] == before["daily"] and after["journal"] == before["journal"]
    assert after["blocking_evidence"]["session_not_committed"] is True


def test_post_opening_callback_failure_rolls_back_trial_and_is_not_retried(evidence, monkeypatch):
    execute(evidence)
    before = head(evidence)
    calls = []
    def invalid(self, intent):
        calls.append(1)
        # This real Engine call has already executed yesterday's pending buys
        # in its trial. A reducer cannot change publication/account identity.
        from dataclasses import replace
        return replace(intent, context=RunContext("wrong", TradingMode.REPLAY))
    monkeypatch.setattr(m.PublishedFixedExpiryReduction, "__call__", invalid)
    execute(evidence, pubs=evidence["pubs"][:2])
    after = head(evidence)
    for key in after["cases"]:
        if after["cases"][key].get("error"):
            assert after["cases"][key]["state"] == before["cases"][key]["state"]
            assert after["cases"][key]["daily"] == before["cases"][key]["daily"]
    assert calls
    called = len(calls)
    execute(evidence, pubs=evidence["pubs"][:2])
    assert len(calls) == called
    assert list((evidence["run"] / "failed_attempts").glob("*.json"))


def test_collaborating_pending_weight_and_receipt_edit_rejected_by_independent_journal(evidence):
    execute(evidence)
    value = head(evidence)
    case = value["cases"][RELAY]
    symbol = SYMBOLS[0]
    case["state"]["pending_targets"][symbol] = .01
    case["state"]["pending_published_close_intent"]["reduced_intent"]["weights"][symbol] = .01
    (evidence["run"] / "head.json").write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="sealed journal"):
        execute(evidence, pubs=evidence["pubs"][:2])
    assert head(evidence)["cases"][RELAY]["state"] == case["state"]


@pytest.mark.parametrize("stamp", ["2026-10-15T18:00:00+08:00", "2026-10-16T09:30:00+08:00"])
def test_forward_registration_cannot_precede_original_or_follow_first_open(evidence, stamp):
    path = evidence["registration"]
    value = json.loads(path.read_text())
    value["registered_at"] = stamp
    path.write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="registration"):
        execute(evidence)
    assert not evidence["run"].exists()


def test_fifth_actual_holding_close_reduces_only_derived_target_without_future_raw(evidence, monkeypatch):
    pubs = list(evidence["pubs"])
    previous = evidence["loaded"][str(evidence["panels"]["2026-10-19"]) ]
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        loaded = deepcopy(previous)
        path = evidence["run"].parent / ("synthetic_input_" + day + ".json")
        path.write_text(json.dumps({"synthetic_only": True, "through": day}), encoding="utf8")
        loaded["source_days"].append(day)
        rows = loaded["raw"].loc[loaded["raw"].date.eq(loaded["raw"].date.max())].copy()
        rows["date"] = day
        loaded["raw"] = pd.concat([loaded["raw"], rows], ignore_index=True)
        panel = deepcopy(loaded["panel_by_day"][max(loaded["panel_by_day"])])
        panel.update(day=day, available_at=day + "T17:00:00+08:00")
        for item in panel["types"]:
            for candidate in item["candidates"]:
                candidate.update(date=day, source_day=day)
        for feature in panel["risk_features"].values():
            feature["date"] = day
        loaded["panel_by_day"][day] = panel
        proof = {"day": day, "raw_daily_sha256": "synthetic-raw-" + day, "ordinary_inventory_rows": len(SYMBOLS)}
        loaded["provenance"]["source_proofs"].append(proof)
        loaded["_bindings"].update(input_manifest=str(path), input_manifest_sha256=digest(path),
            input_recorded_at=day + "T18:00:00+08:00", scope_frozen_at=day + "T17:59:00+08:00",
            source_proofs=deepcopy(loaded["provenance"]["source_proofs"]))
        evidence["loaded"][str(path)] = loaded
        evidence["panels"][day] = path
        observed = datetime.fromisoformat(day + "T18:30:00+08:00")
        d, execution, env, prepared = paper._prepare(loaded, observed)
        record = paper._finalize(d, execution, env, prepared, loaded["_bindings"], datetime.fromisoformat(day + "T18:45:00+08:00"))
        pub = path.with_name("publication_" + day + ".json")
        pub.write_text(json.dumps(record), encoding="utf8")
        pubs.append(pub)
        previous = loaded
    now = datetime.fromisoformat("2026-10-22T20:00:00+08:00")
    monkeypatch.setattr(m, "_now", lambda: now)
    monkeypatch.setattr(engine, "_published_now", lambda: now)
    result = execute(evidence, pubs=pubs, end="2026-10-22")
    assert result["newly_committed_case_sessions"] == 32 * 6
    case = head(evidence)["cases"]["liquidity_capacity_proxy__unguarded__uncapped"]
    receipt = case["journal"][-1]["published_close_reduction"]
    assert receipt["original_intent"]["weights"]
    assert receipt["reduced_intent"]["weights"] == {}
    assert receipt["reduced_intent"]["reason"] == "shortline_v2_fixed_five_session_holding_exit"
    assert receipt["execution_not_before"] == "2026-10-23"
    assert case["state"]["pending_targets"] == {}
    assert case["state"]["pending_risk_exits"]["execution_on"] == "2026-10-23"
    assert len(case["journal_files"]) == 6
    assert "2026-10-23" not in previous["source_days"]


def test_landed_journal_before_failed_head_write_keeps_orphans_and_resumes_same_books(evidence, monkeypatch):
    execute(evidence)
    before = (evidence["run"] / "head.json").read_bytes()
    original_writer = m.write_json
    def interrupted(path, value):
        if Path(path).name == "head.json":
            raise OSError("synthetic interruption after day16 journals, before atomic head replacement")
        return original_writer(path, value)
    monkeypatch.setattr(m, "write_json", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        execute(evidence, pubs=evidence["pubs"][:2])
    assert (evidence["run"] / "head.json").read_bytes() == before
    orphan_files = {p: p.read_bytes() for p in (evidence["run"] / "journal").glob("*/2026-10-16_*.json")}
    assert len(orphan_files) == 32
    monkeypatch.setattr(m, "write_json", original_writer)
    monkeypatch.setattr(m, "new_state", lambda *a: pytest.fail("Recovery must retain the original 32 accounts"))
    later = datetime.fromisoformat("2026-10-19T20:00:01+08:00")
    monkeypatch.setattr(m, "_now", lambda: later)
    result = execute(evidence, pubs=evidence["pubs"][:2])
    assert result["newly_committed_case_sessions"] == 32
    assert all(path.read_bytes() == original for path, original in orphan_files.items())
    assert len(list((evidence["run"] / "journal").glob("*/2026-10-16_*.json"))) == 64
    saved = head(evidence)
    assert all([r["date"] for r in c["daily"]] == DAYS[3:5] for c in saved["cases"].values())
    relay = saved["cases"][RELAY]
    assert len(relay["state"]["orders"]) == 5
    assert len({f["order_id"] for f in relay["state"]["fills"].values()}) == 5
    settled = (evidence["run"] / "head.json").read_bytes()
    assert execute(evidence, pubs=evidence["pubs"][:2])["newly_committed_case_sessions"] == 0
    assert (evidence["run"] / "head.json").read_bytes() == settled
