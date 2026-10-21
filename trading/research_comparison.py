"""Compare pinned saved ledger accounts on an explicit common calendar window."""
import argparse
import ast
import csv
from datetime import date, datetime
import json
import math
from pathlib import Path
import uuid

from . import replay_reports, settings
from .report import metrics

COST_FIELDS = ("initial_cash", "commission", "minimum_commission", "sell_tax",
               "transfer_fee", "slippage_bps", "participation", "dividend_tax_reserve")

EXTERNAL_REPLAY_DIRS = {
    "factor_candidate_cost_aware_execution": "factor_candidate_cost_replays",
    "factor_candidate_fixed_cadence": "factor_candidate_cadence_replays",
    "factor_breadth_regime_overlay": "factor_regime_overlays",
    "factor_execution_projection": "factor_execution_projections",
    "alpha158_turnover_buffer_ensemble": "market_replay",
}


def _read_external_replay(identifier):
    """Load completed research-only replays that use the shared daily-mark format."""
    for family, output_dir in EXTERNAL_REPLAY_DIRS.items():
        run_path = settings.ROOT / "outputs" / output_dir / identifier / "run.json"
        if not run_path.exists():
            continue
        record = json.loads(run_path.read_text(encoding="utf-8"))
        artifacts = Path(record["artifacts"])
        daily_paths = list(artifacts.rglob("daily.csv"))
        if len(daily_paths) != 1:
            raise ValueError(f"Expected one daily.csv for external replay {identifier}")
        marks = []
        with daily_paths[0].open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                row["equity"] = float(row["equity"])
                row["fees"] = float(row["fees"])
                row["turnover_value"] = float(row["turnover_value"])
                stale = row.get("stale_symbols", "[]")
                row["stale_symbols"] = ast.literal_eval(stale) if stale else []
                marks.append(row)
        result = dict(record.get("result") or {})
        result.setdefault("signal", record["signal"])
        result.setdefault("status", "completed" if record["status"].startswith("completed") else record["status"])
        record["results"] = [result]
        record.setdefault("scope", "historical_factor_replay")
        record.setdefault("symbols", None)
        record.setdefault("dataset", record.get("inputs", {}))
        record.setdefault("fund_dataset", None)
        portfolio = record.get("portfolio") or record.get("variant_config")
        if not isinstance(portfolio, dict):
            raise ValueError(f"External replay {identifier} has no saved portfolio configuration")
        record["protocol"] = {
            "fractional_allocation": None,
            "limitations": record.get("limitations", []),
        }
        value = {
            "daily_marks": marks,
            "config": portfolio,
            "status": "completed" if result["status"] == "completed" else result["status"],
        }
        return record, value
    return None, None


def _read_saved_replay(identifier):
    """Read either a normal replay or a compatible research-only external replay."""
    try:
        record, _ = replay_reports._read(identifier)
        return record, None
    except (ValueError, FileNotFoundError) as error:
        record, value = _read_external_replay(identifier)
        if record is None:
            raise error
        return record, value


def nondominated(rows):
    eligible = [row for row in rows if row["status"] == "comparable"
                and not row.get("fractional_allocation_assumption")
                and row["metrics"]["stale_valuation_days"] == 0
                and row["metrics"]["sharpe_zero_rf_252"] is not None
                and math.isfinite(row["metrics"]["sharpe_zero_rf_252"])]
    keys = ("net_return", "sharpe_zero_rf_252", "max_drawdown")
    def dominates(first, second):
        pairs = [(first["metrics"][key], second["metrics"][key]) for key in keys]
        return all(a >= b for a, b in pairs) and any(a > b for a, b in pairs)
    return [row["case_id"] for row in eligible
            if not any(dominates(other, row) for other in eligible if other is not row)]


