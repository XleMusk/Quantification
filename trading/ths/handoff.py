"""Hydrate a research preview with a current account observation for THS planning."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re

from ..config import StrategyConfig
from ..engine import choose_targets
from ..portfolio_allocation import OBSERVED_LOT_ALLOCATION
from .broker import THSAdapter
from .ledger_import import marks_of


_MAINBOARD = re.compile(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)")


def _fresh_stamp(value, current):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or not 0 <= (current - stamp).total_seconds() <= 300:
        raise ValueError("Current quote observation is missing, future or stale")


def _raw_price(value):
    text = str(value).strip()
    price = Decimal(text)
    if not price.is_finite() or price <= 0 or price != price.quantize(Decimal(".01")):
        raise ValueError("Current quote prices require positive RMB values with two decimals")
    return f"{price:.2f}"


def _quote_payload(symbol, quote, *, observed_at, trading_date):
    payload = {
        "symbol": symbol,
        "price_rmb": quote.get("price_rmb"),
        "price_basis": "raw_rmb",
        "observed_at": observed_at,
        "trading_date": trading_date,
    }
    for key in ("preclose_rmb", "is_st", "tradable"):
        if key in quote:
            payload[key] = quote[key]
    return payload


def _observed_account_weights(handoff, snapshot, quotes, current, config,
                              execution_scope):
    """Convert a frozen rank handoff into current-account equity weights.

    The research replay supplies ranks, not executable share quantities.  The
    existing engine is the authority for retention and target count; the
    existing weight consumer then sizes those targets from current marks,
    cash, lots and commissions.
    """
    rank_rows = handoff.get("rank_template")
    if not isinstance(rank_rows, list) or not rank_rows:
        raise ValueError("Observed-account sizing requires a frozen rank template")
    if execution_scope not in ("strict", "mainboard_only"):
        raise ValueError("Unsupported THS execution scope")
    candidates = []
    excluded = []
    seen = set()
    for row in rank_rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            raise ValueError("Rank template contains an invalid candidate")
        symbol = row["symbol"]
        if symbol in seen:
            raise ValueError(f"Duplicate rank-template symbol: {symbol}")
        seen.add(symbol)
        if execution_scope == "mainboard_only" and not _MAINBOARD.fullmatch(symbol):
            excluded.append(symbol)
            continue
        candidates.append({"symbol": symbol, "rank": row.get("rank"),
                           "score": row.get("score")})
    if len(candidates) < config.holdings:
        raise ValueError(
            f"Execution scope leaves {len(candidates)} candidates for {config.holdings} holdings"
        )
    held = [symbol for symbol, position in (snapshot.get("positions") or {}).items()
            if int(position.get("quantity", 0))]
    unsupported_held = [symbol for symbol in held if not _MAINBOARD.fullmatch(symbol)]
    if unsupported_held:
        raise ValueError(
            "Current THS weight plans cannot manage held non-mainboard symbols: "
            + ", ".join(sorted(unsupported_held))
        )
    selected = choose_targets(candidates, held, config)
    unsupported_selected = [symbol for symbol in selected if not _MAINBOARD.fullmatch(symbol)]
    if unsupported_selected:
        raise ValueError(
            "Selected symbols exceed current THS mainboard scope: "
            + ", ".join(sorted(unsupported_selected))
            + "; choose execution_scope=mainboard_only or a compatible strategy"
        )
    day = snapshot["trading_date"]
    rows = quotes["prices"]
    marks = marks_of(snapshot)
    entries = []
    for symbol in sorted(set(selected) | set(held)):
        weight = float(selected.get(symbol, 0))
        item = {"symbol": symbol, "target_weight": weight, "price_basis": "raw_rmb"}
        if symbol in held:
            item["limit_price_rmb"] = f"{marks[symbol] / 100:.2f}"
        elif weight > 0:
            quote = rows.get(symbol)
            if not isinstance(quote, dict):
                raise ValueError(f"Missing current raw RMB quote for {symbol}")
            item["limit_price_rmb"] = str(quote.get("price_rmb"))
            item["quote"] = _quote_payload(
                symbol, quote, observed_at=quotes["observed_at"], trading_date=day
            )
        entries.append(item)
    return {
        "mode": "current_preview",
        "strategy_id": handoff["strategy_id"],
        "account_alias": snapshot["account_alias"],
        "generated_at": current.isoformat(),
        "weight_unit": "equity_fraction",
        "lot_allocation": OBSERVED_LOT_ALLOCATION,
        "targets": entries,
    }, selected, excluded


def prepare_handoff(handoff, snapshot, quotes, *, current=None, execution_config=None,
                    sizing_mode="replay_shares", execution_scope="strict", saved_intent=None):
    """Turn a paper handoff into a current THSAdapter preview, without saving/submitting."""
    current = current or datetime.now(timezone.utc)
    if handoff.get("schema") != "ths_manual_prefill_handoff_v1":
        raise ValueError("Unsupported THS handoff schema")
    if handoff.get("execution_admitted") is not False or handoff.get("submission") != "manual_only":
        raise ValueError("Only an explicitly non-admitted manual handoff can be hydrated")
    if not isinstance(snapshot, dict) or not snapshot.get("account_alias"):
        raise ValueError("A current user account observation is required")
    if handoff.get("task_method") == "saved_fixed_account_next_session_v1":
        if sizing_mode != "saved_intent":
            raise ValueError("Saved fixed-account tasks require saved_intent sizing; research shares are not account targets")
        from .saved_intent import prepare_saved_intent
        payload = deepcopy(handoff)
        if saved_intent is not None:
            if "saved_account_intent" in payload and payload["saved_account_intent"] != saved_intent:
                raise ValueError("Different saved intent attachments cannot replace one another")
            payload["saved_account_intent"] = saved_intent
        return prepare_saved_intent(payload, snapshot, quotes, current=current,
            config=StrategyConfig(**execution_config).validate() if execution_config else None,
            execution_scope=execution_scope)
    if saved_intent is not None or sizing_mode == "saved_intent":
        raise ValueError("Saved intent sizing requires its explicit fixed-account handoff")
    if not isinstance(quotes, dict) or quotes.get("schema") != "ths_raw_quote_observation_v1":
        raise ValueError("Unsupported current quote observation schema")
    if not quotes.get("observed_at"):
        raise ValueError("A current raw-RMB quote observation is required")
    if not str(quotes.get("source") or "").strip():
        raise ValueError("Current quote observation must identify its source")
    _fresh_stamp(quotes["observed_at"], current)
    if quotes.get("price_basis") != "raw_rmb":
        raise ValueError("Only raw RMB quotes may enter a THS plan")
    if quotes.get("trading_date") != snapshot.get("trading_date"):
        raise ValueError("Account and quote observations must share a trading date")
    rows = quotes.get("prices")
    if not isinstance(rows, dict):
        raise ValueError("Quote observation must contain a symbol-to-price mapping")

    if sizing_mode not in ("replay_shares", "observed_account"):
        raise ValueError("Unsupported handoff sizing mode")
    config = StrategyConfig(**(execution_config or {})).validate()
    if sizing_mode == "observed_account":
        weights, selected, excluded = _observed_account_weights(
            handoff, snapshot, quotes, current, config, execution_scope
        )
        result = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_weights(
            snapshot, weights, current=current, config=config
        )
        result["sizing_mode"] = sizing_mode
        result["selected_symbols"] = sorted(selected)
        result["execution_scope"] = execution_scope
        result["scope_excluded_symbols"] = sorted(excluded)
        result["execution_admitted"] = False
        result["submission"] = "manual_only"
        result["client_interacted"] = False
        result["source_handoff"] = deepcopy(handoff)
        result["quote_observed_at"] = quotes["observed_at"]
        result["quote_basis"] = "raw_rmb"
        return result

    targets = handoff.get("target_template")
    if not isinstance(targets, list) or not targets:
        raise ValueError("Paper handoff has no target template")
    positions = snapshot.get("positions") or {}
    hydrated_targets = []
    for template in targets:
        symbol = template.get("symbol")
        target_qty = int(template["target_qty"])
        position = positions.get(symbol) or {}
        current_qty = int(position.get("quantity", 0))
        if target_qty == current_qty:
            # No order will be generated for an unchanged holding, so it does
            # not need a second market quote just to hydrate the full target.
            hydrated_targets.append({
                "symbol": symbol,
                "target_qty": target_qty,
            })
            continue
        quote = rows.get(symbol)
        if not isinstance(quote, dict) or "price_rmb" not in quote:
            raise ValueError(f"Missing current raw RMB quote for {symbol}")
        hydrated_targets.append({
            "symbol": symbol,
            "target_qty": target_qty,
            "limit_price_rmb": _raw_price(quote["price_rmb"]),
            "price_basis": "raw_rmb",
            "quote": _quote_payload(
                symbol, quote, observed_at=quotes["observed_at"], trading_date=quotes["trading_date"]
            ),
        })
    portfolio = {
        "mode": "current_preview",
        "strategy_id": handoff["strategy_id"],
        "account_alias": snapshot["account_alias"],
        "generated_at": current.isoformat(),
        "targets": hydrated_targets,
    }
    result = THSAdapter({"account_alias": snapshot["account_alias"]}).prepare_portfolio(
        snapshot, portfolio, current=current, config=config
    )
    result["execution_admitted"] = False
    result["submission"] = "manual_only"
    result["client_interacted"] = False
    result["source_handoff"] = deepcopy(handoff)
    result["quote_observed_at"] = quotes["observed_at"]
    result["quote_basis"] = "raw_rmb"
    result["sizing_mode"] = sizing_mode
    return result


def prepare_opening_pack(pack, snapshot, quotes, *, strategy_id=None, current=None,
                         sizing_mode="replay_shares", execution_scope="strict", saved_intent=None):
    """Hydrate all candidates or one explicitly selected candidate."""
    if pack.get("schema") != "a_share_opening_pack_v1":
        raise ValueError("Unsupported opening pack schema")
    if pack.get("execution_admitted") is not False:
        raise ValueError("Opening pack must be explicitly non-admitted")
    if saved_intent is not None and strategy_id is None:
        raise ValueError("Select one explicit strategy for a saved intent attachment")
    strategies = pack.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("Opening pack has no strategy candidates")
    if strategy_id is not None:
        selected = [row for row in strategies if row.get("strategy_id") == strategy_id]
        if len(selected) != 1:
            raise ValueError(f"Opening pack strategy is not unique: {strategy_id}")
        strategies = selected
    requested_strategy_id = strategy_id
    current = current or datetime.now(timezone.utc)
    candidates = []
    for candidate in strategies:
        candidate_strategy_id = candidate.get("strategy_id")
        preview_path = candidate.get("preview")
        try:
            preview = json.loads(Path(preview_path).read_text(encoding="utf-8-sig"))
            handoff_path = (preview.get("artifacts") or {}).get("ths_manual_handoff")
            if not handoff_path:
                raise ValueError("Preview has no THS handoff artifact")
            handoff = json.loads(Path(handoff_path).read_text(encoding="utf-8-sig"))
            if sizing_mode == "saved_intent" and (
                    candidate_strategy_id != handoff.get("strategy_id")
                    or pack.get("source_cutoff") != handoff.get("source_cutoff")
                    or pack.get("execution_day") != handoff.get("execution_day")):
                raise ValueError("Opening pack strategy/source/execution day differs from its saved handoff")
            hydrated = prepare_handoff(
                handoff,
                snapshot,
                quotes,
                current=current,
                execution_config=candidate.get("execution_config"),
                sizing_mode=sizing_mode,
                execution_scope=execution_scope,
                **({"saved_intent": saved_intent} if saved_intent is not None else {}),
            )
            diagnostic = hydrated.get("research_only") is True and hydrated.get("prefill_allowed") is False
            candidates.append({
                "strategy_id": candidate_strategy_id,
                "status": "research_diagnostic_only" if diagnostic else "current_preview_only",
                "execution_admitted": False,
                "submission": "manual_only",
                "sizing_mode": sizing_mode,
                "execution_scope": execution_scope,
                "plan": hydrated,
            })
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            candidates.append({
                "strategy_id": candidate_strategy_id,
                "status": "not_ready",
                "execution_admitted": False,
                "submission": "manual_only",
                "error": str(error),
            })
    ready = sum(row["status"] == "current_preview_only" for row in candidates)
    diagnostics = sum(row["status"] == "research_diagnostic_only" for row in candidates)
    return {
        "schema": "ths_opening_pack_hydration_v1",
        "status": "current_preview_only" if ready else "research_diagnostic_only" if diagnostics else "not_ready",
        "execution_admitted": False,
        "submission": "manual_only",
        "client_interacted": False,
        "pack_source_cutoff": pack.get("source_cutoff"),
        "execution_day": pack.get("execution_day"),
        "account_alias": snapshot.get("account_alias"),
        "trading_date": snapshot.get("trading_date"),
        "quote_observed_at": quotes.get("observed_at") if isinstance(quotes, dict) else None,
        "candidate_count": len(candidates),
        "requested_strategy_id": requested_strategy_id,
        "sizing_mode": sizing_mode,
        "execution_scope": execution_scope,
        "ready_count": ready,
        **({"research_diagnostic_count": diagnostics, "prefill_allowed": False} if diagnostics else {}),
        "candidates": candidates,
        "not_attempted": [
            "No journal plan was saved by the batch command.",
            "No THS client operation or broker submission was attempted.",
        ],
    }
