"""Paper-only harness for exercising an Alpha158 checkpoint on a Ledger clone.

The persistent split checkpoint remains immutable.  A scenario operates on a
deep copy and returns a receipt that includes the source hash, so the test can
prove whether account mutations stayed outside the source journal.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from ..config import StrategyConfig
from ..contracts import RunContext
from ..ledger import Ledger


@dataclass
class SplitStateClone:
    source_file: Path
    source_sha256: str
    case_id: str
    context: RunContext
    state: dict[str, Any]


def clone_split_case(head_file: str | Path, case_id: str) -> SplitStateClone:
    source_file = Path(head_file).resolve()
    raw = source_file.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    case = payload.get("cases", {}).get(case_id)
    if not isinstance(case, dict) or not isinstance(case.get("state"), dict):
        raise ValueError("Alpha split case checkpoint is missing a state")
    context = RunContext.from_dict(case["context"])
    return SplitStateClone(source_file, sha256(raw).hexdigest(), case_id,
                           context, deepcopy(case["state"]))


def run_cancel_race(clone: SplitStateClone, config: StrategyConfig, *, symbol: str,
                    day: str, available_day: str, price: int = 1000, quantity: int = 200) -> dict[str, Any]:
    """Apply one late partial fill/cancel race to the cloned paper account."""

    book = Ledger(clone.state, config, clone.context)
    order_id = f"integration-clone-cancel:{clone.case_id}:{day}"
    fill_id = order_id + ":fill"
    order = book.submit(order_id, day, symbol, "buy", quantity, price)
    if order["status"] == "rejected":
        raise ValueError(f"Synthetic integration order rejected: {order.get('reason')}")
    book.request_cancel(order_id, day)
    book.fill(fill_id, order_id, quantity // 2, price, day, available_day)
    pending_status = order["status"]
    book.confirm_cancel(order_id, day)
    return {
        "schema": "integration_clone_cancel_race_v1",
        "case_id": clone.case_id,
        "source_file": str(clone.source_file),
        "source_sha256_before": clone.source_sha256,
        "pending_status_after_late_partial": pending_status,
        "final_order_status": order["status"],
        "filled_quantity": order["filled"],
        "execution_admitted": False,
        "account_path": "ledger_clone_only",
    }


def source_unchanged(clone: SplitStateClone) -> bool:
    return sha256(clone.source_file.read_bytes()).hexdigest() == clone.source_sha256


__all__ = ["SplitStateClone", "clone_split_case", "run_cancel_race", "source_unchanged"]
