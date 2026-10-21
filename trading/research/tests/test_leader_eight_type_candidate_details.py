"""Read saved candidate evidence only; no producers, prices or account replay."""
from copy import deepcopy

import pytest

from trading.research import leader_eight_type_paper_view as m
from trading.research.tests.test_leader_eight_type_paper_view import NOW, save, snapshot, write

DAY = "2026-10-15"
A, B = "000001.SZSE", "000002.SZSE"


def enrich(item):
    for index, candidate in enumerate(item["cap_candidates"]):
        candidate.update(name="原名" + str(index + 1), board="main", stage=index,
            amount_cny=100000000. + index, pct_chg=-1.25 + index, amount_rank_pct=.99,
            evidence={"daily_rebound": True, "definition": "saved proxy", "unknown_peer": None},
            invalidation=["原逐票条件"], ipo_feature_support=True,
            identity_status="daily_price_volume_proxy_not_verified_market_or_sector_leader", trade_ready=False)
    # Opposite array order ensures that references are joined by identity, never zipped.
    symbols = [B, A]
    item["candidate_reference_and_setup"] = [{"symbol": symbol, "source_day": DAY,
        "reference_close_cny": 13.87 if symbol == A else 20., "setup_low_cny": 12.97 if symbol == A else 19.,
        "sma20_observed": None, "sma60_observed": 11., "reference_is_order_price": False,
        "reference_is_actual_held_entry": False, "missing_structural_fields_stay_unknown": True} for symbol in symbols]
    item["current_raw_issues"] = [{"day": DAY, "symbol": B, "reason": "saved_reference_unknown"},
                                  {"day": DAY, "symbol": B, "reason": "saved_status_unknown"}]
    item["current_reference_evidence"] = [{"day": DAY, "symbol": symbol, "status": "consistent_proxy",
        "previous_day_required": "2026-10-14", "preclose_to_previous_close_ratio_minus1": 0.,
        "absence_of_corporate_actions_proven": False} for symbol in symbols]
    item["IPO_evidence"] = [{"day": DAY, "symbol": symbol, "decision_as_of": DAY+"T21:00:00+08:00",
        "ipo_date": "2000-01-01", "ipo_window_feature_support": True, "engine_block_reason": None,
        "available_at": "2026-10-08T12:00:00+08:00", "pit_verified": False, "execution_admitted": False} for symbol in symbols]


def load(folder):
    return m.read_saved_plan(folder / "paper_plan.json", now=NOW)


def test_details_keep_original_cap_order_exact_saved_setup_and_per_symbol_checks(tmp_path):
    folder, record = save(tmp_path)
    original = record["cases"][0]
    enrich(original)
    write(folder / "paper_plan.json", record)
    before = snapshot(tmp_path)
    row = load(folder)["rows"][0]
    assert row["candidates"] == [{"symbol": A, "score": 99}, {"symbol": B, "score": 98}]
    details = row["candidate_details"]
    assert [item["symbol"] for item in details] == [A, B]
    assert details[0]["evidence"] == original["cap_candidates"][0]["evidence"]
    assert details[0]["invalidation"] == ["原逐票条件"] and details[0]["name"] == "原名1"
    assert details[0]["setup_reference"] == original["candidate_reference_and_setup"][1]
    assert details[0]["setup_reference"]["setup_low_cny"] == 12.97
    assert "previous_open" not in details[0]["evidence"]  # raw low is not the rebound rule's prior open.
    assert details[0]["current_raw_issues"] == [] and len(details[1]["current_raw_issues"]) == 2
    assert details[0]["current_reference_evidence"]["symbol"] == A
    assert details[0]["IPO_evidence"]["symbol"] == A
    assert all(item["trade_ready"] is False and item["checks_status"] == "saved_entry_checks" for item in details)
    assert row["target_weights"] == original["target_weights"] and snapshot(tmp_path) == before


