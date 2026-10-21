"""Parse an already captured THS simulation accessibility tree, without UI I/O.

This is funds-only evidence, not a complete account/holdings/order/fill bundle.
It cannot open a prefill, reconcile a Ledger or assert quote freshness.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re

from ..market_dataset import write_json

FIELDS = {"1012": "cash_balance_cents", "1013": "frozen_cash_cents",
          "1016": "available_cash_cents", "1017": "withdrawable_cash_cents",
          "1014": "reported_market_value_cents", "1015": "reported_total_assets_cents"}


def parse_funds_tree(tree, *, expected_account):
    if not expected_account.startswith("模拟炒股-") or not re.search(
            r'^Window: "网上股票交易系统5\.0", App: xiadan\.exe\.$', tree, re.M):
        raise ValueError("Only the observed THS simulation window/account is supported")
    accounts = re.findall(r"组合框.*?Value:\s*(.*?)\s+ID:\s*2322\s*$", tree, re.M)
    if accounts != [expected_account]:
        raise ValueError("Simulation account binding is missing, duplicated or different")
    result, raw_fields = {}, {}
    for control_id, name in FIELDS.items():
        values = re.findall(r"^\s*\d+\s+文本\s+(-?\d+\.\d{2})\s+ID:\s*" + control_id + r"\s*$", tree, re.M)
        if len(values) != 1:
            raise ValueError(f"Missing or ambiguous observed funds field {control_id}")
        cents = int(Decimal(values[0]) * 100)
        if cents < 0:
            raise ValueError("Negative balance is not supported by this cash-account observation")
        result[name] = cents
        raw_fields[control_id] = values[0]
    if (result["cash_balance_cents"] - result["frozen_cash_cents"] != result["available_cash_cents"]
            or result["cash_balance_cents"] + result["reported_market_value_cents"] != result["reported_total_assets_cents"]):
        raise ValueError("Observed cash/available/frozen or total-asset identity is inconsistent")
    return {"schema": "ths_simulation_accessibility_funds_only_v1",
            "status": "observed_funds_only_not_reconciliation_ready", "account_alias": expected_account,
            "account_kind": "simulation", "source": "saved_Computer_Use_accessibility_tree",
            "raw_field_values_by_control_id": raw_fields, **result,
            "ui_clock_values": re.findall(r"图像\s+(\d{2}:\d{2}:\d{2})\s+ID:\s*65535", tree),
            "quote_session": None, "capture_time_verified": False, "freshness_verified": False,
            "holdings_complete": False, "orders_complete": False, "fills_complete": False,
            "cashflows_complete": False, "fees_known": False,
            "native_full_bundle_verified": False, "ready_for_prefill": False,
            "submission_enabled": False, "execution_admitted": False, "ledger_mutated": False,
            "limitations": ["Custom holdings/order/fill tables were not extracted by the accessibility tree",
                            "A visible UI clock does not prove trading date, quote session or account freshness",
                            "No funds-only observation can replace the four-table and fee evidence"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = parse_funds_tree(args.tree.read_text(encoding="utf-8"), expected_account=args.expected_account)
    result.update(source_tree=str(args.tree.resolve()), recorded_at=datetime.now().astimezone().isoformat())
    if args.out.exists():
        raise ValueError("Preserve existing funds receipts; use a new output path")
    write_json(args.out, result)
    print(json.dumps({"status": result["status"], "cash_balance_cents": result["cash_balance_cents"],
                      "reported_total_assets_cents": result["reported_total_assets_cents"], "execution_admitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
