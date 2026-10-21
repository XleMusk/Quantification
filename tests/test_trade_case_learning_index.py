import json
from hashlib import sha256
from pathlib import Path

import pytest

from trading.research.trade_case_learning_index import TradeCaseLearningIndex


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def package(tmp_path):
    old_id = "20260901_000011.SZSE"
    old_fact = tmp_path / "old_fact.json"
    fact_hash = _save(old_fact, {"case_id": old_id, "seat_events": [
        {"seat_name": "机构专用", "published_at": None, "identity_usable": False}]})
    old_review = tmp_path / "old_review.json"
    _save(old_review, {"reviews": [{"case_id": old_id, "case_file": str(old_fact),
        "case_sha256": fact_hash, "summary": "保留失败与未知。"}]})
    legacy = tmp_path / "manifest.json"
    _save(legacy, {"schema": "individual_case_learning_book_v1", "cases": [
        {"case_id": old_id, "kind": "seat_disclosure", "symbol": "000011.SZSE",
         "day": "2026-09-01", "json_file": str(old_fact),
         "individual_interpretation_file": str(old_review),
         "individual_interpretation_status": "agent_interpreted"},
        {"case_id": "leader", "kind": "leader_price_path", "symbol": "000011.SZSE",
         "day": "2026-09-01", "json_file": str(tmp_path / "not_opened_leader.json")},
        {"case_id": "public", "kind": "public_source", "symbol": None,
         "day": None, "json_file": str(tmp_path / "not_opened_public.json")}]})
    new_ids = ["20261015_002343.SZSE", "20261015_301569.SZSE", "20261015_920627.BJ"]
    queue = tmp_path / "queue.json"
    entries = [{"case_id": identity, "symbol": identity.split("_", 1)[1],
        "date": "2026-10-15", "scope_status": "supported_sh_sz_code_proxy"
        if i < 2 else "bse_research_only"} for i, identity in enumerate(new_ids)]
    queue_hash = _save(queue, {"cases": entries})
    review = tmp_path / "review.json"
    evidence = tmp_path / "evidence.json"
    evidence_hash = _save(evidence, {"records": [{"case_id": new_ids[-1],
        "summary": [{"reported_price": 20, "published_at": None}],
        "independent_daily_bars": None}]})
    bar = {"tradestatus": "1", "close": "10.00"}
    review_hash = _save(review, {"source_available_at": "2026-10-16T05:36:41+08:00",
        "published_at": None, "evidence_file": str(evidence),
        "evidence_sha256": evidence_hash, "reviews": [
        {"case_id": new_ids[0], "symbol": entries[0]["symbol"], "summary": "不是个人交割单。",
         "case_evidence": {"raw_bars": {"2026-10-14": bar, "2026-10-15": bar},
                           "normalized_events": [{"net_cny": None}]}},
        {"case_id": new_ids[1], "symbol": entries[1]["symbol"], "summary": "上市首日缺前日。",
         "case_evidence": {"raw_bars": {"2026-10-14": None, "2026-10-15": bar}}},
        {"case_id": new_ids[2], "symbol": entries[2]["symbol"],
         "source_interpretation_completed": True, "independent_daily_bars": None,
         "facts": "只读源报价，尚无独立价格路径。"}]})
    progress = tmp_path / "progress.json"
    progress_value = {"schema": "public_seat_source_case_reading_completion_v1",
        "completed_at": "2026-10-16T15:21:47+08:00", "queue_file": str(queue),
        "queue_sha256": queue_hash, "source_case_readings_completed": 3,
        "batches": [{"batch": "test", "ids": new_ids,
                     "files": [{"file": str(review), "sha256": review_hash}]}]}
    _save(progress, progress_value)
    return {"legacy": legacy, "progress": progress, "review": review,
            "old_fact": old_fact, "evidence": evidence,
            "new_ids": new_ids, "old_id": old_id, "progress_value": progress_value}


def _index(package):
    return TradeCaseLearningIndex(package["legacy"], package["progress"])


def test_union_keeps_different_denominators_and_missing_prices(package):
    index = _index(package)
    summary = index.summary()
    assert summary["archive_records"] == 6
    assert summary["records_by_kind"] == {"seat_disclosure": 1, "leader_price_path": 1,
                                          "public_source": 1, "seat_source_snapshot": 3}
    assert summary["seat_source_interpretations"] == 4
    assert summary["complete_price_path_reviews"] is None
    assert summary["attributable_person_count"] is None
    assert summary["incremental_price_status_counts"] == {
        "embedded_pair_present_not_full_trade_path": 1,
        "embedded_price_slots_missing": 1, "independent_price_path_pending": 1}
    assert summary["execution_admitted"] is False
    assert not (package["legacy"].parent / "not_opened_leader.json").exists()


