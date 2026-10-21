"""Synthetic exact-event annotations, without replay or performance calculation."""
from copy import deepcopy
import csv
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.research import leader_historical_case_annotations as m


NOW = "2026-10-15T21:00:00+08:00"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def csv_write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


def fixture(tmp_path):
    snapshot = tmp_path / "native_snapshot"; snapshot.mkdir()
    manifest = snapshot / "manifest.json"
    write(manifest, {"status": "complete_inventory_only", "execution_admitted": False, "synthetic": True})
    calendar = tmp_path / "calendar.json"
    write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []})
    flags = {"execution_admitted": False, "selection_enabled": False, "strategy_validated": False}
    env = {"schema": "leader_historical_observed_environment_v1",
        "method_id": "native_active_observed_2026_fixed_phase_context_v1",
        "status": "completed_observed_history_environment_context_not_complete_close_or_strategy",
        "available_at": "2026-10-15T20:08:28+08:00", "start": "2026-01-05", "end": "2026-01-06",
        "source": {"snapshot": str(snapshot), "manifest": str(manifest), "manifest_sha256": sha256(manifest.read_bytes()).hexdigest()},
        "calendar": {"file": str(calendar), "sha256": sha256(calendar.read_bytes()).hexdigest(),
            "sessions": ["2026-01-05", "2026-01-06"]},
        "days": [{"day": "2026-01-05", "phase": m.STRENGTH, "breadth": .6, "median_return_pct": 1.,
                  "diagnostic_two_close_phase_agreement": False, "active_observed_non_ST_rows": 100},
                 {"day": "2026-01-06", "phase": m.WEAKNESS, "breadth": -.6, "median_return_pct": -1.,
                  "diagnostic_two_close_phase_agreement": False, "active_observed_non_ST_rows": 100}], **flags}
    environment = tmp_path / "environment.json"; write(environment, env)
    artifact_root = tmp_path / "old_data"; artifact_root.mkdir()
    cases = []
    for pool in m.POOLS:
        for family in m.FAMILIES:
            for policy in m.EXITS:
                completed = pool == m.POOLS[0] and policy == m.EXITS[0]
                status = m.COMPLETED if completed else "unavailable_verified_full_market_breadth" if policy == m.EXITS[1] else "unavailable_complete_cohort_decision_batches"
                key = "__".join((pool, family, policy)); folder = artifact_root / key; folder.mkdir()
                no_orders = family == "stage3_amount_baseline"
                order_id = "2026-01-06:000001.SZSE:buy"
                order = {"id": order_id, "day": "2026-01-06", "symbol": "000001.SZSE", "side": "buy",
                    "qty": 100, "limit_price": 316, "notional": 31600, "fees": 516, "filled": 100}
                events = [{"sequence": 1, "day": "2026-01-05", "kind": "targets_generated", "signal_date": "2026-01-05",
                        "execution_not_before": "2026-01-06", "targets": {"000001.SZSE": .16}},
                    {"sequence": 2, "day": "2026-01-05", "kind": "targets_generated", "signal_date": "2026-01-05",
                        "execution_not_before": "2026-01-06", "targets": {"000001.SZSE": .2}},
                    {"sequence": 3, "day": "2026-01-06", "kind": "submitted", "order_id": order_id},
                    {"sequence": 4, "day": "2026-01-05", "kind": "targets_generated", "signal_date": "2026-01-05",
                        "execution_not_before": "2026-01-06", "targets": {}}]
                account = {"orders": {} if no_orders else {order_id: order},
                    "fills": {} if no_orders else {order_id + ":0": {"order_id": order_id, "day": "2026-01-06", "qty": 50,
                        "price": 316, "fees": 508, "available_day": "2026-01-07"},
                        order_id + ":1": {"order_id": order_id, "day": "2026-01-06", "qty": 50,
                        "price": 316, "fees": 8, "available_day": "2026-01-07"}},
                    "events": events if not no_orders else [{"sequence": 3, "day": "2026-01-06", "kind": "order_blocked", "reason": "limit_up_queue_unknown"}]}
                if completed:
                    write(folder / "account.json", account)
                    daily = [{"date": day, "equity": "100000.0", "cash": "100000.0", "fees": "0.0",
                        "event_time_frontier": "{'historical_assumption': True, 'value': None}"}
                        for day in ["2026-01-05", "2026-01-06", "2026-01-07"]]
                    csv_write(folder / "daily.csv", daily)
                    csv_write(folder / "decisions.csv", [{"day": row["date"], "in_window": str(row["date"] <= "2026-01-06"),
                        "batch_status": "complete" if row["date"] == "2026-01-05" else "incomplete",
                        "no_change": str(row["date"] != "2026-01-05"), "candidate_count": "1" if row["date"] == "2026-01-05" else ""} for row in daily])
                else:
                    # Poison cash traces prove unavailable artifacts are not consumed.
                    (folder / "account.json").write_text("DO NOT READ CASH TRACE", encoding="utf-8")
                    (folder / "daily.csv").write_text("DO NOT READ CASH TRACE", encoding="utf-8")
                cases.append({"family": family, "pool": pool, "exit_policy": policy, "status": status,
                    "artifacts": str(folder), "orders": len(account["orders"]) if completed else 0,
                    "fills": len(account["fills"]) if completed else 0,
                    "metrics": {"net_return": 0. if no_orders else -.123, "synthetic_original_untouched": True} if completed else None, **flags})
    replay = tmp_path / "replay.json"
    write(replay, {"schema": "leader_daily_comparison_v2", "status": "fixed_already_inspected_comparison_complete",
        "snapshot": str(snapshot), "start": "2026-01-05", "end": "2026-01-06", "case_count": 20, "cases": cases, **flags})
    return replay, environment, cases


