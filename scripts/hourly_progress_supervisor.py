from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .progress_supervision_status import (
        close_receipt_complete, observed_workers, progress_status, readonly_runtime_ok,
        scheduling_clock, settlement_summary,
    )
else:
    from progress_supervision_status import (
        close_receipt_complete, observed_workers, progress_status, readonly_runtime_ok,
        scheduling_clock, settlement_summary,
    )

PROJECT = Path(__file__).resolve().parents[1]
TRADING_HOME = Path(os.environ.get("QUANT_TRADING_HOME", r"D:\QuantificationData\trading"))
RESEARCH = TRADING_HOME / "research"
_PRIMARY_OUT_DIR = RESEARCH / "progress_supervision"
try:
    _PRIMARY_OUT_DIR.mkdir(parents=True, exist_ok=True)
    _probe = _PRIMARY_OUT_DIR / ".progress_supervision_write_probe"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink(missing_ok=True)
    OUT_DIR = _PRIMARY_OUT_DIR
except (OSError, PermissionError):
    # The scheduled task normally writes the external research home. In a
    # restricted developer session keep the same snapshot schema in-workspace
    # rather than failing the supervisor altogether.
    OUT_DIR = PROJECT / "outputs" / "progress_supervision_fallback"
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def latest_receipt(root: Path, name: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not root.exists():
        return None, None
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    for child in root.iterdir():
        if not child.is_dir() or not re.fullmatch(r"20\d{6}_v\d+", child.name):
            continue
        path = child / name
        if not path.exists():
            continue
        data, _ = read_json(path)
        if isinstance(data, dict):
            day = str(data.get("day") or child.name[:8])
            rows.append((day, path, data))
    if not rows:
        return None, None
    rows.sort(key=lambda item: (item[0], str(item[1])))
    _, path, data = rows[-1]
    return path, data


def latest_operations_receipt(root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    if not root.exists():
        return None, None
    for path in root.glob("*/operations/*/receipt.json"):
        data, _ = read_json(path)
        if isinstance(data, dict):
            day = str(data.get("day") or path.parent.name)
            rows.append((day, path, data))
    if not rows:
        return None, None
    rows.sort(key=lambda item: (item[0], str(item[1])))
    _, path, data = rows[-1]
    return path, data


def latest_settlement_receipt(root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    if root.exists():
        for path in root.glob("*/settlements/*/receipt.json"):
            data, _ = read_json(path)
            if isinstance(data, dict):
                rows.append((str(data.get("day") or path.parent.name), path, data))
    if not rows:
        return None, None
    rows.sort(key=lambda item: (item[0], str(item[1])))
    _, path, data = rows[-1]
    return path, data


def latest_comparison(root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    if not root.exists():
        return None, None
    for path in root.glob("*/comparison.json"):
        data, _ = read_json(path)
        if isinstance(data, dict):
            stamp = str(data.get("created_at") or path.stat().st_mtime)
            rows.append((stamp, path, data))
    if not rows:
        return None, None
    rows.sort(key=lambda item: (item[0], str(item[1])))
    _, path, data = rows[-1]
    return path, data


def get_local_json(url: str) -> tuple[Any | None, str | None]:
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8")), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def exists(rel: str) -> bool:
    return (PROJECT / rel).exists()


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


now = datetime.now(timezone.utc)
close_path, close = latest_receipt(RESEARCH / "bulk_close_cycles", "receipt.json")
system_path, system = latest_receipt(RESEARCH / "system_daily", "head.json")
operations_path, operations = latest_operations_receipt(RESEARCH / "system_daily")
settlement_path, settlement = latest_settlement_receipt(RESEARCH / "system_daily")
calendar_path = RESEARCH / "calendar" / "cn_a_share_2026_sse_v1.json"
calendar, calendar_error = read_json(calendar_path)
clock = scheduling_clock(now, calendar)
clock["calendar_file"] = str(calendar_path)
clock["calendar_read_error"] = calendar_error
due_path, due_close, due_error = None, None, None
if clock["calendar_status"] == "ready":
    due_path = RESEARCH / "bulk_close_cycles" / (clock["latest_due_day"].replace("-", "") + "_v1") / "receipt.json"
    if due_path.exists():
        due_close, due_error = read_json(due_path)
progress = progress_status(clock, close, due_close, system, operations, due_receipt_error=due_error)
saved_settlement = settlement_summary(settlement)
comparison_path, comparison = latest_comparison(PROJECT / "outputs" / "research_comparisons")
health, health_error = get_local_json("http://127.0.0.1:8765/api/health")
opening, opening_error = get_local_json("http://127.0.0.1:8765/api/trading/research/opening-daily")
ui_ok, ui_error = False, None
try:
    with urllib.request.urlopen("http://127.0.0.1:5175", timeout=3) as response:
        ui_ok = response.status == 200
except Exception as exc:
    ui_error = f"{type(exc).__name__}: {exc}"

flags = {
    "candidate_panel_only": True,
    "strategy_validated": False,
    "selection_enabled": False,
    "execution_admitted": False,
}
if isinstance(close, dict):
    for key in flags:
        if key in close:
            flags[key] = bool(close[key])
if isinstance(opening, dict):
    for key in flags:
        if key in opening:
            flags[key] = bool(opening[key])

close_status = "missing"
if isinstance(close, dict):
    status = str(close.get("status") or "")
    if close_receipt_complete(close):
        close_status = (
            "stale_complete_research_only" if progress["latest_receipt_stale"] is True
            else "complete_research_only" if clock["calendar_status"] == "ready"
            else "complete_calendar_unverified"
        )
    elif status:
        close_status = "incomplete_or_unknown"

system_status = "missing"
system_day = None
if isinstance(operations, dict):
    raw_status = str(operations.get("status") or operations.get("state") or "")
    system_day = operations.get("day")
    if raw_status in {"completed", "complete", "success", "completed_saved_research_refresh"}:
        system_status = "completed"
    elif raw_status:
        system_status = raw_status
elif isinstance(system, dict):
    raw_status = str(system.get("status") or system.get("state") or "")
    system_day = system.get("day")
    system_status = "completed" if raw_status in {"completed", "complete", "success", "completed_saved_research_refresh"} else (raw_status or "present")

operation_steps = operations.get("steps") if isinstance(operations, dict) else None
step_count = len(operation_steps) if isinstance(operation_steps, dict) else None
completed_steps = (
    sum(
        1
        for value in operation_steps.values()
        if isinstance(value, dict) and value.get("status") in {"completed", "complete", "success"}
    )
    if isinstance(operation_steps, dict)
    else None
)

workers_started = observed_workers(health)
runtime_ok = readonly_runtime_ok(health, ui_ok)
restricted_runtime_probe = any(
    "WinError 10013" in str(error)
    for error in (health_error, ui_error)
    if error
)

pipeline = {
    "engine": exists("trading/engine.py"),
    "ledger": exists("trading/ledger.py"),
    "broker_adapter": exists("trading/broker.py"),
    "ths_readonly": exists("trading/ths"),
    "paper_recovery": exists("trading/research/paper_recovery_handoff.py"),
    "opening_daily": exists("trading/research/opening_daily_ledger.py"),
}
strategy_evidence = {
    "trend_factor": (PROJECT / "outputs/leader_strategy_research").exists(),
    "alpha158_or_lightgbm": any((PROJECT / name).exists() for name in ("outputs/alpha158", "outputs/shortline_native_frozen", "outputs/shortline_native_preopen")),
    "shortline_board": (PROJECT / "outputs/shortline_native_preopen").exists(),
    "leader_types": (PROJECT / "outputs/leader_eight_type_inputs").exists() or (RESEARCH / "leader_eight_type_inputs").exists(),
    "public_seat": (PROJECT / "outputs/seat_public_probe").exists() or (RESEARCH / "seat_public_probe").exists(),
}
comparison_summary = None
if isinstance(comparison, dict):
    comparison_summary = {
        "artifact": str(comparison_path) if comparison_path else None,
        "start": comparison.get("start"),
        "end": comparison.get("end"),
        "sessions": comparison.get("sessions"),
        "requested_cases": comparison.get("requested_cases"),
        "comparable_cases": comparison.get("comparable_cases"),
        "same_cost_settings": bool(comparison.get("same_cost_settings")),
        "strategy_validated": bool(comparison.get("strategy_validated")),
        "execution_admitted": bool(comparison.get("execution_admitted")),
        "case_metrics": [
            {
                "signal": row.get("signal"),
                "status": row.get("status"),
                "net_return": (row.get("metrics") or {}).get("net_return"),
                "max_drawdown": (row.get("metrics") or {}).get("max_drawdown"),
                "sharpe_zero_rf_252": (row.get("metrics") or {}).get("sharpe_zero_rf_252"),
                "stale_valuation_days": (row.get("metrics") or {}).get("stale_valuation_days"),
            }
            for row in (comparison.get("cases") or [])
            if isinstance(row, dict)
        ],
        "diagnostic_only": True,
        "fresh_holdout": False,
    }

data_status = progress["data_status"]

if runtime_ok:
    software_status = "readonly_workbench_healthy"
elif restricted_runtime_probe:
    software_status = "runtime_probe_unverified_in_restricted_session"
elif isinstance(health, dict) and health.get("status") == "ok" and ui_ok and workers_started is None:
    software_status = "readonly_workbench_workers_unverified"
elif workers_started is True:
    software_status = "readonly_workbench_workers_active"
elif health_error or ui_error:
    software_status = "readonly_workbench_unavailable"
else:
    software_status = "readonly_workbench_degraded"

paper_status = progress["paper_status"]

strategy_status = (
    "same_cost_comparison_available_unvalidated"
    if comparison_summary is not None and any(strategy_evidence.values())
    else "candidate_evidence_present_unvalidated" if any(strategy_evidence.values())
    else "candidate_evidence_missing"
)

# A compact maturity map separates code existence from repeatable evidence and
# strategy validation. It is deliberately descriptive: it never opens an
# execution gate or promotes a candidate.
maturity = {
    "data_snapshot": (
        "research_snapshot_only"
        if progress["due_source_status"] == "complete_research_only"
        else "missing_or_incomplete"
    ),
    "unified_engine_ledger": (
        "code_and_replay_path_present" if all(pipeline[k] for k in ("engine", "ledger", "broker_adapter"))
        else "implementation_gap"
    ),
    "daily_paper_closure": (
        "source_ready_but_publication_cursor_lagging" if paper_status == "source_ready_system_lagging"
        else "partial_routes_with_receipts" if paper_status == "daily_routes_partial"
        else "completed_routes" if paper_status == "daily_routes_completed"
        else "missing_or_unknown"
    ),
    "recovery": "code_path_present" if pipeline["paper_recovery"] else "implementation_gap",
    "ths_readonly": (
        "workbench_healthy_readonly" if runtime_ok and pipeline["ths_readonly"]
        else "implementation_or_runtime_gap"
    ),
    "continuous_monitoring": (
        "hourly_readonly_supervision" if runtime_ok else "monitoring_degraded"
    ),
    "strategy_oos_validation": (
        "same_cost_diagnostic_only" if comparison_summary is not None
        else "unvalidated_candidate_evidence" if any(strategy_evidence.values())
        else "evidence_missing"
    ),
    "live_broker_execution": "disabled_by_policy",
}

next_actions: list[str] = []
if clock["calendar_status"] != "ready":
    next_actions.append("固定交易日历范围不足或不可读，不能判断应完成日期；补准确日历，不猜测跨年交易日")
elif progress["due_source_status"] != "complete_research_only":
    next_actions.append(f"{clock['latest_due_day']}准确收盘包为{progress['due_source_status']}；核对该日原采集回执，不自动重试或改日期")
elif paper_status == "source_ready_system_lagging":
    next_actions.append(f"{clock['latest_due_day']}收盘包已齐，统一日更信号仍在{progress['system_source_day']}；保留实际发表钟，通过现有入口处理漏日，不补造历史信号")
if not runtime_ok and not restricted_runtime_probe:
    next_actions.append("核实research_only工作台与真实workers状态；未知不当关闭，不启动交易worker")
if not all(pipeline.values()):
    next_actions.append("补统一Engine/Ledger或恢复链路缺口")
if paper_status == "daily_routes_partial":
    next_actions.append("保留已完成路线，逐项核对未完成路线；不重跑已成功子路线")
if not flags["strategy_validated"]:
    if comparison_summary is None:
        next_actions.append("用同样本、同成本、同Engine/Ledger完成候选样本外比较")
    else:
        next_actions.append("登记新的不重叠持有窗口，复用相同成本与Engine/Ledger做真正样本外检验")

snapshot = {
    "schema": "quantification_hourly_progress_snapshot_v1",
    "checked_at": now.isoformat(),
    "project": str(PROJECT),
    "trading_home": str(TRADING_HOME),
    "schedule": clock,
    "freshness": {
        **progress,
        "due_receipt": str(due_path) if due_path else None,
        "due_receipt_error": due_error,
    },
    "latest_native_settlement": {
        **saved_settlement, "receipt": str(settlement_path),
    } if saved_settlement is not None else None,
    "latest_close": {
        "receipt": str(close_path) if close_path else None,
        "day": close.get("day") if isinstance(close, dict) else None,
        "status": close_status,
        "source_status": close.get("source_status") if isinstance(close, dict) else None,
        "ordinary_symbols": close.get("ordinary_symbols") if isinstance(close, dict) else None,
        "full_close_package": close.get("full_close_package") if isinstance(close, dict) else None,
        "source_verified": close.get("close_scope", {}).get("source_verified") if isinstance(close, dict) and isinstance(close.get("close_scope"), dict) else None,
        "pit_verified": close.get("close_scope", {}).get("pit_verified") if isinstance(close, dict) and isinstance(close.get("close_scope"), dict) else None,
    },
    "latest_system": {
        "head": str(system_path) if system_path else None,
        "operations_receipt": str(operations_path) if operations_path else None,
        "status": system_status,
        "day": system_day,
        "step_count": step_count,
        "completed_steps": completed_steps,
        "source_day": progress["system_source_day"],
        "lagging_due_day": progress["system_lagging_due_day"],
    },
    "runtime": {
        "api_ok": isinstance(health, dict) and health.get("status") == "ok",
        "ui_ok": ui_ok,
        "mode": health.get("mode") if isinstance(health, dict) else None,
        "live_trading": health.get("live_trading") if isinstance(health, dict) else None,
        "workers_started": workers_started,
        "health_error": health_error,
        "ui_error": ui_error,
    },
    "pipeline": pipeline,
    "strategy_evidence": strategy_evidence,
    "strategy_comparison": comparison_summary,
    "maturity": maturity,
    "status": {
        "data": data_status,
        "software": software_status,
        "paper": paper_status,
        "strategy": strategy_status,
        "execution": "disabled_by_policy",
    },
    "flags": flags,
    "next_actions": next_actions,
    "source_is_future_data": (
        str(close.get("day")) > clock["latest_closed_day"]
        if isinstance(close, dict) and close.get("day") and clock["latest_closed_day"] else None
    ),
    "user_action_required": False,
}

previous_path = OUT_DIR / "latest.json"
previous, _ = read_json(previous_path)
snapshot["comparison_input"] = {
    "schedule": {key: value for key, value in snapshot["schedule"].items() if key != "checked_at_shanghai"},
    "freshness": snapshot["freshness"],
    "latest_native_settlement": snapshot["latest_native_settlement"],
    "latest_close": snapshot["latest_close"],
    "latest_system": snapshot["latest_system"],
    "runtime": snapshot["runtime"],
    "pipeline": snapshot["pipeline"],
    "strategy_evidence": snapshot["strategy_evidence"],
    "strategy_comparison": snapshot["strategy_comparison"],
    "maturity": snapshot["maturity"],
    "status": snapshot["status"],
    "flags": snapshot["flags"],
}
comparison = {
    "previous_snapshot": str(previous_path) if previous_path.exists() else None,
    "changed_since_previous": None,
}
if isinstance(previous, dict):
    comparison["changed_since_previous"] = stable_digest(previous.get("comparison_input", previous)) != stable_digest(snapshot["comparison_input"])
snapshot["comparison"] = comparison

stamp = now.astimezone().strftime("%Y%m%d_%H%M%S")
archive_path = OUT_DIR / f"snapshot_{stamp}.json"
archive_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
latest_path = OUT_DIR / "latest.json"
latest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "schema": snapshot["schema"],
    "checked_at": snapshot["checked_at"],
    "data": data_status,
    "software": software_status,
    "paper": paper_status,
    "strategy": strategy_status,
    "changed_since_previous": comparison["changed_since_previous"],
    "archive": str(archive_path),
    "latest": str(latest_path),
}, ensure_ascii=False))


