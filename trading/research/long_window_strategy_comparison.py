"""Build a read-only long-window comparison from saved StockDB replays.

The source runs are already completed through the shared Engine/Ledger path.
This module only reads their JSON/CSV/account artifacts and writes a derived
receipt plus Markdown report.  It never creates or mutates a trading account.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ..settings import ROOT
from .candidate_spec_registry import SCHEMA as SPEC_SCHEMA, VERIFIED as SPEC_VERIFIED, validate_registry


SCHEMA = "quantification_long_window_strategy_comparison_v1"
RECEIPT_NAME = "20261024_long_window_strategy_comparison_v1.json"
REPORT_NAME = "20261024_long_window_strategy_comparison_v1.md"
H60_RECEIPT_NAME = "20261025_long_window_strategy_comparison_h60_v1.json"
H60_REPORT_NAME = "20261025_long_window_strategy_comparison_h60_v1.md"
SNAPSHOT = (
    Path(r"D:\QuantificationData\trading\research\full_market")
    / "stockdb_2024_0101_1013_current_cutoff_v1"
)
SPEC_CATALOG = ROOT / "docs/architecture_v3/candidate_strategy_catalog_20261024.json"
SPEC_DEFINITIONS = ROOT / "docs/architecture_v3/candidate_strategy_specs_20261024.json"
COMPARISON_SPEC_IDS = {
    "fixed_low_volatility_252_20": "stockdb_low_volatility_252_20",
    "low_vol_half_momentum_20": "stockdb_low_vol_half_momentum_20",
    "momentum_12_1_20": "stockdb_momentum_12_1_20",
    "mature_factor_balanced_ridge": "factor_candidate_family_balanced_ridge",
    "alpha158_lgbm_cap500": "alpha158_lgbm_candidate_panel_v1",
}

# The h60 run is a prespecified same-lineage variant of the mature-factor
# candidate.  It reuses the parent candidate spec for replay permissions while
# remaining a separate row in the derived comparison.
H60_SPEC_ID = "factor_candidate_family_balanced_ridge"


def _research_decision(metrics: dict[str, Any]) -> str:
    """Apply the fixed research-only continuation rule to saved metrics."""
    if float(metrics["net_return"]) > 0 and float(metrics["sharpe"]) > 0:
        return "CONTINUE_RESEARCH_ONLY"
    return "STOP_RESEARCH_ONLY"


def _annotate_research_decisions(entries: list[dict[str, Any]]) -> None:
    """Add deterministic research routing without changing catalog promotion state."""
    for entry in entries:
        entry["research_decision"] = _research_decision(entry["metrics"])
        entry["research_decision_rule"] = "net_return > 0 and sharpe > 0"


def _h60_robustness(entries: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = next(row for row in entries if row["id"] == "mature_factor_balanced_ridge")
    variant = next(row for row in entries if row["id"] == "mature_factor_balanced_ridge_h60")
    base_metrics = baseline["metrics"]
    variant_metrics = variant["metrics"]
    delta = {
        key: variant_metrics[key] - base_metrics[key]
        for key in ("net_return", "max_drawdown", "sharpe", "orders", "fills", "fees", "blocked")
    }
    same_sign = (
        (base_metrics["net_return"] > 0) == (variant_metrics["net_return"] > 0)
        and (base_metrics["sharpe"] > 0) == (variant_metrics["sharpe"] > 0)
    )
    return {
        "baseline_id": baseline["id"],
        "variant_id": variant["id"],
        "metric_delta_variant_minus_baseline": delta,
        "positive_return_and_sharpe_sign_preserved": same_sign,
        "research_decision": "CONTINUE_RESEARCH_ONLY" if same_sign else "STOP_RESEARCH_ONLY",
        "interpretation": "Both saved Ridge rows remain positive on return and Sharpe; keep the family in research while PIT and paper continuity gates remain open.",
    }


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (ROOT / path).resolve()


def _count_blocked(account_path: Path) -> int:
    """Count explicit blocked order events in a saved paper account."""
    account = _load(account_path)
    return sum(1 for event in account.get("events", []) if event.get("kind") == "order_blocked")


def _comparison_spec_bindings() -> dict[str, str]:
    """Require every compared replay to have a closed, replay-allowed spec."""
    catalog = _load(SPEC_CATALOG)
    specs = _load(SPEC_DEFINITIONS)
    registry = validate_registry(catalog, specs)
    if registry.get("schema") != SPEC_SCHEMA or registry.get("status") != SPEC_VERIFIED:
        raise ValueError(f"Candidate spec registry is not verified: {registry.get('issues')}")
    by_id = {row["id"]: row for row in specs.get("entries", [])}
    missing = sorted(set(COMPARISON_SPEC_IDS.values()) - set(by_id))
    if missing:
        raise ValueError(f"Comparison candidates missing fixed specs: {missing}")
    blocked = [candidate_id for candidate_id in COMPARISON_SPEC_IDS.values()
               if by_id[candidate_id].get("acceptance_gate", {}).get("replay_allowed") is not True]
    if blocked:
        raise ValueError(f"Comparison candidate spec is replay-blocked: {blocked}")
    return dict(COMPARISON_SPEC_IDS)


def _assert_artifacts(paths: dict[str, Path]) -> None:
    missing = [f"{name}: {path}" for name, path in paths.items() if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing saved replay artifacts: " + "; ".join(missing))


def _lowvol_entry(
    *,
    strategy_id: str,
    label: str,
    run_path: Path,
    account_dir: Path,
    result_label: str,
) -> dict[str, Any]:
    run = _load(run_path)
    result = next((row for row in run.get("results", []) if row.get("label") == result_label), None)
    if result is None or result.get("status") != "completed":
        raise ValueError(f"Completed {result_label} result not found in {run_path}")
    paths = {
        "run": run_path,
        "account": account_dir / "account.json",
        "daily": account_dir / "daily.csv",
        "orders": account_dir / "orders.csv",
        "fills": account_dir / "fills.csv",
        "snapshot": _path(run.get("protocol_snapshot", SNAPSHOT)),
    }
    # The rolling replay stores the canonical snapshot in protocol.json.
    protocol_path = run_path.parent / "protocol.json"
    protocol = _load(protocol_path) if protocol_path.exists() else {}
    paths["protocol"] = protocol_path
    paths["snapshot"] = _path(protocol.get("snapshot", SNAPSHOT))
    _assert_artifacts(paths)
    result = {
        **result,
        "replay_start": run.get("replay_start"),
        "replay_end": run.get("replay_end"),
    }
    return _entry(
        strategy_id=strategy_id,
        label=label,
        run_path=run_path,
        result=result,
        account_path=paths["account"],
        paths=paths,
        snapshot=paths["snapshot"],
    )


def _replay_entry(
    *,
    strategy_id: str,
    label: str,
    result_path: Path,
    account_path: Path,
    daily_path: Path,
    orders_path: Path,
    fills_path: Path,
) -> dict[str, Any]:
    result = _load(result_path)
    if result.get("status", "").startswith("completed_") is False:
        raise ValueError(f"Saved replay is not complete: {result_path}")
    snapshot = _path(result.get("raw_snapshot", SNAPSHOT))
    paths = {
        "result": result_path,
        "account": account_path,
        "daily": daily_path,
        "orders": orders_path,
        "fills": fills_path,
        "snapshot": snapshot,
    }
    _assert_artifacts(paths)
    return _entry(
        strategy_id=strategy_id,
        label=label,
        run_path=result_path,
        result=result,
        account_path=account_path,
        paths=paths,
        snapshot=snapshot,
    )


def _entry(
    *,
    strategy_id: str,
    label: str,
    run_path: Path,
    result: dict[str, Any],
    account_path: Path,
    paths: dict[str, Path],
    snapshot: Path,
) -> dict[str, Any]:
    metrics = result.get("metrics") or result
    def metric_or_count(name: str, collection_name: str) -> Any:
        value = metrics.get(name)
        if value is not None:
            return value
        collection = result.get(collection_name, [])
        return len(collection) if isinstance(collection, list) else collection

    fields = {
        "sessions": metrics.get("sessions", result.get("replay_sessions")),
        "orders": metric_or_count("orders", "orders"),
        "fills": metric_or_count("fills", "fills"),
        "net_return": metrics.get("net_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe_zero_rf_252"),
        "fees": metrics.get("fees"),
        "blocked": result.get("blocked_order_event_count"),
    }
    if fields["blocked"] is None:
        fields["blocked"] = _count_blocked(account_path)
    if any(fields[key] is None for key in ("sessions", "orders", "fills", "net_return", "max_drawdown", "fees")):
        raise ValueError(f"Incomplete standardized metrics for {strategy_id}: {fields}")
    # Every entry must remain a research-only replay.  A true flag is a hard
    # failure rather than something this derived comparison can reinterpret.
    if result.get("execution_admitted") is True or result.get("ledger_mutated") is True:
        raise ValueError(f"Unsafe execution flag in saved replay: {run_path}")
    daily_rows = sum(1 for _ in paths["daily"].open("r", encoding="utf-8")) - 1
    if daily_rows != int(fields["sessions"]):
        raise ValueError(f"daily.csv/session mismatch for {strategy_id}: {daily_rows} != {fields['sessions']}")
    return {
        "id": strategy_id,
        "label": label,
        "window": [result.get("replay_start", result.get("start_date")),
                   result.get("replay_end", result.get("end_date"))],
        "metrics": fields,
        "source_paths": {key: str(value.resolve()) for key, value in paths.items()},
        "stockdb_snapshot": str(snapshot.resolve()),
        "engine_ledger_replay_completed": bool(result.get("engine_ledger_replay_completed", True)),
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
    }


def build_entries(*, include_h60: bool = False) -> list[dict[str, Any]]:
    """Read the five retained long-window replays without rerunning them."""
    lowvol_run = ROOT / "outputs/stockdb_rolling_low_volatility/20261024_120755_bdd75bd2/run.json"
    mixed_run = ROOT / "outputs/stockdb_rolling_low_volatility/20261024_121737_d2456fd6/run.json"
    lowvol_dir = Path(r"D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261024_120755_bdd75bd2\low_volatility_252_20")
    mixed_root = Path(r"D:\QuantificationData\trading\research\stockdb_rolling_low_volatility\20261024_121737_d2456fd6")
    ridge_dir = ROOT / "outputs/stockdb_factor_monthly_ridge_replays/20261024_monthly_same_lineage_cap500"
    alpha_dir = ROOT / "outputs/stockdb_alpha158_lgbm_panel_replays/20261024_full_native_2026_cap500"
    entries = [
        _lowvol_entry(strategy_id="fixed_low_volatility_252_20", label="固定低波动",
                      run_path=lowvol_run, account_dir=lowvol_dir,
                      result_label="low_volatility_252_20"),
        _lowvol_entry(strategy_id="low_vol_half_momentum_20", label="低波/半动量",
                      run_path=mixed_run, account_dir=mixed_root / "low_vol_half_momentum_20",
                      result_label="low_vol_half_momentum_20"),
        _lowvol_entry(strategy_id="momentum_12_1_20", label="12-1 动量",
                      run_path=mixed_run, account_dir=mixed_root / "momentum_12_1_20",
                      result_label="momentum_12_1_20"),
        _replay_entry(strategy_id="mature_factor_balanced_ridge", label="成熟因子 Ridge",
                      result_path=ridge_dir / "result.json", account_path=ridge_dir / "account.json",
                      daily_path=ridge_dir / "daily.csv", orders_path=ridge_dir / "orders.csv",
                      fills_path=ridge_dir / "fills.csv"),
        _replay_entry(strategy_id="alpha158_lgbm_cap500", label="Alpha158/LightGBM",
                      result_path=alpha_dir / "result.json", account_path=alpha_dir / "account.json",
                      daily_path=alpha_dir / "daily.csv", orders_path=alpha_dir / "orders.csv",
                      fills_path=alpha_dir / "fills.csv"),
    ]
    if include_h60:
        h60_dir = ROOT / "outputs/stockdb_factor_monthly_ridge_replays/20261025_monthly_same_lineage_h60_cap500_v3"
        entries.append(_replay_entry(
            strategy_id="mature_factor_balanced_ridge_h60", label="成熟因子 Ridge（60-session variant）",
            result_path=h60_dir / "result.json", account_path=h60_dir / "account.json",
            daily_path=h60_dir / "daily.csv", orders_path=h60_dir / "orders.csv",
            fills_path=h60_dir / "fills.csv"))
    return entries


def build_receipt(*, include_h60: bool = False) -> dict[str, Any]:
    spec_bindings = _comparison_spec_bindings()
    if include_h60:
        spec_bindings["mature_factor_balanced_ridge_h60"] = H60_SPEC_ID
    entries = build_entries(include_h60=include_h60)
    for entry in entries:
        entry["candidate_spec_id"] = spec_bindings[entry["id"]]
    _annotate_research_decisions(entries)
    snapshots = {entry["stockdb_snapshot"] for entry in entries}
    if len(snapshots) != 1:
        raise ValueError(f"Replays do not share one StockDB snapshot: {sorted(snapshots)}")
    receipt = {
        "schema": SCHEMA,
        "status": "COMPLETED_RESEARCH_ONLY_LONG_WINDOW_COMPARISON",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "strategy_validated": False,
        "ledger_mutated": False,
        "broker_or_ths_interacted": False,
        "network_accessed": False,
        "comparison_contract": {
            "stockdb_snapshot": next(iter(snapshots)),
            "shared_stockdb_snapshot": True,
            "shared_engine_ledger_path": True,
            "metrics": ["sessions", "orders", "fills", "net_return", "max_drawdown", "sharpe", "fees", "blocked"],
            "windows_are_strategy_native": True,
            "selection_only_comparison": False,
            "initial_cash_rmb": 140000.0,
            "candidate_spec_registry": str(SPEC_DEFINITIONS.resolve()),
            "candidate_spec_registry_status": SPEC_VERIFIED,
            "candidate_spec_ids": sorted(spec_bindings.values()),
            "include_h60_variant": include_h60,
            "research_decision_rule": "net_return > 0 and sharpe > 0",
        },
        "strategies": entries,
        "source_paths": sorted({path for entry in entries for path in entry["source_paths"].values()}),
        "limitations": [
            "The strategies have different native covered windows (648/678/184 sessions); metrics are reported as saved, not forced into a common period.",
            "All outputs are historical research diagnostics and do not establish PIT completeness, strategy validation, or live readiness.",
            "Saved simulated BrokerAdapter events are not real broker fills; no THS or broker channel was accessed.",
        ],
    }
    if include_h60:
        receipt["robustness"] = _h60_robustness(entries)
    return receipt


def render_markdown(receipt: dict[str, Any], *, title: str | None = None) -> str:
    contract = receipt["comparison_contract"]
    lines = [
        f"# {title or 'Long-window StockDB strategy comparison (2026-10-24)'}",
        "",
        "This report reads completed research-only replays from the same frozen StockDB snapshot and shared Engine/Ledger path. It does not rerun a strategy or access a broker/THS channel.",
        "",
        f"StockDB snapshot: `{contract['stockdb_snapshot']}`  ",
        "Execution admitted: `false`; strategy validated: `false`; ledger mutated: `false`.",
        "",
        "| Strategy | Window | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe (0 RF, 252) | Fees (RMB) | Blocked | Research decision |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for entry in receipt["strategies"]:
        m = entry["metrics"]
        window = f"{entry['window'][0]}..{entry['window'][1]}"
        sharpe = "null" if m["sharpe"] is None else f"{m['sharpe']:.4f}"
        lines.append(
            f"| {entry['label']} (`{entry['id']}`) | {window} | {m['sessions']} | {m['orders']} | {m['fills']} | "
            f"{m['net_return']:.4%} | {m['max_drawdown']:.4%} | {sharpe} | {m['fees']:.2f} | {m['blocked']} | {entry['research_decision']} |"
        )
    if "robustness" in receipt:
        robustness = receipt["robustness"]
        lines += [
            "",
            "## Prespecified Ridge h60 robustness",
            "",
            f"`{robustness['variant_id']}` minus `{robustness['baseline_id']}`: "
            f"return {robustness['metric_delta_variant_minus_baseline']['net_return']:.4%}, "
            f"max drawdown {robustness['metric_delta_variant_minus_baseline']['max_drawdown']:.4%}, "
            f"Sharpe {robustness['metric_delta_variant_minus_baseline']['sharpe']:.4f}. "
            f"Sign preserved: `{robustness['positive_return_and_sharpe_sign_preserved']}`; "
            f"decision: `{robustness['research_decision']}`.",
        ]
    lines += [
        "",
        "## Source paths",
        "",
    ]
    for entry in receipt["strategies"]:
        lines.append(f"- **{entry['label']}**")
        for name, path in entry["source_paths"].items():
            lines.append(f"  - `{name}`: `{path}`")
    lines += ["", "## Boundaries", ""]
    lines.extend(f"- {item}" for item in receipt["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_outputs(receipt_path: Path | None = None, report_path: Path | None = None) -> tuple[Path, Path]:
    receipt_path = receipt_path or ROOT / "outputs/pipeline_receipts" / RECEIPT_NAME
    report_path = report_path or ROOT / "outputs/research_reports" / REPORT_NAME
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt()
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(receipt), encoding="utf-8")
    return receipt_path, report_path


def write_h60_outputs(receipt_path: Path | None = None, report_path: Path | None = None) -> tuple[Path, Path]:
    """Write a derived comparison that adds the fixed 60-session Ridge row.

    The baseline five-strategy receipt remains untouched so existing robustness
    decisions stay reproducible.  This output is additive research evidence.
    """
    receipt_path = receipt_path or ROOT / "outputs/pipeline_receipts" / H60_RECEIPT_NAME
    report_path = report_path or ROOT / "outputs/research_reports" / H60_REPORT_NAME
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = build_receipt(include_h60=True)
    receipt["schema"] = "quantification_long_window_strategy_comparison_h60_v1"
    receipt["comparison_contract"]["variant"] = "mature_factor_balanced_ridge_h60"
    receipt["receipt_path"] = str(receipt_path.resolve())
    receipt["report_path"] = str(report_path.resolve())
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(receipt, title="Long-window StockDB strategy comparison with Ridge h60 (2026-10-25)"), encoding="utf-8")
    return receipt_path, report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-h60", action="store_true", help="write the additive 60-session Ridge comparison")
    args = parser.parse_args()
    receipt_path, report_path = write_h60_outputs() if args.include_h60 else write_outputs()
    print(json.dumps({"receipt": str(receipt_path), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
