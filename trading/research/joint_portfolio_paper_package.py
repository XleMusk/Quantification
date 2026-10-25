"""Project an aligned multi-strategy target through the shared paper path.

``joint_portfolio_acceptance`` remains the pure target/constraint contract.
This module is the next explicit slice: it turns that accepted target into a
research-only target payload and delegates orders, BrokerAdapter routing,
paper fills, fees and cash/positions to ``build_paper_target_package``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .joint_portfolio_acceptance import (
    DEFAULT_SELECTED,
    VERIFIED,
    build_joint_portfolio,
    build_joint_portfolio_from_payloads,
)
from .stockdb_paper_target_package import build_paper_target_package


SCHEMA = "quantification_joint_portfolio_paper_package_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_JOINT_PAPER_PACKAGE"
BLOCKED = "BLOCKED_JOINT_TARGET"
BLOCKED_PAPER = "BLOCKED_PAPER_TARGET_ORDER"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _account_paths(path: str | Path | None) -> dict[str, Path]:
    if path is None:
        return {key: Path(value) for key, value in DEFAULT_SELECTED.items()}
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if isinstance(value, Mapping) and isinstance(value.get("accounts"), Mapping):
        value = value["accounts"]
    if not isinstance(value, Mapping) or not value:
        raise ValueError("accounts manifest must be a non-empty mapping")
    return {str(key): Path(str(item)).expanduser().resolve() for key, item in value.items()}


def _target_payload(joint: Mapping[str, Any], *, quote_source: str = "saved_aligned_strategy_terminal_mark") -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [row for row in joint.get("target_rows", [])
            if float(row.get("target_weight", 0.0)) > 0]
    if not rows:
        raise ValueError("Joint target has no positive rows")
    ordered = sorted(rows, key=lambda row: (-float(row["target_weight"]), str(row["symbol"])))
    # The paper order path must use the lot-rounded executable weight.  Using
    # the economic sleeve weight again can produce zero shares for an expensive
    # stock even though joint acceptance deliberately rounded it to one lot.
    weights = {str(row["symbol"]): float(row["actual_weight"]) for row in ordered}
    quantities = {str(row["symbol"]): int(row["target_quantity"]) for row in ordered}
    signals = []
    prices: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(ordered, 1):
        symbol = str(row["symbol"])
        code, exchange = symbol.split(".", 1)
        signals.append({"security": {"code": code, "exchange": exchange},
                        "diagnostics": {"rank": rank, "score": float(row["target_weight"]),
                                        "economic_target_weight": float(row["target_weight"]),
                                        "executable_weight": float(row["actual_weight"]),
                                        "target_quantity": int(row["target_quantity"]),
                                        "contributors": list(row.get("contributors", []))}})
        prices[symbol] = {
            "price_rmb": float(row["mark_cents"]) / 100.0,
            "source": quote_source,
            "observed_date": joint["common_terminal_day"],
        }
    target = {
        "schema": "joint_research_target_v1",
        "strategy_id": "joint_" + "_".join(joint["strategy_ids"]),
        "decision_day": joint["common_terminal_day"],
        "research_only": True,
        "execution_admitted": False,
        "weights": weights,
        "quantities": quantities,
        "signal_frame": {"signals": signals},
    }
    quote = {
        "schema": "ths_raw_quote_observation_v1",
        "source": quote_source,
        "observed_at": f"{joint['common_terminal_day']}T15:00:00+08:00",
        "price_basis": "raw_rmb",
        "snapshot_date": joint["common_terminal_day"],
        "execution_admitted": False,
        "client_interacted": False,
        "prices": prices,
    }
    return target, quote


def _build_paper_from_joint(joint: Mapping[str, Any], *, initial_cash_cny: float,
                            execution_day: str, settlement_day: str,
                            output_dir: str | Path | None,
                            quote_source: str) -> dict[str, Any]:
    if joint["status"] != VERIFIED:
        receipt = {
            "schema": SCHEMA, "status": BLOCKED,
            "reason": "joint_target_constraints_not_verified",
            "joint": joint, "execution_admitted": False,
            "broker_interacted": False, "ths_interacted": False,
            "paper_package": None,
        }
        return _persist(receipt, output_dir)

    # A joint target is a terminal research observation.  Reusing it for an
    # earlier execution day would introduce look-ahead into the paper route.
    if execution_day <= joint["common_terminal_day"]:
        raise ValueError("execution_day must be later than the common terminal day")

    target, quote = _target_payload(joint, quote_source=quote_source)
    package_dir = Path(output_dir).expanduser().resolve() / "paper_package" if output_dir else None
    try:
        package = build_paper_target_package(
            target, quote, initial_cash_cny,
            execution_day=execution_day, settlement_day=settlement_day,
            output_dir=package_dir,
        )
    except (ValueError, TypeError) as error:
        # Preserve the target and the exact paper-path rejection so a single
        # board, cash or quantity constraint is recoverable as evidence rather
        # than an unstructured CLI traceback.
        receipt = {
            "schema": SCHEMA, "status": BLOCKED_PAPER,
            "reason": str(error), "joint": joint,
            "target_payload": target, "quote_payload": quote,
            "execution_admitted": False, "broker_interacted": False,
            "ths_interacted": False, "persistent_ledger_mutated": False,
            "paper_package": None,
        }
        return _persist(receipt, output_dir)
    result = package["result"]
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "created_at": datetime.now().astimezone().isoformat(),
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "joint": joint,
        "target_payload": target,
        "quote_payload": quote,
        "paper_package": {
            "result": result,
            "target_count": result["target_count"],
            "paper_order_count": result["paper_order_count"],
            "route_available_count": result["route_available_count"],
            "projected_final_cash_rmb": result["projected_final_cash_rmb"],
            "projected_final_equity_rmb": result["projected_final_equity_rmb"],
        },
        "limitations": [
            "Saved terminal marks are historical research observations, not current broker quotes.",
            "Paper fills use the shared local Ledger and do not mutate a persistent account.",
            "No THS or broker client was contacted and no submission payload was sent.",
        ],
    }
    if package_dir is not None:
        receipt["paper_package_dir"] = str(package_dir)
    return _persist(receipt, output_dir)


def build_joint_paper_package(account_paths: Mapping[str, Path] = DEFAULT_SELECTED, *,
                              initial_cash_cny: float = 140_000.0,
                              gross_limit: float = 0.8,
                              single_limit: float = 0.2,
                              lot_size: int = 100,
                              capacity_policy: str = "strict",
                              max_target_rows: int = 22,
                              execution_day: str,
                              settlement_day: str,
                              output_dir: str | Path | None = None) -> dict[str, Any]:
    """Build one research-only joint target from saved account snapshots."""
    joint = build_joint_portfolio(
        account_paths, initial_cash_cny=initial_cash_cny, gross_limit=gross_limit,
        single_limit=single_limit, lot_size=lot_size,
        capacity_policy=capacity_policy, max_target_rows=max_target_rows,
    )
    return _build_paper_from_joint(
        joint, initial_cash_cny=initial_cash_cny, execution_day=execution_day,
        settlement_day=settlement_day, output_dir=output_dir,
        quote_source="saved_aligned_strategy_terminal_mark",
    )


def build_joint_paper_from_payloads(
    payloads: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]], *,
    initial_cash_cny: float = 140_000.0, gross_limit: float = 0.8,
    single_limit: float = 0.2, lot_size: int = 100,
    capacity_policy: str = "strict", max_target_rows: int = 22,
    execution_day: str, settlement_day: str,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build the same package directly from same-day Target+Quote payloads."""
    joint = build_joint_portfolio_from_payloads(
        payloads, initial_cash_cny=initial_cash_cny, gross_limit=gross_limit,
        single_limit=single_limit, lot_size=lot_size,
        capacity_policy=capacity_policy, max_target_rows=max_target_rows,
    )
    return _build_paper_from_joint(
        joint, initial_cash_cny=initial_cash_cny, execution_day=execution_day,
        settlement_day=settlement_day, output_dir=output_dir,
        quote_source="aligned_target_quote_payload",
    )


