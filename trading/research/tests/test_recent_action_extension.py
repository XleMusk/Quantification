"""Synthetic scoped extension contracts; no provider, price history or Engine run."""
from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.ledger import new_state
from trading.research.recent_action_extension import load_extension, extension_readiness, SCHEMA
from trading.research.tests.test_stockdb_action_accounting import fixture_pair, SYMBOL, DAYS


START, END = "2026-06-26", "2026-06-29"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def pin(path):
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def fixture_extension(tmp_path):
    event, ref = fixture_pair(tmp_path)
    old = pd.read_parquet(event / "canonical_actions.parquet").to_dict("records")[0]
    pd.DataFrame([old]).to_parquet(event / "input_reported_actions.parquet", index=False)
    events = json.loads((event / "result.json").read_text())
    events["files"]["input_reported_actions.parquet"] = pin(event / "input_reported_actions.parquet")["sha256"]
    write(event / "result.json", events)
    (ref / "input_events.json").write_bytes((event / "result.json").read_bytes())
    refs = json.loads((ref / "result.json").read_text())
    refs["files"]["input_events.json"] = pin(ref / "input_events.json")["sha256"]
    write(ref / "result.json", refs)
    root = tmp_path / "fresh"
    (root / "receipts").mkdir(parents=True)
    (root / "actions" / SYMBOL).mkdir(parents=True)
    pd.DataFrame([{"code": "sh.600000", "symbol": SYMBOL}]).to_csv(root / "universe.csv", index=False)
    pd.DataFrame({"date": DAYS + [START, END]}).to_csv(root / "calendar.csv", index=False)
    pdf = tmp_path / "primary.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic reviewed document")
    action = {"code": "sh.600000", "symbol": SYMBOL, "dividRegistDate": START,
        "dividOperateDate": END, "dividPayDate": END, "dividStockMarketDate": "",
        "dividCashPsBeforeTax": "0.05", "dividStocksPs": "0", "dividReserveToStockPs": "0",
        "terms_basis": "issuer_confirmed_cash_only_terms", "source_file": str(pdf),
        "source_sha256": pin(pdf)["sha256"], "source_url": "https://example.test/issuer.pdf",
        "source_date": "2026-06-22", "reviewed_at": "2026-06-30T10:00:00+08:00",
        "review_status": "manual_fulltext_and_pages_reviewed", "review_note": "Synthetic full cash scheme and dates"}
    raw = {k: action[k] for k in old}
    raw["dividReserveToStockPs"] = ""
    payload = root / "actions" / SYMBOL / "2026.csv"
    pd.DataFrame([old, raw]).to_csv(payload, index=False)
    receipt = {"symbol": SYMBOL, "tasks": {"actions_2026": {
        "path": f"actions/{SYMBOL}/2026.csv", "retrieved_at": "2026-06-30T09:00:00+08:00",
        "quality": {"rows": 2}, "sha256": pin(payload)["sha256"], "source_error_code": "0"}}}
    write(root / "receipts" / f"{SYMBOL}.json", receipt)
    plan = {"source": "BaoStock", "summary_stage": "actions", "end": END, "symbols": 1,
        "expected_queries": 1, "universe_sha256": pin(root / "universe.csv")["sha256"],
        "calendar_sha256": pin(root / "calendar.csv")["sha256"]}
    write(root / "collection_plan.json", plan)
    write(root / "status.json", {"status": "source_cache_complete", "error": None,
        "summary_stage": "actions", "end": END, "complete_symbols": 1, "action_queries": 1, "action_rows": 2})
    calendar = tmp_path / "calendar.json"
    write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026, "closed_ranges": []})
    evidence = {**action, "reference_basis": "issuer_confirmed_standard_cash_only_reference",
        "reference_adjustment_cash_ps": ".05", "reference_adjustment_share_ratio": "0"}
    bundle = {"schema": SCHEMA, "created_at": "2026-06-30T10:01:00+08:00",
        "base_event": pin(event / "result.json"), "base_reference": pin(ref / "result.json"),
        "calendar": pin(calendar), "window_start": START, "window_end": END,
        "scope_symbols": [SYMBOL], "provider_root": str(root),
        "provider_files": {n: pin(root / n) for n in ("collection_plan.json", "universe.csv", "calendar.csv", "status.json")},
        "queries": [{"symbol": SYMBOL, "year": 2026,
            "receipt": pin(root / "receipts" / f"{SYMBOL}.json"), "payload": pin(payload)}],
        "actions": [action], "reference_evidence": [evidence],
        **{k: False for k in ("execution_admitted", "selection_enabled", "strategy_validated", "pit_verified", "returns_reportable")}}
    path = tmp_path / "bundle.json"
    write(path, bundle)
    return event, ref, path, bundle


def load(parts, **kwargs):
    return load_extension(*parts[:3], symbols=[SYMBOL, "600001.SSE"],
        raw_days=DAYS + [START, END], replay_days=DAYS[1:] + [START, END], **kwargs)


