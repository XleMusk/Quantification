"""Saved router projection only: synthetic sealed metadata, no Engine/source."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from trading.contracts import PortfolioIntent, decision_time
from trading.ledger import new_state
from trading.published_close_router import PublishedCloseRouterEnvelope, POLICY_ID, checked_receipt
from trading.research import leader_type_router_paper as paper
from trading.research import leader_type_router_forward as forward
from trading.research import registered_research_saved_opening as opening
from trading.research import opening_daily_ledger


DAY, NEXT, SYMBOL = "2026-10-19", "2026-10-20", "000001.SZSE"


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf8")
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def evidence(tmp_path):
    calendar = save(tmp_path / "sources/calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    anchor = save(tmp_path / "sources/anchor.json", {"fixture": "original_anchor"})
    anchor_publication = save(tmp_path / "sources/anchor_publication.json", {"fixture": "small_original_publication"})
    config = forward._config(100000, 5)
    registration = {"schema": paper.REGISTRATION_SCHEMA, "method_id": paper.METHOD_ID,
        "registered_at": DAY+"T02:00:00+08:00", "first_decision_day": DAY,
        "first_execution_day": NEXT, "case_ids": list(paper.CASE_IDS), "config": config.to_dict(),
        "anchor_source_prefix": [{"day": "2026-10-16", "fixture": "original_source"}],
        "references": {"calendar": calendar, "anchor_inputs": anchor, "anchor_publication": anchor_publication,
                       "ipo_evidence": anchor, "research_master_run": anchor}, **paper.FLAGS}
    reg = save(tmp_path / "registration/registration.json", registration)
    pubs = []
    for day, execution in ((DAY, NEXT), (NEXT, "2026-10-21")):
        manifest = save(tmp_path / day / "own_inputs/manifest.json", {
            "source_days": [DAY] if day == DAY else [DAY, NEXT], "calendar_sha256": calendar["sha256"]})
        base = save(tmp_path / day / "original32/paper_plan.json", {"source_close_day": day,
            "proposed_execution_day": execution, "recorded_at": day+"T18:30:00+08:00"})
        proofs = registration["anchor_source_prefix"] + [{"day": d, "fixture": "new_source"}
            for d in ([DAY] if day == DAY else [DAY, NEXT])]
        pub = {"schema": paper.PUBLICATION_SCHEMA, "method_id": paper.METHOD_ID,
            "registration": reg, "source_close_day": day, "proposed_execution_day": execution,
            "recorded_at": day+"T19:00:00+08:00", "publication_time_backfilled": False,
            "source": {"input_manifest": manifest["path"], "input_manifest_sha256": manifest["sha256"],
                       "source_proofs": proofs},
            "source_prefix": {"through": day, "sources": proofs, "raw": []},
            "base_publication": base, "routing_inputs": {"all_type_evidence": [{"symbol": SYMBOL}]},
            "cases": [{"case_id": key, "active_type": None, "target_weights": None,
                       "account_binding_pending": True} for key in paper.CASE_IDS], **paper.FLAGS}
        pubs.append(save(tmp_path / day / "publication.json", pub))
    return reg, config, pubs


def saved_head(tmp_path, reg, config, publication):
    run = tmp_path / "accounts"
    pub = {"file": publication["path"], "sha256": publication["sha256"], "day": DAY,
           "recorded_at": DAY+"T19:00:00+08:00", "execution_day": NEXT}
    head = {"schema": forward.CHECKPOINT_SCHEMA, "method_id": paper.METHOD_ID,
        "binding": {"registration": reg, "method_id": paper.METHOD_ID, "config": config.to_dict(),
                    "run_directory": str(run.resolve()), "first_decision_day": DAY},
        "publications": [pub], "source_prefix": json.loads(Path(publication["path"]).read_text())["source_prefix"],
        "cases": {}, **paper.FLAGS}
    for index,key in enumerate(paper.CASE_IDS):
        context = forward._context(run, key)
        meta = {"context": context, "decided_at": decision_time(DAY), "execution_not_before": NEXT,
                "source": "research:fixture"}
        intent = (PortfolioIntent.no_change("selected_type_unknown_preserve_shares", **meta) if index == 0 else
                  PortfolioIntent.full_target({SYMBOL: .16} if index == 1 else {}, **meta))
        envelope = PublishedCloseRouterEnvelope(intent, DAY, NEXT, DAY+"T19:00:00+08:00", POLICY_ID,
            "trend_leader", ())
        receipt = checked_receipt(envelope, intent)
        state = new_state(config.initial_cash)
        state.update(day=DAY, pending_targets=intent.to_legacy_pending(),
            last_published_close_router=receipt,
            leader_type_router={"active_type": "trend_leader", "exit_sticky": index > 1,
                "last_day": DAY, "exit_reason": "saved_fixture"})
        if index != 0:
            state["pending_published_close_intent"] = receipt
        row = {"date": DAY, "equity": config.initial_cash, "cash": config.initial_cash,
               "published_close_router": receipt}
        audit = {"synthetic_saved_decision": True}
        journal = {"schema": forward.JOURNAL_SCHEMA, "day": DAY, "execution_day": NEXT,
            "publication": pub, "envelope": envelope.to_dict(), "post_state_sha256": paper._hash(state),
            "row_sha256": paper._hash(row), "decision_sha256": paper._hash(audit),
            "committed_at": DAY+"T19:01:00+08:00"}
        sealed = save(run / "journal" / key / "19.json", journal)
        head["cases"][key] = {"context": context.to_dict(), "state": state, "daily": [row],
            "decisions": [audit], "journal": [journal], "journal_files": [sealed],
            "error": "fixture_blocked" if index == 3 else None, "blocking_evidence": None}
    save(run / "head.json", head)
    return run


def test_no_head_keeps_four_unbound_cases_without_cash_targets_or_raw_access(tmp_path, monkeypatch):
    reg, _, pubs = evidence(tmp_path)
    # Historical anchor/own manifests stand in for caches above64MiB: no parse
    # or hash read is allowed, even for a future D publication.
    master = tmp_path / "large_source/research_master_run.json"
    registered = json.loads(Path(reg["path"]).read_text())
    registered["references"]["research_master_run"] = {"path": str(master.resolve()), "sha256": "a" * 64}
    reg = save(reg["path"], registered)
    pub = json.loads(Path(pubs[0]["path"]).read_text())
    pub["registration"] = reg
    pubs[0] = save(pubs[0]["path"], pub)
    forbidden = {Path(registered["references"]["anchor_inputs"]["path"]).resolve(), master.resolve()}
    forbidden.update(Path(json.loads(Path(p["path"]).read_text())["source"]["input_manifest"]).resolve()
        for p in pubs)
    file_ref = opening._file_ref
    def bounded_ref(path, expected=None):
        assert Path(path).resolve() not in forbidden, "Display must not parse/hash the historical input payload"
        return file_ref(path, expected)
    monkeypatch.setattr(opening, "_file_ref", bounded_ref)
    run, source = tmp_path / "uncreated_account", tmp_path / "source_projection"
    now = datetime.fromisoformat(DAY+"T20:00:00+08:00")
    opening.build_source("leader_type_router", reg["path"], [pubs[0]["path"]], run, source, current=now)
    requests = opening.export_requests(source, current=now)
    assert len(requests) == 4 and not run.exists()
    for row in requests.values():
        assert row["status"] == "not_ready" and row["cash_cents"] is row["equity_cents"] is None
        assert row["weights"] is row["active_type"] is row["context"] is None
        assert row["published_input"]["decision_kind"] == "UNBOUND_ROUTING_EVIDENCE"
        assert row["published_input"]["candidate_symbols"] == []
    monkeypatch.setattr(opening, "_quotes", lambda *a: (_ for _ in ()).throw(AssertionError("No raw needed")))
    _, product = opening.build_opening(source, tmp_path / "opening_product", current=now)
    assert product["account_count"] == 0 and product["opening_pack"] is None
    assert product["preview_count"] == 0 and product["candidate_order_count"] is None
    own = Path(pub["source"]["input_manifest"])
    with own.open("a", encoding="utf8") as stream:
        stream.write(" ")
    assert opening.source_dependencies_current(source) is False  # Cheap stat changes still invalidate it.


def test_saved_postopen_receipts_preserve_NC_and_actual_owner_and_do_not_bind_future_pub(tmp_path, monkeypatch):
    reg, config, pubs = evidence(tmp_path)
    registered = json.loads(Path(reg["path"]).read_text())
    forbidden = {Path(registered["references"]["anchor_inputs"]["path"]).resolve()}
    forbidden.update(Path(json.loads(Path(p["path"]).read_text())["source"]["input_manifest"]).resolve()
        for p in pubs)
    file_ref = opening._file_ref
    def bounded_ref(path, expected=None):
        assert Path(path).resolve() not in forbidden, "Saved account display must not parse/hash historical inputs"
        return file_ref(path, expected)
    monkeypatch.setattr(opening, "_file_ref", bounded_ref)
    run = saved_head(tmp_path, reg, config, pubs[0])
    source = tmp_path / "saved_projection"
    before = (run / "head.json").read_bytes()
    now = datetime.fromisoformat(DAY+"T20:00:00+08:00")
    opening.build_source("leader_type_router", reg["path"], [pubs[0]["path"]], run, source, current=now)
    rows = opening.export_requests(source, current=now)
    a,b,c,d = [rows[k] for k in paper.CASE_IDS]
    assert a["kind"] == "NO_CHANGE" and a["weights"] is None and a["active_type"] == "trend_leader"
    assert b["weights"] == {SYMBOL: .16} and c["kind"] == "FULL_TARGET" and c["weights"] == {}
    assert d["status"] == "not_ready" and d["weights"] is None
    assert opening.read_request(a, current=now) == a
    saved = opening.load_saved_case(source, paper.CASE_IDS[0], current=now)
    projection, reason = opening_daily_ledger._project(saved["state"], config, DAY, saved["context"])
    assert reason is None and projection["active_type"] == "trend_leader"
    assert projection["cash"]["equity_cents"] == round(config.initial_cash * 100)
    # New D20 publication cannot substitute an uncommitted next owner or target.
    later = datetime.fromisoformat(NEXT+"T20:00:00+08:00")
    opening.build_source("leader_type_router", reg["path"], [p["path"] for p in pubs], run, source, current=later)
    assert all(row["status"] == "not_ready" and row["weights"] is None
        for row in opening.export_requests(source, current=later).values())
    assert (run / "head.json").read_bytes() == before
    assert opening.load_saved_case(source, paper.CASE_IDS[0], current=later)["ledger_day"] == DAY