def _payload_manifest(path: str | Path) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    items = value.get("strategies") if isinstance(value, Mapping) else value
    if not isinstance(items, list) or not items:
        raise ValueError("payload manifest must contain a non-empty strategies list")
    result = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("payload manifest strategy entries must be objects")
        strategy_id = item.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError("payload manifest strategy_id is required")
        target_path = Path(str(item.get("target"))).expanduser().resolve()
        quote_path = Path(str(item.get("quotes"))).expanduser().resolve()
        if not target_path.is_file() or not quote_path.is_file():
            raise ValueError(f"payload files do not exist: {strategy_id}")
        result[strategy_id] = (
            json.loads(target_path.read_text(encoding="utf-8")),
            json.loads(quote_path.read_text(encoding="utf-8")),
        )
    return result


def _persist(receipt: dict[str, Any], output_dir: str | Path | None) -> dict[str, Any]:
    if output_dir is None:
        return receipt
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if receipt.get("target_payload") is not None:
        _write_json(root / "target_payload.json", receipt["target_payload"])
        _write_json(root / "quote_payload.json", receipt["quote_payload"])
    path = root / "joint_paper_receipt.json"
    receipt["receipt_path"] = str(path)
    _write_json(path, receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--accounts-manifest", type=Path)
    source_group.add_argument("--payload-manifest", type=Path)
    parser.add_argument("--initial-cash", type=float, default=140_000.0)
    parser.add_argument("--gross-limit", type=float, default=0.8)
    parser.add_argument("--single-limit", type=float, default=0.2)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--capacity-policy", choices=("strict", "budgeted"), default="strict")
    parser.add_argument("--max-target-rows", type=int, default=22)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    common = dict(initial_cash_cny=args.initial_cash, gross_limit=args.gross_limit,
                  single_limit=args.single_limit, lot_size=args.lot_size,
                  capacity_policy=args.capacity_policy, max_target_rows=args.max_target_rows,
                  execution_day=args.execution_day, settlement_day=args.settlement_day,
                  output_dir=args.output_dir)
    if args.payload_manifest is not None:
        result = build_joint_paper_from_payloads(_payload_manifest(args.payload_manifest), **common)
    else:
        result = build_joint_paper_package(_account_paths(args.accounts_manifest), **common)
    print(json.dumps({"status": result["status"],
                      "execution_admitted": result["execution_admitted"],
                      "paper_order_count": (result.get("paper_package") or {}).get("paper_order_count", 0),
                      "receipt": result.get("receipt_path")}, ensure_ascii=False))
    return 0 if result["status"] == STATUS else 1


__all__ = ["BLOCKED", "BLOCKED_PAPER", "SCHEMA", "STATUS",
           "build_joint_paper_package", "build_joint_paper_from_payloads", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
