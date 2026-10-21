"""Show the existing single next-plan as a read-only visual operator card.

This module never creates a client window, writes fields, saves plans, or advances
an order queue. Account sizing and order sequencing remain in THSAdapter.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from .adapter import (
    build_prefill_payload,
    compare_prefill_readback,
    gui_prefill_profile_status,
    now,
    validate_gui_prefill_profile,
)


def build_visual_operator_card(
    preview: dict,
    profile: dict,
    *,
    current: datetime | None = None,
    readback: dict | None = None,
) -> dict:
    """Describe only THSAdapter's next_plan; do not turn paper packages into plans."""
    if (
        not isinstance(preview, dict)
        or preview.get("execution_admitted") is not False
        or preview.get("submission") != "manual_only"
        or preview.get("status") not in {"awaiting_review", "blocked", "no_change"}
        or not isinstance(preview.get("adjustments"), list)
        or not preview.get("account_alias")
    ):
        raise ValueError("Use an existing non-admitted THSAdapter portfolio preview, not a research paper package")
    current = current or now()
    profile_status = gui_prefill_profile_status(profile)
    card = {
        "schema": "ths_visual_operator_card_v1",
        "status": "pending_plan_validation",
        "generated_at": current.isoformat(),
        "strategy_id": preview.get("strategy_id"),
        "account_alias": preview["account_alias"],
        "account_observed_at": preview.get("account_observed_at"),
        "source_preview_status": preview["status"],
        "source_plan_saved": preview.get("saved") is True,
        "profile_status": profile_status,
        "next_plan_id": None,
        "next_order": None,
        "expected_fields": None,
        "readback_check": None,
        "blockers": [],
        "operator_steps": [],
        "remaining_adjustments": [
            {key: deepcopy(row[key]) for key in (
                "symbol", "side", "held_qty", "target_qty", "delta_qty", "status", "validation_reason"
            ) if key in row}
            for row in preview["adjustments"] if isinstance(row, dict) and row.get("status") != "next_plan"
        ],
        "execution_admitted": False,
        "write_allowed": False,
        "submission_enabled": False,
        "submitted": False,
        "cancelled": False,
        "client_interacted": False,
        "ledger_mutated": False,
        "automatic_next_step_allowed": False,
        "boundary": "Offline instructions only; supplied profile/readback are not proof of a current client window.",
        "next_after_manual_checkpoint": (
            "Read orders, trades, actual fees and refreshed account; reconcile the existing plan, "
            "then re-run the same THSAdapter portfolio consumer for its next_plan."
        ),
    }
    if preview["status"] == "no_change":
        if preview.get("next_plan") is not None:
            raise ValueError("A no-change preview cannot contain a next_plan")
        card["status"] = "no_adjustment_required"
        return card
    if preview["status"] == "blocked":
        if preview.get("next_plan") is not None:
            raise ValueError("A blocked preview cannot contain a next_plan")
        card.update(status="blocked_portfolio_preview", blockers=[preview.get("reason") or "portfolio_preview_blocked"])
        return card

    plan = preview.get("next_plan")
    next_rows = [row for row in preview["adjustments"] if isinstance(row, dict) and row.get("status") == "next_plan"]
    if (
        not isinstance(plan, dict)
        or len(next_rows) != 1
        or plan.get("account_alias") != preview["account_alias"]
        or next_rows[0].get("symbol") != plan.get("symbol")
        or next_rows[0].get("side") != plan.get("side")
        or abs(next_rows[0].get("delta_qty", 0)) != plan.get("qty")
    ):
        raise ValueError("Preview must identify exactly the existing next_plan adjustment")
    try:
        fields = build_prefill_payload(plan, current=current)
    except (ValueError, KeyError, TypeError) as error:
        card.update(status="blocked_plan_validation", blockers=[str(error)])
        return card

    card.update(
        next_plan_id=plan["id"],
        next_order={"symbol": plan["symbol"], "side": plan["side"], "qty": plan["qty"],
                    "price_cents": plan["price_cents"], "expires_at": plan["expires_at"]},
        expected_fields=fields,
    )
    if profile_status["status"] != "ready":
        card["blockers"].append("gui_profile_" + profile_status["status"])
    elif profile.get("account_alias") != plan["account_alias"]:
        card["blockers"].append("gui_profile_account_mismatch")

    guard_expectations = {}
    if not card["blockers"]:
        validate_gui_prefill_profile(profile)
        # These are the same three guards consumed by PrefillAdapter.guard.
        # Their current text and resolved controls can only be checked in-client.
        for name in ("account", "side", "price_mode"):
            guard = (profile.get("guards") or {}).get(name) or {}
            expected = guard.get("expected")
            if name == "side":
                expected = expected.get(plan["side"]) if isinstance(expected, dict) else None
            if not isinstance(guard.get("selector"), dict) or not guard["selector"] or not expected:
                card["blockers"].append("gui_profile_missing_guard_" + name)
            else:
                guard_expectations[name] = expected
    if card["blockers"]:
        card["status"] = "blocked_gui_profile"
        return card

    card["operator_steps"] = [
        {"sequence": 1, "kind": "inspect", "instruction": "核对可见账户、买卖页和限价模式；弹窗或页面不符立即停止。",
         "expected": guard_expectations, "mapping_refs": ["guards.account", "guards.side", "guards.price_mode"]},
        {"sequence": 2, "kind": "reference", "instruction": "仅核对这一笔的证券代码、价格、股数。预填后逐字段回读。",
         "expected": deepcopy(fields), "mapping_refs": ["fields.symbol", "fields.price", "fields.qty"]},
        {"sequence": 3, "kind": "readback", "instruction": "核对回读代码、价格和股数完全一致；不一致停止，不重发。"},
        {"sequence": 4, "kind": "manual_checkpoint", "instruction": "停在人工提交检查点。本卡片不提交，也不自动切到下一笔。"},
        {"sequence": 5, "kind": "refresh", "instruction": "取得委托、成交、实际费用和刷新账户证据；对账后重新计算下一笔。"},
    ]
    for step in card["operator_steps"]:
        step["execution_enabled"] = False
    if readback is None:
        card["status"] = "offline_single_plan_reference_ready"
    else:
        check = compare_prefill_readback(plan, readback, current=current)
        check["source"] = "caller_supplied_json_not_live_client_capture"
        card["readback_check"] = check
        card["status"] = "readback_verified_manual_checkpoint" if check["status"] == "readback_verified" else "readback_mismatch_stop"
        if check["status"] != "readback_verified":
            card["blockers"].append("form_readback_mismatch")
    return card


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True, help="Existing THSAdapter portfolio preview JSON")
    parser.add_argument("--profile", type=Path, required=True, help="Existing local GUI mapping JSON")
    parser.add_argument("--readback-json", type=Path, help="Optional saved symbol/price/qty readback JSON")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    load = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    try:
        card = build_visual_operator_card(load(args.preview), load(args.profile),
                                          readback=load(args.readback_json) if args.readback_json else None)
    except (ValueError, KeyError, TypeError) as error:
        card = {"schema": "ths_visual_operator_card_v1", "status": "blocked_input",
                "blockers": [str(error)], "next_plan_id": None, "next_order": None,
                "expected_fields": None, "operator_steps": [], "write_allowed": False,
                "submission_enabled": False, "execution_admitted": False,
                "client_interacted": False, "ledger_mutated": False}
    card["source_artifacts"] = {"preview": str(args.preview.resolve()), "profile": str(args.profile.resolve()),
                                "readback": str(args.readback_json.resolve()) if args.readback_json else None}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.out.resolve()), "status": card["status"], "next_plan_id": card["next_plan_id"],
                      "write_allowed": False, "submission_enabled": False, "execution_admitted": False}, ensure_ascii=False))
    return 2 if card["status"] == "blocked_input" else 0


if __name__ == "__main__":
    raise SystemExit(main())