def _period_metrics(marks, selected, start, end, label, fallback_opening=None):
    """Summarize one saved-mark subperiod without resetting the account."""
    period = [mark for mark in selected if start <= mark["date"] <= end]
    if not period:
        return {"label": label, "status": "no_saved_marks", "start": start, "end": end}
    first = marks.index(period[0])
    opening = marks[first - 1]["equity"] if first else fallback_opening
    if opening is None:
        return {"label": label, "status": "missing_preperiod_equity", "start": period[0]["date"],
                "end": period[-1]["date"], "sessions": len(period)}
    value = metrics(period, opening)
    return {"label": label, "status": "comparable", "start": period[0]["date"],
            "end": period[-1]["date"], "opening_equity": opening, **value}


def _annual_periods(selected):
    """Return fixed calendar-year slices present in the requested window."""
    years = sorted({mark["date"][:4] for mark in selected})
    return [(year, f"calendar_year_{year}") for year in years]


def _library_impact(rows):
    """Compare saved leave-outs with the saved full candidate, without refitting."""
    base = [row for row in rows if row.get("family") == "factor_candidate_ridge"
            and row.get("status") == "comparable"]
    leaveouts = [row for row in rows if row.get("family") == "factor_candidate_library_leave_out"
                 and row.get("status") == "comparable"]
    if len(base) != 1 or not leaveouts:
        return []
    base = base[0]
    results = []
    for row in leaveouts:
        full = {key: row["metrics"][key] - base["metrics"][key]
                for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees")
                if row["metrics"].get(key) is not None and base["metrics"].get(key) is not None}
        periods = []
        base_periods = {period["label"]: period for period in base.get("period_metrics", [])
                        if period.get("status") == "comparable"}
        for period in row.get("period_metrics", []):
            reference = base_periods.get(period.get("label"))
            if period.get("status") != "comparable" or reference is None:
                continue
            delta = {key: period[key] - reference[key]
                     for key in ("net_return", "max_drawdown", "sharpe_zero_rf_252", "fees")
                     if period.get(key) is not None and reference.get(key) is not None}
            periods.append({"label": period["label"], **delta})
        results.append({"leave_out_case": row["label"], "leave_out_signal": row["signal"],
                        "base_case": base["label"], "base_signal": base["signal"],
                        "full_window_delta": full, "period_deltas": periods,
                        "diagnostic_only": True})
    return results


