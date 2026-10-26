"""Join candidate, mature-factor and shortline research decisions.

This is a research-only rollup. It makes the remaining strategy decisions
visible in one receipt without rerunning a replay, changing a ledger, or
opening a paper/live execution path.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "quantification_research_adoption_rollup_v1"
DEFAULT_CANDIDATE = Path("outputs/pipeline_receipts/20261026_candidate_adoption_decision_v1.json")
DEFAULT_FACTOR = Path("outputs/pipeline_receipts/20261026_mature_factor_horizon_comparison_v1.json")
DEFAULT_SHORTLINE = Path("outputs/pipeline_receipts/20261026_shortline_adoption_gate_v2.json")
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261026_research_adoption_rollup_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261026_research_adoption_rollup_v1.md")
RETAINED_JOINT_SCHEMA = "quantification_retained_joint_candidate_comparison_v1"
D0_D1_SCHEMA = "stockdb_next_close_d0_d1_contract_v1"
D0_D1_STATUS = "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _require_research_only(receipt: Mapping[str, Any], path: Path) -> None:
    if receipt.get("execution_admitted") is not False:
        raise ValueError(f"execution boundary crossed: {path}")
    if receipt.get("paper_allowed") is True or receipt.get("promotion_allowed") is True:
        raise ValueError(f"paper or promotion permission present: {path}")


def _factor_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    if receipt.get("schema") != "mature_factor_horizon_comparison_v1":
        raise ValueError(f"mature-factor schema mismatch: {path}")
    machine = receipt.get("machine_status")
    if not isinstance(machine, Mapping):
        raise ValueError("mature-factor machine_status is missing")
    if machine.get("decision") != "NO_ADOPTION_RESEARCH_ONLY":
        raise ValueError("mature-factor comparison must remain no-adoption")
    comparison = receipt.get("comparison")
    delta = receipt.get("metric_delta_variant_minus_baseline")
    if not isinstance(comparison, Mapping) or not isinstance(delta, Mapping):
        raise ValueError("mature-factor comparison payload is incomplete")
    baseline = comparison.get("baseline")
    variant = comparison.get("variant")
    if not isinstance(baseline, Mapping) or not isinstance(variant, Mapping):
        raise ValueError("mature-factor baseline/variant is missing")
    return {
        "status": receipt.get("status"),
        "decision": machine.get("decision"),
        "baseline": {
            "strategy_id": baseline.get("strategy_id"),
            "horizon_sessions": baseline.get("horizon_sessions"),
            "sessions": baseline.get("sessions"),
            "net_return": baseline.get("metrics", {}).get("net_return"),
            "max_drawdown": baseline.get("metrics", {}).get("max_drawdown"),
            "sharpe_zero_rf_252": baseline.get("metrics", {}).get("sharpe_zero_rf_252"),
        },
        "variant": {
            "strategy_id": variant.get("strategy_id"),
            "horizon_sessions": variant.get("horizon_sessions"),
            "sessions": variant.get("sessions"),
            "net_return": variant.get("metrics", {}).get("net_return"),
            "max_drawdown": variant.get("metrics", {}).get("max_drawdown"),
            "sharpe_zero_rf_252": variant.get("metrics", {}).get("sharpe_zero_rf_252"),
        },
        "variant_minus_baseline": {
            "net_return": delta.get("net_return"),
            "max_drawdown": delta.get("max_drawdown"),
            "sharpe_zero_rf_252": delta.get("sharpe_zero_rf_252"),
        },
        "paper_allowed": False,
        "promotion_allowed": False,
        "next_gate": "PIT-complete untouched holdout before horizon selection",
    }


def _shortline_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    if receipt.get("schema") != "quantification_shortline_adoption_gate_v1":
        raise ValueError(f"shortline schema mismatch: {path}")
    models = receipt.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("shortline models are missing")
    rows = []
    for model in models:
        if not isinstance(model, Mapping):
            raise ValueError("shortline model row is malformed")
        if model.get("paper_allowed") is True or model.get("promotion_allowed") is True:
            raise ValueError("shortline model crossed execution boundary")
        rows.append({
            "model_kind": model.get("model_kind"),
            "research_action": model.get("research_action"),
            "replay_sessions": model.get("replay_sessions"),
            "orders": model.get("orders"),
            "fills": model.get("fills"),
            "net_return": model.get("net_return"),
            "decision_reason": model.get("decision_reason"),
            "paper_allowed": False,
            "promotion_allowed": False,
        })
    return {
        "status": receipt.get("status"),
        "models": rows,
        "summary": receipt.get("summary"),
        "next_gates": receipt.get("next_gates", []),
        "paper_allowed": False,
        "promotion_allowed": False,
    }


def _retained_joint_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Bind the retained joint target without ranking it as a return series."""
    if receipt.get("schema") != RETAINED_JOINT_SCHEMA:
        raise ValueError(f"retained joint schema mismatch: {path}")
    if receipt.get("execution_admitted") is not False:
        raise ValueError("retained joint comparison crossed execution boundary")
    if receipt.get("promotion_allowed") is True:
        raise ValueError("retained joint comparison permits promotion")
    decision = receipt.get("incremental_decision")
    target = receipt.get("joint_target")
    if not isinstance(decision, Mapping) or not isinstance(target, Mapping):
        raise ValueError("retained joint comparison is missing decision or target")
    if decision.get("decision") != "CONTINUE_RESEARCH_ONLY":
        raise ValueError("retained joint decision must remain continue-research-only")
    if decision.get("return_incremental_status") != "NOT_COMPARABLE_PROJECTION_ONLY":
        raise ValueError("retained joint return must remain projection-only")
    return {
        "status": receipt.get("status"),
        "decision": decision.get("decision"),
        "return_incremental_status": decision.get("return_incremental_status"),
        "strategy_ids": target.get("strategy_ids", []),
        "decision_day": target.get("decision_day"),
        "target_count": target.get("target_count"),
        "paper_order_count": target.get("paper_order_count"),
        "route_available_count": target.get("route_available_count"),
        "route_blocked_count": target.get("route_blocked_count"),
        "promotion_allowed": False,
        "source": {"path": str(path.resolve()), "sha256": _sha256(path)},
        "next_gate": "forward_account_replay_with_same_window_and_cost_model",
    }