def test_filters_are_exact_and_pagination_preserves_source_order(package):
    index = _index(package)
    rows = index.list_cases(day="2026-10-15", kind="seat_source_snapshot", offset=1, limit=1)
    assert rows["total_filtered"] == 3
    assert rows["cases"][0]["case_id"] == package["new_ids"][1]
    assert index.list_cases(scope="bse_research_only")["total_filtered"] == 1
    assert index.list_cases(symbol="002343")["total_filtered"] == 1
    assert index.list_cases(symbol="002343.SZSE")["total_filtered"] == 1
    with pytest.raises(ValueError):
        index.list_cases(limit=0)


def test_new_detail_keeps_body_null_clock_and_embedded_unknown(package):
    index = _index(package)
    first = index.detail(package["new_ids"][0])
    assert first["interpretation"]["summary"] == "不是个人交割单。"
    assert first["source_facts"]["normalized_events"][0]["net_cny"] is None
    assert first["source_metadata"]["published_at"] is None
    assert first["source_metadata"]["source_available_at"] == "2026-10-16T05:36:41+08:00"
    missing = index.detail(package["new_ids"][1])
    assert missing["source_facts"]["raw_bars"]["2026-10-14"] is None
    first["interpretation"]["summary"] = "caller mutation"
    assert index.detail(package["new_ids"][0])["interpretation"]["summary"] != "caller mutation"
    with pytest.raises(KeyError):
        index.detail("../../not_a_case")


def test_external_scope_evidence_is_not_a_daily_price_or_person(package):
    index = _index(package)
    detail = index.detail(package["new_ids"][-1])
    assert detail["source_facts"]["independent_daily_bars"] is None
    assert detail["source_facts"]["summary"][0]["reported_price"] == 20
    assert detail["case"]["price_path_review_complete"] is False
    assert detail["case"]["price_path_status"] == "independent_price_path_pending"
    package["evidence"].write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        index.detail(package["new_ids"][-1])


def test_old_detail_reads_only_requested_fact_and_preserves_original_body(package):
    index = _index(package)
    detail = index.detail(package["old_id"])
    assert detail["interpretation"]["summary"] == "保留失败与未知。"
    assert detail["source_facts"]["seat_events"][0]["published_at"] is None
    assert detail["source_facts"]["seat_events"][0]["identity_usable"] is False
    _save(package["old_fact"], {"case_id": package["old_id"], "seat_events": []})
    with pytest.raises(ValueError, match="source changed"):
        index.detail(package["old_id"])


def test_changed_batch_or_duplicate_identity_is_rejected(package):
    package["review"].write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        _index(package)


def test_incomplete_receipt_cannot_claim_full_queue_read(package):
    doc = json.loads(package["review"].read_text("utf-8"))
    doc["reviews"] = doc["reviews"][:-1]
    review_hash = _save(package["review"], doc)
    progress = package["progress_value"]
    progress["batches"][0]["ids"] = progress["batches"][0]["ids"][:-1]
    progress["batches"][0]["files"][0]["sha256"] = review_hash
    _save(package["progress"], progress)
    with pytest.raises(ValueError, match="exact fixed queue"):
        _index(package)


def test_six_digit_symbol_search_keeps_full_suffix_exact_and_empty_unfiltered(package):
    manifest = json.loads(package["legacy"].read_text("utf-8"))
    manifest["cases"].append({"case_id": "other_suffix", "kind": "leader_price_path",
        "symbol": "000011.SSE", "day": "2026-09-01",
        "json_file": str(package["legacy"].parent / "not_opened_other.json")})
    _save(package["legacy"], manifest)
    index = _index(package)
    six_digit = index.list_cases(symbol="000011")
    assert six_digit["total_filtered"] == 3
    assert {row["kind"] for row in six_digit["cases"]} == {"seat_disclosure", "leader_price_path"}
    assert index.list_cases(symbol="000011.SZSE")["total_filtered"] == 2
    assert index.list_cases(symbol="000011.SSE")["total_filtered"] == 1
    assert index.list_cases(symbol="999999")["cases"] == []
    assert index.list_cases(symbol="")["total_filtered"] == 7
    assert index.list_cases(symbol=None)["total_filtered"] == 7
