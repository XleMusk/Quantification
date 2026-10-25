"""Audit whether an existing financial input run can support F01."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


REQUIRED = ("parent_net_profit", "total_assets")


def audit(input_run: str | Path) -> dict:
    root = Path(input_run).resolve()
    result_path = root / "result.json"
    frame_path = root / "annual_inputs.parquet"
    if not result_path.is_file() or not frame_path.is_file():
        raise FileNotFoundError("annual financial input run is incomplete")
    source = json.loads(result_path.read_text(encoding="utf-8"))
    frame = pd.read_parquet(frame_path)
    absent = sorted(set(REQUIRED) - set(frame))
    periods = sorted(frame["report_day"].dropna().astype(str).unique().tolist()) if "report_day" in frame else []
    observed = {field: int(frame[field + "_status"].eq("observed_numeric").sum())
                for field in REQUIRED if field + "_status" in frame}
    joint = pd.Series(True, index=frame.index)
    for field in REQUIRED:
        if field not in frame or field + "_status" not in frame:
            joint &= False
        else:
            joint &= frame[field + "_status"].eq("observed_numeric")
    reasons = []
    if absent:
        reasons.append("required_fields_absent")
    if not periods or not all(value.endswith("-12-31") for value in periods):
        reasons.append("period_kind_not_annual_only")
    else:
        reasons.append("single_quarter_inputs_missing")
    if source.get("pit_verified") is not True:
        reasons.append("pit_not_verified")
    status = "READY_FOR_F01_FACT_ASSEMBLY" if not reasons else "BLOCKED_DATA"
    return {
        "schema": "f01_input_readiness_audit_v1", "status": status,
        "input_run": str(root), "source_status": source.get("status"),
        "rows": len(frame), "securities": int(frame.symbol.nunique()) if "symbol" in frame else 0,
        "periods": periods, "required_fields": list(REQUIRED), "observed_numeric": observed,
        "joint_required_numeric_rows": int(joint.sum()), "blockers": reasons,
        "pit_verified": source.get("pit_verified") is True, "units_verified": source.get("units_verified") is True,
        "execution_admitted": False, "account_replay": False,
        "limitations": ["This is an input readiness audit, not a factor result or market evaluation.",
                        "Annual consolidated inputs cannot substitute for event-timed single-quarter facts."]}


__all__ = ["REQUIRED", "audit"]
