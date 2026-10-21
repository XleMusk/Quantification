"""Build one opening-session consumer from several fixed paper previews.

This is an index and comparison artifact over existing Engine/Ledger previews.
It never creates broker observations, submits orders, or selects a strategy
automatically. An explicit caller-selected strategy may be narrowed to one
paper pack for the next hydration step.
"""

import argparse
import csv
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import uuid

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..ledger import Ledger
from ..settings import DATA_ROOT, ROOT


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _price_cents(value):
    try:
        amount = (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return int(amount) if amount > 0 else None


def _result_path(value):
    path = Path(value).resolve()
    return path if path.name == "result.json" else path / "result.json"


def _execution_config(result):
    embedded = result.get("execution_config")
    if isinstance(embedded, dict):
        fields = StrategyConfig.__dataclass_fields__
        values = {name: embedded[name] for name in fields if name in embedded}
        return StrategyConfig(**values).validate().to_dict()
    source_path = result.get("source_run")
    if source_path and Path(source_path).is_file():
        source = _read_json(source_path)
        portfolio = source.get("portfolio") or (source.get("protocol") or {}).get("portfolio") or {}
        fields = StrategyConfig.__dataclass_fields__
        values = {name: portfolio[name] for name in fields if name in portfolio}
        return StrategyConfig(**values).validate().to_dict()
    return StrategyConfig().validate().to_dict()


def _load_preview(value):
    result_path = _result_path(value)
    result = _read_json(result_path)
    if result.get("status") != "paper_preview_only":
        raise ValueError(f"Preview is not paper_preview_only: {result_path}")
    if result.get("execution_admitted") is not False:
        raise ValueError(f"Preview execution flag is not false: {result_path}")
    artifacts = result.get("artifacts") or {}
    order_path = artifacts.get("provisional_order_plan")
    handoff_path = artifacts.get("ths_manual_handoff")
    if not order_path or not Path(order_path).is_file():
        raise ValueError(f"Missing provisional order plan: {result_path}")
    if not handoff_path or not Path(handoff_path).is_file():
        raise ValueError(f"Missing THS handoff: {result_path}")
    orders = []
    for row in _read_csv(order_path):
        qty = _int(row.get("qty"))
        if qty <= 0:
            continue
        orders.append({
            "strategy_id": result["strategy_id"],
            "symbol": row.get("symbol", ""),
            "side": row.get("side", ""),
            "qty": qty,
            "price_rmb": row.get("price_rmb"),
            "estimated_notional_rmb": row.get("estimated_notional_rmb"),
            "source_status": row.get("status", ""),
            "source_reason": row.get("reason", ""),
        })
    handoff = _read_json(handoff_path)
    targets = []
    for row in handoff.get("target_template", []):
        targets.append({
            "strategy_id": result["strategy_id"],
            "symbol": row.get("symbol", ""),
            "target_qty": _int(row.get("target_qty")),
            "active_target": _int(row.get("target_qty")) > 0,
            "price_basis": row.get("price_basis", ""),
        })
    return {
        "result_path": str(result_path),
        "result": result,
        "execution_config": _execution_config(result),
        "orders": orders,
        "targets": targets,
        "handoff": handoff,
    }


def _load_comparison(path):
    comparison = _read_json(path)
    if not isinstance(comparison.get("cases"), list):
        raise ValueError(f"Comparison has no case list: {path}")
    by_case_id = {row.get("case_id"): row for row in comparison["cases"] if row.get("case_id")}
    by_signal = {}
    for row in comparison["cases"]:
        by_signal.setdefault(row.get("signal"), row)
    return comparison, by_case_id, by_signal


def _write_csv(path, rows, fields):
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _manual_order_sheet(order_rows, ledger_preflight, *, explicit_strategy):
    """Build a human-readable, non-admitting execution sequence.

    The sheet is intentionally derived from the copied Ledger preflight when
    available.  It is a paper consumer only: current account state and broker
    quotes must still be supplied to the THS handoff before any manual review.
    """
    preflight_by_key = {
        (row.get("symbol"), row.get("side")): row
        for row in ledger_preflight.get("rows", [])
    }
    rows = []
    for row in order_rows:
        key = (row.get("symbol"), row.get("side"))
        preflight = preflight_by_key.get(key)
        status = preflight.get("preflight_status") if preflight else "not_preflighted"
        if not explicit_strategy:
            stage = "candidate_only"
            action = "review_selected_strategy"
            reason = "multi_strategy_pack_not_preflighted"
        elif row.get("side") == "sell" and status == "accepted":
            stage = "reduce_first"
            action = "review_sell_after_account_refresh"
            reason = "accepted_by_copied_ledger"
        elif row.get("side") == "buy" and status == "accepted":
            stage = "cash_available_if_revalidated"
            action = "review_buy_after_account_refresh"
            reason = "accepted_by_copied_ledger"
        elif row.get("side") == "buy":
            stage = "deferred_buy"
            action = "defer_until_fresh_cash_observation"
            reason = (preflight or {}).get("preflight_reason") or "fresh_cash_required"
        else:
            stage = "review_rejected"
            action = "do_not_prefill"
            reason = (preflight or {}).get("preflight_reason") or "copied_ledger_rejected"
        rows.append({
            **row,
            "stage": stage,
            "action": action,
            "preflight_status": status,
            "reason": reason,
            "requires_fresh_account": "true",
            "execution_admitted": "false",
        })
    stage_order = {
        "reduce_first": 0,
        "cash_available_if_revalidated": 1,
        "deferred_buy": 2,
        "review_rejected": 3,
        "candidate_only": 4,
    }
    rows.sort(key=lambda row: (
        stage_order.get(row["stage"], 99), row.get("side", ""), row.get("symbol", "")
    ))
    for index, row in enumerate(rows, start=1):
        row["sequence"] = index
    return rows


def _quote_request_rows(order_rows):
    """Build one fillable raw-RMB quote row per candidate symbol.

    Research close prices stay in the provenance columns; ``price_rmb`` is
    intentionally blank so the file can be filled with same-session broker
    quotes before it reaches ``build-quote-observation``.
    """
    indexed = {}
    for row in order_rows:
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").strip()
        strategy = str(row.get("strategy_id") or "").strip()
        if not symbol or side not in {"buy", "sell"} or not strategy:
            continue
        market = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
        item = indexed.setdefault(symbol, {
            "symbol": symbol.split(".", 1)[0],
            "market": market,
            "price_rmb": "",
            "strategy_ids": set(),
            "sides": set(),
            "candidate_qty_by_strategy": {},
            "research_reference_prices_rmb": {},
            "status": "awaiting_same_session_raw_rmb_quote",
            "price_basis_required": "raw_rmb",
        })
        item["strategy_ids"].add(strategy)
        item["sides"].add(side)
        item["candidate_qty_by_strategy"].setdefault(strategy, {})[side] = _int(row.get("qty"))
        item["research_reference_prices_rmb"][strategy] = row.get("price_rmb")
    rows = []
    for symbol in sorted(indexed):
        item = indexed[symbol]
        rows.append({
            **item,
            "strategy_ids": ",".join(sorted(item["strategy_ids"])),
            "sides": ",".join(sorted(item["sides"])),
            "candidate_qty_by_strategy": json.dumps(
                item["candidate_qty_by_strategy"], ensure_ascii=False, sort_keys=True
            ),
            "research_reference_prices_rmb": json.dumps(
                item["research_reference_prices_rmb"], ensure_ascii=False, sort_keys=True
            ),
        })
    return rows


def _strategy_review_board(strategy_rows):
    """Rank candidates for human review without selecting or promoting one."""
    def number(value, default=-float("inf")):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    comparable = [row for row in strategy_rows if row.get("comparison_status") == "comparable"]
    ranked = sorted(
        comparable,
        key=lambda row: (
            number(row.get("comparison_sharpe")),
            number(row.get("comparison_max_drawdown")),
            -int(row.get("ledger_preflight_deferred_buys") or 0),
        ),
        reverse=True,
    )
    board = []
    for priority, row in enumerate(ranked, start=1):
        deferred = int(row.get("ledger_preflight_deferred_buys") or 0)
        reason = "descriptive same-window ranking only; no automatic promotion"
        if deferred == 0:
            reason += "; no copied-ledger cash-deferred buys"
        board.append({
            "paper_review_priority": priority,
            "strategy_id": row["strategy_id"],
            "comparison_return": row.get("comparison_return"),
            "comparison_max_drawdown": row.get("comparison_max_drawdown"),
            "comparison_sharpe": row.get("comparison_sharpe"),
            "ledger_preflight_accepted": row.get("ledger_preflight_accepted"),
            "ledger_preflight_rejected": row.get("ledger_preflight_rejected"),
            "deferred_buys": deferred,
            "paper_review_reason": reason,
            "automatic_strategy_selection": "false",
        })
    for row in strategy_rows:
        if row.get("comparison_status") == "comparable":
            continue
        board.append({
            "paper_review_priority": "deferred",
            "strategy_id": row["strategy_id"],
            "comparison_return": row.get("comparison_return"),
            "comparison_max_drawdown": row.get("comparison_max_drawdown"),
            "comparison_sharpe": row.get("comparison_sharpe"),
            "ledger_preflight_accepted": row.get("ledger_preflight_accepted"),
            "ledger_preflight_rejected": row.get("ledger_preflight_rejected"),
            "deferred_buys": row.get("ledger_preflight_deferred_buys", 0),
            "paper_review_reason": "comparison evidence unavailable; no promotion",
            "automatic_strategy_selection": "false",
        })
    return board


def _preflight_config(item):
    """Reuse the source replay's declared cost and lot configuration."""
    return StrategyConfig(**item["execution_config"]).validate()


def _native_source_price_block(result, row):
    if result.get("source_task_family") != "native_preopen":
        return None
    quote = result.get("source_quote_status_by_symbol", {}).get(row["symbol"])
    if not isinstance(quote, dict) or quote.get("trading_date") != result["source_cutoff"]:
        return "native_exact_source_status_missing"
    status = quote.get("tradestatus")
    if type(status) is not int or status not in (0, 1):
        return "native_source_trade_status_unknown"
    if status == 0:
        return "native_source_suspended"
    st = quote.get("is_st")
    if type(st) is not int or st not in (0, 1):
        return "native_source_ST_unknown"
    if row["side"] == "buy" and st == 1:
        return "native_source_ST_buy_excluded"
    return None


def _ledger_preflight(item, execution_day):
    """Dry-run candidate orders through a copied shared Ledger state.

    This intentionally does not save the copied state or create fills. It makes
    the paper pack consume the same order rules used by replay and paper mode,
    while keeping research-close prices separate from live broker quotes.
    """
    result = item["result"]
    source_artifacts = Path(result.get("source_artifacts", ""))
    account_path = source_artifacts / "account.json"
    if not account_path.is_file():
        return {
            "status": "unavailable_source_account",
            "reason": "Selected preview has no saved Engine/Ledger account state",
            "rows": [],
            "submitted": 0,
            "rejected": 0,
            "saved_ledger_mutated": False,
        }
    state = _read_json(account_path)
    config = _preflight_config(item)
    context = RunContext(
        f"opening_pack_preflight:{result['strategy_id']}:{execution_day}",
        TradingMode.REPLAY,
    )
    book = Ledger(deepcopy(state), config, context)
    rows = []
    for row in item["orders"]:
        price_cents = _price_cents(row.get("price_rmb"))
        if price_cents is None:
            rows.append({
                **row,
                "preflight_status": "rejected",
                "preflight_reason": "missing_or_invalid_research_price",
                "price_cents": None,
            })
            continue
        source_block = _native_source_price_block(result, row)
        if source_block is not None:
            rows.append({**row, "preflight_status": "rejected", "preflight_reason": source_block,
                         "price_cents": price_cents, "reserved_cents": 0})
            continue
        order_id = ":".join(("opening", result["strategy_id"], execution_day,
                              row["symbol"], row["side"]))
        order = book.submit(order_id, execution_day, row["symbol"], row["side"],
                            row["qty"], price_cents)
        rows.append({
            **row,
            "preflight_status": "accepted" if order["status"] == "submitted" else "rejected",
            "preflight_reason": order.get("reason"),
            "price_cents": price_cents,
            "reserved_cents": order.get("reserve", 0),
        })
    accepted = sum(row["preflight_status"] == "accepted" for row in rows)
    reduce_first = [row for row in rows if row["side"] == "sell"
                    and row["preflight_status"] == "accepted"]
    deferred_buys = [row for row in rows if row["side"] == "buy"
                     and row["preflight_status"] != "accepted"]
    return {
        "status": "completed",
        "source_account": str(account_path),
        "source_account_is_replay_state": True,
        "ledger_context": context.to_dict(),
        "configuration": config.to_dict(),
        "rows": rows,
        "submitted": accepted,
        "rejected": len(rows) - accepted,
        "saved_ledger_mutated": False,
        "fills_created": 0,
        "quote_basis": "research_raw_close_rmb",
        "staged_execution": {
            "status": "paper_staged_only" if deferred_buys else "no_deferred_buys",
            "reduce_first_count": len(reduce_first),
            "reduce_first_order_ids": [
                ":".join(("opening", result["strategy_id"], execution_day,
                          row["symbol"], row["side"])) for row in reduce_first
            ],
            "deferred_buy_count": len(deferred_buys),
            "deferred_buy_symbols": [row["symbol"] for row in deferred_buys],
            "next_step": (
                "After any explicitly approved manual reductions are observed, "
                "capture a fresh same-session account/cash observation and "
                "recalculate deferred buys; do not fund them from this dry-run."
                if deferred_buys else
                "No cash-deferred buy was identified by this copied-ledger preflight."
            ),
        },
    }


def build(previews, *, execution_day=None, comparison=None, strategy_id=None):
    loaded = [_load_preview(value) for value in previews]
    if not loaded:
        raise ValueError("At least one paper preview is required")
    supplied_strategy_ids = [item["result"].get("strategy_id") for item in loaded]
    if strategy_id is not None:
        loaded = [item for item in loaded if item["result"].get("strategy_id") == strategy_id]
        if len(loaded) != 1:
            raise ValueError(
                f"Explicit strategy must match exactly one preview: {strategy_id}; "
                f"supplied={supplied_strategy_ids}"
            )
    results = [item["result"] for item in loaded]
    cutoffs = {item.get("source_cutoff") for item in results}
    days = {item.get("execution_day") for item in results}
    if len(cutoffs) != 1 or None in cutoffs:
        raise ValueError(f"Previews must share one source cutoff: {sorted(cutoffs)}")
    if len(days) != 1 or None in days:
        raise ValueError(f"Previews must share one execution day: {sorted(days)}")
    if execution_day is not None and execution_day not in days:
        raise ValueError(f"Requested execution day differs from previews: {execution_day}")
    comparison_value = None
    comparison_by_case = {}
    comparison_by_signal = {}
    if comparison:
        comparison_value, comparison_by_case, comparison_by_signal = _load_comparison(comparison)
        if comparison_value.get("end") != next(iter(cutoffs)):
            raise ValueError("Comparison end must equal preview source cutoff")

    order_rows = [row for item in loaded for row in item["orders"]]
    target_rows = [row for item in loaded for row in item["targets"]]
    if strategy_id is not None:
        ledger_preflight = _ledger_preflight(loaded[0], next(iter(days)))
        per_strategy_preflights = {loaded[0]["result"]["strategy_id"]: ledger_preflight}
    else:
        # Keep every candidate comparable without mutating the sole persistent ledger.
        # Each strategy gets its own copied replay state and dry-run order sequence.
        per_strategy_preflights = {
            item["result"]["strategy_id"]: _ledger_preflight(item, next(iter(days)))
            for item in loaded
        }
        ledger_preflight = {
            "status": "completed_per_strategy_dry_run",
            "rows": [],
            "submitted": 0,
            "rejected": 0,
            "saved_ledger_mutated": False,
        }
    support = {}
    for row in order_rows:
        key = (row["symbol"], row["side"])
        support.setdefault(key, set()).add(row["strategy_id"])
    support_rows = []
    for (symbol, side), strategies in sorted(support.items()):
        support_rows.append({
            "symbol": symbol,
            "side": side,
            "strategy_count": len(strategies),
            "strategies": ",".join(sorted(strategies)),
        })

    strategy_rows = []
    for item in loaded:
        result = item["result"]
        preflight = per_strategy_preflights[result["strategy_id"]]
        buys = sum(1 for row in item["orders"] if row["side"] == "buy")
        sells = sum(1 for row in item["orders"] if row["side"] == "sell")
        notional = sum(_decimal(row["estimated_notional_rmb"]) for row in item["orders"])
        source_run = Path(result.get("source_run", "")).parent.name
        case = (comparison_by_case.get(f"{source_run}:{result['strategy_id']}")
                or comparison_by_signal.get(result["strategy_id"], {}))
        metrics = case.get("metrics") or {}
        # Older comparison receipts expose ``status`` directly.  The saved
        # StockDB comparison uses ``source_status`` plus a top-level
        # comparable-case count.  Normalize only the fallback shape so the
        # review board can distinguish completed evidence from a blocked arm;
        # this does not select or promote a strategy.
        comparison_status = case.get("status")
        if comparison_status is None:
            if case.get("source_status") == "completed" and metrics:
                comparison_status = "comparable"
            elif case.get("source_status"):
                comparison_status = case.get("source_status")
        strategy_rows.append({
            "strategy_id": result["strategy_id"],
            "preview": item["result_path"],
            "source_cutoff": result["source_cutoff"],
            "execution_day": result["execution_day"],
            "preview_rank_day": result.get("provisional_rank_day"),
            "rank_current": result.get("rank_is_current_for_execution_day"),
            # A completed monthly decision may be carried to the next
            # session under the declared schedule. Preserve that distinction
            # instead of treating every non-current rank as stale.
            "rank_carry_forward_allowed": result.get("rank_carry_forward_allowed", False),
            "rank_schedule": result.get("rank_schedule"),
            "rank_source": result.get("rank_source"),
            "rank_override": result.get("rank_override"),
            "execution_scope": result.get("execution_scope", "strict"),
            "terminal_equity_rmb": result.get("terminal_equity_rmb"),
            "cash_rmb": result.get("cash_rmb"),
            "position_count": result.get("position_count"),
            "candidate_order_count": len(item["orders"]),
            "buys": buys,
            "sells": sells,
            "estimated_notional_rmb": f"{notional:.2f}",
            "missing_quotes": len(result.get("provisional_missing_quote_symbols", [])),
            "handoff_status": result.get("adapter_handoff_status"),
            "comparison_status": comparison_status,
            "comparison_return": metrics.get("net_return"),
            "comparison_max_drawdown": metrics.get("max_drawdown"),
            "comparison_sharpe": metrics.get("sharpe_zero_rf_252"),
            "comparison_fees_rmb": metrics.get("fees"),
            "comparison_stale_days": metrics.get("stale_valuation_days"),
            "ledger_preflight_status": preflight.get("status"),
            "ledger_preflight_accepted": preflight.get("submitted", 0),
            "ledger_preflight_rejected": preflight.get("rejected", 0),
            "ledger_preflight_reduce_first": (preflight.get("staged_execution") or {}).get("reduce_first_count", 0),
            "ledger_preflight_deferred_buys": (preflight.get("staged_execution") or {}).get("deferred_buy_count", 0),
            "execution_config": item["execution_config"],
        })
        if result.get("task_method") == "saved_fixed_account_next_session_v1":
            strategy_rows[-1].update(task_method=result["task_method"],
                trade_status=result["trade_status"], saved_rank_display_only=True)
    review_board = _strategy_review_board(strategy_rows)

    identifier = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output = ROOT / "outputs" / "opening_pack" / identifier
    data_output = DATA_ROOT / "research" / "opening_pack" / identifier
    output.mkdir(parents=True, exist_ok=False)
    data_output.mkdir(parents=True, exist_ok=False)

    strategy_fields = list(strategy_rows[0])
    order_fields = ["strategy_id", "symbol", "side", "qty", "price_rmb",
                    "estimated_notional_rmb", "source_status", "source_reason"]
    target_fields = ["strategy_id", "symbol", "target_qty", "active_target", "price_basis"]
    support_fields = ["symbol", "side", "strategy_count", "strategies"]
    for rows, fields, name in (
        (strategy_rows, strategy_fields, "strategy_summary.csv"),
        (review_board, list(review_board[0]), "strategy_review_board.csv"),
        (order_rows, order_fields, "candidate_orders.csv"),
        (target_rows, target_fields, "candidate_targets.csv"),
        (support_rows, support_fields, "order_consensus.csv"),
    ):
        _write_csv(data_output / name, rows, fields)
        (output / name).write_bytes((data_output / name).read_bytes())
    preflight_fields = [
        "strategy_id", "symbol", "side", "qty", "price_rmb", "price_cents",
        "estimated_notional_rmb", "preflight_status", "preflight_reason",
        "reserved_cents",
    ]
    # Keep a per-strategy preflight artifact even for an explicitly narrowed
    # pack.  The offline strategy-plan consumer uses the same stable contract
    # for single- and multi-candidate packs.
    all_preflight_rows = [
        row
        for item in loaded
        for row in per_strategy_preflights[item["result"]["strategy_id"]]["rows"]
    ]
    _write_csv(data_output / "ledger_preflight_by_strategy.csv", all_preflight_rows, preflight_fields)
    (output / "ledger_preflight_by_strategy.csv").write_bytes(
        (data_output / "ledger_preflight_by_strategy.csv").read_bytes()
    )
    if strategy_id is not None:
        _write_csv(data_output / "ledger_preflight.csv", ledger_preflight["rows"], preflight_fields)
        (output / "ledger_preflight.csv").write_bytes((data_output / "ledger_preflight.csv").read_bytes())

    manual_order_rows = _manual_order_sheet(
        order_rows, ledger_preflight, explicit_strategy=strategy_id is not None
    )
    manual_order_fields = [
        "sequence", "strategy_id", "symbol", "side", "qty", "price_rmb",
        "estimated_notional_rmb", "stage", "action", "preflight_status",
        "reason", "requires_fresh_account", "execution_admitted",
    ]
    _write_csv(data_output / "manual_order_sheet.csv", manual_order_rows, manual_order_fields)
    (output / "manual_order_sheet.csv").write_bytes((data_output / "manual_order_sheet.csv").read_bytes())

    quote_request_rows = _quote_request_rows(order_rows)
    quote_request_fields = [
        "symbol", "market", "price_rmb", "strategy_ids", "sides",
        "candidate_qty_by_strategy", "research_reference_prices_rmb",
        "status", "price_basis_required",
    ]
    _write_csv(data_output / "quote_request.csv", quote_request_rows, quote_request_fields)
    (output / "quote_request.csv").write_bytes((data_output / "quote_request.csv").read_bytes())

    selected_rank_current = None
    selected_rank_carry_forward = False
    if strategy_id is not None and strategy_rows:
        selected_rank_current = strategy_rows[0].get("rank_current")
        selected_rank_carry_forward = bool(
            strategy_rows[0].get("rank_carry_forward_allowed", False)
        )
    saved_fixed_only = all(item["result"].get("task_method") == "saved_fixed_account_next_session_v1"
                          for item in loaded)
    if saved_fixed_only:
        release_state = "blocked_research_saved_account_only"
        release_blockers = sorted({reason for item in loaded
                                   for reason in item["result"].get("release_blockers", [])})
    elif selected_rank_current is False and not selected_rank_carry_forward:
        release_state = "blocked_stale_rank"
        release_blockers = ["rank_not_current_for_execution_day"]
    else:
        release_state = "blocked_missing_live_inputs"
        release_blockers = [
            "current_user_account_observation_missing",
            "same_session_raw_rmb_quotes_missing",
            "sellable_quantity_and_t1_status_required",
            "manual_user_review_required",
        ]

    pack = {
        "schema": "a_share_opening_pack_v1",
        "status": "paper_candidates_only",
        "execution_admitted": False,
        "release_control": {
            "state": release_state,
            "execution_release_allowed": False,
            "rank_is_current_for_execution_day": selected_rank_current,
            "rank_carry_forward_allowed": selected_rank_carry_forward,
            "blockers": release_blockers,
            "paper_ledger_preflight_is_not_release": True,
        },
        "source_cutoff": next(iter(cutoffs)),
        "execution_day": next(iter(days)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy_count": len(strategy_rows),
        "candidate_order_count": len(order_rows),
        "candidate_target_count": len(target_rows),
        "order_union_count": len(support_rows),
        "order_consensus_count": sum(row["strategy_count"] >= 2 for row in support_rows),
        "comparison": str(Path(comparison).resolve()) if comparison else None,
        "automatic_strategy_selection": False,
        "selection_mode": "explicit_strategy_argument" if strategy_id else "multi_strategy_candidates",
        "selected_strategy_id": strategy_id,
        "execution_scopes": sorted({row.get("execution_scope", "strict") for row in strategy_rows}),
        "ledger_preflight": {
            key: value for key, value in ledger_preflight.items() if key != "rows"
        },
        "artifacts": {
            "strategy_summary": str(data_output / "strategy_summary.csv"),
            "strategy_review_board": str(data_output / "strategy_review_board.csv"),
            "candidate_orders": str(data_output / "candidate_orders.csv"),
            "candidate_targets": str(data_output / "candidate_targets.csv"),
            "order_consensus": str(data_output / "order_consensus.csv"),
            "ledger_preflight_by_strategy": str(data_output / "ledger_preflight_by_strategy.csv"),
            "manual_order_sheet": str(data_output / "manual_order_sheet.csv"),
            "quote_request": str(data_output / "quote_request.csv"),
        },
        "quote_request_count": len(quote_request_rows),
        "strategies": strategy_rows,
        "requires_before_ths_prefill": [
            "same-session user THS account observation",
            "same-session raw RMB quotes",
            "sellable quantities and T+1 status",
            "manual review of one selected strategy",
        ],
        "not_attempted": [
            "No account or quote observation was fabricated or imported by this pack.",
            "No THS client operation, prefill, submission, cancellation, or transfer was attempted.",
            "This pack does not choose a strategy automatically; an explicit strategy argument is caller-owned and remains paper-only.",
            "The ledger preflight uses a copied replay state; it does not save orders or create fills in the sole persistent ledger.",
        ],
    }
    if strategy_id is not None:
        pack["artifacts"]["ledger_preflight"] = str(data_output / "ledger_preflight.csv")
        pack["ledger_preflight_staging"] = ledger_preflight.get("staged_execution")
        pack["ledger_preflight_by_strategy"] = {
            strategy: {
                key: value for key, value in preflight.items() if key != "rows"
            }
            for strategy, preflight in per_strategy_preflights.items()
        }
    else:
        total_submitted = sum(
            value.get("submitted", 0) for value in per_strategy_preflights.values()
        )
        total_rejected = sum(
            value.get("rejected", 0) for value in per_strategy_preflights.values()
        )
        pack["artifacts"]["ledger_preflight_by_strategy"] = str(
            data_output / "ledger_preflight_by_strategy.csv"
        )
        pack["ledger_preflight_summary"] = {
            "status": "completed_per_strategy_dry_run",
            "strategy_count": len(per_strategy_preflights),
            "submitted": total_submitted,
            "rejected": total_rejected,
            "saved_ledger_mutated": False,
            "fills_created": 0,
        }
        # Keep the original top-level field useful to existing consumers;
        # detailed rows remain available under the per-strategy artifact.
        pack["ledger_preflight"] = dict(pack["ledger_preflight_summary"])
        pack["ledger_preflight_by_strategy"] = {
            strategy: {
                key: value for key, value in preflight.items() if key != "rows"
            }
            for strategy, preflight in per_strategy_preflights.items()
        }
    _write_json(data_output / "opening_pack.json", pack)
    (output / "opening_pack.json").write_bytes((data_output / "opening_pack.json").read_bytes())
    if strategy_id is None:
        report_preflight = pack["ledger_preflight_summary"]
        report_preflight_label = "per-strategy Engine/Ledger dry-run"
    else:
        report_preflight = ledger_preflight
        report_preflight_label = "Engine/Ledger dry-run"
    report = [
        "# A-share opening-session paper pack",
        "",
        f"- Source cutoff: `{pack['source_cutoff']}`; intended execution day: `{pack['execution_day']}`",
        f"- Strategies: {pack['strategy_count']}; candidate order lines: {pack['candidate_order_count']}; target rows: {pack['candidate_target_count']}",
        f"- Unique symbol/side order rows: {pack['order_union_count']}; supported by at least two strategies: {pack['order_consensus_count']}",
        f"- Same-session raw-RMB quote request: `{pack['quote_request_count']}` symbols; `price_rmb` is intentionally blank until current quotes are observed",
        f"- Status: `paper_candidates_only`; selection mode: `{pack['selection_mode']}`; selected strategy: `{pack['selected_strategy_id'] or 'none'}`",
        f"- Release control: `{pack['release_control']['state']}`; execution release allowed: `false`; blockers: `{', '.join(pack['release_control']['blockers'])}`",
        f"- Human review board: `{pack['artifacts']['strategy_review_board']}`; this ranks evidence for review and never selects a live strategy",
        f"- {report_preflight_label}: `{report_preflight['status']}`; accepted: {report_preflight['submitted']}; rejected: {report_preflight['rejected']}; saved orders: 0; fills: {report_preflight.get('fills_created', 0)}",
        "",
        "| Strategy | Rank day | Orders | Buy | Sell | Estimated notional | Ledger A/R | Reduce/defer | 2026 return | MDD | Sharpe | Handoff |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in strategy_rows:
        report.append(f"| `{row['strategy_id']}` | `{row['preview_rank_day']}` | {row['candidate_order_count']} | {row['buys']} | {row['sells']} | {row['estimated_notional_rmb']} CNY | {row['ledger_preflight_accepted']}/{row['ledger_preflight_rejected']} | {row['ledger_preflight_reduce_first']}/{row['ledger_preflight_deferred_buys']} | {row['comparison_return']} | {row['comparison_max_drawdown']} | {row['comparison_sharpe']} | `{row['handoff_status']}` |")
    report.extend([
        "",
        "## Human review priority",
        "",
        "| Priority | Strategy | Return | MDD | Sharpe | Deferred buys | Reason |",
        "|---:|---|---:|---:|---:|---:|---|",
    ])
    for row in review_board:
        report.append(
            f"| {row['paper_review_priority']} | `{row['strategy_id']}` | "
            f"{row['comparison_return']} | {row['comparison_max_drawdown']} | "
            f"{row['comparison_sharpe']} | {row['deferred_buys']} | "
            f"{row['paper_review_reason']} |"
        )
    if strategy_id is not None and ledger_preflight.get("staged_execution"):
        staged = ledger_preflight["staged_execution"]
        report.extend([
            "",
            "## Paper cash staging",
            "",
            f"- Reduce-first lines accepted by the copied Ledger: `{staged['reduce_first_count']}`",
            f"- Buys deferred for a fresh cash/account observation: `{staged['deferred_buy_count']}`",
            "- This is a planning aid only: it does not assume sale proceeds, save an order, create a fill, or operate THS.",
            f"- Next step: {staged['next_step']}",
        ])
    report.extend([
        "",
        "The pack keeps supplied strategies comparable on the same cutoff and intended session. Comparison metrics are descriptive only; no automatic winner is selected. When `--strategy` is supplied, the caller explicitly narrows the paper pack to that fixed strategy; this is not metric-based selection. It is a paper-research consumer, not a user-account target and not a THS order ticket.",
        "Before any manual THS prefill, hydrate one selected handoff through the existing account-observation and same-session raw-RMB quote commands. The physical client remains manual-only and disabled here.",
        "",
        f"Artifacts: `{data_output}`",
    ])
    (data_output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output, pack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="append", required=True,
                        help="Preview directory or result.json; repeat for each strategy")
    parser.add_argument("--execution-day")
    parser.add_argument("--comparison", help="Optional machine-readable same-window comparison.json")
    parser.add_argument("--strategy", help="Explicitly narrow to one fixed strategy for paper hydration")
    args = parser.parse_args(argv)
    output, pack = build(
        args.preview,
        execution_day=args.execution_day,
        comparison=args.comparison,
        strategy_id=args.strategy,
    )
    print(json.dumps({"output": str(output), **pack}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