def test_unknown_and_legacy_absent_fields_stay_null_without_zero_or_price_fill(tmp_path):
    folder, record = save(tmp_path)
    view = load(folder)
    details = view["rows"][0]["candidate_details"]
    for item in details:
        assert item["name"] is item["board"] is item["stage"] is item["amount_cny"] is None
        assert item["evidence"] is item["invalidation"] is item["ipo_feature_support"] is None
        assert item["setup_reference"] is item["current_raw_issues"] is item["current_reference_evidence"] is item["IPO_evidence"] is None
        assert item["checks_status"] == "unavailable_in_saved_plan" and item["trade_ready"] is False
    enrich(record["cases"][0])
    record["cases"][0]["candidate_reference_and_setup"][0]["setup_low_cny"] = None
    record["cases"][0]["cap_candidates"][0].update(evidence=None, invalidation=None, ipo_feature_support=None, stage=None)
    write(folder / "paper_plan.json", record)
    updated = load(folder)["rows"][0]["candidate_details"]
    assert updated[0]["stage"] is updated[0]["evidence"] is None
    assert updated[1]["setup_reference"]["setup_low_cny"] is None


def test_independent_exit_marks_entry_checks_not_run_and_keeps_unbound_empty_target(tmp_path):
    folder, record = save(tmp_path)
    item = record["cases"][2]
    enrich(item)
    item.update(current_raw_issues=[], current_reference_evidence=[], IPO_evidence=[])
    write(folder / "paper_plan.json", record)
    row = load(folder)["rows"][2]
    assert row["decision"] == "EXIT_ALL" and row["target_weights"] == {} and row["target_cash_weight"] == 1
    assert all(c["checks_status"] == "not_run_independent_exit" and c["trade_ready"] is False for c in row["candidate_details"])
    assert row["candidate_details"][0]["setup_reference"]["setup_low_cny"] == 12.97
    assert row["candidate_details"][0]["current_reference_evidence"] is None
    assert row["candidate_details"][0]["IPO_evidence"] is None


def test_single_candidate_facts_do_not_override_whole_batch_NO_CHANGE(tmp_path):
    folder, record = save(tmp_path)
    item = next(row for row in record["cases"] if row["decision"] == "NO_CHANGE")
    enrich(item)
    write(folder / "paper_plan.json", record)
    row = next(row for row in load(folder)["rows"] if row["case_id"] == item["case_id"])
    assert row["decision"] == "NO_CHANGE" and row["target_weights"] is row["target_cash_weight"] is None
    assert row["unresolved_member_count"] == 2
    assert row["candidate_details"][0]["evidence"]["daily_rebound"] is True
    assert row["candidate_details"][0]["checks_status"] == "saved_entry_checks"
    assert all(c["trade_ready"] is False for c in row["candidate_details"])


@pytest.mark.parametrize("fault", ["evidence_not_mapping", "cross_day_setup", "duplicate_setup", "nonpositive_setup",
    "nested_admission", "unselected_issue_symbol", "wrong_IPO_day", "future_evidence_clock"])
def test_malformed_cross_day_duplicate_or_admitting_detail_refused(tmp_path, fault):
    folder, record = save(tmp_path)
    item = record["cases"][0]
    enrich(item)
    if fault == "evidence_not_mapping":
        item["cap_candidates"][0]["evidence"] = []
    elif fault == "cross_day_setup":
        item["candidate_reference_and_setup"][0]["source_day"] = "2026-10-14"
    elif fault == "duplicate_setup":
        item["candidate_reference_and_setup"].append(deepcopy(item["candidate_reference_and_setup"][0]))
    elif fault == "nonpositive_setup":
        item["candidate_reference_and_setup"][0]["setup_low_cny"] = 0
    elif fault == "nested_admission":
        item["cap_candidates"][0]["evidence"]["nested"] = {"execution_admitted": True}
    elif fault == "unselected_issue_symbol":
        item["current_raw_issues"][0]["symbol"] = "000003.SZSE"
    elif fault == "wrong_IPO_day":
        item["IPO_evidence"][0]["decision_as_of"] = "2026-10-14T21:00:00+08:00"
    else:
        item["cap_candidates"][0]["evidence"]["available_at"] = "2026-10-16T01:00:00+08:00"
    write(folder / "paper_plan.json", record)
    with pytest.raises(ValueError):
        load(folder)
