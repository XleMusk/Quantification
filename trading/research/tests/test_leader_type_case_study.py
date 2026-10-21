"""Synthetic fixed sampling and missing exact-session paths, not strategy tests."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import leader_type_case_study as m


CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}
SESSIONS, _, _ = m._calendar_view(CALENDAR, "2026-01-01", "2026-12-31", [])
DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
SYMBOLS = [f"{i:06d}.SZSE" for i in range(1, 6)]
NOW = "2026-10-15T19:30:00+08:00"


def inputs():
    panels = {day: {"day": day, "available_at": "2026-10-15T18:42:00+08:00",
        "environment": {"source_complete": True, "breadth": .1 if day == DAYS[1] else -.6,
            "median_return_pct": .2 if day == DAYS[1] else -2.},
        "risk_features": {s: {"name": "synthetic " + s, "stage": 2,
            "listing_qualification_status": "synthetic_original"} for s in SYMBOLS},
        "types": [{"id": identity, "status": "not_matched", "candidates": [], "unresolved_symbols": [],
            "known_exclusions": {s: "known_rule_not_satisfied" for s in SYMBOLS},
            "missing_evidence": []} for identity in m.TYPE_IDS]} for day in DAYS}
    raw = pd.DataFrame([{ "date": day, "code": "sz." + s[:6], "open": "10", "high": "10.1",
        "low": "9.9", "close": "10", "preclose": "10", "volume": "1000000", "amount": "10000000",
        "pctChg": "0", "tradestatus": "1", "isST": "0", "adjustflag": "3"} for day in DAYS for s in SYMBOLS])
    return {"panel_by_day": panels, "scope_members": SYMBOLS, "raw": raw, "source_days": DAYS,
        "calendar_payload": CALENDAR, "context_anchor": {"day": "2026-10-09", "closes": {s: 999. for s in SYMBOLS}}}


def set_type(loaded, day, identity, candidates=(), unresolved=()):
    item = next(t for t in loaded["panel_by_day"][day]["types"] if t["id"] == identity)
    item.update(candidates=[{"symbol": s, "date": day, "source_day": day, "name": s,
        "amount_cny": 100000000 * (i + 1), "evidence": {"through": day}, "ipo_feature_support": True}
        for i, s in enumerate(candidates)], unresolved_symbols=list(unresolved),
        known_exclusions={s: "known_rule_not_satisfied" for s in SYMBOLS if s not in set(candidates) | set(unresolved)},
        status="matched" if candidates else "unknown" if unresolved else "not_matched")
    return item


def select(loaded):
    return m.select_cases(loaded["panel_by_day"], loaded["scope_members"], SESSIONS, as_of=NOW)


def cell(result, identity, phase):
    return next(r for r in result["coverage_matrix"] if r["type_id"] == identity and r["phase"] == phase)


def test_per_type_phase_date_symbol_first2_distinct_not_amount_or_later_winners():
    loaded = inputs(); identity = m.TYPE_IDS[0]
    set_type(loaded, DAYS[0], identity, [SYMBOLS[2], SYMBOLS[1], SYMBOLS[0]])
    set_type(loaded, DAYS[1], identity, [SYMBOLS[4], SYMBOLS[3]])
    set_type(loaded, DAYS[2], identity, [SYMBOLS[0], SYMBOLS[3]])
    result = select(loaded)
    weak = [r for r in result["selected"] if r["type_id"] == identity and r["phase"] == m.WEAKNESS]
    mixed = [r for r in result["selected"] if r["type_id"] == identity and r["phase"] == m.MIXED]
    assert [(r["day"], r["symbol"]) for r in weak] == [(DAYS[0], SYMBOLS[0]), (DAYS[0], SYMBOLS[1])]
    assert [r["symbol"] for r in mixed] == [SYMBOLS[3], SYMBOLS[4]]
    assert len(result["coverage_matrix"]) == 8 * 4
    assert cell(result, identity, m.STRENGTH)["coverage_status"] == "phase_not_observed_in_source_window"
    assert result["environments"][DAYS[1]]["phase"] == m.MIXED
    assert result["environments"][DAYS[1]]["two_close_confirmation"] is False
    assert result["environments"][DAYS[3]]["two_close_confirmation"] is True
    changed = deepcopy(loaded)
    for p in changed["panel_by_day"].values():
        for t in p["types"]:
            for row in t["candidates"]:
                row.update(amount_cny=-999, score=999999, run_max=100, hit_4=True, hit_5=True,
                    future_return=999, future_coverage=False)
    assert select(changed)["selected"] == result["selected"]


def test_unresolved_only_zero_matched_cell_knownnotmatched_preserved_and_overlap():
    loaded = inputs(); unknown_type, known_type = m.TYPE_IDS[4], m.TYPE_IDS[6]
    set_type(loaded, DAYS[0], unknown_type, unresolved=[SYMBOLS[2], SYMBOLS[1]])
    set_type(loaded, DAYS[2], unknown_type, unresolved=[SYMBOLS[0]])
    set_type(loaded, DAYS[1], unknown_type, unresolved=[SYMBOLS[1]])
    set_type(loaded, DAYS[0], m.TYPE_IDS[1], candidates=[SYMBOLS[1]])
    set_type(loaded, DAYS[2], m.TYPE_IDS[1], unresolved=[SYMBOLS[0]])
    result = select(loaded)
    weak = cell(result, unknown_type, m.WEAKNESS)
    assert (weak["matched"], weak["unresolved"], weak["known_not_matched"], weak["selected_unresolved"]) == (0, 3, 12, 1)
    row = next(r for r in result["selected"] if r["type_id"] == unknown_type and r["phase"] == m.WEAKNESS)
    assert row["day"] == DAYS[0] and row["symbol"] == SYMBOLS[1]
    assert row["selection_kind"] == "unresolved_observation"
    assert row["selection_reason"] == "first_unresolved_in_fixed_date_symbol_order"
    assert len(row["same_symbol_other_case_ids"]) == 2 and row["independent_sample"] is False
    assert cell(result, m.TYPE_IDS[1], m.WEAKNESS)["selected_unresolved"] == 0
    assert cell(result, m.TYPE_IDS[1], m.WEAKNESS)["selected_matched"] == 1
    assert cell(result, known_type, m.WEAKNESS)["coverage_status"] == "known_no_match_in_observed_phase"
    assert cell(result, known_type, m.WEAKNESS)["unresolved"] == 0


def test_outside_scope_not_added_and_partition_gaps_or_overlap_rejected():
    loaded = inputs(); identity = m.TYPE_IDS[3]
    item = set_type(loaded, DAYS[0], identity, candidates=["000000.SZSE", SYMBOLS[2], SYMBOLS[0]])
    result = select(loaded)
    assert cell(result, identity, m.WEAKNESS)["outside_fixed_scope_count"] == 1
    assert [r["symbol"] for r in result["selected"]] == [SYMBOLS[0], SYMBOLS[2]]
    item["unresolved_symbols"] = [SYMBOLS[0]]
    with pytest.raises(ValueError, match="cannot overlap"):
        select(loaded)
    item["unresolved_symbols"] = []
    item["known_exclusions"].pop(SYMBOLS[1])
    with pytest.raises(ValueError, match="Missing type membership"):
        select(loaded)


def test_exact_calendar_missing_not_observed_bar_count_and_no_refill_on_E_missing_or_IPO():
    loaded = inputs(); identity = m.TYPE_IDS[0]
    item = set_type(loaded, DAYS[0], identity, candidates=SYMBOLS[:3])
    item["candidates"][0]["ipo_feature_support"] = False
    loaded["raw"] = loaded["raw"].loc[~(loaded["raw"].date.eq(DAYS[0]) & loaded["raw"].code.eq("sz.000001"))]
    result = m.prepare_cases(loaded, recorded_at=NOW)
    assert [r["symbol"] for r in result["cases"]] == SYMBOLS[:2]
    case = result["cases"][0]
    assert [r["day"] for r in case["daily_slots"]] == ["2026-10-08", "2026-10-09", *DAYS[:3]]
    assert case["counts"] == {"expected_calendar_slots": 5, "raw_rows": 2, "missing_slots": 3, "halted_slots": 0}
    assert case["information_through_E"]["saved_candidate"]["ipo_feature_support"] is False
    assert all(r["close_below_fixed_E_raw_low"] is None for r in case["after_E_observed_path"]["daily_slots"])
    assert any("不补位" in text for text in case["limitations"])
    assert case["strategy_validated"] is case["execution_admitted"] is case["confirmed_leader"] is False


def test_negative_halt_reference_mismatch_and_missing_exact_successor_all_retained():
    loaded = inputs(); identity = m.TYPE_IDS[2]
    set_type(loaded, DAYS[0], identity, candidates=[SYMBOLS[0]])
    set_type(loaded, DAYS[3], m.TYPE_IDS[1], candidates=[SYMBOLS[1]])
    negative = loaded["raw"].date.eq(DAYS[1]) & loaded["raw"].code.eq("sz.000001")
    loaded["raw"].loc[negative, ["close", "low", "pctChg"]] = ["9.8", "9.7", "-2"]
    halted = loaded["raw"].date.eq(DAYS[2]) & loaded["raw"].code.eq("sz.000001")
    loaded["raw"].loc[halted, ["tradestatus", "close", "volume"]] = ["0", "", "0"]
    result = m.prepare_cases(loaded, recorded_at=NOW)
    first = next(r for r in result["cases"] if r["type_id"] == identity)
    assert first["after_E_observed_path"]["daily_slots"][0]["raw"]["pctChg"] == "-2"
    assert first["after_E_observed_path"]["daily_slots"][0]["close_below_fixed_E_raw_low"] is True
    assert first["after_E_observed_path"]["daily_slots"][1]["status"] == "halted_source_row"
    assert first["after_E_observed_path"]["daily_slots"][1]["close_below_fixed_E_raw_low"] is None
    last = next(r for r in result["cases"] if r["type_id"] == m.TYPE_IDS[1])
    assert [r["day"] for r in last["after_E_observed_path"]["daily_slots"]] == ["2026-10-16", "2026-10-19"]
    assert [r["status"] for r in last["after_E_observed_path"]["daily_slots"]] == ["missing_raw_row"] * 2
    assert last["after_E_observed_path"]["account_return"] is None
    loaded["raw"].loc[negative, "preclose"] = "8"
    first = next(r for r in m.prepare_cases(loaded, recorded_at=NOW)["cases"] if r["type_id"] == identity)
    assert first["after_E_observed_path"]["daily_slots"][0]["reference_vs_previous_exact_session"]["status"] == "reference_mismatch"
    assert first["after_E_observed_path"]["daily_slots"][0]["close_below_fixed_E_raw_low"] is None


def test_saved_unknown_identity_not_promoted_and_future_prices_do_not_change_samples():
    loaded = inputs(); identity = m.TYPE_IDS[4]
    item = set_type(loaded, DAYS[0], identity, unresolved=[SYMBOLS[0]])
    item.update(missing_evidence={"identity_report_not_available_asof_requested_day": 1},
        identity_proof={"registered_at": "2026-10-15T11:54:13+08:00", "prospective_identity_not_backfilled": True})
    before = m.prepare_cases(loaded, recorded_at=NOW)
    changed = deepcopy(loaded)
    changed["raw"].loc[changed["raw"].date.gt(DAYS[0]), "close"] = "9999"
    after = m.prepare_cases(changed, recorded_at=NOW)
    assert before["selected"] == after["selected"]
    case = after["cases"][0]
    assert case["original_type_status"] == "unknown" and case["confirmed_leader"] is False
    assert case["information_through_E"]["saved_candidate"] is None
    assert case["information_through_E"]["unknown_reason_for_symbol"] is None
    assert case["information_through_E"]["original_type_missing_evidence"] == item["missing_evidence"]
    assert case["information_through_E"]["original_identity_proof"] == item["identity_proof"]
    assert "不能把15日登记回填" in case["limitations"][0]


def test_new_registered_protocol_actual_clocks_inputhash_and_small_output(tmp_path, monkeypatch):
    loaded = inputs(); set_type(loaded, DAYS[0], m.TYPE_IDS[0], candidates=[SYMBOLS[0]])
    source = tmp_path / "input.json"; source.write_text("synthetic input", encoding="utf-8")
    loaded["provenance"] = {"panel": str(source), "panel_sha256": m.digest(source),
        "created_at": "2026-10-15T18:42:17+08:00", "input_record": {"identity_report": {"synthetic": True}}}
    record = {"schema": m.PROTOCOL_SCHEMA, "method_id": m.METHOD_ID,
        "recorded_at": "2026-10-15T19:00:00+08:00", "status": "late_registered_retrospective_case_learning",
        "protocol": deepcopy(m.PROTOCOL), "input_manifest": str(source), "input_manifest_sha256": m.digest(source)}
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(record), encoding="utf-8")
    assert m._read_protocol(protocol, loaded["provenance"], NOW)["recorded_at"] == record["recorded_at"]
    for patch in ({"recorded_at": "2026-10-15T19:31:00+08:00"}, {"input_manifest_sha256": "wrong"},
                  {"recorded_at": "2026-10-15T18:00:00+08:00"}, {"execution_admitted": True}):
        protocol.write_text(json.dumps({**record, **patch}), encoding="utf-8")
        with pytest.raises(ValueError):
            m._read_protocol(protocol, loaded["provenance"], NOW)
    protocol.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(m, "load_inputs", lambda *args: loaded)
    out = tmp_path / "new_cases"
    result = m.build_case_studies(panel=source, ipo_evidence="unchanged_original", research_master_run="unchanged_original", protocol=protocol, out=out)
    assert result["counts"]["cases"] == 1 and result["counts"]["selected_cases_discarded_for_future_outcomes"] == 0
    assert result["individual_case_interpretation_completed"] is False
    assert result["status"] == "completed_fixed_case_evidence_generation_not_deep_review"
    assert result["source"]["identity_report"] == {"synthetic": True}
    saved = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert saved["schema"] == m.SCHEMA and saved["source"]["context_anchor_not_read_for_case_bars"] is True
    case = saved["cases"][0]
    assert m.digest(case["json_file"]) == case["case_sha256"]
    assert "E后行只作事后" in open(case["markdown_file"], encoding="utf-8").read()
    assert result["account_created"] is result["orders_generated"] is result["engine_advanced"] is False
    with pytest.raises(FileExistsError):
        m.build_case_studies(panel=source, ipo_evidence="unchanged_original", research_master_run="unchanged_original", protocol=protocol, out=out)
