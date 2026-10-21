"""Explicit small summaries of saved native low-volatility shared-ledger runs.

The CLI reads a completed run once. Read-only views check its four exact saved
outputs without loading the large result/registration JSON or recalculating it.
"""
from __future__ import annotations

import argparse
import csv
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from ..contracts import MARKET_TZ


SCHEMA = "native_lowvol_shared_ledger_summary_v1"
SOURCE_SCHEMA = "saved_lowvol_native_whole_quotes_reported_actions_full_exit_replay_v1"
MULTIYEAR_SOURCE_SCHEMA = "saved_lowvol_native_multiyear_whole_quotes_reported_actions_full_exit_replay_v1"
SOURCE_SCHEMAS = {SOURCE_SCHEMA, MULTIYEAR_SOURCE_SCHEMA}
FAMILY = "native_lowvol_shared_ledger_diagnostic"
FILES = ("result.json", "registration.json", "account.json", "daily.csv")
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "returns_reportable": False,
    "total_return_verified": False, "pit_verified": False,
    "fresh_holdout_claimed": False, "account_mutated": False,
    "model_refit": False, "signal_features_recomputed": False}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _sha(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refs(directory):
    return {name: {"path": str(directory / name), "sha256": _sha(directory / name)} for name in FILES}


def _check_refs(summary):
    directory = Path(summary["source_run_directory"]).resolve()
    refs = summary["source_refs"]
    if set(refs) != set(FILES):
        raise ValueError("Four exact native replay output pins are required")
    for name in FILES:
        ref = refs[name]
        if (Path(ref["path"]).resolve() != directory / name
                or len(ref["sha256"]) != 64 or _sha(directory / name) != ref["sha256"]):
            raise ValueError(f"Saved native replay source changed: {name}")


def _count(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f"Nonnegative saved {field} required")
    return value


def _finite(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)):
        raise ValueError("Saved metrics must remain finite or explicitly null")
    return value


def _multiyear_calendar(record):
    """Retain the producer's continuous calendar, never combine annual accounts."""
    first, last = int(record["start"][:4]), int(record["end"][:4])
    proof = record.get("calendar_method", {})
    years = proof.get("years", [])
    files = proof.get("files", [])
    if (first >= last or not years or any(type(year) is not int for year in years)
            or years != list(range(min(years), last + 1)) or first not in years
            or proof.get("method_id") != "continuous_explicit_year_exchange_calendars_v1"
            or proof.get("missing_years_inferred") is not False
            or [item.get("year") for item in files] != years
            or len({item.get("path") for item in files}) != len(years)
            or any(not item.get("path") or len(item.get("sha256", "")) != 64 for item in files)
            or record.get("account_year_boundary_reset") is not False):
        raise ValueError("A saved multi-year summary requires the original continuous calendar and no year reset")
    return deepcopy(proof)


