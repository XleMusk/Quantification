"""Route a completed research replay into a historical shadow paper package.

The route consumes only a terminal research result and its replay-state prices.
It deliberately stays separate from the daily paper account and all broker/THS
state.  The existing paper target package owns the local Ledger projection.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .stockdb_paper_target_package import build_paper_target_package


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _target_payload(result: dict[str, Any], *, legacy_account: bool = False,
                    strategy_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build target and quote payloads from a terminal replay state.

    The current replay contract nests the state and explicitly carries the
    closed execution flags.  The rolling StockDB runner predates that result
    envelope and persists the same terminal fields directly in ``account.json``.
    That legacy form is accepted only through the explicit ``legacy_account``
    route, so a missing safety marker can never silently become an admitted
    research input.
    """

    if legacy_account:
        if result.get("execution_admitted") not in (None, False):
            raise ValueError("Legacy shadow account must not be execution-admitted")
        if result.get("strategy_validated") is True:
            raise ValueError("Legacy shadow account cannot be strategy-validated")
        state = result
        events = result.get("candidates") or []
        source = "stockdb_rolling_terminal_account_shadow_only"
    else:
        if result.get("execution_admitted") is not False:
            raise ValueError("Shadow paper requires an unadmitted replay result")
        if result.get("strategy_validated") is not False:
            raise ValueError("Shadow paper requires strategy_validated=false")
        state = result.get("state")
        events = result.get("selected_events", [])
        source = "stockdb_replay_terminal_state_shadow_only"
    if not isinstance(state, dict) or not isinstance(state.get("targets"), dict):
        raise ValueError("Replay result has no terminal target state")
    targets = {str(symbol): float(weight) for symbol, weight in state["targets"].items()}
    prices = state.get("prices") or {}
    if not targets or any(symbol not in prices for symbol in targets):
        raise ValueError("Terminal target state has missing replay-state prices")
    cutoff = str(result.get("end_date") or state.get("day") or "")
    if not cutoff:
        raise ValueError("Replay result has no terminal decision date")
    events = [row for row in events
              if row.get("date", cutoff) == cutoff and row.get("symbol") in targets]
    scores = {row["symbol"]: float(row.get("score", 0.0)) for row in events}
    ordered = sorted(targets, key=lambda symbol: (-scores.get(symbol, 0.0), symbol))
    signals = []
    for rank, symbol in enumerate(ordered, 1):
        code, exchange = symbol.split(".", 1)
        signals.append({
            "security": {"code": code, "exchange": exchange},
            "diagnostics": {"rank": rank, "score": scores.get(symbol, 0.0)},
        })
    resolved_strategy_id = strategy_id or result.get("strategy_id") or result.get("config", {}).get("strategy_id")
    if not isinstance(resolved_strategy_id, str) or not resolved_strategy_id:
        raise ValueError("Shadow paper requires a strategy_id")
    target = {
        "schema": "stockdb_candidate_shadow_target_v1",
        "strategy_id": resolved_strategy_id,
        "decision_day": cutoff,
        "research_only": True,
        "execution_admitted": False,
        "weights": targets,
        "signal_frame": {"signals": signals},
    }
    quote = {
        "schema": "ths_raw_quote_observation_v1",
        "source": source,
        "observed_at": f"{cutoff}T15:00:00+08:00",
        "price_basis": "raw_rmb",
        "execution_admitted": False,
        "client_interacted": False,
        "prices": {
            symbol: {
                "price_rmb": float(prices[symbol]) / 100.0,
                "source": "StockDB replay terminal state cents",
                "observed_date": cutoff,
            }
            for symbol in targets
        },
    }
    return target, quote


def build_shadow_package(replay_result: Path, output_dir: Path, *, initial_cash: float,
                         execution_day: str, settlement_day: str,
                         legacy_account: bool = False,
                         strategy_id: str | None = None) -> dict[str, Any]:
    replay_result = Path(replay_result).resolve()
    output_dir = Path(output_dir).resolve()
    result = _read_json(replay_result)
    target, quote = _target_payload(result, legacy_account=legacy_account, strategy_id=strategy_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = output_dir / "target_payload.json"
    quote_path = output_dir / "quote_payload.json"
    target_path.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quote_path.write_text(json.dumps(quote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    package = build_paper_target_package(
        target, quote, initial_cash,
        execution_day=execution_day, settlement_day=settlement_day,
        output_dir=output_dir / "package",
    )
    package_result = package["result"]
    receipt = {
        "schema": "a_share_candidate_shadow_paper_run_v1",
        "status": "COMPLETED_RESEARCH_ONLY_SHADOW_PAPER_PACKAGE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "historical_shadow_only": True,
        "source_replay_result": str(replay_result),
        "source_kind": "legacy_terminal_account" if legacy_account else "nested_replay_result",
        "target_payload": str(target_path),
        "quote_payload": str(quote_path),
        "package_result": package_result,
        "validation": {
            "account_observed": package_result["account_observed"],
            "client_interacted": package_result["client_interacted"],
            "submitted": package_result["submitted"],
            "cancelled": package_result["cancelled"],
            "ledger_persisted": package_result["ledger_persisted"],
        },
        "boundary_assertions": {
            "account_observed_is_false": package_result["account_observed"] is False,
            "client_interacted_is_false": package_result["client_interacted"] is False,
            "submitted_is_false": package_result["submitted"] is False,
            "cancelled_is_false": package_result["cancelled"] is False,
            "ledger_persisted_is_false": package_result["ledger_persisted"] is False,
        },
        "limitations": [
            "Replay-state prices are historical references, not current THS quotes.",
            "Initial cash and execution/settlement dates are research assumptions.",
            "This package is separate from the daily paper account and cannot be submitted.",
        ],
    }
    receipt_path = output_dir / "shadow_receipt.json"
    receipt["artifacts"] = {
        "target_payload": str(target_path),
        "quote_payload": str(quote_path),
        "package_result": str(output_dir / "package" / "result.json"),
        "shadow_receipt": str(receipt_path),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--replay-result", type=Path)
    source.add_argument("--terminal-account", type=Path,
                        help="legacy StockDB rolling-run account.json; research-only")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--strategy-id", help="required when the source has no strategy id")
    args = parser.parse_args(argv)
    input_path = args.replay_result or args.terminal_account
    legacy_account = args.terminal_account is not None
    receipt = build_shadow_package(
        input_path, args.output_dir,
        initial_cash=args.initial_cash,
        execution_day=args.execution_day,
        settlement_day=args.settlement_day,
        legacy_account=legacy_account,
        strategy_id=args.strategy_id,
    )
    print(json.dumps({
        "status": receipt["status"],
        "strategy_id": receipt["package_result"]["strategy_id"],
        "target_count": receipt["package_result"]["target_count"],
        "paper_order_count": receipt["package_result"]["paper_order_count"],
        "execution_admitted": receipt["execution_admitted"],
        "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