def compare(run_ids, start, end):
    run_ids = list(run_ids)
    if (not 1 <= len(run_ids) <= 16 or len(set(run_ids)) != len(run_ids)
            or any(not replay_reports.RUN_ID.fullmatch(identifier) for identifier in run_ids)):
        raise ValueError("Choose1-16 distinct saved research run IDs")
    if (date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end
            or start > end):
        raise ValueError("Choose an explicit ordered canonical comparison window")
    rows, calendar, signatures = [], None, []
    for identifier in run_ids:
        record, external_value = _read_saved_replay(identifier)
        signals = [row["signal"] for row in record["results"]]
        if not signals or len(signals) != len(set(signals)):
            raise ValueError("Saved run must expose distinct actual ledger cases")
        for result in record["results"]:
            signal = result["signal"]
            row = {"case_id": identifier + ":" + signal, "run_id": identifier, "signal": signal,
                   "label": result.get("label") or signal, "family": record.get("research_family", "rules"),
                   "source_period": {"start": record["start"], "end": record["end"]},
                   "scope": record["scope"], "symbols": record["symbols"], "dataset": record["dataset"],
                   "fund_dataset": record.get("fund_dataset"),
                   "metrics": None, "opening_equity": None, "account_reset": False,
                   "strategy_validated": False, "execution_admitted": False}
            row["fractional_allocation_assumption"] = record["protocol"].get("fractional_allocation")
            row["source_limitations"] = record["protocol"].get("limitations", [])
            if record["protocol"].get("entry_risk_policy"):
                row.update(entry_risk_policy=record["protocol"]["entry_risk_policy"],
                           entry_risk_blocks=result.get("entry_risk_blocks"),
                           evaluation_basis=record["protocol"]["basis"])
            if result.get("status") != "completed":
                row.update(status="failed_source_case", reason=result.get("error", "Source ledger not complete"))
                rows.append(row)
                continue
            value = external_value if external_value is not None else replay_reports.detail(identifier, signal, include_marks=True)
            marks = value["daily_marks"]
            selected = [mark for mark in marks if start <= mark["date"] <= end]
            row["cost_settings"] = {key: value["config"].get(key) for key in COST_FIELDS}
            row["portfolio_settings"] = {key: value["config"].get(key) for key in (
                "signal", "holdings", "retain_rank", "gross_limit", "single_limit", "rebalance_sessions")}
            row["source_status"] = value["status"]
            row["available_sessions"] = len(selected)
            if (value["status"] != "completed" or not selected
                    or selected[0]["date"] != start or selected[-1]["date"] != end):
                row.update(status="window_not_covered", reason="Exact requested interval is not covered; no intersection, extrapolation or shortened-period metrics")
                rows.append(row)
                continue
            days = [mark["date"] for mark in selected]
            if calendar is None:
                calendar = days
            if days != calendar:
                row.update(status="calendar_mismatch", reason="Requested-window exchange sessions differ from the comparison calendar")
                rows.append(row)
                continue
            first = marks.index(selected[0])
            opening = marks[first - 1]["equity"] if first else value["config"]["initial_cash"]
            row.update(status="comparable", opening_equity=opening,
                       opening_basis="preceding_saved_session_equity" if first else "original_account_initial_cash",
                       metrics=metrics(selected, opening))
            row["period_metrics"] = [
                _period_metrics(marks, selected, f"{year}-01-01", f"{year}-12-31", label,
                                fallback_opening=opening if year == selected[0]["date"][:4] else None)
                for year, label in _annual_periods(selected)
            ]
            row["risk_summary_eligible"] = (row["metrics"]["stale_valuation_days"] == 0
                                            and row["metrics"]["sharpe_zero_rf_252"] is not None
                                            and not row["fractional_allocation_assumption"])
            signatures.append(row["cost_settings"])
            rows.append(row)
    front = nondominated(rows)
    for row in rows:
        row["descriptive_nondominated"] = row["case_id"] in front
    comparable = [row for row in rows if row["status"] == "comparable"]
    return {"status": "compared_saved_accounts" if comparable else "no_common_completed_window",
        "start": start, "end": end, "calendar": calendar or [], "sessions": len(calendar or []),
        "requested_run_ids": run_ids, "requested_cases": len(rows), "comparable_cases": len(comparable),
        "unavailable_cases": len(rows) - len(comparable), "same_cost_settings": bool(signatures) and all(
            signature == signatures[0] for signature in signatures),
        "cases": rows, "descriptive_nondominated_case_ids": front,
        "library_impact_diagnostics": _library_impact(rows),
        "strategy_validated": False, "execution_admitted": False, "account_created": False,
        "backtest_run": False, "created_at": datetime.now().astimezone().isoformat(),
        "basis": "Read-only date-window metrics from existing sole-ledger daily marks; prior session equity carried, no cash reset or curve blending",
        "limits": ["Annual period metrics reuse saved daily marks and do not create new held-out evidence or independent replications",
            "Already inspected diagnostic periods, not new held-out evidence or independent replications",
            "Same dates do not make different source snapshots, fund/stock scopes, capital histories or holdings/cadences a signal-only controlled experiment",
            "Cost-setting equality refers to configured assumptions; stock/fund tax,lot,settlement rules still depend on their existing profiles",
            "Different opening equities preserve actual histories and lot effects; these are not reset-to140000 matched new accounts",
            "Descriptive nondominated set uses return/Sharpe/drawdown only on complete nonstale marks; no optimized weights, winner admission or future-return guarantee",
            "A losing strategy can be nondominated solely for smaller drawdown; descriptive frontier does not mean profitable or suitable",
            "A missing/flat Sharpe is unknown, never infinity; stale marks remain visible but excluded from the descriptive set",
            "Explicit fractional-share assumption cases retain their metrics/labels but are excluded from the descriptive frontier; no observed settlement or global bound claim",
            "Missing quarter/fund coverage remains missing; no synthetic zero-return rows or partial-window ranking",
            "Worst historical day and drawdown are not black-swan execution guarantees"]}


