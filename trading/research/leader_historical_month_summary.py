"""Read one saved standalone 32-case month without replaying or recomputing PnL."""
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import argparse

from ..contracts import MARKET_TZ
from .leader_eight_type_panel_replay import CAPS, GUARDS, PROTOCOL, SCHEMA as RESULT_SCHEMA, TYPE_IDS
from .shortline_panel_replay import _config


SCHEMA = "saved_historical_identity_standalone_month_comparison_v1"


def _read(path):
    payload = path.read_bytes()
    return json.loads(payload), {"path": str(path), "sha256": sha256(payload).hexdigest()}


def summarize(result_file, plan_file, out_file):
    source, plan_path, out = [Path(value).resolve() for value in (result_file, plan_file, out_file)]
    if out in (source, plan_path):
        raise ValueError("Month index cannot overwrite its result or registered window plan")
    if out.exists():
        raise FileExistsError("Keep the original saved month index; use a new explicit output")
    result, result_ref = _read(source)
    plan, plan_ref = _read(plan_path)
    decision_days, tail_days = plan["decision_days"], plan["tail_days"]
    days = decision_days + tail_days
    config = _config(100000, 5).to_dict()
    expected = [(f"{kind}__{guard}__{cap}", kind, guard, cap)
        for kind in TYPE_IDS for guard in GUARDS for cap, _ in CAPS]
    if (plan.get("schema") != "leader_eight_type_chronological_month_plan_v1"
            or result.get("schema") != RESULT_SCHEMA or plan.get("protocol") != PROTOCOL
            or result.get("protocol") != plan["protocol"] or result.get("config") != config
            or (plan.get("config") is not None and plan["config"] != config)
            or result.get("case_count") != 32 or len(result["cases"]) != 32
            or [(case.get("case_id"), case.get("type"), case.get("guard"), case.get("capacity"))
                for case in result["cases"]] != expected
            or any(case.get("config") != config for case in result["cases"])):
        raise ValueError("Exact original 32-case order and fixed window/fee protocol required")
    if (not decision_days or len(tail_days) != PROTOCOL["tail_open_sessions"]
            or days != sorted(set(days)) or (decision_days[0], decision_days[-1], tail_days[-1]) != (
                plan["start"], plan["end"], plan["through"])
            or (result["start"], result["end"], result["source_days"]) != (plan["start"], plan["end"], days)):
        raise ValueError("Saved result must use the exact registered decision month and six-session tail")
    for record in (result, plan):
        if (record.get("candidate_panel_only") is not True
                or any(record.get(key) is not False for key in
                    ("strategy_validated", "selection_enabled", "execution_admitted", "returns_reportable"))):
            raise ValueError("Month summary retains the original unadmitted research boundary")
    rows = []
    for case in result["cases"]:
        daily = case["daily"]
        if ([item["date"] for item in daily] != days[:len(daily)]
                or case["engine_sessions_committed"] != len(daily)):
            raise ValueError("Saved case daily rows must retain their exact committed source prefix")
        events = [event for day in daily for event in day["events"]]
        blocked = [event for event in events if event.get("kind") in ("order_blocked", "rejected")]
        decisions = [item for item in case["decisions"] if item["in_decision_window"]]
        if any(item["day"] not in decision_days for item in decisions):
            raise ValueError("A saved decision cannot move outside its registered month")
        rows.append({key: case[key] for key in (
            "case_id", "type", "guard", "capacity", "status", "engine_sessions_committed",
            "metrics", "blocking_evidence", "residual_positions", "complete_PnL_available")}
            | {"source_result": str(source), "order_count": len(case["orders"]),
               "fill_count": len(case["fills"]), "blocked_event_count": len(blocked),
               "blocked_reasons": dict(Counter(event.get("reason", "unknown") for event in blocked)),
               "decision_statuses": dict(Counter(item["status"] for item in decisions)),
               "decision_reasons": dict(Counter(reason for item in decisions for reason in item["reasons"])),
               "saved_final_equity": daily[-1]["equity"] if daily else None,
               "saved_final_cash": daily[-1]["cash"] if daily else None})
    environments = result["environment_decisions"]
    phases = [environments[day]["phase"] for day in decision_days]
    summary = {"schema": SCHEMA, "read_at": datetime.now(MARKET_TZ).isoformat(), "source_result": str(source),
        "window_plan": str(plan_path), "start": plan["start"], "end": plan["end"],
        "source_days": result["source_days"], "executed_cases": 32, "baseline_cases_rerun": 0,
        "case_rows": rows, "decision_environment_counts": dict(Counter(phases)),
        "decision_environment_switches": sum(a != b for a, b in zip(phases, phases[1:])),
        "cases_with_fills": sum(bool(row["fill_count"]) for row in rows),
        "cases_with_complete_PnL": sum(row["complete_PnL_available"] for row in rows),
        "case_session_count": sum(row["engine_sessions_committed"] for row in rows),
        "case_order_record_count": sum(row["order_count"] for row in rows),
        "case_fill_record_count": sum(row["fill_count"] for row in rows),
        "case_blocked_record_count": sum(row["blocked_event_count"] for row in rows),
        "residual_case_count": sum(bool(row["residual_positions"]) for row in rows),
        "engine_error_case_count": sum(row["blocking_evidence"] is not None for row in rows),
        "independent_trade_sample_count_claimed": False, "fresh_holdout_claimed": False,
        "profits_recomputed": False, "returns_reportable": False, "candidate_panel_only": True,
        "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
        "source_refs": {"result": result_ref, "plan": plan_ref}}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = summarize(args.result, args.plan, args.out)
    print(json.dumps({key: value for key, value in summary.items()
        if key not in ("case_rows", "source_days")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
