"""New-family coordinator/detail/Ledger integration with saved tiny files only."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import native_lowvol_publication as pub
from trading.research import native_lowvol_saved_opening as adapter
from trading.research import opening_daily as core
from trading.research import opening_daily_artifacts as factory
from trading.research import opening_daily_detail as detail
from trading.research import opening_daily_ledger as ledger
from trading.research.opening_daily_view import RUN_RELATIVE
from trading.research.tests.test_native_lowvol_publication import fixture as publication_fixture
from trading.research.tests.test_native_lowvol_saved_opening import saved_continuation, write


def coordinator_with_legacy(tmp_path, monkeypatch, current):
    """Only the unrelated old product is a stub; the new family uses real adopt."""
    data = tmp_path / "data"
    monkeypatch.setattr(core, "DATA_ROOT", data)
    monkeypatch.setattr(core, "_lock", lambda path: nullcontext())
    old_source, old_result = data / "old_fixed_engineering", data / "old_existing" / "result.json"
    write(old_source / "head.json", {"old_engineering_route_identity": "unchanged"})
    write(old_result, {"old_saved_engineering_product": True})
    route = {"id": "fixed_lowvol", "family": "fixed", "signal": pub.VARIANT,
        "source": str(old_source), "adopt": {"result": str(old_result)}}

    def old_adopt(route, source_ref, refs):
        return {"source_head": source_ref, "source_day": "2026-10-16", "execution_day": "2026-10-19",
            "status": "old_engineering_saved_product", "family": "fixed", "case_count": 1,
            "ready_case_count": 1, "not_ready_case_count": 0, "candidate_order_count": 0,
            "refs": refs, **core.FLAGS}

    monkeypatch.setattr(core, "_factory", lambda: SimpleNamespace(adopt=old_adopt,
        validate_product=lambda *a: None))
    directory = data / RUN_RELATIVE
    assert core.register(directory, [route], current=current)["status"] == "current_saved_opening_products"
    monkeypatch.setattr(core, "_factory", lambda: factory)
    return data, directory


def attach(directory, source, opening, current):
    result_path = opening / "result.json"
    artifacts = {"result": str(result_path)}
    if core._read(result_path).get("opening_pack"):
        artifacts["opening_pack"] = core._read(result_path)["opening_pack"]
    result = core.extend(directory, [{"id": "native_lowvol", "family": adapter.FAMILY,
        "source": str(source), "adopt": artifacts}], current=current)
    assert result["newly_registered_routes"] == result["newly_committed_routes"] == ["native_lowvol"]
    return result


def read_only_guards(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("A saved detail/Ledger GET must not produce, publish, advance or lock")
    monkeypatch.setattr(core, "_factory", forbidden)
    monkeypatch.setattr(core, "_lock", forbidden)
    monkeypatch.setattr(adapter, "build_source", forbidden)
    monkeypatch.setattr(adapter, "build_opening", forbidden)
    monkeypatch.setattr(pub, "publish", forbidden)
    monkeypatch.setattr("trading.engine.advance", forbidden)


def test_extend_adopts_bootstrap_one_unknown_case_without_replacing_old_route(tmp_path, monkeypatch):
    current = datetime.fromisoformat("2026-10-17T10:00:00+08:00")
    calendar, cache, close, symbols, _ = publication_fixture(tmp_path, monkeypatch)
    publication = pub.publish(cache("2026-10-16"), calendar, close("2026-10-16"), tmp_path / "publication",
        inception=pub.INCEPTIONS[1], now="2026-10-17T01:00:00+08:00")
    native_run, source = tmp_path / "uncreated_native_account", tmp_path / "source"
    adapter.build_source(native_run, publication["publication_ref"]["file"], source, current=current)
    opening, _ = adapter.build_opening(source, tmp_path / "opening", current=current)
    data, directory = coordinator_with_legacy(tmp_path, monkeypatch, current)
    original_registration = (directory / "registration.json").read_bytes()
    old_head, _ = core._load(directory)
    old_attempts = {p: p.read_bytes() for p in (directory / "attempts").rglob("*.json")}
    attach(directory, source, opening, current)
    head, registration = core._load(directory)
    assert [r["id"] for r in registration["routes"]] == ["fixed_lowvol", "native_lowvol"]
    assert (directory / "registration.json").read_bytes() == original_registration
    assert head["routes"]["fixed_lowvol"] == old_head["routes"]["fixed_lowvol"]
    assert all(path.read_bytes() == value for path, value in old_attempts.items())
    product = head["routes"]["native_lowvol"]["product"]
    assert product["status"] == "not_ready" and product["case_count"] == product["not_ready_case_count"] == 1
    assert product["ready_case_count"] == 0 and product["candidate_order_count"] is None
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    read_only_guards(monkeypatch)
    value = detail.opening_daily_detail(data, "native_lowvol", now=current)
    case = value["cases"][0]
    assert len(value["cases"]) == 1 and case["strategy_id"] == "native_lowvol:" + pub.VARIANT
    assert case["status"] == "not_ready" and case["saved_source"]["weights"] == {s: .04 for s in symbols[:20]}
    assert case["saved_source"]["cash_rmb"] is None and case["saved_source"]["equity_rmb"] is None
    assert all(case[k] is None for k in ("intent", "positions", "targets", "orders", "fills", "returns"))
    book = ledger.opening_daily_ledger(data, "native_lowvol", pub.VARIANT, now=current)
    assert book["status"] == "unavailable" and book["reason"] == "saved_opening_case_not_ready"
    assert all(book[k] is None for k in ("cash", "positions", "orders", "fills", "order_count", "fill_count", "blocked_count"))
    assert not native_run.exists()
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_saved_native_family_detail_and_ledger_project_sealed_book_and_consumed_decision(tmp_path, monkeypatch):
    run, old_next, original_head, held = saved_continuation(tmp_path, monkeypatch)
    head = deepcopy(original_head)
    head["state"]["prices"] = {held: 1000}
    receipt = head["state"]["last_preopen_intent"]
    receipt.update(schema="simulation_preopen_portfolio_intent_receipt_v1", source_day="2026-10-16",
        execution_day="2026-10-19", published_at="2026-10-17T01:00:00+08:00")
    head["daily"][-1].update(cash=head["state"]["cash"] / 100, equity=head["state"]["cash"] / 100 + 1000)
    journal_file = Path(head["journal_files"][-1]["file"])
    journal = core._read(journal_file)
    journal.update(post_state=head["state"], post_state_sha256=adapter._hash(head["state"]),
        post_daily=head["daily"], post_daily_sha256=adapter._hash(head["daily"]))
    head["journal_files"][-1] = write(journal_file, journal)
    write(run / "head.json", head)
    saved_run = next(r["file"] for r in old_next["source_refs"] if r["role"] == "saved_run")
    publication = pub.publish(saved_run, old_next["calendar_ref"]["file"], old_next["source_proof"]["source_folder"],
        tmp_path / "exact_next", account_path=run / "head.json",
        previous_publication=old_next["previous_publication_ref"]["file"], inception=old_next["inception"],
        now="2026-10-19T19:01:00+08:00")
    current = datetime.fromisoformat("2026-10-19T20:00:00+08:00")
    data, directory = coordinator_with_legacy(tmp_path, monkeypatch, current)
    from trading.research import opening_pack
    monkeypatch.setattr(opening_pack, "DATA_ROOT", data)
    source = tmp_path / "source"
    adapter.build_source(run, publication["publication_ref"]["file"], source, current=current)
    opening, _ = adapter.build_opening(source, tmp_path / "opening", current=current)
    attach(directory, source, opening, current)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    read_only_guards(monkeypatch)
    value = detail.opening_daily_detail(data, "native_lowvol", now=current)
    assert value["cases"][0]["status"] == "paper_preview_only"
    assert value["cases"][0]["intent"]["kind"] == "NO_CHANGE" and value["cases"][0]["orders"] == []
    book = ledger.opening_daily_ledger(data, "native_lowvol", pub.VARIANT, now=current)
    assert book["status"] == "saved_completed_research_ledger" and book["ledger_day"] == "2026-10-19"
    assert book["cash"]["total_cents"] == head["state"]["cash"] and book["cash"]["equity_cents"] == 14100000
    assert book["positions"][0]["symbol"] == held and book["positions"][0]["quantity"] == 100
    assert book["positions"][0]["sellable_on_ledger_day"] == 0
    assert book["order_count"] == book["fill_count"] == 0
    assert book["executed_decision"]["kind"] == "FULL_TARGET"
    assert book["executed_decision"]["source_day"] == "2026-10-16"
    assert book["executed_decision"]["execution_day"] == "2026-10-19" and book["executed_decision_reason"] is None
    assert book["planned_execution_day"] == "2026-10-20" and book["next_plan_is_processed_fill_evidence"] is False
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