def build_summary(run_dir, out):
    """Write a new separate summary; never alter or replay the saved account."""
    directory, destination = Path(run_dir).resolve(), Path(out).resolve()
    if destination.exists():
        raise FileExistsError("A saved replay summary needs a new output file")
    if destination == directory or directory in destination.parents or destination in directory.parents:
        raise ValueError("Summary output must remain outside the saved replay directory")
    pins = _refs(directory)
    result, registration, state = [_json(directory / name) for name in FILES[:3]]
    source_schema = result.get("schema")
    if (source_schema not in SOURCE_SCHEMAS or result.get("status") != "completed"
            or result.get("error") is not None or result.get("registration") != registration
            or registration.get("method_id") != source_schema or state.get("broker_binding")
            or any(result.get(k) is not v for k, v in FLAGS.items()
                if k in ("candidate_panel_only", "execution_admitted", "selection_enabled",
                    "strategy_validated", "returns_reportable", "total_return_verified", "model_refit",
                    "signal_features_recomputed"))
            or registration.get("research_only") is not True
            or registration.get("execution_admitted") is not False):
        raise ValueError("A completed unadmitted native shared-ledger replay is required")
    calendar = _multiyear_calendar(registration) if source_schema == MULTIYEAR_SOURCE_SCHEMA else None
    sessions = _count(result["completed_sessions"], "completed sessions")
    if sessions != _count(result["requested_sessions"], "requested sessions") or not sessions:
        raise ValueError("An incomplete run cannot be displayed as a completed replay")
    with (directory / "daily.csv").open(encoding="utf-8-sig", newline="") as stream:
        daily = list(csv.DictReader(stream))
    dates = [row["date"] for row in daily]
    if (len(daily) != sessions or dates != sorted(set(dates)) or dates[0] != registration["start"]
            or dates[-1] != registration["end"] or state.get("day") != dates[-1]):
        raise ValueError("Saved daily/account cursor differs from the completed replay")
    orders, fills = _count(result["orders"], "orders"), _count(result["fills"], "fills")
    if orders != len(state["orders"]) or fills != len(state["fills"]) or type(state.get("cash")) is not int:
        raise ValueError("Saved account order/fill/cash evidence differs from the result")
    if Decimal(daily[-1]["cash"]) * 100 != state["cash"]:
        raise ValueError("Saved final daily cash differs from the account cents")
    saved_metrics = result.get("metrics")
    if saved_metrics is not None:
        if (not isinstance(saved_metrics, dict) or saved_metrics.get("sessions") != sessions or not fills
                or Decimal(str(saved_metrics["final_equity"])) != Decimal(daily[-1]["equity"])):
            raise ValueError("Saved metrics need the same completed daily/fill evidence")
        for value in saved_metrics.values():
            _finite(value)
    elif fills:
        raise ValueError("A completed traded replay must retain its saved metrics")
    reused = _count(result.get("reused_sessions", 0), "reused sessions")
    new = _count(result.get("newly_completed_sessions", sessions), "new sessions")
    if reused + new != sessions:
        raise ValueError("Saved reused/new session counts do not cover the replay")
    symbols = registration["execution_source"]["requested_symbols"]
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("Frozen native replay symbol scope required")
    summary = {"schema": SCHEMA, "status": "saved_native_shared_ledger_summary",
        "created_at": datetime.now(MARKET_TZ).isoformat(), "source_run_directory": str(directory),
        "source_schema": source_schema, "source_refs": pins,
        "start": dates[0], "end": dates[-1], "sessions": sessions, "symbols": len(symbols),
        "reused_sessions": reused, "newly_completed_sessions": new,
        "resume_from": result.get("resume_from"), "metrics": saved_metrics,
        "orders": orders, "fills": fills, "cash_cents": state["cash"],
        "final_equity_rmb": None if saved_metrics is None else saved_metrics["final_equity"],
        "open_positions": result.get("open_positions"),
        "remaining_full_exits": result.get("remaining_full_exits"),
        "pending_targets": result.get("pending_targets"),
        "config": registration["config"], "metric_basis": result["metric_basis"],
        "shared_engine_ledger": True, "new_cash_account_created": False if reused else True,
        "basis": "独立原生原价与已保存公司行动组成项，经共用Engine/Ledger；沿原账户接续，非9月30日排名初始化的新账户",
        "limitations": ["这是已观察历史窗口的研究诊断，保留原负收益；未重新排名、拟合或回放。",
            "原生原价口径独立于旧StockDB混合前收盘口径，不能将两者当相同账户或纯信号对照。",
            "公司行动资料为事后取得，PIT及完整总回报未验证；不作可报告投资收益或当前信号。",
            "持仓、待退出任务及未清仓状态按原件保留；不属于19日bootstrap纸面首仓账户。"],
        **FLAGS}
    if calendar is not None:
        summary.update(calendar_method=calendar, account_year_boundary_reset=False)
    _check_refs(summary)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return summary


def load_summary(path):
    """Read only the small summary, then stream-check its exact saved outputs."""
    summary = _json(path)
    if (summary.get("schema") != SCHEMA or summary.get("source_schema") not in SOURCE_SCHEMAS
            or summary.get("status") != "saved_native_shared_ledger_summary"
            or summary.get("shared_engine_ledger") is not True
            or any(summary.get(k) is not v for k, v in FLAGS.items())):
        raise ValueError("Unsupported saved native replay summary or research boundary")
    if summary["source_schema"] == MULTIYEAR_SOURCE_SCHEMA:
        _multiyear_calendar(summary)
    try:
        _check_refs(summary)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {**summary, "status": "unavailable_saved_sources_changed", "metrics": None,
            "error": f"{type(error).__name__}: {error}"}
    return summary


def catalog_fingerprint(directory):
    """Invalidate board caches even if source size and modification time revert."""
    values = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            record = _json(path)
            values.append([str(path), _sha(path), [[name, _sha(ref["path"])]
                for name, ref in sorted(record.get("source_refs", {}).items())]])
        except (OSError, ValueError, KeyError, TypeError) as error:
            values.append([str(path), type(error).__name__, str(error)])
    return sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_summary(args.run_dir, args.out)
    print(json.dumps({k: result[k] for k in ("schema", "status", "start", "end", "sessions",
        "reused_sessions", "newly_completed_sessions", "orders", "fills", "metrics")}, ensure_ascii=False))
    return 0