def _d0_d1_summary(receipt: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Bind one completed close-day paper package without treating it as adoption."""
    if receipt.get("schema") != D0_D1_SCHEMA:
        raise ValueError(f"D0/D1 schema mismatch: {path}")
    if receipt.get("status") != D0_D1_STATUS:
        raise ValueError(f"D0/D1 is not complete: {path}")
    if any(receipt.get(key) is not False for key in
           ("execution_admitted", "broker_interacted", "ths_interacted",
            "persistent_ledger_mutated")):
        raise ValueError(f"D0/D1 execution boundary crossed: {path}")
    coverage = receipt.get("snapshot_coverage")
    fixed = receipt.get("fixed")
    candidate = receipt.get("candidate_batch")
    joint = receipt.get("joint_paper")
    if not all(isinstance(value, Mapping) for value in
               (coverage, fixed, candidate, joint)):
        raise ValueError(f"D0/D1 completion payload is incomplete: {path}")
    missing = coverage.get("missing_symbols")
    if coverage.get("status") != "COMPLETE" or not isinstance(missing, list) or missing:
        raise ValueError(f"D0/D1 target coverage is incomplete: {path}")
    for payload, field in ((fixed, "target_count"), (fixed, "paper_order_count"),
                           (candidate, "candidate_count"), (joint, "paper_order_count")):
        if type(payload.get(field)) is not int or payload[field] < 0:
            raise ValueError(f"D0/D1 {field} is missing or invalid: {path}")
    return {
        "status": receipt.get("status"),
        "close_date": receipt.get("close_date"),
        "requested_symbol_count": coverage.get("requested_symbol_count"),
        "available_symbol_count": coverage.get("available_symbol_count"),
        "missing_symbols": list(missing),
        "fixed_strategy_id": fixed.get("strategy_id"),
        "fixed_target_count": fixed.get("target_count"),
        "fixed_paper_order_count": fixed.get("paper_order_count"),
        "candidate_count": candidate.get("candidate_count"),
        "joint_paper_order_count": joint.get("paper_order_count"),
        "source_scope": receipt.get("persistent_fixed_daily_account_scope"),
        "source": {"path": str(path.resolve()), "sha256": _sha256(path)},
        "paper_allowed": False,
        "promotion_allowed": False,
        "next_gate": "reuse the same contract on the next closed session; persistent rollover still requires a complete fixed_daily close package",
    }


def build_rollup(candidate_path: Path = DEFAULT_CANDIDATE,
                 factor_path: Path = DEFAULT_FACTOR,
                 shortline_path: Path = DEFAULT_SHORTLINE,
                 retained_joint_path: Path | None = None,
                 d0_d1_path: Path | None = None) -> dict[str, Any]:
    candidate = _read(candidate_path)
    factor = _read(factor_path)
    shortline = _read(shortline_path)
    if candidate.get("schema") != "quantification_candidate_adoption_decision_v1":
        raise ValueError(f"candidate adoption schema mismatch: {candidate_path}")
    _require_research_only(candidate, candidate_path)
    _require_research_only(factor.get("machine_status", {}), factor_path)
    _require_research_only(shortline, shortline_path)
    strategies = candidate.get("strategies")
    if not isinstance(strategies, list):
        raise ValueError("candidate adoption strategies are missing")
    separate_gates = {
        "mature_factor_horizon": _factor_summary(factor, factor_path),
        "shortline_native_lr_lgbm": _shortline_summary(shortline, shortline_path),
    }
    if retained_joint_path is not None:
        separate_gates["retained_joint_portfolio"] = _retained_joint_summary(
            _read(retained_joint_path), retained_joint_path)
    d0_d1 = None
    if d0_d1_path is not None:
        d0_d1 = _d0_d1_summary(_read(d0_d1_path), d0_d1_path)
        separate_gates["latest_d0_d1_paper"] = d0_d1
    next_actions = [
        "Run a PIT-complete untouched holdout before selecting a mature-factor horizon.",
        "Replace the shortline independent raw path with StockDB-equivalent history and untouched-forward validation.",
        "Replay the retained joint target over a matching forward account before any portfolio adoption decision.",
        "Keep all candidates research-only until PIT, forward and channel evidence gates pass.",
    ]
    if d0_d1 is None:
        next_actions.insert(0, "Run 2026-10-26 post-close StockDB D0/D1 with the existing shared paper route.")
    else:
        next_actions.insert(0, (
            f"D0/D1 close {d0_d1['close_date']} is complete for the target set; "
            "reuse the same contract on the next closed session and keep persistent fixed_daily rollover gated by a complete close package."
        ))
    return {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP",
        "execution_admitted": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "inputs": {
            "candidate_adoption": {"path": str(candidate_path.resolve()), "sha256": _sha256(candidate_path)},
            "mature_factor_comparison": {"path": str(factor_path.resolve()), "sha256": _sha256(factor_path)},
            "shortline_adoption": {"path": str(shortline_path.resolve()), "sha256": _sha256(shortline_path)},
            **({"d0_d1_paper": {"path": str(d0_d1_path.resolve()), "sha256": _sha256(d0_d1_path)}}
               if d0_d1_path is not None else {}),
        },
        "candidate_summary": candidate.get("summary"),
        "candidate_decisions": [
            {
                "strategy_id": row.get("strategy_id"),
                "label": row.get("label"),
                "research_action": row.get("research_action"),
                "decision_reason": row.get("decision_reason"),
                "strategy_validated": row.get("strategy_validated") is True,
            }
            for row in strategies if isinstance(row, Mapping)
        ],
        "separate_gates": separate_gates,
        "next_actions": next_actions,
    }


def render_markdown(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Research adoption rollup (research only)",
        "",
        "This receipt joins the candidate adoption matrix with the mature-factor horizon comparison and the shortline model gate.",
        "",
        "| Candidate | Decision | Validated |",
        "|---|---|---|",
    ]
    for row in receipt["candidate_decisions"]:
        lines.append(f"| {row['label']} (`{row['strategy_id']}`) | {row['research_action']} | {'yes' if row['strategy_validated'] else 'no'} |")
    factor = receipt["separate_gates"]["mature_factor_horizon"]
    lines += [
        "",
        f"Mature factor: `{factor['decision']}`; baseline return {float(factor['baseline']['net_return']):.4%}, "
        f"variant return {float(factor['variant']['net_return']):.4%}, "
        f"delta {float(factor['variant_minus_baseline']['net_return']):.4%}.",
        "",
        "| Shortline model | Decision | Sessions | Orders | Fills | Net return |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in receipt["separate_gates"]["shortline_native_lr_lgbm"]["models"]:
        lines.append(f"| {row['model_kind']} | {row['research_action']} | {row['replay_sessions']} | {row['orders']} | {row['fills']} | {float(row['net_return']):.4%} |")
    joint = receipt["separate_gates"].get("retained_joint_portfolio")
    if joint is not None:
        lines += [
            "",
            f"Retained joint target: `{joint['decision']}` on `{joint['decision_day']}`, "
            f"{joint['target_count']} target rows, {joint['paper_order_count']} paper orders; "
            f"return status `{joint['return_incremental_status']}`.",
        ]
    d0_d1 = receipt["separate_gates"].get("latest_d0_d1_paper")
    if d0_d1 is not None:
        lines += [
            "",
            f"Latest D0/D1 paper: close `{d0_d1['close_date']}`, "
            f"{d0_d1['available_symbol_count']}/{d0_d1['requested_symbol_count']} target symbols, "
            f"fixed {d0_d1['fixed_paper_order_count']} orders, "
            f"joint {d0_d1['joint_paper_order_count']} orders; "
            f"source scope `{d0_d1['source_scope']}`.",
        ]
    lines += ["", "All rows keep `paper_allowed=false`, `promotion_allowed=false`, and `execution_admitted=false`.", ""]
    return "\n".join(lines)


def write_outputs(*, candidate_path: Path = DEFAULT_CANDIDATE,
                  factor_path: Path = DEFAULT_FACTOR,
                  shortline_path: Path = DEFAULT_SHORTLINE,
                  retained_joint_path: Path | None = None,
                  d0_d1_path: Path | None = None,
                  output: Path = DEFAULT_OUTPUT,
                  report: Path = DEFAULT_REPORT) -> tuple[Path, Path]:
    receipt = build_rollup(candidate_path, factor_path, shortline_path, retained_joint_path, d0_d1_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(render_markdown(receipt), encoding="utf-8")
    return output, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-adoption", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--mature-factor-comparison", type=Path, default=DEFAULT_FACTOR)
    parser.add_argument("--shortline-adoption", type=Path, default=DEFAULT_SHORTLINE)
    parser.add_argument("--retained-joint-comparison", type=Path,
                        help="optional retained joint target comparison kept as a separate gate")
    parser.add_argument("--d0-d1-receipt", type=Path,
                        help="optional completed same-day D0/D1 paper receipt to bind as a separate gate")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    output, report = write_outputs(candidate_path=args.candidate_adoption,
                                   factor_path=args.mature_factor_comparison,
                                   shortline_path=args.shortline_adoption,
                                   retained_joint_path=args.retained_joint_comparison,
                                   d0_d1_path=args.d0_d1_receipt,
                                   output=args.output, report=args.report)
    print(json.dumps({"status": "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP",
                      "output": str(output), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