def test_scoped_cash_only_prefix_and_unknown_are_preserved(tmp_path):
    parts = fixture_extension(tmp_path)
    overlay = load(parts)
    assert len(overlay.actions) == 2
    assert overlay.actions[0]["dividReserveToStockPs"] == "0.45"
    assert overlay.actions[1]["dividReserveToStockPs"] == "0"
    assert pd.read_csv(parts[3]["queries"][0]["payload"]["path"], keep_default_na=False).iloc[1].dividReserveToStockPs == ""
    saved = new_state(1000)
    saved["positions"] = {SYMBOL: [{"qty": 900, "available": START}]}
    saved["entitlements"][f"{SYMBOL}:{END}"] = 900
    rows = {START: {SYMBOL: {"close": 5.64}}, END: {SYMBOL: {"preclose": 5.59,
        "open": 5.59, "close": 5.59, "high": 5.59, "low": 5.59, "tradestatus": 1, "isST": 0}}}
    before = deepcopy(saved)
    overlay.validate(saved, END, rows)
    assert saved == before and overlay.checks[-1]["reference_formula_status"] == "reference_match"
    assert overlay.start == DAYS[1] and overlay.provenance["full_market_recent_coverage"] is False
    assert overlay.provenance["returns_reportable"] is False
    saved["positions"]["600001.SSE"] = [{"qty": 100, "available": START}]
    with pytest.raises(ValueError, match="outside freshly queried"):
        overlay.validate(saved, END, rows)


def test_unreviewed_new_event_and_old_economic_revision_are_rejected(tmp_path):
    parts = fixture_extension(tmp_path)
    parts[3]["actions"] = []
    parts[3]["reference_evidence"] = []
    write(parts[2], parts[3])
    with pytest.raises(ValueError, match="Every new registration/ex"):
        load(parts)
    second = fixture_extension(tmp_path / "second")
    query = second[3]["queries"][0]
    payload = query["payload"]["path"]
    rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
    rows.loc[0, "dividCashPsBeforeTax"] = ".21"
    rows.to_csv(payload, index=False)
    from pathlib import Path
    query["payload"] = pin(Path(payload))
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"]["sha256"] = query["payload"]["sha256"]
    query["receipt"] = write(receipt_path, receipt)
    write(second[2], second[3])
    with pytest.raises(ValueError, match="economically revised"):
        load(second)


def test_preclose_query_and_missing_exact_session_are_rejected(tmp_path):
    parts = fixture_extension(tmp_path)
    from pathlib import Path
    query = parts[3]["queries"][0]
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"]["retrieved_at"] = END + "T14:59:00+08:00"
    query["receipt"] = write(receipt_path, receipt)
    write(parts[2], parts[3])
    with pytest.raises(ValueError, match="Fresh post-close"):
        load(parts)
    with pytest.raises(ValueError, match="contiguous raw/session"):
        load_extension(*parts[:3], symbols=[SYMBOL], raw_days=[DAYS[-1], END], replay_days=[END])


def test_recent_record_with_unknown_ex_date_cannot_disappear_as_old_event(tmp_path):
    from pathlib import Path
    parts = fixture_extension(tmp_path)
    query = parts[3]["queries"][0]
    payload = Path(query["payload"]["path"])
    rows = pd.read_csv(payload, dtype=str, keep_default_na=False)
    rows.loc[1, "dividOperateDate"] = ""
    rows.to_csv(payload, index=False)
    query["payload"] = pin(payload)
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"]["sha256"] = query["payload"]["sha256"]
    query["receipt"] = write(receipt_path, receipt)
    write(parts[2], parts[3])
    with pytest.raises(ValueError, match="unknown ex-date"):
        load(parts)


def test_forward_inception_and_cumulative_execution_prefix_use_their_own_context(tmp_path):
    """Synthetic Fri->Mon first account, then exact Tue cumulative resume."""
    from pathlib import Path
    parts = fixture_extension(tmp_path)
    value = parts[3]
    value["window_end"] = "2026-06-30"
    value["created_at"] = "2026-07-01T10:01:00+08:00"
    root = Path(value["provider_root"])
    for name in ("collection_plan.json", "status.json"):
        saved = json.loads((root / name).read_text())
        saved["end"] = value["window_end"]
        value["provider_files"][name] = write(root / name, saved)
    pd.DataFrame({"date": DAYS + [START, END, "2026-06-30"]}).to_csv(root / "calendar.csv", index=False)
    value["provider_files"]["calendar.csv"] = pin(root / "calendar.csv")
    plan = json.loads((root / "collection_plan.json").read_text())
    plan["calendar_sha256"] = value["provider_files"]["calendar.csv"]["sha256"]
    value["provider_files"]["collection_plan.json"] = write(root / "collection_plan.json", plan)
    query = value["queries"][0]
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"]["retrieved_at"] = "2026-07-01T09:00:00+08:00"
    query["receipt"] = write(receipt_path, receipt)
    write(parts[2], value)
    first = load_extension(*parts[:3], symbols=[SYMBOL], raw_days=[START, END], replay_days=[END])
    assert first.start == END and first.previous == {END: START}
    resumed = load_extension(*parts[:3], symbols=[SYMBOL], raw_days=[START, END, "2026-06-30"],
        replay_days=[END, "2026-06-30"])
    assert resumed.start == END and resumed.previous["2026-06-30"] == END
    assert resumed.actions == first.actions


def test_readiness_missing_execution_date_and_indirect_mutation_prevent_cached_success(tmp_path):
    from pathlib import Path
    parts = fixture_extension(tmp_path)
    calendar = parts[3]["calendar"]["path"]
    ready = extension_readiness(parts[2], *parts[:2], calendar, through=END)
    assert ready["ready_for_day"] is True and ready["full_accounting_validation_performed"] is False
    assert ready["engine_called"] is False
    waiting = extension_readiness(parts[2], *parts[:2], calendar, through="2026-06-30")
    assert waiting["ready_for_day"] is False
    assert waiting["missing_action_sessions"] == ["2026-06-30"]
    assert waiting["source_empty_inferred"] is False
    assert str(Path(parts[3]["queries"][0]["payload"]["path"]).parent) in waiting["input_directories"]
    Path(parts[3]["queries"][0]["payload"]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source hash changed"):
        extension_readiness(parts[2], *parts[:2], calendar, through=END)
