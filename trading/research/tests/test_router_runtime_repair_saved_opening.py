"""Small saved metadata only; no runtime registration/source revalidation."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_type_router_forward as forward
from trading.research import leader_type_router_paper as paper
from trading.research import leader_type_router_runtime_repair as repair
from trading.research import registered_research_saved_opening as opening


def test_repair_saved_route_keeps_strict_identity_clocks_and_unknown_account_without_producers(tmp_path, monkeypatch):
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return paper._pin(path)

    def forbidden(*args, **kwargs):
        pytest.fail("Saved opening cannot run a producer, Engine or heavy repair registration loader")

    for module, names in ((repair, ("registration", "_origin")),
            (paper, ("registration", "register", "publish", "load_inputs")), (forward, ("run",))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    day, execution, kind = "2026-10-19", "2026-10-20", "leader_type_router_runtime_repair"
    now = datetime.fromisoformat(day + "T20:00:00+08:00")
    calendar = save(tmp_path / "small/calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    anchor_publication = save(tmp_path / "small/anchor_publication.json", {"synthetic": True})
    # These input declarations deliberately have no readable payload.
    anchor = {"path": str(tmp_path / "large/anchor.json"), "sha256": "a" * 64}
    inputs = {"path": str(tmp_path / "large/manifest.json"), "sha256": "b" * 64}
    own_inputs = {"path": str(tmp_path / "large/router_own_manifest.json"), "sha256": "d" * 64}
    base = save(tmp_path / "small/base.json", {"source_close_day": day,
        "proposed_execution_day": execution, "recorded_at": day + "T18:30:00+08:00",
        "source": {"input_manifest": inputs["path"], "input_manifest_sha256": inputs["sha256"]}})
    original = {"schema": paper.REGISTRATION_SCHEMA, "method_id": paper.METHOD_ID,
        "registered_at": day + "T01:00:00+08:00", "first_decision_day": day,
        "first_execution_day": execution, "case_ids": list(paper.CASE_IDS),
        "config": paper._config(100000, 5).to_dict(), "spec": {"synthetic_frozen": True},
        "code": [{"path": "synthetic_engine.py", "sha256": "old-runtime"}],
        "anchor_source_prefix": [{"day": "2026-10-16"}], "references": {
            "calendar": calendar, "anchor_inputs": anchor, "anchor_publication": anchor_publication},
        "accounts_created": False, "engine_advanced": False, "orders_generated": False, **paper.FLAGS}
    original_ref = save(tmp_path / "original_registration/registration.json", original)
    original_bytes = Path(original_ref["path"]).read_bytes()
    run = tmp_path / "new_repair_account"
    reg = {**deepcopy(original), "schema": repair.SCHEMA,
        "registered_at": day + "T18:50:00+08:00",
        "strategy_definition_registered_at": original["registered_at"], "after_close_registration": True,
        "not_original_preregistered_route_success": True, "repair_policy": deepcopy(repair.POLICY),
        "code": [{"path": "synthetic_engine.py", "sha256": "new-runtime"}],
        "repair_adapter": {"path": "synthetic_adapter.py", "sha256": "adapter"},
        "repair_evidence": {"original_registration": original_ref, "failure_terminal": {
            "path": str(tmp_path / "saved_terminal/terminal.json"), "sha256": "c" * 64}},
        "repair_source": {"inputs": inputs, "base_publication": base},
        "repair_run_directory": str(run.resolve()), "original_run_directory": str(tmp_path / "original_account"),
        "original_failed_publication_directory": str(tmp_path / "original_failed_publication")}
    reg["repair_identity"] = repair._identity(reg)
    reg_path = tmp_path / "new_registration/registration.json"
    reg_ref = save(reg_path, reg)
    proofs = original["anchor_source_prefix"] + [{"day": day}]
    pub = {"schema": paper.PUBLICATION_SCHEMA, "method_id": paper.METHOD_ID,
        "registration": reg_ref, "source_close_day": day, "proposed_execution_day": execution,
        "recorded_at": day + "T19:00:00+08:00", "publication_at": day + "T19:00:00+08:00",
        "publication_time_backfilled": False, "runtime_repair": repair._marker(reg, reg_ref),
        "source": {"input_manifest": own_inputs["path"], "input_manifest_sha256": own_inputs["sha256"],
            "source_proofs": proofs}, "source_prefix": {"through": day, "sources": proofs},
        "base_publication": base, "cases": [{"case_id": key, "active_type": None,
            "target_weights": None, "account_binding_pending": True} for key in paper.CASE_IDS], **paper.FLAGS}
    pub_path = tmp_path / "new_publication/publication.json"
    save(pub_path, pub)
    forbidden_paths = {Path(anchor["path"]), Path(inputs["path"]), Path(own_inputs["path"])}
    file_ref = opening._file_ref
    def small_ref(path, expected=None):
        assert Path(path).resolve() not in forbidden_paths, "GET cannot parse/hash a declared large input"
        return file_ref(path, expected)
    monkeypatch.setattr(opening, "_file_ref", small_ref)
    source_path = tmp_path / "saved_projection"
    with pytest.raises(ValueError, match="Exact registered four-book router method"):
        opening.build_source("leader_type_router", reg_path, [pub_path], run, source_path, current=now)
    source = opening.build_source(kind, reg_path, [pub_path], run, source_path, current=now)
    assert source["runtime_repair"] == pub["runtime_repair"]
    assert source["runtime_generation"].startswith("explicit_after_close_runtime_repair_")
    assert source["account_count"] == 0 and source["route_kind"] == kind
    rows = opening.export_requests(source_path, current=now)
    assert len(rows) == 4 and not run.exists()
    for row in rows.values():
        assert row["status"] == "not_ready" and row["weights"] is row["cash_cents"] is row["equity_cents"] is None
        assert row["active_type"] is row["router_state"] is None
        assert row["published_input"]["decision_kind"] == "UNBOUND_ROUTING_EVIDENCE"
        assert row["runtime_repair"] == source["runtime_repair"]
    monkeypatch.setattr(opening, "_quotes", forbidden)
    _, product = opening.build_opening(source_path, tmp_path / "opening_product", current=now)
    assert product["preview_count"] == 0 and product["candidate_order_count"] is None
    assert product["runtime_repair"] == source["runtime_repair"]

    bad_pub = deepcopy(pub)
    bad_pub["runtime_repair"]["original_D_route_succeeded"] = True
    save(pub_path, bad_pub)
    with pytest.raises(ValueError, match="exact runtime marker"):
        opening.build_source(kind, reg_path, [pub_path], run, tmp_path / "bad_marker", current=now)
    bad_pub = {**deepcopy(pub), "publication_at": day + "T19:01:00+08:00"}
    save(pub_path, bad_pub)
    with pytest.raises(ValueError, match="actual publication clock"):
        opening.build_source(kind, reg_path, [pub_path], run, tmp_path / "bad_pub_clock", current=now)
    save(pub_path, pub)
    bad_reg = {**deepcopy(reg), "registered_at": day + "T14:00:00+08:00"}
    bad_reg["repair_identity"] = repair._identity(bad_reg)
    save(reg_path, bad_reg)
    with pytest.raises(ValueError, match="two actual clocks"):
        opening.build_source(kind, reg_path, [pub_path], run, tmp_path / "bad_reg_clock", current=now)
    save(reg_path, {**deepcopy(reg), "execution_admitted": True})
    with pytest.raises(ValueError, match="research-only boundary"):
        opening.build_source(kind, reg_path, [pub_path], run, tmp_path / "bad_flags", current=now)
    save(reg_path, reg)
    with pytest.raises(ValueError, match="independent identity"):
        opening.build_source(kind, reg_path, [pub_path], tmp_path / "other_account", tmp_path / "bad_run", current=now)
    assert opening.source_dependencies_current(source_path)
    assert Path(original_ref["path"]).read_bytes() == original_bytes
    save(run / "head.json", {"unknown_saved_account": True})
    assert opening.source_dependencies_current(source_path) is False
    with pytest.raises(ValueError, match="checkpoint binding is invalid"):
        opening.build_source(kind, reg_path, [pub_path], run, tmp_path / "unknown_head", current=now)
