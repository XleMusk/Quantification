"""Record an explicit paper strategy choice without admitting execution."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


SCHEMA = "paper_strategy_selection_record_v1"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_csv(path: Path, rows):
    fields = list(rows[0]) if rows else ["strategy_id", "selection_status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _load_robustness(path: str | Path | None):
    if not path:
        return None, {}
    path = Path(path).resolve()
    payload = _read_json(path)
    if (payload.get("status") != "saved_mark_robustness_diagnostic"
            or payload.get("fresh_holdout_claimed") is not False
            or payload.get("strategy_validated") is not False
            or payload.get("execution_admitted") is not False):
        raise ValueError("Robustness input must be an unadmitted saved-mark diagnostic")
    indexed = {}
    for row in payload.get("rolling_window_summaries", []):
        signal = str(row.get("signal") or "")
        if not signal:
            continue
        indexed.setdefault(signal, {})[str(row.get("window_sessions"))] = row
    for row in payload.get("cost_sensitivity", []):
        signal = str(row.get("signal") or "")
        if signal and int(row.get("stress_additional_slippage_bps", -1)) == 50:
            indexed.setdefault(signal, {})["cost_50bps"] = row
    # Keep downstream selection records as references. Embedding every rolling
    # window here duplicates the diagnostic and makes the operator package
    # needlessly large; candidate-level metrics are already copied below.
    reference = {
        "path": str(path),
        "status": payload.get("status"),
        "fresh_holdout_claimed": payload.get("fresh_holdout_claimed"),
        "strategy_validated": payload.get("strategy_validated"),
        "execution_admitted": payload.get("execution_admitted"),
        "source_comparison": payload.get("source_comparison"),
        "source_cutoff": payload.get("source_cutoff"),
        "session_count": payload.get("session_count"),
        "case_count": payload.get("case_count"),
        "window_sessions": payload.get("window_sessions"),
        "stress_additional_slippage_bps": payload.get("stress_additional_slippage_bps"),
    }
    return reference, indexed


def build(matrix_path: str | Path, strategy_id: str | None = None,
          out_path: str | Path | None = None,
          robustness_path: str | Path | None = None):
    matrix_path = Path(matrix_path).resolve()
    matrix = _read_json(matrix_path)
    if matrix.get("schema") != "offline_selected_strategy_matrix_v1":
        raise ValueError("Paper selection requires the frozen offline selected-strategy matrix")
    if matrix.get("automatic_strategy_selection") is not False:
        raise ValueError("Automatic strategy selection is not allowed in the paper selector")
    if matrix.get("execution_admitted") is not False or matrix.get("submission_attempted") is not False:
        raise ValueError("Selection matrix cannot contain admitted or submitted orders")
    candidates = []
    robustness_artifact, robustness_by_signal = _load_robustness(robustness_path)
    seen_ids = set()
    for row in matrix.get("rows", []):
        strategy_key = str(row.get("strategy_id") or "")
        comparability_group = str(row.get("comparability_group") or "")
        family = str(row.get("family") or "")
        if not strategy_key or strategy_key in seen_ids:
            raise ValueError("Selection matrix contains a missing or duplicate strategy_id")
        if row.get("execution_admitted") is not False:
            raise ValueError(f"Candidate {strategy_key!r} is execution-admitted")
        if row.get("paper_account_observed") is True or row.get("quotes_observed") is True:
            raise ValueError(f"Candidate {strategy_key!r} contains observed live state")
        if "diagnostic" in comparability_group or family == "alpha158_lightgbm":
            raise ValueError(f"Diagnostic factor row {strategy_key!r} cannot enter paper selection")
        seen_ids.add(strategy_key)
        robustness = robustness_by_signal.get(strategy_key, {})
        robustness_status = "matched_saved_mark_diagnostic" if robustness else (
            "unmatched_saved_mark_diagnostic" if robustness_artifact else "not_supplied"
        )
        candidates.append({
            "strategy_id": strategy_key,
            "plan": row.get("plan", ""),
            "comparison_return": row.get("comparison_return", ""),
            "comparison_max_drawdown": row.get("comparison_max_drawdown", ""),
            "comparison_sharpe": row.get("comparison_sharpe", ""),
            "preflight_accepted": row.get("ledger_preflight_accepted", ""),
            "preflight_rejected": row.get("ledger_preflight_rejected", ""),
            "robustness_status": robustness_status,
            "robustness_60d_positive_window_ratio": (robustness.get("60", {}) or {}).get("positive_window_ratio", ""),
            "robustness_60d_median_return": (robustness.get("60", {}) or {}).get("return_median", ""),
            "robustness_120d_positive_window_ratio": (robustness.get("120", {}) or {}).get("positive_window_ratio", ""),
            "robustness_120d_median_return": (robustness.get("120", {}) or {}).get("return_median", ""),
            "robustness_extra_50bps_return": (robustness.get("cost_50bps", {}) or {}).get("stressed_net_return", ""),
            "execution_admitted": "false",
        })
    candidate_ids = {row["strategy_id"] for row in candidates}
    if strategy_id is not None and strategy_id not in candidate_ids:
        raise ValueError(f"Unknown strategy_id {strategy_id!r}; candidates={sorted(candidate_ids)}")

    selected = next((row for row in candidates if row["strategy_id"] == strategy_id), None)
    if selected is None:
        selection_status = "selection_required"
        selected_strategy_id = None
        selected_plan = None
        next_action = "operator must choose exactly one candidate; no automatic winner selection"
    else:
        selection_status = "paper_candidate_explicitly_selected"
        selected_strategy_id = selected["strategy_id"]
        selected_plan = selected["plan"]
        next_action = "review fresh account, same-session quotes, route, T+1 and recovery state; keep paper-only"

    payload = {
        "schema": SCHEMA,
        "status": "paper_strategy_selection_only",
        "source_matrix": str(matrix_path),
        "robustness_artifact": robustness_artifact,
        "execution_day": matrix.get("execution_day"),
        "candidate_count": len(candidates),
        "selection_required": selected is None,
        "automatic_strategy_selection": False,
        "selected_strategy_id": selected_strategy_id,
        "selected_plan": selected_plan,
        "selection_status": selection_status,
        "next_action": next_action,
        "account_observed": False,
        "quotes_observed": False,
        "prefill_allowed": False,
        "ledger_mutated": False,
        "submission_attempted": False,
        "execution_admitted": False,
        "candidates": candidates,
        "not_attempted": [
            "No strategy was selected automatically from return, Sharpe or drawdown.",
            "Selection does not create orders, prefill a client or mutate the persistent ledger.",
            "A selected candidate remains subject to account, quote, route, T+1 and recovery review.",
        ],
    }
    if out_path:
        json_path = Path(out_path).resolve()
    else:
        json_path = matrix_path.parent / "paper_strategy_selection_record_v1.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = json_path.with_suffix(".csv")
    csv_rows = []
    for row in candidates:
        csv_rows.append({
            **row,
            "selected": "true" if row["strategy_id"] == selected_strategy_id else "false",
            "selection_status": selection_status,
            "execution_admitted": "false",
        })
    _write_csv(csv_path, csv_rows)
    return json_path, csv_path, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--strategy-id")
    parser.add_argument("--out")
    parser.add_argument("--robustness")
    args = parser.parse_args(argv)
    json_path, csv_path, payload = build(args.matrix, args.strategy_id, args.out, args.robustness)
    print(json.dumps({
        "json": str(json_path),
        "csv": str(csv_path),
        "selection_status": payload["selection_status"],
        "selected_strategy_id": payload["selected_strategy_id"],
        "execution_admitted": payload["execution_admitted"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
