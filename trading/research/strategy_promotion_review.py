"""Create a paper-only, explicit strategy-promotion review record.

The record mirrors the human sign-off boundary used by institutional desks:
comparison and capacity evidence can be reviewed, but promotion requires a
fresh holdout, current account/quote evidence, and an explicit decision.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .. import settings
from .independent_window_evidence import by_strategy, latest_artifact


SCHEMA = "strategy_promotion_review_record_v1"


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: str | Path | None):
    if not path or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _artifact_ref(path: str | Path | None, payload: dict | None = None):
    if not path:
        return None
    result = {"path": str(Path(path).resolve())}
    if payload:
        for key in (
            "status", "source_cutoff", "session_count", "case_count",
            "fresh_holdout_claimed", "strategy_validated", "execution_admitted",
        ):
            if key in payload:
                result[key] = payload[key]
    return result


def _norm_strategy(value):
    return "_".join(str(value or "").strip().lower().replace("-", "_").split())


def _number(value):
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stale_valuation(row, sizing, risk):
    for source in (row, sizing, risk):
        for key in ("stale_valuation_days", "stale_days"):
            value = _number(source.get(key))
            if value is not None:
                return int(value) if value.is_integer() else value
    return None


def _data_usability(stale_days):
    """Describe mark freshness without implying validation or execution readiness."""
    if stale_days is None:
        return (
            "paper_review_stale_valuation_unknown",
            "saved comparison does not expose stale valuation count; liquidation loss remains unknown",
        )
    if stale_days > 0:
        return (
            "paper_review_stale_valuation",
            f"saved comparison contains {stale_days:g} stale valuation day(s); liquidation loss remains unknown",
        )
    return (
        "paper_review_marks_complete",
        "saved comparison reports no stale valuation days; fresh holdout and broker evidence are still absent",
    )


def _robustness_by_strategy(payload):
    """Index saved-mark diagnostics without turning them into a selector."""
    result = {}
    if not isinstance(payload, dict):
        return result
    for item in payload.get("rolling_window_summaries", []) or []:
        key = _norm_strategy(item.get("signal") or item.get("label"))
        if not key:
            continue
        result[(key, str(item.get("window_sessions")))] = item
    return result


def _cost_sensitivity_by_strategy(payload):
    result = {}
    if not isinstance(payload, dict):
        return result
    for item in payload.get("cost_sensitivity", []) or []:
        key = _norm_strategy(item.get("signal") or item.get("label"))
        if not key:
            continue
        result[(key, str(item.get("stress_additional_slippage_bps")))] = item
    return result


def _latest_family_holdout_by_signal():
    """Read saved family-holdout receipts without treating them as promotion."""
    root = settings.ROOT / "outputs" / "family_holdout"
    if not root.is_dir():
        return {}
    records = []
    for path in root.glob("*/run.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        protocol = payload.get("protocol")
        if isinstance(protocol, str):
            protocol_path = Path(protocol)
            try:
                protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                protocol = None
        if not isinstance(protocol, dict) or not protocol.get("candidate_signal"):
            continue
        records.append((path.stat().st_mtime, str(protocol["candidate_signal"]), {
            "status": payload.get("status", "unknown"),
            "source_file": str(path.resolve()),
            "candidate_run": protocol.get("candidate_run"),
            "candidate_signal": protocol.get("candidate_signal"),
            "candidate_end": protocol.get("candidate_end"),
            "source_cutoff": payload.get("source_cutoff"),
            "holdout_sessions": payload.get("holdout_sessions", 0),
            "fresh_holdout_claimed": bool(payload.get("fresh_holdout_claimed")),
            "strategy_validated": bool(payload.get("strategy_validated")),
            "execution_admitted": False,
        }))
    latest = {}
    for mtime, signal, record in sorted(records, key=lambda item: item[0], reverse=True):
        latest.setdefault(signal, record)
    return latest


def build(
    comparison_path: str | Path,
    sizing_path: str | Path | None,
    robustness_path: str | Path | None,
    out: str | Path,
    portfolio_risk_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
):
    comparison_path = Path(comparison_path).resolve()
    sizing_path = Path(sizing_path).resolve() if sizing_path else None
    robustness_path = Path(robustness_path).resolve() if robustness_path else None
    portfolio_risk_path = Path(portfolio_risk_path).resolve() if portfolio_risk_path else None
    evidence_path = Path(evidence_path).resolve() if evidence_path else None
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    comparison_rows = [
        row for row in _read_csv(comparison_path)
        if row.get("strategy_id")
    ]
    sizing_rows = _read_csv(sizing_path) if sizing_path and sizing_path.exists() else []
    sizing_by_id = {row.get("strategy_id", ""): row for row in sizing_rows}
    robustness = _read_json(robustness_path)
    robustness_by_key = _robustness_by_strategy(robustness)
    cost_by_key = _cost_sensitivity_by_strategy(robustness)
    portfolio_risk = _read_json(portfolio_risk_path) or {}
    portfolio_risk_by_id = {
        str(item.get("strategy_id")): item
        for item in portfolio_risk.get("rows", []) or []
        if item.get("strategy_id")
    }
    evidence = _read_json(evidence_path)
    saved_comparison = evidence.get("saved_ledger_comparison") if isinstance(evidence, dict) else None
    window_stability = (saved_comparison or {}).get("window_stability") or {}
    stability_strategy_id = str(window_stability.get("selected_strategy_id") or "")
    stability_status = str(window_stability.get("status") or "")
    independent_window_artifact, independent_windows_by_strategy = by_strategy(latest_artifact())
    holdout_by_signal = _latest_family_holdout_by_signal()

    rows = []
    for row in comparison_rows:
        strategy_id = row["strategy_id"]
        sizing = sizing_by_id.get(strategy_id, {})
        risk = portfolio_risk_by_id.get(strategy_id, {})
        key = _norm_strategy(strategy_id)
        rolling_60 = robustness_by_key.get((key, "60"), {})
        rolling_120 = robustness_by_key.get((key, "120"), {})
        stress_50 = cost_by_key.get((key, "50"), {})
        independent = independent_windows_by_strategy.get(strategy_id, {})
        holdout = holdout_by_signal.get(strategy_id)
        stale_days = _stale_valuation(row, sizing, risk)
        data_usability_status, data_usability_reason = _data_usability(stale_days)
        rows.append({
            "strategy_id": strategy_id,
            "comparison_source_cutoff": row.get("source_cutoff", ""),
            "stale_valuation_days": stale_days if stale_days is not None else "",
            "data_usability_status": data_usability_status,
            "data_usability_reason": data_usability_reason,
            "candidate_order_count": row.get("candidate_order_count", ""),
            "ledger_preflight_accepted": row.get("ledger_preflight_accepted", ""),
            "ledger_preflight_rejected": row.get("ledger_preflight_rejected", ""),
            "deferred_buys": row.get("deferred_buys", ""),
            "comparison_return": row.get("comparison_return", ""),
            "comparison_max_drawdown": row.get("comparison_max_drawdown", ""),
            "comparison_sharpe": row.get("comparison_sharpe", ""),
            "saved_mark_positive_window_ratio_60d": rolling_60.get("positive_window_ratio", ""),
            "saved_mark_positive_window_ratio_120d": rolling_120.get("positive_window_ratio", ""),
            "saved_mark_median_window_return_60d": rolling_60.get("return_median", ""),
            "saved_mark_median_window_return_120d": rolling_120.get("return_median", ""),
            "saved_mark_50bp_stressed_return": stress_50.get("stressed_net_return", ""),
            "saved_mark_50bp_additional_cost_rmb": stress_50.get("additional_cost", ""),
            "paper_risk_status": (
                "paper_reference_only" if risk else sizing.get("risk_status", "unavailable")
            ),
            "paper_buy_gross_rmb": risk.get(
                "paper_accepted_buy_gross_rmb", sizing.get("accepted_buy_gross_rmb", "")
            ),
            "paper_cash_after_buys_rmb": risk.get(
                "paper_cash_after_buys_only_rmb", sizing.get("cash_after_buys_only_rmb", "")
            ),
            "paper_risk_flags": risk.get("risk_flags", ""),
            "fresh_holdout_status": "not_claimed",
            "fresh_holdout_source": holdout.get("source_file") if holdout else "",
            "fresh_holdout_sessions": holdout.get("holdout_sessions", "") if holdout else "",
            "fresh_holdout_strategy_validated": (
                str(bool(holdout.get("strategy_validated"))).lower()
                if holdout else ""
            ),
            "saved_mark_robustness_status": (
                "available_reference_only" if robustness else "not_supplied"
            ),
            "saved_ledger_window_stability_status": (
                stability_status if strategy_id == stability_strategy_id else ""
            ),
            "saved_ledger_window_count": (
                window_stability.get("evaluated_windows", "")
                if strategy_id == stability_strategy_id else ""
            ),
            "saved_ledger_positive_window_count": (
                window_stability.get("positive_windows", "")
                if strategy_id == stability_strategy_id else ""
            ),
            "saved_ledger_worst_return": (
                window_stability.get("worst_net_return", "")
                if strategy_id == stability_strategy_id else ""
            ),
            "saved_ledger_worst_drawdown": (
                window_stability.get("worst_max_drawdown", "")
                if strategy_id == stability_strategy_id else ""
            ),
            "independent_sample_window_count": independent.get("window_count", ""),
            "independent_sample_window_start": independent.get("window_start", ""),
            "independent_sample_window_end": independent.get("window_end", ""),
            "independent_sample_alternate_provider": independent.get("alternate_provider", ""),
            "independent_sample_source": independent.get("source", ""),
            "independent_sample_positive_window_count": independent.get("positive_windows", ""),
            "independent_sample_mean_return": independent.get("mean_net_return", ""),
            "independent_sample_worst_return": independent.get("worst_net_return", ""),
            "independent_sample_worst_drawdown": independent.get("worst_max_drawdown", ""),
            "independent_sample_paired_rows_all_windows": independent.get("paired_rows_all_windows", ""),
            "independent_sample_non_overlapping": independent.get("non_overlapping", ""),
            "independent_sample_calendar_window_coverage_verified": independent.get(
                "calendar_window_coverage_verified", ""
            ),
            "independent_sample_calendar_continuity_verified": independent.get("calendar_continuity_verified", ""),
            "independent_sample_calendar_gap_count": independent.get("calendar_gap_count", ""),
            "independent_sample_calendar_source": independent.get("calendar_source", ""),
            "account_observed": "false",
            "quotes_observed": "false",
            "human_decision": "pending",
            "promotion_status": "blocked_pending_explicit_review",
            "execution_admitted": "false",
            "blockers": ";".join([
                "fresh_holdout_required",
                "account_observation_required",
                "quote_observation_required",
                "human_decision_required",
                *(["saved_ledger_window_stability_review_required"]
                  if strategy_id == stability_strategy_id
                  and stability_status in {
                      "mixed_window_results",
                      "negative_across_available_windows",
                  }
                  else []),
            ]),
        })
        if holdout:
            rows[-1]["fresh_holdout_status"] = (
                "completed_fresh_holdout"
                if holdout["fresh_holdout_claimed"] else holdout["status"]
            )

    payload = {
        "schema": SCHEMA,
        "status": "manual_promotion_review_template",
        "source_comparison": str(comparison_path),
        "source_sizing": str(sizing_path) if sizing_path else None,
        "source_robustness": _artifact_ref(robustness_path, robustness),
        "source_evidence": _artifact_ref(evidence_path, evidence),
        "source_portfolio_risk": _artifact_ref(portfolio_risk_path, portfolio_risk),
        "source_independent_window_stability": str(independent_window_artifact) if independent_window_artifact else None,
        "fresh_holdout_evidence": sorted(
            holdout_by_signal.values(), key=lambda item: item["candidate_signal"]
        ),
        "saved_ledger_window_stability": window_stability or None,
        "candidate_count": len(rows),
        "eligible_candidate_count": 0,
        "selection_required": True,
        "automatic_promotion": False,
        "fresh_holdout_claimed": False,
        "account_observed": False,
        "quotes_observed": False,
        "human_decision_required": True,
        "execution_admitted": False,
        "required_evidence": [
            "fresh untouched holdout with pinned data and costs",
            "same-session account cash, holdings and sellable T+1 quantities",
            "same-session raw RMB quotes and route/readback evidence",
            "window-stability review across the saved same-ledger comparison windows",
            "explicit human decision with reviewer and timestamp",
        ],
        "rows": rows,
        "data_usability_status_counts": {
            status: sum(1 for row in rows if row.get("data_usability_status") == status)
            for status in sorted({row.get("data_usability_status") for row in rows})
            if status
        },
        "interpretation": (
            "This is an offline review template. Ranking, saved-mark robustness, "
            "window stability, and paper capacity do not promote a strategy or authorize an order."
        ),
    }
    json_path = out / "strategy_promotion_review_v1.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out / "strategy_promotion_review_v1.csv"
    fields = list(rows[0]) if rows else [
        "strategy_id", "fresh_holdout_status", "promotion_status", "execution_admitted"
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path, payload
