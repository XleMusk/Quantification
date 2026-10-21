"""Evaluate candidate readiness from existing evidence without replay or order side effects."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

from .. import settings
from .experiment import write_json


ID = "candidate_admission_gate_v1"
LIMITS = [
    "This consumer reads saved evidence only; it does not replay, refit, tune or submit orders",
    "A viewed development replay cannot satisfy the independent holdout requirement",
    "Unknown, stale or blocked execution evidence remains a blocker and is never treated as zero",
    "NO_TRADE remains in force even when a research stage becomes ready",
]


def _plain(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        raise ValueError(f"{label} must be a short plain identifier")
    return value


def _read(path, label):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _flag(record, key, expected=False):
    return record.get(key) is expected


def evaluate(holdout, risk, stress, broker=None, turnover=None, holdout_protocol=None):
    """Return a conservative staged decision from immutable saved receipts."""
    reasons = []
    checks = []

    holdout_status = holdout.get("status")
    holdout_ready = holdout_status == "holdout_replay_completed"
    checks.append({"name": "independent_holdout_replay", "passed": holdout_ready,
                   "observed": holdout_status})
    if not holdout_ready:
        reasons.append(f"holdout_not_completed:{holdout_status or 'unknown'}")
    if holdout.get("fresh_holdout_claimed") is not True:
        reasons.append("fresh_holdout_not_claimed")
    if not _flag(holdout, "execution_admitted"):
        reasons.append("holdout_execution_flag_not_false")
    if holdout_ready and holdout.get("strategy_validated") is not True:
        reasons.append("holdout_strategy_not_validated")

    rights_policy = (holdout_protocol or {}).get("candidate_rights_policy")
    if isinstance(rights_policy, dict) and rights_policy.get("id") == "rights_replay_waive_all_v1":
        reasons.append("rights_replay_waiver_not_account_evidence")
    elif rights_policy is None:
        reasons.append("rights_replay_policy_missing")

    risk_status = risk.get("status")
    checks.append({"name": "risk_concentration_receipt", "passed": risk_status == "completed_saved_account_concentration",
                   "observed": risk_status})
    if risk_status != "completed_saved_account_concentration":
        reasons.append(f"risk_receipt_invalid:{risk_status or 'unknown'}")
    stale_days = risk.get("stale_valuation_days")
    if not isinstance(stale_days, int) or stale_days < 0:
        reasons.append("risk_stale_days_unknown")
    elif stale_days:
        reasons.append(f"stale_valuation_days:{stale_days}")
    blocked = risk.get("blocked_attempts")
    blocked_count = sum(value for value in blocked.values() if isinstance(value, int) and value > 0) if isinstance(blocked, dict) else None
    if blocked_count is None:
        reasons.append("blocked_execution_attempts_unknown")
    elif blocked_count:
        reasons.append(f"blocked_execution_attempts:{blocked_count}")
    risk_complete = (risk_status == "completed_saved_account_concentration"
                     and isinstance(stale_days, int) and stale_days == 0
                     and isinstance(blocked_count, int) and blocked_count == 0)
    checks.append({"name": "risk_evidence_completeness", "passed": risk_complete,
                   "observed": {"stale_valuation_days": stale_days,
                                 "blocked_attempts": blocked_count}})

    stress_status = stress.get("status")
    checks.append({"name": "stress_exposure_receipt", "passed": stress_status == "completed_saved_stress_exposure",
                   "observed": stress_status})
    if stress_status != "completed_saved_stress_exposure":
        reasons.append(f"stress_receipt_invalid:{stress_status or 'unknown'}")
    unknown_rows = stress.get("unknown_rows")
    if not isinstance(unknown_rows, int) or unknown_rows < 0:
        reasons.append("stress_unknown_marks_unknown")
    elif unknown_rows:
        reasons.append(f"unknown_stress_marks:{unknown_rows}")
    stress_complete = (stress_status == "completed_saved_stress_exposure"
                       and isinstance(unknown_rows, int) and unknown_rows == 0)
    checks.append({"name": "stress_mark_completeness", "passed": stress_complete,
                   "observed": {"unknown_rows": unknown_rows}})

    broker_verified = isinstance(broker, dict) and broker.get("status") == "verified_account_observation" \
        and broker.get("cash_positions_reconciled") is True
    checks.append({"name": "broker_account_observation", "passed": broker_verified,
                   "observed": broker.get("status") if isinstance(broker, dict) else "missing"})
    if not broker_verified:
        reasons.append("broker_account_observation_missing_or_unreconciled")

    cost_case = None
    if isinstance(turnover, dict) and turnover.get("status") == "completed_saved_parent_order_attribution":
        cases = turnover.get("cases")
        if isinstance(cases, list) and len(cases) == 1 and isinstance(cases[0], dict):
            case = cases[0]
            fees = case.get("fees_cny")
            turnover_value = case.get("filled_turnover_cny")
            if (isinstance(fees, (int, float)) and fees >= 0
                    and isinstance(turnover_value, (int, float)) and turnover_value > 0):
                cost_case = {"fees_cny": float(fees), "filled_turnover_cny": float(turnover_value),
                             "fee_to_turnover": float(fees) / float(turnover_value)}
    checks.append({"name": "cost_and_turnover_receipt", "passed": cost_case is not None,
                   "observed": cost_case or (turnover.get("status") if isinstance(turnover, dict) else "missing")})
    if cost_case is None:
        reasons.append("cost_and_turnover_receipt_missing_or_invalid")

    # Stage classification must use the evidence fields themselves.  Reason
    # text is for humans and is deliberately not a control-flow contract.
    research_blocked = (
        not holdout_ready
        or holdout.get("fresh_holdout_claimed") is not True
        or not _flag(holdout, "execution_admitted")
        or (holdout_ready and holdout.get("strategy_validated") is not True)
        or (isinstance(rights_policy, dict)
            and rights_policy.get("id") == "rights_replay_waive_all_v1")
        or rights_policy is None
        or not risk_complete
        or not stress_complete
        or cost_case is None
    )
    if holdout_ready and not research_blocked:
        stage = "broker_readiness_blocked" if not broker_verified else "paper_replay_eligible"
    elif holdout_ready:
        stage = "holdout_complete_evidence_blocked"
    else:
        stage = "diagnostic_only"
    return {
        "id": ID,
        "status": "blocked",
        "stage": stage,
        "execution_admitted": False,
        "strategy_validated": False,
        "fresh_holdout_claimed": bool(holdout.get("fresh_holdout_claimed") is True),
        "reasons": reasons,
        "checks": checks,
        "limits": LIMITS,
    }


def _report(out, result, inputs):
    lines = ["# Candidate Admission Gate", "", f"Status: `{result['status']}`; stage: `{result['stage']}`; NO_TRADE.",
             "This is a read-only evidence decision. No replay, refit, tuning or broker action was run.", "",
             "| Check | Passed | Observed |", "| --- | --- | --- |"]
    for check in result["checks"]:
        lines.append(f"| {check['name']} | {check['passed']} | {check['observed']} |")
    lines += ["", "## Blocking reasons", ""]
    lines.extend(f"- {reason}" for reason in result["reasons"])
    lines += ["", "## Limits", "", *[f"- {item}" for item in LIMITS], "", f"Inputs: `{inputs}`", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def run(holdout_name, risk_run, stress_run, broker_evidence=None, turnover_run=None, *, current=None):
    holdout_name = _plain(holdout_name, "holdout name")
    risk_run = _plain(risk_run, "risk run")
    stress_run = _plain(stress_run, "stress run")
    turnover_run = _plain(turnover_run, "turnover run") if turnover_run else None
    holdout_path = settings.DATA_ROOT / "research/family_holdout" / holdout_name / "head.json"
    holdout_protocol_path = settings.DATA_ROOT / "research/family_holdout" / holdout_name / "protocol.json"
    risk_path = settings.ROOT / "outputs/risk_concentration" / risk_run / "run.json"
    stress_path = settings.ROOT / "outputs/stress_exposure" / stress_run / "run.json"
    turnover_path = settings.ROOT / "outputs/turnover_attribution" / turnover_run / "run.json" if turnover_run else None
    holdout = _read(holdout_path, "holdout head")
    holdout_protocol = _read(holdout_protocol_path, "holdout protocol")
    risk = _read(risk_path, "risk receipt")
    stress = _read(stress_path, "stress receipt")
    broker = _read(broker_evidence, "broker evidence") if broker_evidence else None
    turnover = _read(turnover_path, "turnover receipt") if turnover_path else None
    result = evaluate(holdout, risk, stress, broker, turnover, holdout_protocol)
    identifier = (current or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/candidate_admission" / identifier
    artifacts = settings.DATA_ROOT / "research/candidate_admission" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    inputs = {"holdout": str(holdout_path), "holdout_protocol": str(holdout_protocol_path),
              "risk": str(risk_path), "stress": str(stress_path),
              "turnover": str(turnover_path) if turnover_path else None,
              "broker": str(broker_evidence) if broker_evidence else None}
    result.update({"run_id": identifier, "created_at": (current or datetime.now().astimezone()).isoformat(),
                   "inputs": inputs, "artifacts": str(artifacts)})
    write_json(artifacts / "result.json", result)
    write_json(out / "run.json", result)
    _report(out, result, inputs)
    print(json.dumps({"output": str(out), **result}, ensure_ascii=False), flush=True)
    return out, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout-name", required=True)
    parser.add_argument("--risk-run", required=True)
    parser.add_argument("--stress-run", required=True)
    parser.add_argument("--turnover-run")
    parser.add_argument("--broker-evidence", type=Path)
    args = parser.parse_args(argv)
    run(args.holdout_name, args.risk_run, args.stress_run, args.broker_evidence, args.turnover_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
