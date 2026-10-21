"""Registered producer case directories stay exact and traversal-safe."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.research import opening_daily_detail as detail
from trading.research.tests.test_opening_daily_detail import saved, write, NOW, D, E


def test_registered_hashed_case_detail_and_resealed_foreign_preview_are_distinct(saved):
    kind, key = "leader_type_router_runtime_repair", "single_active_type__unguarded__causal_first_seen_cap20"
    strategy = kind + ":" + key
    parent = saved["data"] / "existing_registered_opening"
    child = parent / ("case_" + sha256(key.encode()).hexdigest()[:24])
    child.mkdir(parents=True)
    (child / "target_holdings.csv").write_bytes(saved["targets"].read_bytes())
    product = saved["head"]["routes"]["fixed"]["product"]
    original_refs = product["refs"]
    source_head = product["source_head"]
    intent = json.loads(Path(original_refs["intent"]["path"]).read_text())
    intent.update(schema="saved_registered_research_account_intent_v1", case_id=key,
        route_kind=kind, strategy_id=strategy, refs={"head": source_head})
    write(child / "intent.json", intent)
    preview = json.loads(Path(original_refs["preview"]["path"]).read_text())
    preview.update(strategy_id=strategy, source_refs={"head": source_head},
        saved_intent=str(child / "intent.json"),
        artifacts={"provisional_target_holdings": str(child / "target_holdings.csv")})
    write(child / "result.json", preview)
    saved["pack"]["strategies"] = [{"strategy_id": strategy, "preview": str(child / "result.json")}]
    saved["pack"]["ledger_preflight_by_strategy"] = {strategy:
        next(iter(saved["pack"]["ledger_preflight_by_strategy"].values()))}
    for path in (saved["orders"], Path(saved["pack"]["artifacts"]["ledger_preflight_by_strategy"])):
        path.write_text(path.read_text(encoding="utf-8-sig").replace("synthetic_fixed", strategy), encoding="utf-8-sig")
    result = {"source_family": "registered_research", "route_kind": kind,
        "source_cutoff": D, "execution_day": E, "source_run_directory": str(saved["source"]),
        "source_head_sha256": source_head["sha256"], "opening_pack": str(saved["pack_path"]),
        "cases": [{"case_id": key, "strategy_id": strategy, "status": "paper_preview_only",
            "preview": str(child / "result.json")}], **detail.FLAGS}
    result_path = parent / "result.json"
    product["refs"] = {"result": write(result_path, result), "opening_pack": write(saved["pack_path"], saved["pack"])}
    registration = json.loads((saved["directory"] / "registration.json").read_text())
    registration["routes"][0]["family"] = "registered_research"
    saved["head"]["registration"] = write(saved["directory"] / "registration.json", registration)
    write(saved["directory"] / "head.json", saved["head"])
    before = {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}
    value = detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
    assert value["status"] == "saved_case_details" and len(value["cases"]) == 1
    assert value["cases"][0]["case_id"] == key
    assert value["cases"][0]["intent"]["kind"] == "NO_CHANGE"
    assert value["cases"][0]["intent"]["weights"] is None
    assert before == {p: p.read_bytes() for p in saved["data"].rglob("*") if p.is_file()}
    assert detail._case_directory("safe_case", family="eight_type") == "safe_case"
    assert detail._case_directory("../outside", family="registered_research") == "case_" + sha256(b"../outside").hexdigest()[:24]
    # Even a resealed parent cannot point a case at another valid JSON file.
    result["cases"][0]["preview"] = original_refs["preview"]["path"]
    product["refs"]["result"] = write(result_path, result)
    write(saved["directory"] / "head.json", saved["head"])
    with pytest.raises(ValueError, match="escaped its registered fixed path"):
        detail.opening_daily_detail(saved["data"], "fixed", now=NOW)