def run(tmp_path, monkeypatch, replay, environment):
    monkeypatch.setattr(m, "_now", lambda: NOW)
    return m.build_annotations(replay_result=replay, environment_result=environment, out=tmp_path / "new_annotations")


def test_twenty_statuses_five_annotated_fifteen_not_consumed_units_and_clocks_unchanged(tmp_path, monkeypatch):
    replay, environment, original = fixture(tmp_path)
    before = {p: sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    result = run(tmp_path, monkeypatch, replay, environment)
    assert result["counts"] == {"original_cases": 20, "annotated_completed_cases": 5, "unavailable_cases_not_annotated": 15,
        "original_completed_orders": 4, "original_completed_fills": 8}
    assert [r["original_case"] for r in result["cases"]] == original
    for entry in result["cases"]:
        side = json.loads(open(entry["sidecar_file"], encoding="utf-8").read())
        if entry["original_status"] != m.COMPLETED:
            assert side["original_metrics"] is None
            assert side["counts"] is side["daily_annotations"] is side["order_annotations"] is side["fill_annotations"] is None
            assert side["source_files"] == [] and side["unavailable_artifact_traces_not_consumed"] is True
            continue
        daily = side["daily_annotations"]
        assert daily[0]["previous_exact_session"] == "2026-01-02"
        assert daily[0]["previous_exact_close_phase"]["status"] == "out_of_environment_window"
        assert daily[1]["decision_day_close_phase"]["phase"] == m.WEAKNESS
        assert daily[1]["original_decision_record"]["no_change"] == "True"
        assert daily[1]["original_batch_status_not_upgraded"] is True
        assert daily[-1]["valuation_day_close_phase"]["phase"] is None
        assert daily[-1]["valuation_day_close_phase"]["status"] == "out_of_environment_window"
        assert daily[-1]["in_original_decision_window"] is False
        assert daily[0]["original_daily_record"]["event_time_frontier"].startswith("{'historical_assumption'")
        if side["order_annotations"]:
            order = side["order_annotations"][0]
            assert order["signal_link"]["target_sequence"] == 2
            assert order["signal_link"]["signal_date"] == "2026-01-05"
            assert order["signal_date_phase"]["phase"] == m.STRENGTH
            assert order["execution_day_close_phase"]["phase"] == m.WEAKNESS
            assert order["execution_day_close_phase"]["role"] == "post_execution_close_context"
            assert order["execution_day_close_phase"]["known_at_execution_open_claimed"] is False
            assert order["original_order"]["limit_price"] == 316 and order["original_order"]["notional"] == 31600
            assert side["fill_annotations"][0]["original_fill"]["fees"] == 508
        else:
            assert side["counts"]["original_order_blocked_events"] == 1 and side["original_metrics"]["net_return"] == 0
    assert result["created_at"] == result["available_at"] == NOW
    assert result["source"]["environment_available_at"].startswith("2026-10-15T20:08:28")
    assert result["engine_advanced"] is result["metrics_recomputed"] is result["execution_admitted"] is False
    assert all(sha256(p.read_bytes()).hexdigest() == checksum for p, checksum in before.items())
    with pytest.raises(FileExistsError):
        run(tmp_path, monkeypatch, replay, environment)


def test_signal_link_unknown_does_not_use_neighbor_and_empty_exit_target_is_valid():
    calendar = ["2026-01-05", "2026-01-06", "2026-01-07"]
    order = {"id": "order", "day": "2026-01-06", "side": "sell"}
    target = {"sequence": 1, "kind": "targets_generated", "day": "2026-01-05",
        "signal_date": "2026-01-05", "execution_not_before": "2026-01-06", "targets": {}}
    submit = {"sequence": 2, "kind": "submitted", "day": "2026-01-06", "order_id": "order"}
    assert m.order_signal_link("order", order, [target, submit], calendar)["signal_date"] == "2026-01-05"
    for events, reason in [([target], "missing_submitted_event"),
            ([target, submit, deepcopy(submit)], "ambiguous_submitted_events"),
            ([{**target, "execution_not_before": "2026-01-07"}, submit], "no_prior_target_for_exact_execution_day"),
            ([target, deepcopy(target), submit], "ambiguous_latest_target_sequence"),
            ([{**target, "signal_date": "2026-01-06"}, submit], "target_signal_or_exact_next_session_disagrees")]:
        result = m.order_signal_link("order", order, events, calendar)
        assert result["signal_date"] is None and result["unknown_reason"] == reason


def test_missing_original_session_in_both_logs_cannot_pass_as_same_calendar(tmp_path, monkeypatch):
    replay, environment, cases = fixture(tmp_path)
    case = next(row for row in cases if row["status"] == m.COMPLETED)
    folder = __import__("pathlib").Path(case["artifacts"])
    for name, field in (("daily.csv", "date"), ("decisions.csv", "day")):
        file = folder / name
        with file.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        csv_write(file, [row for row in rows if row[field] != "2026-01-06"])
    with pytest.raises(ValueError, match="decision-window sessions differ"):
        run(tmp_path, monkeypatch, replay, environment)
    assert not (tmp_path / "new_annotations").exists()


def test_same_snapshot_manifest_window_and_actual_available_clock_required(tmp_path, monkeypatch):
    replay, environment, _ = fixture(tmp_path)
    original = json.loads(environment.read_text(encoding="utf-8"))
    patches = [{"source": {**original["source"], "snapshot": str(tmp_path / "different")}},
        {"source": {**original["source"], "manifest_sha256": "changed"}},
        {"available_at": "2026-10-15T21:01:00+08:00"}]
    for patch in patches:
        write(environment, {**original, **patch})
        with pytest.raises(ValueError):
            run(tmp_path, monkeypatch, replay, environment)
        assert not (tmp_path / "new_annotations").exists()


def test_unknown_original_event_link_preserves_order_and_unknown_saved_phase_not_zero(tmp_path, monkeypatch):
    replay, environment, cases = fixture(tmp_path)
    case = next(r for r in cases if r["status"] == m.COMPLETED)
    file = __import__("pathlib").Path(case["artifacts"]) / "account.json"
    account = json.loads(file.read_text(encoding="utf-8"))
    account["events"] = [e for e in account["events"] if e["kind"] != "submitted"]
    write(file, account)
    env = json.loads(environment.read_text(encoding="utf-8"))
    env["days"][1].update(phase="unknown", breadth=None, median_return_pct=None)
    write(environment, env)
    result = run(tmp_path, monkeypatch, replay, environment)
    side = json.loads(open(result["cases"][0]["sidecar_file"], encoding="utf-8").read())
    order = side["order_annotations"][0]
    assert order["signal_link"]["signal_date"] is None
    assert order["signal_date_phase"]["status"] == "unknown_date_link"
    assert order["signal_date_phase"]["unknown_reason"] == "missing_submitted_event"
    assert order["execution_day_close_phase"]["status"] == "observed_phase_unknown"
    assert order["execution_day_close_phase"]["phase"] == "unknown" and order["execution_day_close_phase"]["breadth"] is None
    assert side["counts"]["orders"] == 1 and side["counts"]["orders_with_unknown_signal_link"] == 1
    assert side["original_metrics"] == case["metrics"]


def test_fill_uses_its_own_execution_day_but_inherits_order_signal_without_carry(tmp_path, monkeypatch):
    replay, environment, cases = fixture(tmp_path)
    file = __import__("pathlib").Path(cases[0]["artifacts"]) / "account.json"
    account = json.loads(file.read_text(encoding="utf-8"))
    first = next(iter(account["fills"]))
    account["fills"][first]["day"] = "2026-01-07"
    write(file, account)
    result = run(tmp_path, monkeypatch, replay, environment)
    side = json.loads(open(result["cases"][0]["sidecar_file"], encoding="utf-8").read())
    fill = side["fill_annotations"][0]
    assert fill["signal_link"]["signal_date"] == "2026-01-05"
    assert fill["execution_day_close_phase"]["day"] == "2026-01-07"
    assert fill["execution_day_close_phase"]["status"] == "out_of_environment_window"
    assert fill["execution_day_close_phase"]["phase"] is None
    assert fill["original_fill"]["price"] == 316 and fill["original_fill"]["fees"] == 508
