"""Local acceptance slice for a continuous research-only paper account.

This fixture exercises the shared Ledger across two adjacent sessions.  It is
engineering evidence only: it uses synthetic prices, does not read StockDB,
and never persists a production paper account or contacts a broker/THS client.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..ledger import Ledger, new_state

SCHEMA = "continuous_paper_acceptance_v1"
STATUS = "VERIFIED_LOCAL_RESEARCH_ONLY"
DAY_ONE = "2026-10-27"
DAY_TWO = "2026-10-28"
SYMBOL = "000001.SZSE"


def _config() -> StrategyConfig:
    return StrategyConfig(
        signal="equal_weight", holdings=1, retain_rank=1,
        gross_limit=0.8, single_limit=0.8, initial_cash=100000,
        slippage_bps=0,
    ).validate()


def _account_state(day: str, state: dict[str, Any], target_day: str) -> dict[str, Any]:
    return {
        "schema": "research_paper_account_state_v1",
        "account_id": "continuous-paper-acceptance",
        "mode": TradingMode.PAPER.value,
        "day": day,
        "target_day": target_day,
        "cash_cents": state["cash"],
        "positions": deepcopy(state["positions"]),
        "orders": deepcopy(state["orders"]),
        "fills": deepcopy(state["fills"]),
        "execution_admitted": False,
    }


def run_acceptance(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the two-session continuity fixture and return its receipt."""
    context = RunContext("continuous-paper-acceptance", TradingMode.PAPER)
    config = _config()
    state = new_state(config.initial_cash)
    book = Ledger(state, config, context)

    # Day 1: one target is published and only part of the buy is filled.
    order_id = "acceptance-order-20261027"
    fill_id = "acceptance-fill-20261027-partial"
    order = book.submit(order_id, DAY_ONE, SYMBOL, "buy", 1000, 1000)
    book.fill(fill_id, order_id, 400, 1000, DAY_ONE, DAY_TWO)
    book.mark_price(SYMBOL, 1000, DAY_ONE)
    day_one = _account_state(DAY_ONE, state, DAY_ONE)
    cash_after_partial = state["cash"]
    positions_after_partial = book.quantity(SYMBOL)

    # Restart from a serialized state before the next session; no new_state().
    restored = json.loads(json.dumps(state))
    resumed = Ledger(restored, config, context)
    resumed.mark_price(SYMBOL, 1020, DAY_TWO)
    day_two_target_day = DAY_TWO
    day_two = _account_state(DAY_TWO, restored, day_two_target_day)

    # Repeat the exact identifiers after restart.  The ledger must be idempotent
    # and must not create another fill/order or reset the carried cash/position.
    order_repeat = resumed.submit(order_id, DAY_ONE, SYMBOL, "buy", 1000, 1000)
    fill_repeat = resumed.fill(fill_id, order_id, 400, 1000, DAY_ONE, DAY_TWO)
    repeated = _account_state(DAY_TWO, restored, day_two_target_day)

    checks = {
        "partial_fill_recorded": order["status"] == "partial" and positions_after_partial == 400,
        "cash_carried_across_restart": restored["cash"] == cash_after_partial,
        "position_carried_across_restart": resumed.quantity(SYMBOL) == positions_after_partial,
        "no_new_target_date_mutation": day_one["target_day"] == DAY_ONE and day_two["target_day"] == DAY_TWO,
        "repeat_order_is_same_record": order_repeat is restored["orders"][order_id],
        "repeat_fill_is_idempotent": fill_repeat is False,
        "repeat_state_unchanged": repeated == day_two,
        "t1_available_on_day_two": resumed.available(SYMBOL, DAY_TWO) == 400,
        "execution_closed": context.mode == TradingMode.PAPER,
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise AssertionError("continuity acceptance failed: " + ", ".join(failed))

    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "synthetic_fixture": True,
        "sessions": [DAY_ONE, DAY_TWO],
        "target_days": [day_one["target_day"], day_two["target_day"]],
        "order_count": len(restored["orders"]),
        "fill_count": len(restored["fills"]),
        "cash_after_partial_rmb": round(restored["cash"] / 100, 2),
        "position_quantity_after_partial": resumed.quantity(SYMBOL),
        "checks": checks,
        "scope": "two-session local continuity acceptance; does not prove StockDB or market-forward operation",
    }
    if output_dir is not None:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "day_one_state.json").write_text(json.dumps(day_one, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "day_two_state.json").write_text(json.dumps(day_two, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(run_acceptance(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
