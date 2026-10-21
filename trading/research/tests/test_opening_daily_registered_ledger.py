"""Saved registered Ledger visibility is independent of next target readiness."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from trading.ledger import new_state
from trading.research import opening_daily as core
from trading.research import opening_daily_ledger as m
from trading.research import registered_research_saved_opening as adapter
from trading.research.opening_daily_view import RUN_RELATIVE
from trading.research.tests.test_registered_research_saved_opening import seat_files, save


NOW = datetime.fromisoformat("2026-10-20T19:00:00+08:00")
A = "600001.SSE"


def saved_route(tmp_path, name, *, with_account):
    b = seat_files(tmp_path/name, "public_seat_static")
    first_path = b["publications"][0]
    first_pub = json.loads(first_path.read_text(encoding="utf-8"))
    first_bundle = json.loads(Path(first_pub["bundle"]["file"]).read_text(encoding="utf-8"))
    for d,e in (("2026-10-19", "2026-10-20"), ("2026-10-20", "2026-10-21")):
        bundle = deepcopy(first_bundle)
        bundle.update(decision_day=d, next_execution_day=e)
        bundle_ref = save(tmp_path/name/d/"bundle.json", bundle)
        pub = {**first_pub, "decision_day": d, "execution_day": e,
               "published_at": d+"T18:30:00+08:00", "bundle": bundle_ref}
        path = tmp_path/name/d/"publication.json"
        save(path, pub); b["publications"].append(path)
    if with_account:
        implementation, _, _, arms, method = adapter._definition("public_seat_static")
        reg = json.loads(b["registration"].read_text(encoding="utf-8"))
        binding = {"method_id": method, "config": reg["config"], "registration":
            adapter._file_ref(b["registration"]), "run_directory": str(b["run"].resolve())}
        cases = {}
        for arm in arms:
            state = new_state(100000)
            order_id = "2026-10-19:"+A+":buy"
            state.update(day="2026-10-19", cash=9_799_497, positions={A: [{"qty": 200, "available": "2026-10-20"}]},
                prices={A: 1000}, orders={order_id: {"id": order_id, "day": "2026-10-19", "symbol": A,
                    "side": "buy", "qty": 200, "filled": 200, "fees": 503, "reserve": 0, "status": "filled"}},
                fills={order_id+":0": {"order_id": order_id, "day": "2026-10-19", "symbol": A,
                    "side": "buy", "qty": 100, "price": 1000, "fees": 501},
                    order_id+":1": {"order_id": order_id, "day": "2026-10-19", "symbol": A,
                    "side": "buy", "qty": 100, "price": 1000, "fees": 2}})
            cases[arm] = {"context": implementation._context(b["run"].resolve(), arm).to_dict(), "state": state,
                "daily": [{"date": "2026-10-19", "cash": 97994.97, "equity": 99994.97, "fees": 5.03}]}
        prior = {"publication": adapter._file_ref(first_path), "bundle": first_pub["bundle"],
            "decision_day": "2026-10-16", "execution_day": "2026-10-19", "published_at": first_pub["published_at"]}
        journal = {"schema": implementation.JOURNAL_SCHEMA, "binding_sha256": adapter._hash(binding),
            "publication": prior, "raw_sources_sha256": adapter._hash([]), "committed_at": "2026-10-19T18:40:00+08:00",
            "post_case_sha256": {key: adapter._hash(case) for key,case in cases.items()}, **adapter.FLAGS}
        seal = save(b["run"]/"journal"/"saved.json", journal)
        save(b["run"]/"head.json", {"schema": implementation.SCHEMA, "binding": binding, "cases": cases,
            "consumed_publications": [prior], "journal_files": [seal], "raw_sources": [], **adapter.FLAGS})
    adapter.build_source("public_seat_static", b["registration"], b["publications"], b["run"], b["source"], current=NOW)
    destination = tmp_path.parent/(tmp_path.name+"-"+name+"-opening")
    _, product = adapter.build_opening(b["source"], destination, current=NOW)
    assert product["preview_count"] == 0 and product["opening_pack"] is None
    return {"id": name, "family": "registered_research", "source": str(b["source"]),
        "adopt": {"result": str(destination/"result.json")}}, b


def test_unbound_next_candidate_keeps_exact_old_ledger_while_missing_or_replaced_source_is_unknown(tmp_path, monkeypatch):
    data = tmp_path/"data"
    monkeypatch.setattr(core, "DATA_ROOT", data)
    route, b = saved_route(tmp_path, "with_book", with_account=True)
    missing_route, missing = saved_route(tmp_path, "no_book", with_account=False)
    core.register(data/RUN_RELATIVE, [route, missing_route], current=NOW)
    before = {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = m.opening_daily_ledger(data, "with_book", b["arms"][0], now=NOW)
    assert result["status"] == "saved_completed_research_ledger"
    assert result["source_day"] == "2026-10-20" and result["planned_execution_day"] == "2026-10-21"
    assert result["ledger_day"] == "2026-10-19" and result["daily_summary"]["date"] == "2026-10-19"
    assert result["next_target_status"] == "not_ready" and result["order_count"] == 1 and result["fill_count"] == 2
    assert result["cash"]["total_cents"] == 9_799_497 and result["daily_summary"]["fees"] == 5.03
    assert result["positions"][0]["quantity"] == 200 and result["positions"][0]["sellable_on_ledger_day"] == 0
    assert "intent" not in result and "target_weights" not in result and result["executed_decision"] is None
    unknown = m.opening_daily_ledger(data, "no_book", missing["arms"][0], now=NOW)
    assert unknown["status"] == "unavailable" and unknown["cash"] is None and unknown["fills"] is None
    assert before == {p:p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    source = b["source"]/"head.json"
    source.write_bytes(source.read_bytes()+b"\n")
    stale = m.opening_daily_ledger(data, "with_book", b["arms"][0], now=NOW)
    assert stale["status"] == "unavailable" and stale["cash"] is None and stale["fills"] is None