def saved_catalog(limit=12):
    """Return compact saved same-ledger comparisons for the operator UI.

    These are derived JSON artifacts only.  The catalog never replays an
    account, ranks a winner, or changes promotion/execution state.
    """
    root = settings.ROOT / "outputs" / "research_comparisons"
    snapshots = []
    for path in sorted(root.glob("*/comparison.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        cases = []
        for case in value.get("cases", []) or []:
            metric = case.get("metrics") or {}
            cases.append({
                "case_id": case.get("case_id"),
                "family": case.get("family"),
                "signal": case.get("signal"),
                "label": case.get("label") or case.get("signal"),
                "status": case.get("status"),
                "net_return": metric.get("net_return"),
                "max_drawdown": metric.get("max_drawdown"),
                "sharpe_zero_rf_252": metric.get("sharpe_zero_rf_252"),
                "fees": metric.get("fees"),
                "stale_valuation_days": metric.get("stale_valuation_days"),
                "descriptive_nondominated": bool(case.get("descriptive_nondominated")),
                "strategy_validated": bool(case.get("strategy_validated")),
                "execution_admitted": bool(case.get("execution_admitted")),
            })
        snapshots.append({
            "id": path.parent.name,
            "source_artifact": str(path.resolve()),
            "created_at": value.get("created_at"),
            "start": value.get("start"),
            "end": value.get("end"),
            "sessions": value.get("sessions", 0),
            "requested_cases": value.get("requested_cases", len(cases)),
            "comparable_cases": value.get("comparable_cases", 0),
            "unavailable_cases": value.get("unavailable_cases", 0),
            "same_cost_settings": bool(value.get("same_cost_settings")),
            "descriptive_nondominated_case_ids": value.get("descriptive_nondominated_case_ids", []),
            "strategy_validated": bool(value.get("strategy_validated")),
            "execution_admitted": bool(value.get("execution_admitted")),
            "backtest_run": bool(value.get("backtest_run")),
            "cases": cases,
        })
        if len(snapshots) >= max(1, min(int(limit), 50)):
            break
    return {
        "schema": "saved_ledger_comparison_catalog_v1",
        "status": "read_only_saved_comparisons",
        "rows": snapshots,
        "total_snapshots": len(list(root.glob("*/comparison.json"))) if root.exists() else 0,
        "truncated": bool(root.exists() and len(list(root.glob("*/comparison.json"))) > len(snapshots)),
        "selection_enabled": False,
        "execution_admitted": False,
    }


def report(value):
    lines = ["# Saved Ledger Window Comparison", "",
        f"{value['start']}..{value['end']}: {value['sessions']} exchange sessions; {value['comparable_cases']}/{value['requested_cases']} cases covered. NO_TRADE.",
        value["basis"], f"Same configured cash/cost assumptions: {value['same_cost_settings']}. No account/reset/backtest created.", "",
        "| Family / Case | Status | Opening CNY | Net return | Max drawdown | Sharpe zero rf | Worst day | Fees CNY | Stale days | Descriptive frontier |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in value["cases"]:
        m = row["metrics"]
        if m is None:
            lines.append(f"| {row['family']} / {row['signal']} | {row['status']} | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | no |")
        else:
            sharpe = m["sharpe_zero_rf_252"]
            lines.append(f"| {row['family']} / {row['label']} | comparable | {row['opening_equity']:.2f} | {m['net_return']:.2%} | {m['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} | {m['worst_day']:.2%} | {m['fees']:.2f} | {m['stale_valuation_days']} | {'yes' if row['descriptive_nondominated'] else 'no'} |")
    lines += ["", "## Source Accounts", ""]
    for row in value["cases"]:
        kind = {
            "alpha158_lightgbm": "factor_mining",
            "factor_candidate_ridge": "factor_candidate_replays",
            "factor_candidate_family_balanced_ridge": "factor_candidate_family_replays",
            "factor_candidate_fixed_cadence": "factor_candidate_cadence_replays",
            "factor_candidate_library_leave_out": "factor_candidate_library_ablations",
            "factor_candidate_cost_aware_execution": "factor_candidate_cost_replays",
        }.get(row["family"], "market_replay")
        path = settings.ROOT / "outputs" / kind / row["run_id"] / "run.json"
        lines.append(f"- {row['case_id']}: `{path}`; source {row['source_period']['start']}..{row['source_period']['end']}; {row['symbols']} identities; {row['scope']}.")
        if row.get("entry_risk_policy"):
            lines.append(f"  Entry rule: {row['entry_risk_policy']['id']}; blocks: {row.get('entry_risk_blocks')}; {row['evaluation_basis']}.")
        for limitation in row.get("source_limitations", []):
            if "post-event issuer cash-only confirmations" in limitation:
                lines.append("  Historical accounting: " + limitation + ".")
    lines += ["", "## Calendar-year diagnostics", "",
              "These rows reuse the same saved daily marks and carry prior saved-session equity into each year; they are not new replays or resets.", "",
              "| Family / Case | Period | Sessions | Opening CNY | Net return | Max drawdown | Sharpe zero rf | Fees CNY | Stale days |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in value["cases"]:
        for period in row.get("period_metrics", []):
            if period["status"] != "comparable":
                lines.append(f"| {row['family']} / {row['label']} | {period['label']} | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |")
                continue
            sharpe = period["sharpe_zero_rf_252"]
            lines.append(f"| {row['family']} / {row['label']} | {period['label']} | {period['sessions']} | {period['opening_equity']:.2f} | {period['net_return']:.2%} | {period['max_drawdown']:.2%} | {sharpe if sharpe is None else round(sharpe,4)} | {period['fees']:.2f} | {period['stale_valuation_days']} |")
    if value["library_impact_diagnostics"]:
        lines += ["", "## Factor-library leave-out deltas", "",
                  "These are arithmetic differences between saved ledgers with the same configured window and costs; they are not causal factor attribution, refits, or promotion decisions.", "",
                  "| Leave-out case | Full-window return delta | MDD delta | Sharpe delta | Fee delta CNY | 2026 return delta |", "|---|---:|---:|---:|---:|---:|"]
        for item in value["library_impact_diagnostics"]:
            full = item["full_window_delta"]
            y2026 = next((period for period in item["period_deltas"] if period["label"] == "calendar_year_2026"), {})
            lines.append(f"| {item['leave_out_case']} | {full.get('net_return', float('nan')):.2%} | {full.get('max_drawdown', float('nan')):.2%} | {full.get('sharpe_zero_rf_252', float('nan')):.4f} | {full.get('fees', float('nan')):.2f} | {y2026.get('net_return', float('nan')):.2%} |")
    lines += ["", "## Boundaries", "", *value["limits"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", dest="runs", action="append", required=True, help="Include every actual case in this saved run; repeat for multiple runs")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args(argv)
    value = compare(args.runs, args.start, args.end)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs/research_comparisons" / identifier
    out.mkdir(parents=True, exist_ok=False)
    (out / "comparison.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (out / "REPORT.md").write_text(report(value), encoding="utf-8")
    print(json.dumps({key: value[key] for key in ("status", "sessions", "requested_cases", "comparable_cases",
          "unavailable_cases", "same_cost_settings", "descriptive_nondominated_case_ids")}, ensure_ascii=False), flush=True)
    print(out, flush=True)
    return 0 if value["comparable_cases"] == value["requested_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
