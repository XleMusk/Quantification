"""One synthetic missing-event regression, with no network or Engine run."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.ledger import new_state
from trading.research import official_action_supplement as official
from trading.research.tests.test_recent_action_extension import (
    fixture_extension, load, pin, write, SYMBOL, START, END,
)


def missing_provider_case(tmp_path):
    parts = fixture_extension(tmp_path / "original")
    value, query = parts[3], parts[3]["queries"][0]
    action = deepcopy(value["actions"][0])
    payload = Path(query["payload"]["path"])
    pd.read_csv(payload, dtype=str, keep_default_na=False).iloc[:1].to_csv(payload, index=False)
    query["payload"] = pin(payload)
    receipt_path = Path(query["receipt"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"].update(sha256=query["payload"]["sha256"], quality={"rows": 1})
    query["receipt"] = write(receipt_path, receipt)
    status_path = Path(value["provider_root"]) / "status.json"
    status = json.loads(status_path.read_text())
    status["action_rows"] = 1
    value["provider_files"]["status.json"] = write(status_path, status)
    value["actions"], value["reference_evidence"] = [], []
    write(parts[2], value)
    original_bundle, original_payload = parts[2].read_bytes(), payload.read_bytes()
    source = tmp_path / "official_source"
    source.mkdir()
    pdf = source / "implementation.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic complete implementation notice")
    page = source / "page-1.png"
    page.write_bytes(b"synthetic rendered page")
    clock = "2026-06-30T10:00:00+08:00"
    action.update(dividCashPsBeforeTax="0.08008", source_file=str(pdf),
        source_sha256=pin(pdf)["sha256"], source_url="https://static.cninfo.com.cn/synthetic.PDF",
        reviewed_at=clock, review_note="Synthetic full notice: actual holder cash differs from stated reference cash")
    reference = {**action, "reference_basis": "issuer_confirmed_differential_cash_only_reference",
        "reference_adjustment_cash_ps": "0.08000", "reference_adjustment_share_ratio": "0",
        "stated_reference_formula": {"kind": "prior_close_minus_issuer_stated_diluted_cash",
            "total_shares": "19862892778", "participating_shares": "19841951478",
            "excluded_repurchase_shares": "20941300", "issuer_stated_diluted_cash_ps": "0.08000",
            "literal_formula": "prior_close - 0.08000; actual holder cash remains 0.08008", "page": 1}}
    receipt = source / "documents.json"
    write(receipt, {"documents": [{"file": str(pdf), "page_count": 1,
        "source": {"status_code": 200, "status": "downloaded", "sha256": pin(pdf)["sha256"],
            "url": action["source_url"], "bytes": pdf.stat().st_size,
            "retrieved_at": "2026-06-30T09:59:00+08:00"}}]})
    review = source / "review.json"
    write(review, {"schema": official.SCHEMA, "method": official.METHOD, "action": action,
        "reference": reference, "retrieval_receipt": pin(receipt), "rendered_pages": [pin(page)],
        "reviewed_page_numbers": [1], "review_available_at": clock, **official.FLAGS})
    return parts, review, pdf, payload, original_bundle, original_payload


def test_missing_provider_differential_cash_and_actual_knowledge_remain_separate(tmp_path):
    parts, review, pdf, payload, original_bundle, original_payload = missing_provider_case(tmp_path)
    saved = new_state(1000)
    saved["pending_targets"] = {SYMBOL: .2}
    saved["entitlements"][f"{SYMBOL}:{END}"] = 0
    rows = {START: {SYMBOL: {"close": "5.04"}}, END: {SYMBOL: {
        "preclose": "4.96", "open": "4.96", "high": "4.96", "low": "4.96", "close": "4.96",
        "tradestatus": "1", "isST": "0"}}}
    before = deepcopy(saved)
    with pytest.raises(ValueError, match="missing_source_event"):
        load(parts).validate(saved, END, rows)
    prospective = official.build(parts[2], [review], tmp_path / "prospective")
    prospective_parts = (*parts[:2], Path(prospective["bundle"]))
    known = load(prospective_parts)
    assert known.provenance["official_event_knowledge"][0]["first_knowledge_eligible_session"] == "2026-07-01"
    with pytest.raises(ValueError, match="not actually available"):
        known.validate(saved, END, rows)
    diagnostic = official.build(parts[2], [review], tmp_path / "diagnostic",
        knowledge_mode="posthoc_component_diagnostic")
    diagnosed = load((*parts[:2], Path(diagnostic["bundle"])))
    diagnosed.validate(saved, END, rows)
    assert diagnosed.actions[-1]["dividCashPsBeforeTax"] == "0.08008"
    assert diagnosed.evidence[-1]["reference_adjustment_cash_ps"] == "0.08000"
    assert diagnosed.checks[-1]["expected_reference_rmb"] == "4.96000"
    assert diagnosed.checks[-1]["reference_formula_status"] == "reference_match"
    assert saved == before and parts[2].read_bytes() == original_bundle and payload.read_bytes() == original_payload
    pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="source hash changed"):
        load((*parts[:2], Path(diagnostic["bundle"])))


def test_refresh_inherits_registered_official_review_when_provider_later_reports_it(tmp_path, monkeypatch):
    import shutil
    from trading.research import recent_action_sources, recent_action_refresh
    from trading.research.recent_action_extension import load_extension
    from trading.research.tests.test_recent_action_extension import DAYS

    parts, review, pdf, _, _, _ = missing_provider_case(tmp_path)
    prior = official.build(parts[2], [review], tmp_path / "registered")
    before = review.read_bytes()
    current = tmp_path / "new_day_provider"
    shutil.copytree(Path(parts[3]["provider_root"]), current)
    days = DAYS + [START, END, "2026-06-30", "2026-07-01"]
    pd.DataFrame({"date": days}).to_csv(current / "calendar.csv", index=False)
    raw = current / "actions" / SYMBOL / "2026.csv"
    rows = pd.read_csv(raw, dtype=str, keep_default_na=False)
    event = json.loads(review.read_text())["action"]
    rows = pd.concat([rows, pd.DataFrame([{k: event.get(k, "") for k in rows.columns}])], ignore_index=True)
    rows.to_csv(raw, index=False)
    receipt_path = current / "receipts" / (SYMBOL + ".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["tasks"]["actions_2026"].update(retrieved_at="2026-07-02T09:00:00+08:00",
        sha256=pin(raw)["sha256"], quality={"rows": 2})
    write(receipt_path, receipt)
    for name in ("collection_plan.json", "status.json"):
        record = json.loads((current / name).read_text())
        record["end"] = "2026-07-01"
        if name == "collection_plan.json":
            record["calendar_sha256"] = pin(current / "calendar.csv")["sha256"]
        else:
            record["action_rows"] = 2
        write(current / name, record)
    calls = []
    monkeypatch.setattr(recent_action_sources, "prepare", lambda *a, **k:
        calls.append("prepare") or {"provider_root": str(current)})
    monkeypatch.setattr(recent_action_sources, "collect_prepared", lambda *a, **k:
        calls.append("collect") or {"source_complete": True})
    result = recent_action_refresh.run("2026-07-01", tmp_path / "close", parts[3]["calendar"]["path"],
        tmp_path / "refresh", base_event_view=parts[0], base_reference_view=parts[1], prior_bundle=prior["bundle"])
    assert result["status"] == "ready" and calls == ["prepare", "collect"]
    saved = json.loads(Path(result["bundle"]).read_text())
    assert saved["window_end"] == "2026-07-01" and saved["actions"] == []
    assert saved["official_event_supplements"] == json.loads(Path(prior["bundle"]).read_text())["official_event_supplements"]
    assert saved["official_event_supplement_mode"] == "prospective_actual_knowledge"
    accounting = load_extension(parts[0], parts[1], result["bundle"], symbols=[SYMBOL],
        raw_days=days, replay_days=days[1:])
    assert len([a for a in accounting.actions if a["dividOperateDate"] == END]) == 1
    assert accounting.actions[-1]["dividCashPsBeforeTax"] == "0.08008"
    assert accounting.evidence[-1]["reference_adjustment_cash_ps"] == "0.08000"
    assert review.read_bytes() == before
    assert accounting.provenance["official_event_knowledge"][0]["review_available_at"] == "2026-06-30T10:00:00+08:00"
    pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="source hash changed"):
        recent_action_refresh.run("2026-07-01", tmp_path / "close", parts[3]["calendar"]["path"],
            tmp_path / "changed_evidence", base_event_view=parts[0], base_reference_view=parts[1], prior_bundle=prior["bundle"])
    assert calls == ["prepare", "collect"] and not (tmp_path / "changed_evidence").exists()
