"""Small saved research fixtures; no features, policy, returns or Engine run."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from trading.research import leader_eight_type_history_view as view


SELECTED = "sector_catchup_leader__unguarded__uncapped"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def source(folder, month, ids, marker):
    start, tail = month + "-05", month + "-06"
    case_rows = []
    env = {day: {"day": day, "phase": "broad_weakness_snapshot", "mode": "EXIT_ALL",
        "reason": "synthetic_known_weakness", "two_close_confirmation": False,
        "exact_previous_present": False, "per_type": {t: {"mode": "EXIT_ALL", "reason": "synthetic"}
            for t in view.TYPE_IDS}} for day in (start, tail)}
    for case_id in ids:
        kind, guard, cap = case_id.split("__")
        chosen = case_id == SELECTED
        orders = [{"id": "toy-buy", "day": tail, "symbol": "002491.SZSE", "side": "buy", "qty": 100,
            "filled": 100, "limit_price": 1019, "fees": 500, "status": "filled"}] if chosen else []
        fills = [{"order_id": "toy-buy", "day": tail, "symbol": "002491.SZSE", "side": "buy", "qty": 100,
            "price": 1019, "fees": 500}] if chosen else []
        risk = {"kind": "leader_type_risk_assessed", "day": tail, "symbol": "002491.SZSE", "status": "invalid",
            "entry": {"decision_day": start, "setup_low": 9.67, "surviving_qty": 100},
            "facts": {"close": 9.53, "structure_price": 9.67}, "reasons": ["current_close_below_fixed_entry_structure"]}
        decisions = [{"day": start, "execution_day": tail, "environment_mode": "EXIT_ALL", "status": "NO_CHANGE",
            "original_top5": [{"symbol": "002491.SZSE", "score": 12.3}], "cap_candidates": [], "cap_dropped_rows": 1,
            "reasons": ["type_membership_unknown_in_fixed_panel", "type_empty_or_capacity_filtered_all_no_signal"]},
            {"day": tail, "execution_day": None, "environment_mode": "NO_CHANGE", "status": "NO_CHANGE",
             "original_top5": [], "cap_candidates": [], "cap_dropped_rows": 0, "reasons": ["execution_tail_no_new_entry"]}]
        events = [risk, {"kind": "order_blocked", "day": tail, "symbol": "002491.SZSE", "reason": "limit_down_queue_unknown"}] if chosen else []
        daily = [{"date": start, "cash": 100000, "equity": 100000, "holdings": 0, "fees": 0, "events": []},
            {"date": tail, "cash": 98976, "equity": 99995, "holdings": 1 if chosen else 0, "fees": 5 if chosen else 0, "events": events}]
        case_rows.append({"case_id": case_id, "type": kind, "guard": guard, "capacity": cap,
            "status": "synthetic_saved_result", "metrics": {"net_return": marker} if chosen else None,
            "orders": orders, "fills": fills, "daily": daily, "decisions": decisions,
            "residual_positions": ["002491.SZSE"] if chosen else [], "blocking_evidence": None})
    path = folder / "result.json"
    record = {"start": start, "end": start, "source_days": [start, tail], "cases": case_rows,
        "environment_decisions": env, "protocol": deepcopy(view.PROTOCOL), **view.FLAGS}
    save(path, record)
    return path, record


def fixture_history(tmp_path):
    sources = tmp_path / "sources"
    old, old_record = source(sources / "jan_old", "2026-01", view.CASE_IDS, -.7)
    identity_ids = [c for c in view.CASE_IDS if c.startswith(("cross_regime_candidate__", "sector_catchup_leader__"))]
    new, new_record = source(sources / "jan_identity", "2026-01", identity_ids, .0123)
    feb, feb_record = source(sources / "feb", "2026-02", view.CASE_IDS, .028249)
    def summary(case, file, origin):
        return {**{k: deepcopy(case[k]) for k in ("case_id", "type", "guard", "capacity", "status", "metrics", "residual_positions")},
            "order_count": len(case["orders"]), "fill_count": len(case["fills"]),
            "source_result": str(file), "origin": origin}
    old_map, new_map = [{c["case_id"]: c for c in r["cases"]} for r in (old_record, new_record)]
    jan_rows = [summary(new_map[c], new, "new_identity") if c in new_map else summary(old_map[c], old, "reused_original_24")
        for c in view.CASE_IDS]
    jan_index = sources / "jan_index.json"
    save(jan_index, {"schema": "saved_historical_identity_comparison_index_v1", "start": "2026-01-05", "end": "2026-01-05",
        "source_days": ["2026-01-05", "2026-01-06"], "comparison": jan_rows,
        "old_baseline": [summary(c, old, "wrong_old_identity") for c in old_record["cases"]], **view.FLAGS})
    feb_index = sources / "feb_index.json"
    save(feb_index, {"schema": "saved_historical_identity_standalone_month_comparison_v1", "start": "2026-02-05", "end": "2026-02-05",
        "source_days": ["2026-02-05", "2026-02-06"], "case_rows": [summary(c, feb, "standalone") for c in reversed(feb_record["cases"])], **view.FLAGS})
    data = tmp_path / "data"
    output = data / view.DEFAULT_SUBDIR
    return [jan_index, feb_index], output, data, old


def test_both_saved_mappings_preserve_order_metrics_null_and_read_no_originals(tmp_path, monkeypatch):
    paths, output, data, original = fixture_history(tmp_path)
    view.build(paths, output)
    result = view.read_history(data, "2026-01", SELECTED)
    assert result["detail"]["summary"]["metrics"] == {"net_return": .0123}
    assert result["detail"]["summary"]["origin"] == "new_identity"
    assert [r["case_id"] for r in result["windows"][1]["case_rows"]] == list(view.CASE_IDS)
    assert result["windows"][0]["case_rows"][0]["metrics"] is None
    original.write_text("source changed after explicit build", encoding="utf-8")
    read = view._read
    def only_projection(path):
        assert output.resolve() in Path(path).resolve().parents
        return read(path)
    monkeypatch.setattr(view, "_read", only_projection)
    actual = view.read_history(data)
    assert actual["status"] == "saved_history_projection"
    assert actual["selected_window_id"] == "2026-02" and actual["selected_case_id"] == view.CASE_IDS[0]
    assert actual["source_originals_rechecked_at_read"] is False
    assert actual["engine_advanced"] is False and actual["profits_recomputed"] is False


def test_detail_keeps_original_facts_tail_units_and_parallel_reasons_without_positions(tmp_path):
    paths, output, data, _ = fixture_history(tmp_path)
    view.build(paths, output)
    result = view.read_history(data, "2026-02", SELECTED)
    first, tail = result["detail"]["days"]
    assert {c["code"] for c in first["classifications"]} == {"data_unknown", "capacity_filtered", "environment_blocked"}
    assert {c["code"] for c in tail["classifications"]} == {"no_signal", "order_blocked"}
    assert first["raw_top5_count"] == 1 and first["cap_candidate_count"] == 0
    assert first["environment"]["two_close_confirmation"] is False
    assert tail["is_tail"] is True and tail["in_decision_window"] is False
    assert tail["holdings_count"] == 1 and tail["position_quantities_available"] is False
    assert "positions" not in tail
    assert tail["fills"][0]["price"] == 1019 and tail["fills"][0]["price_cny"] == 10.19
    assert tail["fills"][0]["fees_cny"] == 5 and tail["fees_cny"] == 5
    assert tail["risk_assessments"][0]["facts"] == {"close": 9.53, "structure_price": 9.67}
    assert tail["orders"][0]["order_snapshot_basis"] == "final_saved_order_record_by_creation_day"


def test_changed_selected_projection_is_unavailable_without_fallback(tmp_path):
    paths, output, data, _ = fixture_history(tmp_path)
    view.build(paths, output)
    (output / "windows/2026-02" / (SELECTED + ".json")).write_text("changed", encoding="utf-8")
    assert view.read_history(data, "2026-02", SELECTED)["status"] == "unavailable"
    assert view.read_history(data, "2026-02", view.CASE_IDS[0])["status"] == "saved_history_projection"
    assert view.read_history(data, "missing", SELECTED)["status"] == "invalid_selection"
    (output / "index.json").write_text("changed", encoding="utf-8")
    assert view.read_history(data)["status"] == "unavailable"


def test_incomplete_case_mapping_refuses_build_without_output(tmp_path):
    paths, output, _, _ = fixture_history(tmp_path)
    bad = json.loads(paths[0].read_text())
    bad["comparison"].pop()
    save(paths[0], bad)
    with pytest.raises(ValueError, match="exactly once"):
        view.build(paths, output)
    assert not output.exists()
