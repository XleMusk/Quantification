"""Prospective audit of saved market-regime observations; no fitting or trading.

Registration freezes the already published target signals and trailing-only
baselines. Scoring reads closed proxy bars and never reconstructs a prediction.
The 5-session result is an extension diagnostic of the original 1-day signal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any

import pandas as pd

from a_stock_committee.trend_calibration import TREND_HISTORY_DIR
from .exchange_calendar import DEFAULT_CALENDAR_PATH, load_calendar, next_exchange_session
from ..settings import ROOT


TZ = timezone(timedelta(hours=8))
HORIZONS = (1, 5)
NEUTRAL_BAND = 0.0025
BASELINE_LOOKBACK = 160
SCHEMA = "saved_market_regime_prospective_v1"
DEFAULT_OUTPUT = ROOT / "outputs" / "market_regime_scoreboard"


def _clock(value: datetime | None) -> datetime:
    result = value or datetime.now(TZ)
    if result.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return result.astimezone(TZ)


def _label(value: float) -> str:
    if abs(value) <= NEUTRAL_BAND:
        return "震荡"
    return "看多" if value > 0 else "看空"


def _sessions(origin: str, horizon: int, calendar_path: Path) -> tuple[list[str], str]:
    calendar = load_calendar(calendar_path)
    if calendar is None:
        return [], "calendar_artifact_unavailable"
    session = origin
    days = []
    for _ in range(horizon):
        session, evidence = next_exchange_session(session, calendar_path)
        if session is None:
            return [], evidence["status"]
        # The shared helper must not infer a date outside its pinned calendar.
        if date.fromisoformat(session).year != int(calendar["year"]):
            return [], "calendar_year_not_supported"
        days.append(session)
    return days, "resolved_authoritative_static_calendar"


def _history(path: Path, target_id: str, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"target_id": str, "symbol": str})
    required = {"date", "close", "target_id", "symbol"}
    if not required.issubset(frame.columns):
        raise ValueError(f"raw proxy history lacks identity or close: {path}")
    if not frame["target_id"].eq(target_id).all() or not frame["symbol"].eq(symbol).all():
        raise ValueError(f"raw proxy history identity mismatch: {path}")
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    frame["close"] = pd.to_numeric(frame["close"], errors="raise")
    if frame["date"].duplicated().any():
        raise ValueError(f"duplicate raw proxy sessions: {path}")
    if not frame["close"].map(lambda x: math.isfinite(x) and x > 0).all():
        raise ValueError(f"nonpositive or nonfinite raw proxy close: {path}")
    return frame.sort_values("date").reset_index(drop=True)


def _majority(frame: pd.DataFrame, horizon: int, calendar_path: Path) -> dict[str, Any]:
    """Only labels whose exact calendar horizon was closed by the origin exist."""
    closes = dict(zip(frame["date"], frame["close"]))
    latest = frame["date"].iloc[-1]
    labels = []
    for day in frame["date"]:
        sessions, _ = _sessions(day, horizon, calendar_path)
        if sessions and sessions[-1] <= latest and all(d in closes for d in sessions):
            labels.append(_label(closes[sessions[-1]] / closes[day] - 1.0))
    labels = labels[-BASELINE_LOOKBACK:]
    counts = Counter(labels)
    # Fixed tie order, declared before outcomes; no score-dependent selection.
    order = ("震荡", "看多", "看空")
    signal = max(order, key=lambda x: counts.get(x, 0)) if labels else None
    return {"signal": signal, "matured_training_labels": len(labels),
            "class_counts": dict(counts), "training_cutoff": latest,
            "lookback": BASELINE_LOOKBACK, "tie_order": list(order)}


def register_observation(
    report_path: Path,
    output_dir: Path = DEFAULT_OUTPUT,
    history_dir: Path = TREND_HISTORY_DIR,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Freeze a dated report now; registration time can never be backdated."""
    now = _clock(observed_at)
    report_path = Path(report_path).resolve()
    raw = report_path.read_bytes()
    digest = sha256(raw).hexdigest()
    registration_dir = Path(output_dir) / "registrations"
    registration_path = registration_dir / f"{report_path.stem}_{digest[:12]}.json"
    if registration_path.is_file():
        saved = json.loads(registration_path.read_text(encoding="utf-8"))
        if saved.get("schema") != SCHEMA or saved.get("source_sha256") != digest:
            raise ValueError("registration content conflicts with source")
        return {**saved, "registration_path": str(registration_path.resolve()), "reused": True}
    match = re.fullmatch(r"market_regime_(\d{8}_\d{6})", report_path.stem)
    if match is None:
        raise ValueError("expected a timestamped market_regime_YYYYMMDD_HHMMSS.json")
    generation_at = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=TZ)
    modified_at = datetime.fromtimestamp(report_path.stat().st_mtime, TZ)
    publication_bound = max(generation_at, modified_at)
    if publication_bound > now:
        raise ValueError("source publication timestamp is after registration")
    payload = json.loads(raw)
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("dated report has no target observations")
    rows = []
    excluded = []
    for target in targets:
        if target.get("error"):
            excluded.append({"target_id": target.get("target_id"), "reason": target["error"]})
            continue
        if any(target.get(key) is not None for key in ("prediction_date", "actual_ret_pct", "correct")):
            raise ValueError("evaluated/reconstructed forecasts cannot be registered prospectively")
        target_id, symbol = str(target["target_id"]), str(target["symbol"])
        if any(re.fullmatch(r"[a-zA-Z0-9_]+", value) is None for value in (target_id, symbol)):
            raise ValueError("unsafe target/symbol in raw cache path")
        diagnostics = target["diagnostics"]
        origin = str(diagnostics["pred_base_date"])
        if origin != target["latest_date"]:
            raise ValueError("prediction origin differs from latest input date")
        origin_day = date.fromisoformat(origin)
        origin_session, _ = next_exchange_session((origin_day - timedelta(days=1)).isoformat(), Path(calendar_path))
        if origin_session != origin:
            raise ValueError("prediction origin is not a pinned exchange session")
        source_close_at = datetime.combine(origin_day, time(15, 0), TZ)
        if generation_at < source_close_at or now < source_close_at:
            raise ValueError("report generated before origin session closed")
        source = Path(history_dir) / f"{target_id}__{symbol}.csv"
        frame = _history(source, target_id, symbol)
        trailing = frame[frame["date"] <= origin].copy()
        base = trailing.loc[trailing["date"] == origin, "close"]
        if len(base) != 1:
            raise ValueError(f"origin raw close missing: {target_id} {origin}")
        close = float(base.iloc[0])
        if not math.isclose(close, float(diagnostics["close"]), abs_tol=0.00051, rel_tol=0):
            raise ValueError(f"report/raw origin close mismatch: {target_id} {origin}")
        if target["signal"] not in ("看多", "看空", "震荡"):
            raise ValueError("unsupported saved signal")
        sessions, status = _sessions(origin, max(HORIZONS), Path(calendar_path))
        next_open = datetime.combine(date.fromisoformat(sessions[0]), time(9, 30), TZ) if sessions else None
        eligible = next_open is not None and now < next_open and publication_bound < next_open
        rows.append({"target_id": target_id, "name": target.get("name", target_id),
                     "symbol": symbol, "origin": origin, "origin_close": close,
                     "score": target["score"], "signal": target["signal"],
                     "params": target["params"], "train_accuracy": target.get("train_accuracy"),
                     "event_adjustment": target.get("event_adjustment"),
                     "raw_history_path": str(source.resolve()),
                     "calendar_status": status, "next_session_open_at": next_open.isoformat() if next_open else None,
                     "prospective_eligible": eligible,
                     "eligibility_reason": "registered_before_next_session_open" if eligible else
                         ("registered_after_next_session_open" if next_open else status),
                     "baselines": {str(h): _majority(trailing, h, Path(calendar_path)) for h in HORIZONS}})
    keys = [(row["target_id"], row["origin"]) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate target/origin in report")
    for prior in sorted(registration_dir.glob("*.json")):
        old = json.loads(prior.read_text(encoding="utf-8"))
        overlap = set(keys) & {(row["target_id"], row["origin"]) for row in old["targets"]}
        if overlap:
            raise ValueError(f"target/origin already frozen; no same-day report replacement: {sorted(overlap)}")
    saved = {"schema": SCHEMA, "registered_at": now.isoformat(),
             "source_report": str(report_path), "source_sha256": digest,
             "source_generation_at": generation_at.isoformat(),
             "source_mtime_at": modified_at.isoformat(),
             "publication_time_evidence": "filename plus filesystem mtime; registration is the prospective boundary",
             "regime": payload.get("regime"), "targets": rows, "excluded": excluded,
             "calendar_path": str(Path(calendar_path).resolve()),
             "calendar_snapshot": load_calendar(Path(calendar_path)),
             "horizons": list(HORIZONS), "neutral_band": NEUTRAL_BAND,
             "five_session_interpretation": "fixed original one-day signal extension diagnostic",
             "model_refit": False, "future_labels_used_for_score": False,
             "selection_enabled": False, "strategy_validated": False, "execution_admitted": False}
    registration_dir.mkdir(parents=True, exist_ok=True)
    with registration_path.open("x", encoding="utf-8") as stream:
        json.dump(saved, stream, ensure_ascii=False, indent=2)
    return {**saved, "registration_path": str(registration_path.resolve()), "reused": False}


def _successor_source(path: Path, registrations: list[Path], now: datetime):
    """Read an explicitly supplied receipt; never rewrite a registered history."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    record = json.loads(raw)
    registration = Path(record["registration"]).resolve()
    if (record.get("schema") != "registered_market_proxy_bounded_successor_source_v1"
            or record.get("status") != "completed_bounded_source_queries"
            or registration not in {p.resolve() for p in registrations}
            or sha256(registration.read_bytes()).hexdigest() != record.get("registration_sha256")
            or (record.get("query_method"), record.get("fields"), record.get("frequency"), record.get("adjustflag"))
                != ("query_history_k_data_plus", "date,code,close", "d", "3")
            or any(record.get(key) is not False for key in
                ("old_caches_modified", "model_refit", "execution_admitted", "pit_verified", "source_independently_verified"))):
        raise ValueError("Successor source receipt/registration/raw-close protocol differs")
    saved = json.loads(registration.read_text(encoding="utf-8"))
    if load_calendar(Path(saved["calendar_path"])) != saved["calendar_snapshot"]:
        raise ValueError("Successor source cannot use a changed registered calendar")
    targets = {(t["target_id"], t["symbol"]): t for t in saved["targets"]}
    entries = record.get("entries", [])
    if (len(targets) != len(saved["targets"]) or len(entries) != len(targets)
            or record.get("max_queries") != len(targets)
            or {(e["target_id"], e["registered_symbol"]) for e in entries} != set(targets)):
        raise ValueError("Successor source must preserve every registered target exactly once")
    dates = record["expected_dates"]
    origins = {t["origin"] for t in targets.values()}
    if (not isinstance(dates, list) or not 2 <= len(dates) <= max(HORIZONS) + 1
            or len(origins) != 1 or dates[0] != next(iter(origins))
            or dates != [dates[0], *_sessions(dates[0], len(dates) - 1, Path(saved["calendar_path"]))[0]]
            or record.get("source_start") != dates[0] or record.get("source_end") != dates[-1]):
        raise ValueError("Successor dates must be the exact registered-origin exchange-session prefix")

    def clock(value):
        point = datetime.fromisoformat(value)
        if point.tzinfo is None or point > now:
            raise ValueError("Successor source has a naive or future acquisition clock")
        return point.astimezone(TZ)

    started, finished = clock(record["started_at"]), clock(record["finished_at"])
    if not datetime.combine(date.fromisoformat(dates[-1]), time(15, 0), TZ) <= started <= finished:
        raise ValueError("Successor source was not actually acquired after its final close")
    values, evidence = {}, []
    for entry in entries:
        target = targets[(entry["target_id"], entry["registered_symbol"])]
        symbol = target["symbol"]
        if re.fullmatch(r"\d{6}", symbol):
            code = ("sh." if symbol[0] in "56" else "sz.") + symbol
        elif re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
            code = symbol[:2] + "." + symbol[2:]
        else:
            raise ValueError("Unsupported registered successor source symbol")
        source = Path(entry["file"]).resolve()
        source.relative_to(path.parent)
        data = source.read_bytes()
        if (sha256(data).hexdigest() != entry.get("sha256") or entry.get("provider_code") != code
                or entry.get("error_code") != "0" or entry.get("fields") != ["date", "code", "close"]
                or not started <= clock(entry["started_at"]) <= clock(entry["completed_at"]) <= finished):
            raise ValueError("Successor source hash, identity or acquisition clock differs")
        frame = pd.read_csv(source, dtype=str, keep_default_na=False)
        if (list(frame) != ["date", "code", "close"] or len(frame) != entry.get("rows")
                or frame.date.duplicated().any() or not frame.code.eq(code).all()
                or sorted(frame.date.tolist()) != entry.get("dates") or not set(frame.date) <= set(dates)
                or any(date.fromisoformat(day).isoformat() != day for day in frame.date)):
            raise ValueError("Successor CSV date/code/row identities differ")
        expected_status = "exact_dates_returned" if frame.date.tolist() == dates else "source_response_incomplete_preserved"
        if entry.get("status") != expected_status:
            raise ValueError("Successor response completeness differs from saved raw rows")
        closes = dict(zip(frame.date, pd.to_numeric(frame.close, errors="raise")))
        if not all(math.isfinite(value) and value > 0 for value in closes.values()):
            raise ValueError("Successor source has unknown/nonpositive close; never impute it")
        origin = closes.get(target["origin"])
        matches = origin is not None and math.isclose(origin, target["origin_close"], abs_tol=1e-9, rel_tol=0)
        status = ("empty_success_no_prices" if not closes else "blocked_source_origin_missing" if origin is None
                  else "accepted_exact_origin" if matches else "blocked_registered_origin_precision_conflict")
        proof = {"target_id": target["target_id"], "symbol": symbol, "provider_code": code,
                 "file": str(source), "sha256": entry["sha256"], "available_at": entry["completed_at"],
                 "dates": sorted(closes), "status": status, "source_origin_close": origin,
                 "registered_origin_close": target["origin_close"], "origin_matches_strict_tolerance": matches}
        values[(str(registration), target["target_id"], symbol)] = (closes, proof)
        evidence.append(proof)
    return {"receipt": {"file": str(path), "sha256": sha256(raw).hexdigest()},
            "registration": {"file": str(registration), "sha256": record["registration_sha256"]},
            "actual_available_at": record["finished_at"], "targets": evidence,
            "history_files_modified": False, "source_independently_verified": False, "pit_verified": False}, values


def score_observations(
    output_dir: Path = DEFAULT_OUTPUT, *, as_of: str | None = None,
    observed_at: datetime | None = None,
    successor_source: Path | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    now = _clock(observed_at)
    cutoff = date.fromisoformat(as_of) if as_of else now.date()
    if cutoff > now.date():
        raise ValueError("as_of must not be in the future")
    if cutoff == now.date() and now.time().replace(tzinfo=None) < time(15, 0):
        cutoff -= timedelta(days=1)
    registrations = sorted((Path(output_dir) / "registrations").glob("*.json"))
    successor_proof, successor_values = (_successor_source(successor_source, registrations, now)
                                         if successor_source is not None else (None, {}))
    rows = []
    for path in registrations:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("schema") != SCHEMA or saved.get("horizons") != list(HORIZONS) or saved.get("neutral_band") != NEUTRAL_BAND:
            raise ValueError(f"unsupported registration protocol: {path}")
        calendar_path = Path(saved["calendar_path"])
        calendar_changed = load_calendar(calendar_path) != saved["calendar_snapshot"]
        for target in saved["targets"]:
            source = Path(target["raw_history_path"])
            frame = _history(source, target["target_id"], target["symbol"])
            closes = dict(zip(frame.loc[frame["date"] <= cutoff.isoformat(), "date"],
                              frame.loc[frame["date"] <= cutoff.isoformat(), "close"]))
            extra = successor_values.get((str(path.resolve()), target["target_id"], target["symbol"]))
            source_problem = None
            if extra:
                supplied, proof = extra
                if proof["status"].startswith("blocked_"):
                    source_problem = proof["status"]
                elif proof["status"] == "accepted_exact_origin":
                    additions = {day: value for day, value in supplied.items()
                                 if target["origin"] < day <= cutoff.isoformat()}
                    if any(day in closes and not math.isclose(closes[day], value, abs_tol=1e-9, rel_tol=0)
                           for day, value in additions.items()):
                        source_problem = "existing_successor_close_conflicts_with_opt_in_source"
                    else:
                        closes.update(additions)
            for h in HORIZONS:
                sessions, calendar_status = _sessions(target["origin"], h, calendar_path)
                row = {"registration": str(path.resolve()), "registered_at": saved["registered_at"],
                       "target_id": target["target_id"], "name": target["name"], "symbol": target["symbol"],
                       "origin": target["origin"], "horizon": h,
                       "target_session": sessions[-1] if sessions else None,
                       "signal": target["signal"], "score": target["score"],
                       "prospective_eligible": target["prospective_eligible"],
                       "eligibility_reason": target["eligibility_reason"],
                       "majority_signal": target["baselines"][str(h)]["signal"],
                       "nochange_signal": "震荡", "status": "pending", "actual_return": None,
                       "actual_signal": None, "correct": None, "majority_correct": None,
                       "nochange_correct": None, "included_in_prospective_metrics": False}
                if extra:
                    row["successor_source"] = proof
                    if source_problem and sessions and sessions[-1] <= cutoff.isoformat() and all(
                            day in supplied for day in [target["origin"], *sessions]):
                        diagnostic = supplied[sessions[-1]] / supplied[target["origin"]] - 1.0
                        row["excluded_source_diagnostic"] = {
                            "return_basis": "new_source_origin_and_target_only_not_registered_score",
                            "source_actual_return": diagnostic, "source_actual_signal": _label(diagnostic),
                            "registered_signal": target["signal"], "direction_agrees": target["signal"] == _label(diagnostic),
                            "included_in_prospective_metrics": False}
                if calendar_changed:
                    row.update(status="blocked", reason="registered_calendar_changed")
                elif not sessions:
                    row.update(status="blocked", reason=calendar_status)
                elif source_problem:
                    row.update(status="blocked", reason=source_problem)
                elif target["origin"] not in closes:
                    row.update(status="blocked", reason="origin_raw_close_missing")
                elif not math.isclose(closes[target["origin"]], target["origin_close"], abs_tol=1e-9, rel_tol=0):
                    row.update(status="blocked", reason="registered_origin_close_revised")
                elif sessions[-1] > cutoff.isoformat():
                    row["reason"] = "next_closed_session_not_yet_due"
                elif any(day not in closes for day in sessions):
                    row["reason"] = "required_successor_raw_closed_bar_missing"
                    row["missing_sessions"] = [day for day in sessions if day not in closes]
                else:
                    actual = closes[sessions[-1]] / target["origin_close"] - 1.0
                    direction = _label(actual)
                    row.update(status="scored", reason="exact_calendar_raw_close_observed", actual_return=actual,
                               actual_signal=direction, correct=target["signal"] == direction,
                               majority_correct=row["majority_signal"] == direction if row["majority_signal"] else None,
                               nochange_correct=direction == "震荡",
                               included_in_prospective_metrics=target["prospective_eligible"])
                rows.append(row)
    summary = {}
    for h in HORIZONS:
        scope = [row for row in rows if row["horizon"] == h]
        scored = [row for row in scope if row["included_in_prospective_metrics"]]
        summary[str(h)] = {"registered_targets": len(scope), "status_counts": dict(Counter(r["status"] for r in scope)),
                           "prospective_scored": len(scored), "unique_origin_days": len({r["origin"] for r in scored}),
                           "signal_accuracy": sum(r["correct"] for r in scored) / len(scored) if scored else None,
                           "majority_accuracy": _accuracy(scored, "majority_correct"),
                           "nochange_accuracy": _accuracy(scored, "nochange_correct"),
                           "retrospective_scored_excluded": sum(r["status"] == "scored" and not r["prospective_eligible"] for r in scope)}
    result = {"schema": "market_regime_scoreboard_v1", "observed_at": now.isoformat(),
              "closed_as_of": cutoff.isoformat(), "registrations": len(registrations),
              "summary_by_horizon": summary, "rows": rows, "model_refit": False,
              "future_labels_used_for_score": False, "selection_enabled": False,
              "strategy_validated": False, "execution_admitted": False,
              "limitations": ["Training accuracy is not prospective accuracy.",
                  "5-session labels evaluate a frozen 1-day signal extension; no 5-day fitted forecast.",
                  "Proxy caches are local unadjusted daily closes; independent validation and corporate action evidence are not supplied here.",
                  "Targets and overlapping horizons are correlated; target counts are not independent market days.",
                  "Market regime describes observed structure; target forecast accuracy cannot prove knowledge of capital intent."]}
    if successor_proof is not None:
        result["successor_source"] = successor_proof
        result["limitations"].append("Explicit successor receipt adds only exact-origin matches in memory; conflicts remain excluded diagnostics, empty success remains unknown.")
    destination = Path(report_dir) if report_dir is not None else Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scoreboard.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 大盘环境事前核验", "", f"登记报告 {len(registrations)} 份；可使用已收盘日期截至 {cutoff.isoformat()}。",
             "", "|持有诊断周期|目标数|事前已核验|原信号命中|多数类基线|不变基线|状态|", "|---|---:|---:|---|---|---|---|"]
    for h, metrics in summary.items():
        fmt = lambda value: f"{value:.1%}" if value is not None else "待后继收盘"
        lines.append(f"|{h}交易日|{metrics['registered_targets']}|{metrics['prospective_scored']}|{fmt(metrics['signal_accuracy'])}|{fmt(metrics['majority_accuracy'])}|{fmt(metrics['nochange_accuracy'])}|{metrics['status_counts']}|")
    pending_days = sorted({row["target_session"] for row in rows if row["status"] == "pending" and row["target_session"]})
    if pending_days:
        lines.extend(["", "待核验的确切交易日：" + "、".join(pending_days) + "；只有相应 raw 收盘已形成才能计成绩。"])
    if successor_proof is not None:
        lines += ["", "本次显式读取后继原件，仅在内存追加原点严格一致的收盘；旧缓存、登记、信号和基线均未重建。",
                  "本次只有一个预测原点市场日，多个相关标的命中不能证明预测能力。",
                  "空成功响应保留缺价；原点精度冲突不计入成绩，也不放宽原点检查。"]
        for row in rows:
            diagnostic = row.get("excluded_source_diagnostic")
            if diagnostic:
                lines.append(f"{row['name']}：原登记{row['signal']}；新源{row['target_session']}实际{diagnostic['source_actual_signal']}"
                             f"（{diagnostic['source_actual_return']:.4%}），因{row['reason']}排除于成绩，保留方向核对。")
    lines.extend(["", "登记后固定报告信号、参数和仅用基准日以前已成熟标签构造的160条多数类基线。晚于下一交易日开盘的登记不计事前成绩。",
                  "5日是固定1日信号的延伸诊断，不代表模型已预测5日。训练命中率、环境标签和资金意图不能互相替代。",
                  "", "execution_admitted=false；不拟合模型、不调整阈值、不生成交易命令。", ""])
    (destination / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def _accuracy(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [row[field] for row in rows if row[field] is not None]
    return sum(values) / len(values) if values else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--report", type=Path, required=True)
    register.add_argument("--history-dir", type=Path, default=TREND_HISTORY_DIR)
    register.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    register.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    score = commands.add_parser("score")
    score.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    score.add_argument("--as-of", default=None)
    score.add_argument("--successor-source", type=Path, help="Explicit saved raw-close receipt; no cache/registration replacement")
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register_observation(args.report, args.output_dir, args.history_dir, args.calendar)
        print(json.dumps({key: result[key] for key in ("registration_path", "registered_at", "reused")}, ensure_ascii=False))
    else:
        result = score_observations(args.output_dir, as_of=args.as_of, successor_source=args.successor_source)
        print(json.dumps(result["summary_by_horizon"], ensure_ascii=False, indent=2))
        print(str((args.output_dir / "REPORT.md").resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
