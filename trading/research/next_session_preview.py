"""Build an honest next-session paper rebalance preview from one fixed replay.

This is a consumer of an existing Engine/Ledger replay, not another backtester.
It carries the terminal paper account forward when the fixed strategy has no new
decision in the source run.  It never creates a current THS bundle or an order.
"""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

import numpy as np
import pyarrow.parquet as pq

from ..config import StrategyConfig
from ..settings import DATA_ROOT, ROOT

EXECUTION_RANK_LIMIT = 100


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _read_rankings(path):
    """Read saved rankings from either the CSV or parquet replay contract."""
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        rows = pq.read_table(path).to_pylist()
        for row in rows:
            if row.get("date") is not None:
                row["date"] = str(row["date"])[:10]
            if row.get("symbol") is not None:
                row["symbol"] = _symbol(row["symbol"])
        return rows
    return _read_csv(path)


def _execution_config(source):
    """Recover the source replay's portfolio rules for paper sizing."""
    raw = (source.get("portfolio") or source.get("config") or
           (source.get("protocol") or {}).get("portfolio") or {})
    fields = StrategyConfig.__dataclass_fields__
    values = {name: raw[name] for name in fields if name in raw}
    return StrategyConfig(**values).validate()


def _symbol(value):
    text = str(value or "").strip()
    match = re.search(r"(\d{6})\.(SSE|SZSE)$", text.upper())
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"(?:SH|SZ)\.?([0-9]{6})$", text.upper())
    if match:
        suffix = "SSE" if text.lower().startswith("sh") else "SZSE"
        return f"{match.group(1)}.{suffix}"
    return ""


def _paper_lots(value):
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _quantity(value):
    return sum(int(item["qty"]) for item in _paper_lots(value) if item.get("qty") not in (None, ""))


def _cents(value):
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _quote_rows(price_view, cutoff, wanted):
    price_view = Path(price_view)
    manifest_path = price_view / "manifest.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
        snapshot_date = int(str(cutoff).replace("-", ""))
        rows = []
        for item in manifest.get("chunks", []):
            chunk_path = price_view / item["parquet_file"]
            table = pq.read_table(
                chunk_path,
                columns=["date", "code", "close", "volume", "amount", "is_st"],
                filters=[("date", "=", snapshot_date)],
            ).to_pydict()
            for index, date in enumerate(table["date"]):
                code = str(table["code"][index] or "").zfill(6)
                if code.startswith(("6", "68")):
                    symbol = f"{code}.SSE"
                elif code.startswith(("0", "1", "2", "3")):
                    symbol = f"{code}.SZSE"
                else:
                    symbol = ""
                if symbol not in wanted:
                    continue
                close = table["close"][index]
                volume = table["volume"][index]
                rows.append({
                    "symbol": symbol,
                    "trading_date": (str(date)[:4] + "-" + str(date)[4:6] + "-" + str(date)[6:8]),
                    "close_rmb": close,
                    "close_cents": _cents(close),
                    "tradestatus": int(volume is not None and float(volume) > 0),
                    "is_st": bool(table["is_st"][index]),
                    "amount_rmb": table["amount"][index],
                    "price_basis": "stockdb_raw_close_rmb",
                })
        deduped = {}
        for row in rows:
            deduped.setdefault(row["symbol"], row)
        return deduped

    table = pq.read_table(
        price_view / "canonical_daily.parquet",
        columns=["date", "code", "security_id", "tradable_code", "close", "tradestatus", "isST", "amount"],
        filters=[("date", "=", cutoff)],
    ).to_pydict()
    rows = []
    for index, date in enumerate(table["date"]):
        candidates = (_symbol(table["security_id"][index]), _symbol(table["tradable_code"][index]),
                      _symbol(table["code"][index]))
        symbol = next((candidate for candidate in candidates if candidate), "")
        if symbol in wanted:
            rows.append({
                "symbol": symbol,
                "trading_date": date,
                "close_rmb": table["close"][index],
                "close_cents": _cents(table["close"][index]),
                "tradestatus": table["tradestatus"][index],
                "is_st": table["isST"][index],
                "amount_rmb": table["amount"][index],
                "price_basis": "research_raw_close_rmb",
            })
    deduped = {}
    for row in rows:
        deduped.setdefault(row["symbol"], row)
    return deduped


def _stockdb_supplement_quotes(source, protocol, price_view, cutoff, wanted, quotes):
    """Fill missing exact-day marks from the replay's bound append sidecar.

    Check only the requested day's original small source and selected sidecar
    rows, rather than rereading or hashing the complete historical supplement.
    Existing StockDB keys keep their original priority, including unknown marks.
    """
    binding = source.get("supplemental_raw_view") or {}
    missing = set(wanted) - set(quotes)
    if not binding.get("path") or not missing:
        return quotes
    if (binding != (protocol.get("supplemental_raw_view") or {})
            or Path(protocol["snapshot"]).resolve() != Path(price_view).resolve()
            or _read_json(Path(price_view) / "manifest.json") != protocol["snapshot_manifest"]):
        raise ValueError("Rolling preview supplemental or original snapshot binding changed")
    raw_path = Path(binding["path"]).resolve()
    receipt_path = raw_path.parent / "result.json"
    receipt = _read_json(receipt_path)
    if (receipt.get("schema") != "lowvol_append_only_complete_source_supplement_v1"
            or receipt.get("status") != "completed_append_only_research_supplement"
            or Path(receipt["raw_daily_file"]).resolve() != raw_path
            or receipt.get("original_full_row_prefix_preserved") is not True
            or any(receipt.get(key) is not False for key in
                   ("execution_admitted", "selection_enabled", "strategy_validated", "source_verified",
                    "pit_verified", "corporate_actions_verified", "stockdb_modified", "ledger_mutated"))):
        raise ValueError("Rolling preview requires its exact unadmitted append-only supplement")
    recorded = datetime.fromisoformat(receipt["created_at"])
    completed = datetime.fromisoformat(source["completed_at"])
    if (recorded.utcoffset() is None or completed.utcoffset() is None
            or not recorded <= completed <= datetime.now().astimezone()):
        raise ValueError("Supplement must be available before the completed replay")
    proofs = [row for row in receipt["source_proofs"] if row["day"] == cutoff]
    if not proofs:
        # No prior or next available source may substitute for this cutoff.
        return quotes
    if len(proofs) != 1:
        raise ValueError("One exact source proof per preview date is required")
    proof = proofs[0]
    if (proof.get("read_only_verified") is not True or proof.get("source_inventory_complete") is not True
            or proof.get("full_dated_roster_inventory_complete") is not True
            or proof.get("receipt_status") != "complete_full_dated_close_inventory_not_admitted"):
        raise ValueError("Supplement quote source is not a complete exact dated inventory")
    source_raw = Path(proof["source_folder"]) / "raw_daily.csv"
    if sha256(source_raw.read_bytes()).hexdigest() != proof["raw_daily_sha256"]:
        raise ValueError("Pinned same-day original quote source changed")
    with source_raw.open(encoding="utf-8-sig", newline="") as stream:
        original = {}
        for row in csv.DictReader(stream):
            symbol = _symbol(row["code"])
            if symbol not in missing:
                continue
            if row["date"] != cutoff or symbol in original:
                raise ValueError("Original supplemental quote date or identity changed")
            original[symbol] = row
    columns = ["date", "code", "open", "high", "low", "close", "preclose",
               "volume", "amount", "adjustflag", "tradestatus", "pctChg", "isST"]
    # Exact ISO date, never nearest/as-of/next-row matching.
    rows = pq.read_table(raw_path, columns=columns, filters=[("date", "=", cutoff)]).to_pylist()
    result, seen = dict(quotes), set()
    for row in rows:
        symbol = _symbol(row["code"])
        if symbol not in missing:
            continue
        if (symbol in seen or row["date"] != cutoff or symbol not in original
                or any(row[field] != original[symbol][field] for field in columns)):
            raise ValueError("Supplement quote differs from its exact original date/symbol/raw values")
        seen.add(symbol)
        if row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"} or row["isST"] not in {"0", "1"}:
            raise ValueError("Supplement quote raw basis or status is unsupported")
        try:
            close = float(row["close"])
            amount = float(row["amount"]) if row["amount"] else None
            volume = float(row["volume"]) if row["volume"] else None
        except (TypeError, ValueError):
            continue
        if (not np.isfinite(close) or close <= 0 or
                (row["tradestatus"] == "1" and
                 (amount is None or volume is None or not np.isfinite([amount, volume]).all()
                  or amount <= 0 or volume <= 0))):
            continue
        result[symbol] = {"symbol": symbol, "trading_date": cutoff,
            "close_rmb": close, "close_cents": _cents(close), "tradestatus": int(row["tradestatus"]),
            "is_st": row["isST"] == "1", "amount_rmb": amount,
            "price_basis": "research_raw_close_rmb", "supplement_source_file": str(raw_path),
            "same_day_original_file": str(source_raw.resolve()),
            "same_day_original_sha256": proof["raw_daily_sha256"],
            "source_verified": False, "pit_verified": False, "corporate_actions_verified": False,
            "executable_quote_verified": False}
    return result


def _terminal_month_end_rank(source, signal, cutoff):
    """Compute the next-open research rank without changing the replay calendar."""
    if not signal.startswith("stock_"):
        raise ValueError("The preview currently supports a stock family signal")
    recipe = signal.removeprefix("stock_")
    import pandas as pd

    from .bulk_feed import BulkResearchFeed
    from .family_portfolios import common_pool, ranked_recipe, read_native_monthends, training_directions
    from .low_risk import LowRiskFeed, features as low_risk_features, portfolio, VARIANTS

    library = _read_json(source["library_run"])
    summary = pd.read_csv(Path(library["artifacts"]) / "decay_summary.csv")
    directions = training_directions(summary)
    inputs = source["protocol"]["stock_inputs"]
    stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"], inputs["reference_view"],
                               inputs["listing_master"])
    try:
        values = low_risk_features(stocks, cutoff, feature_days=[cutoff])
        native = read_native_monthends(stocks, inputs["native_view"], [cutoff])
        helper = LowRiskFeed(stocks, values, VARIANTS[2], cutoff, diagnostic_extension=True)
        pool = helper.pool(cutoff, portfolio())
        if not isinstance(pool, list):
            raise ValueError(f"Terminal cutoff stock pool unavailable: {pool.reason}")
        shared = common_pool(pool, native.loc[pd.Timestamp(cutoff)])
        ranked = ranked_recipe(shared, recipe, directions)
        return [{"date": cutoff, "rank": index,
                 **{key: (float(value) if hasattr(value, "item") else value) for key, value in row.items()}}
                for index, row in enumerate(ranked[:EXECUTION_RANK_LIMIT], start=1)]
    finally:
        stocks.close()


def _latest_low_risk_rank(source, signal, cutoff):
    """Recompute the frozen low-risk rank at the replay's terminal cutoff."""
    from .bulk_feed import BulkResearchFeed
    from .low_risk import (LowRiskFeed, VARIANTS, features as low_risk_features,
                           portfolio, rank as low_risk_rank)

    if signal not in VARIANTS:
        raise ValueError(f"Unsupported low-risk signal: {signal}")
    inputs = source["inputs"]
    stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"],
                              inputs["reference_view"], inputs["listing_master"])
    try:
        values = low_risk_features(stocks, cutoff, feature_days=[cutoff])
        helper = LowRiskFeed(stocks, values, signal, cutoff, diagnostic_extension=True)
        pool = helper.pool(cutoff, portfolio())
        if not isinstance(pool, list):
            raise ValueError(f"Low-risk terminal pool unavailable: {pool.reason}")
        ranked = low_risk_rank(pool, signal)
        return [{"date": cutoff, "rank": index, **row}
                for index, row in enumerate(ranked[:EXECUTION_RANK_LIMIT], start=1)]
    finally:
        stocks.close()


def _latest_continuous_momentum_rank(source, cutoff):
    """Recompute the fixed continuous-information rank at the terminal cutoff."""
    from .bulk_feed import BulkResearchFeed
    from .continuous_momentum import (continuous_rank,
                                      information_discreteness)
    from .low_risk import LowRiskFeed, features, portfolio

    inputs = source["inputs"]
    stocks = BulkResearchFeed(inputs["price_view"], inputs["event_view"],
                              inputs["reference_view"], inputs["listing_master"])
    try:
        values = features(stocks, cutoff, feature_days=[cutoff])
        closes = stocks.closes.loc[:cutoff].where(stocks.volumes.loc[:cutoff].gt(0))
        closes = closes.where(np.isfinite(closes) & closes.gt(0))
        information = information_discreteness(closes).reindex(values["momentum"].index)
        pool = LowRiskFeed(stocks, values, "momentum_12_1_20", cutoff,
                           diagnostic_extension=True).pool(cutoff, portfolio())
        if not isinstance(pool, list):
            raise ValueError(f"Continuous-momentum terminal pool unavailable: {pool.reason}")
        annotated = [{**row, "information_discreteness": float(information.at[cutoff, row["symbol"]])}
                     for row in pool]
        if any(not np.isfinite(row["information_discreteness"]) for row in annotated):
            raise ValueError("Continuous-momentum terminal information discreteness is incomplete")
        ranked = continuous_rank(annotated)
        return [{"date": cutoff, "rank": index, **row}
                for index, row in enumerate(ranked[:EXECUTION_RANK_LIMIT], start=1)]
    finally:
        stocks.close()


def _factor_family_rank(source, cutoff, rankings_root=None):
    """Use the saved candidate scores when the source is a factor-family replay."""
    ranking_root = Path(rankings_root or source.get("source_artifacts", source["artifacts"]))
    ranking_path = ranking_root / "rankings.csv"
    if not ranking_path.exists():
        ranking_path = ranking_root / "predictions" / "rankings.csv"
    rows = _read_rankings(ranking_path)
    available = sorted({row.get("date", "") for row in rows if row.get("date", "") <= cutoff})
    if not available:
        raise ValueError("Factor-family replay has no ranking at or before the preview cutoff")
    day = available[-1]
    selected = [row for row in rows if row.get("date") == day]
    selected.sort(key=lambda row: (int(row.get("rank", 10**9)), row.get("symbol", "")))
    # Current-score bundles keep a compact top-30 CSV for reporting but also
    # retain the full frozen score snapshot.  Use that same snapshot only to
    # extend the execution candidate pool; the replay target remains the top
    # ``holdings`` rows below.
    if len(selected) < EXECUTION_RANK_LIMIT:
        score_path = ranking_root.parent / "current_scores.parquet"
        if score_path.exists():
            score_rows = pq.read_table(score_path, columns=["date", "symbol", "score"]).to_pylist()
            full = []
            for row in score_rows:
                row_day = str(row.get("date"))[:10]
                score = row.get("score")
                symbol = _symbol(row.get("symbol"))
                if row_day == day and symbol and score is not None and np.isfinite(score):
                    full.append({"date": day, "symbol": symbol, "score": float(score)})
            full.sort(key=lambda row: (-row["score"], row["symbol"]))
            selected = full
    return [{"date": day, "rank": index, "symbol": row["symbol"],
             "score": float(row["score"])}
            for index, row in enumerate(selected[:EXECUTION_RANK_LIMIT], start=1)]


def _alpha158_rank(source, signal, cutoff):
    """Convert a saved Alpha158 score slice into the paper-preview rank contract."""
    score_path = (Path(source["artifacts"]).resolve() / "predictions" / signal /
                  "historical_scores.parquet")
    if not score_path.exists():
        raise FileNotFoundError(f"Alpha158 score bundle is missing: {score_path}")
    table = pq.read_table(score_path, columns=["datetime", "instrument", "score"])
    rows = []
    for day, instrument, score in zip(table["datetime"].to_pylist(),
                                      table["instrument"].to_pylist(),
                                      table["score"].to_pylist()):
        if str(day)[:10] != cutoff or score is None or not np.isfinite(score):
            continue
        symbol = _symbol(instrument)
        if symbol:
            rows.append({"date": cutoff, "symbol": symbol, "score": float(score)})
    if len(rows) < 5:
        raise ValueError(f"Alpha158 score bundle has only {len(rows)} finite rows at {cutoff}")
    rows.sort(key=lambda row: (-row["score"], row["symbol"]))
    return [{**row, "rank": index} for index, row in enumerate(rows[:EXECUTION_RANK_LIMIT], start=1)]


def _saved_rank(rankings_path, cutoff):
    """Return the latest saved rank tail for replay families with native ranks."""
    rows = _read_rankings(rankings_path)
    available = sorted({row.get("date", "") for row in rows if row.get("date", "") <= cutoff})
    if not available:
        raise ValueError("Saved replay has no ranking at or before the preview cutoff")
    day = available[-1]
    selected = [row for row in rows if row.get("date") == day]
    selected.sort(key=lambda row: (int(row.get("rank", 10**9)), row.get("symbol", "")))
    return [{"date": day, "rank": int(row["rank"]), "symbol": row["symbol"],
             "score": float(row["score"]),
             **{key: row[key] for key in ("entry_channel", "exit_channel", "atr", "median_amount")
                if key in row and row[key] is not None}}
            for row in selected[:EXECUTION_RANK_LIMIT]]


def _stockdb_rolling_rank(source, signal, cutoff):
    """Use the last fixed monthly StockDB rank at or before the cutoff."""
    rankings_path = Path(source["artifacts"]).resolve() / f"{signal}_ranks.csv"
    rows = _read_rankings(rankings_path)
    available = sorted({str(row.get("date", ""))[:10] for row in rows
                        if str(row.get("date", ""))[:10] <= cutoff})
    if not available:
        raise ValueError("StockDB rolling replay has no rank at or before the preview cutoff")
    day = available[-1]
    selected = [row for row in rows if str(row.get("date", ""))[:10] == day]
    selected.sort(key=lambda row: (int(row.get("rank", 10**9)), row.get("symbol", "")))
    ranked = []
    for index, row in enumerate(selected[:EXECUTION_RANK_LIMIT], start=1):
        volatility = float(row["volatility"])
        momentum = float(row["momentum"])
        score = -volatility if signal == "low_volatility_252_20" else momentum
        ranked.append({"date": day, "rank": index, "symbol": row["symbol"],
                       "score": score, "volatility": volatility, "momentum": momentum,
                       "median_amount": float(row["median_amount"])})
    return ranked


def _fixed_daily_binding(source, protocol, account, artifacts, config, cutoff, execution_day):
    """Validate the opt-in saved-account method without advancing its Engine."""
    from ..contracts import RunContext, TradingMode
    from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE, validate_binding
    from .stockdb_rolling_low_volatility import CALENDAR_MONTH_END_METHOD

    if protocol.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE:
        if source.get("full_exit_lifecycle") is not None or account.get("full_exit_lifecycle") is not None:
            raise ValueError("Saved fixed daily protocol/account method mismatch")
        return None
    schedule = protocol.get("month_end_schedule") or {}
    if schedule.get("method_id") != CALENDAR_MONTH_END_METHOD:
        raise ValueError("Saved full-exit preview requires fixed-calendar month ends")
    artifact_protocol = _read_json(Path(source["artifacts"]) / "protocol.json")
    receipt = _read_json(artifacts / "replay.json")
    if (source.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or artifact_protocol.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or source.get("month_end_schedule") != schedule
            or artifact_protocol.get("month_end_schedule") != schedule
            or receipt.get("full_exit_lifecycle") != FULL_EXIT_LIFECYCLE
            or receipt.get("status") != "completed"
            or receipt.get("config") != config.to_dict()
            or receipt.get("last_completed_session") != cutoff or account.get("day") != cutoff
            or receipt.get("account_basis") != "last_successfully_completed_session"
            or account.get("broker_binding")):
        raise ValueError("Saved fixed daily source/account/replay method mismatch")
    calendar = source.get("calendar_verification", {}).get("fixed_open_sessions", [])
    if cutoff not in calendar or next((day for day in calendar if day > cutoff), None) != execution_day:
        raise ValueError("Fixed daily preview requires the exact fixed-calendar next session")
    lifecycle = validate_binding(account, FULL_EXIT_LIFECYCLE,
        RunContext("readonly_fixed_daily_preview", TradingMode.REPLAY))
    if account.get("full_exit_lifecycle") is None:
        raise ValueError("Saved fixed daily account is missing its full-exit lifecycle")
    return lifecycle


def _fixed_daily_rows(account, lifecycle, positions, quotes, equity, config, cutoff, execution_day,
                      excluded, *, explicit_exit_symbols=None):
    """Project only saved pending weights or exact held shares at research closes."""
    from ..engine import shares_for_target
    from ..rules import execution_rule

    pending = account.get("pending_targets")
    if pending is not None and (not isinstance(pending, dict) or any(
            _symbol(symbol) != symbol or not isinstance(weight, (int, float))
            or not np.isfinite(weight) or weight < 0 or weight > 1
            for symbol, weight in pending.items())):
        raise ValueError("Invalid saved pending full target")
    effective = ({symbol: min(weight, config.single_limit) for symbol, weight in pending.items()}
                 if pending is not None else {})
    if sum(effective.values()) > config.gross_limit + 1e-12:
        raise ValueError("Saved pending target exceeds the fixed gross exposure limit")
    if type(equity) is not int or equity <= 0:
        raise ValueError("Positive terminal equity is required for fixed daily preview")
    residual = set(explicit_exit_symbols if explicit_exit_symbols is not None
                   else lifecycle["remaining_full_exits"]) & set(positions)
    rows, orders, missing = [], [], []
    for symbol in sorted(set(positions) | set(pending or {}) | residual):
        current = positions.get(symbol, 0)
        quote = quotes.get(symbol) or {}
        price = quote.get("close_cents")
        known_price = (quote.get("trading_date") == cutoff and type(price) is int and price > 0)
        if not known_price:
            missing.append(symbol)
            price = None
        weight = (pending.get(symbol, 0) if pending is not None else None)
        sizing_rule = (execution_rule(symbol, execution_day)
            if weight and symbol not in excluded and symbol not in residual else None)
        if symbol in excluded:
            target, reason = current, "carry_forward_scope_excluded"
        elif symbol in residual:
            target, reason = 0, "saved_remaining_full_exit"
        elif pending is None:
            target, reason = current, "no_change_exact_saved_quantity"
        elif not weight:
            target, reason = 0, "saved_full_target_exit"
        elif price is None:
            target, reason = None, "saved_full_target_quote_unknown"
        else:
            target = shares_for_target(equity, effective[symbol], price, sizing_rule)
            if not current and target < sizing_rule.quantity.minimum:
                target = 0
            reason = "saved_pending_full_target"
        # Explain the existing sizing result without changing its quantity,
        # saved weights or cash. A reference close is not the next open quote.
        target_budget = (float(Decimal(equity) * Decimal(str(effective[symbol])) / 100)
                         if sizing_rule is not None else None)
        minimum_buy = sizing_rule.quantity.minimum if sizing_rule is not None else None
        minimum_notional = price * minimum_buy / 100 if price and minimum_buy is not None else None
        below_minimum = (target_budget < minimum_notional
            if not current and target_budget is not None and minimum_notional is not None else None)
        sizing_note = ("target_budget_below_minimum_buy_notional_at_saved_close" if below_minimum else
                       "saved_close_sizing_fees_slippage_and_execution_quote_not_checked" if sizing_rule is not None and price else
                       reason)
        delta = target - current if target is not None else None
        side = "unknown" if delta is None else "buy" if delta > 0 else "sell" if delta < 0 else "none"
        rule_rejection = (execution_rule(symbol, execution_day).quantity.rejection(abs(delta), side, current)
                          if delta and side in {"buy", "sell"} else None)
        status = ("blocked_missing_research_quote" if price is None else
                  "blocked_below_minimum_buy_quantity" if below_minimum else
                  "blocked_research_close_not_tradable" if delta and quote.get("tradestatus") != 1 else
                  "blocked_quantity_rule" if rule_rejection else
                  "paper_preview_not_created" if delta else "no_change")
        rows.append({"symbol": symbol, "current_qty": current, "target_qty": target,
            "delta_qty": delta, "target_action": reason, "target_weight": weight,
            "effective_target_weight": effective.get(symbol) if pending is not None else None,
            "realized_weight": (target * price / equity if target is not None and price else None),
            "close_rmb": quote.get("close_rmb") if known_price else None,
            "close_cents": price, "tradestatus": quote.get("tradestatus"),
            "is_st": quote.get("is_st"), "price_basis": "research_raw_close_rmb",
            "sizing_status": status, "sizing_note": sizing_note,
            "target_budget_rmb": target_budget, "minimum_buy_quantity": minimum_buy,
            "quantity_step": sizing_rule.quantity.step if sizing_rule is not None else None,
            "minimum_buy_notional_rmb": minimum_notional,
            "budget_below_minimum_buy_notional": below_minimum,
            "sizing_reference_basis": "saved_source_raw_close_not_execution_quote",
            "sizing_fees_included": False, "sizing_slippage_included": False,
            "rank": None, "score": None})
        orders.append({"symbol": symbol, "side": side, "qty": abs(delta) if delta is not None else None,
            "price_rmb": quote.get("close_rmb") if known_price else None,
            "estimated_notional_rmb": abs(delta) * price / 100 if delta is not None and price else None,
            "status": status, "reason": reason, "quantity_rule_rejection": rule_rejection,
            "current_account_sellable_qty": None,
            "sellable_evidence_required": side == "sell", "execution_admitted": False})
    trade_status = ("PAPER_TARGET_PENDING" if pending is not None else
                    "PAPER_EXIT_PENDING" if residual - set(excluded) else "NO_TRADE")
    return rows, orders, sorted(missing), trade_status


def build(source_run, signal, cutoff, execution_day, rank_override=None,
          execution_scope="strict", saved_intent=None):
    source_run = Path(source_run).resolve()
    source = _read_json(source_run)
    if saved_intent is not None:
        from .saved_account_intent import SCHEMA as INTENT_SCHEMA, read_request
        saved_intent = read_request(saved_intent)
        if (saved_intent["schema"] != INTENT_SCHEMA or saved_intent["strategy_id"] != signal
                or saved_intent["source_day"] != cutoff or saved_intent["execution_day"] != execution_day
                or Path(saved_intent["refs"]["source_run"]["path"]).resolve() != source_run):
            raise ValueError("Saved intent differs from the requested fixed preview")
    is_stockdb_rolling = source.get("schema") == "stockdb_rolling_low_volatility_ledger_v1"
    source_protocol = source.get("protocol") or {}
    if is_stockdb_rolling and not source_protocol:
        protocol_path = Path(source.get("artifacts", "")) / "protocol.json"
        if protocol_path.is_file():
            source_protocol = _read_json(protocol_path)
    rank_cadence = source.get("rank_cadence") or source_protocol.get("rank_cadence")
    rank_override_root = Path(rank_override).resolve() if rank_override else None
    if rank_override_root is not None and rank_override_root.is_file():
        override = _read_json(rank_override_root)
        source_artifacts = override.get("source_artifacts")
        if not source_artifacts:
            raise ValueError("Current rank override run has no source_artifacts")
        rank_override_root = Path(source_artifacts).resolve() / "predictions"
    if rank_override_root is not None and not (rank_override_root / "rankings.csv").exists():
        raise FileNotFoundError(f"Current rank override is missing rankings.csv: {rank_override_root}")
    execution_config = _execution_config(source)
    supported_statuses = {"completed_family_portfolio_diagnostics", "completed_factor_candidate_family_replay",
                          "completed_factor_candidate_library_ablation",
                          "completed_factor_candidate_current_score",
                          "completed_factor_candidate_cadence_replay", "completed_low_risk_diagnostic",
                          "completed_continuous_momentum_diagnostic", "partial_seat_bulk_diagnostic",
                          "completed_seat_bulk_diagnostic", "completed_factor_breadth_regime_overlay",
                          "completed_alpha158_linear_diagnostics", "completed_profitability_diagnostics"}
    if not is_stockdb_rolling and source.get("status") not in supported_statuses:
        raise ValueError("A completed fixed-family, low-risk, continuous-momentum or factor-family replay is required")
    source_end = source.get("end") or source.get("replay_end")
    if source_end != cutoff:
        raise ValueError("The preview cutoff must equal the replay terminal day")
    is_factor_family = source.get("research_family") in {"factor_candidate_family_balanced_ridge",
                                                          "factor_candidate_fixed_cadence"}
    is_factor_library = source.get("research_family") == "factor_candidate_library_leave_out"
    is_regime_overlay = source.get("research_family") == "factor_breadth_regime_overlay"
    is_seat_family = source.get("research_family") == "full_source_public_seat_donchian_v1"
    is_alpha158 = source.get("research_family") == "alpha158_linear_ensemble"
    is_profitability_family = source.get("research_family") in {
        "economic_profitability_monthly_fixed_v1",
        "economic_profitability_same_period_v1",
        "economic_profitability_reporting_calendar_v1",
    }
    if is_stockdb_rolling:
        case = next((row for row in source.get("results", []) if row.get("signal") == signal), None)
    elif is_regime_overlay:
        case = source.get("result") if source.get("signal") == signal else None
    elif is_factor_family:
        case = source.get("result") if source.get("result", {}).get("signal") == signal else None
    elif is_factor_library:
        case = next((row for row in source.get("results", {}).values()
                     if row.get("signal") == signal), None)
    else:
        case = next((row for row in source.get("results", []) if row.get("signal") == signal), None)
    if not case or case.get("status") != "completed":
        raise ValueError(f"Completed signal case not found: {signal}")
    if is_stockdb_rolling:
        artifacts = Path(source["artifacts"]).resolve() / signal
        rankings_root = Path(source["artifacts"]).resolve()
    elif is_factor_library:
        library_accounts = list(Path(source["artifacts"]).resolve().glob(f"*/{signal}/account.json"))
        if len(library_accounts) != 1:
            raise ValueError(f"Expected one saved library account for {signal}, found {len(library_accounts)}")
        artifacts = library_accounts[0].parent
        rankings_root = artifacts.parent / "predictions"
    elif is_seat_family:
        artifacts = Path(source["artifacts"]).resolve() / signal
        rankings_root = artifacts
    elif is_profitability_family:
        artifacts = Path(source["artifacts"]).resolve() / signal
        rankings_root = Path(source["rankings"]).resolve() / signal
    elif is_regime_overlay:
        artifacts = Path(source["artifacts"]).resolve() / signal
        source_replay = _read_json(source["source_run"])
        rankings_root = Path(source_replay["artifacts"]).resolve()
    elif is_alpha158:
        artifacts = Path(source["artifacts"]).resolve() / signal
        rankings_root = Path(source["artifacts"]).resolve() / "predictions" / signal
    else:
        artifacts = Path(source["artifacts"]).resolve() / signal
        rankings_root = Path(source.get("source_artifacts", source["artifacts"])).resolve()
    account = _read_json(artifacts / "account.json")
    fixed_daily = (_fixed_daily_binding(source, source_protocol, account, artifacts,
                       execution_config, cutoff, execution_day) if is_stockdb_rolling else None)
    is_fixed_daily = fixed_daily is not None
    if is_fixed_daily and rank_override_root is not None:
        raise ValueError("Saved fixed daily targets cannot be replaced by a rank override")
    daily = _read_csv(artifacts / "daily.csv")
    if rank_override_root is not None:
        rankings_root = rank_override_root
    rankings_path = None if is_alpha158 and rank_override_root is None else (
        rankings_root / "rankings.csv"
        if rank_override_root is not None or is_factor_library
        else rankings_root / "predictions" / "rankings.csv"
        if is_factor_family or is_regime_overlay
        else Path(source["artifacts"]).resolve() / f"{signal}_ranks.csv"
        if is_stockdb_rolling
        else Path(source["artifacts"]).resolve() / f"{signal}_ranks_1_30.csv"
        if source.get("research_family") == "low_risk_stock_selection"
        else Path(source["artifacts"]).resolve() / "source_target_ranks.csv"
        if source.get("research_family") == "continuous_information_momentum"
        else artifacts / "rankings.parquet"
        if is_seat_family
        else rankings_root / "rankings.csv"
        if is_profitability_family
        else artifacts / "rankings.csv")
    rankings = [] if rankings_path is None else _read_rankings(rankings_path)
    orders = _read_csv(artifacts / "orders.csv")
    terminal = next((row for row in reversed(daily) if row.get("date") == cutoff), None)
    if terminal is None:
        raise ValueError("Terminal daily mark is missing")
    positions = {symbol: _quantity(value) for symbol, value in account.get("positions", {}).items()}
    positions = {symbol: qty for symbol, qty in positions.items() if qty > 0}
    is_low_risk = source.get("research_family") == "low_risk_stock_selection"
    is_continuous_momentum = source.get("research_family") == "continuous_information_momentum"
    if is_stockdb_rolling:
        provisional_rank = _stockdb_rolling_rank(source, signal, cutoff)
        inputs = source.get("inputs") or {"price_view": source_protocol.get("snapshot")}
    elif is_alpha158:
        provisional_rank = _alpha158_rank(source, signal, cutoff)
        rankings = provisional_rank
        inputs = source["protocol"]["stock_inputs"]
    elif is_factor_family or is_factor_library or is_regime_overlay:
        provisional_rank = _factor_family_rank(source, cutoff, rankings_root=rankings_root)
        inputs = source["inputs"]
    elif is_seat_family:
        provisional_rank = _saved_rank(rankings_path, cutoff)
        inputs = source["protocol"]["stock_inputs"]
    elif is_low_risk:
        provisional_rank = _latest_low_risk_rank(source, signal, cutoff)
        inputs = source["inputs"]
    elif is_continuous_momentum:
        provisional_rank = _latest_continuous_momentum_rank(source, cutoff)
        inputs = source["inputs"]
    elif is_profitability_family:
        provisional_rank = _saved_rank(rankings_path, cutoff)
        inputs = source["protocol"]["stock_inputs"]
    else:
        provisional_rank = _terminal_month_end_rank(source, signal, cutoff)
        inputs = source["protocol"]["stock_inputs"]
    price_view = Path(inputs["price_view"])
    if execution_scope not in ("strict", "mainboard_only"):
        raise ValueError("Unsupported execution scope")
    scope_excluded_symbols = []
    if execution_scope == "mainboard_only":
        scope_excluded_symbols = [
            row["symbol"] for row in provisional_rank
            if not re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", row["symbol"])
        ]
        provisional_rank = [
            row for row in provisional_rank
            if re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", row["symbol"])
        ]
        if len(provisional_rank) < execution_config.holdings and not is_fixed_daily:
            raise ValueError(
                f"Execution scope leaves {len(provisional_rank)} candidates for "
                f"{execution_config.holdings} holdings"
            )
    scope_excluded_position_symbols = []
    execution_positions = dict(positions)
    if execution_scope == "mainboard_only":
        scope_excluded_position_symbols = sorted(
            symbol for symbol in positions
            if not re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", symbol)
        )
        execution_positions = {
            symbol: quantity for symbol, quantity in positions.items()
            if symbol not in set(scope_excluded_position_symbols)
        }
    selected_rank = provisional_rank[:execution_config.holdings]
    provisional_symbols = {row["symbol"] for row in selected_rank}
    if is_fixed_daily:
        provisional_symbols = set(account.get("pending_targets") or {})
        if execution_scope == "mainboard_only":
            scope_excluded_symbols = sorted(symbol for symbol in provisional_symbols
                if not re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", symbol))
    quotes = _quote_rows(price_view, cutoff, set(positions) | provisional_symbols)
    if is_stockdb_rolling:
        quotes = _stockdb_supplement_quotes(source, source_protocol, price_view, cutoff,
                                           set(positions) | provisional_symbols, quotes)
    last_rank_day = max((row["date"] for row in rankings), default=None)
    provisional_rank_day = max((row.get("date") for row in provisional_rank), default=None)
    last_order_day = max((row["day"] for row in orders), default=None)
    rank_is_current = provisional_rank_day == cutoff
    rank_carry_forward_allowed = (
        is_stockdb_rolling
        and not rank_is_current
        and rank_cadence == "monthly_completed_session_end"
        and provisional_rank_day is not None
        and provisional_rank_day < cutoff
    )
    rank_usable_for_schedule = rank_is_current or rank_carry_forward_allowed
    release_blockers = []
    if not rank_usable_for_schedule:
        release_blockers.append("rank_not_current_for_execution_day")
    release_blockers.extend([
        "current_user_account_observation_missing",
        "same_session_raw_rmb_quotes_missing",
        "sellable_quantity_and_t1_status_missing",
        "manual_user_review_required",
    ])
    release_state = "blocked_missing_live_inputs" if rank_usable_for_schedule else "blocked_stale_rank"
    terminal_equity_cents = _cents(terminal["equity"])
    cash_cents = int(account["cash"])
    target_rows = []
    order_rows = []
    missing_quotes = []
    marked_value_cents = 0
    rank_by_symbol = {
        row["symbol"]: row for row in rankings if row.get("date") == last_rank_day
    }
    for symbol, quantity in sorted(positions.items()):
        quote = quotes.get(symbol)
        rank = rank_by_symbol.get(symbol, {})
        price_cents = quote.get("close_cents") if quote else None
        if price_cents is None:
            missing_quotes.append(symbol)
        else:
            marked_value_cents += price_cents * quantity
        target_rows.append({
            "symbol": symbol,
            "current_qty": quantity,
            "target_qty": quantity,
            "delta_qty": 0,
            "target_action": ("carry_forward_scope_excluded"
                              if symbol in scope_excluded_position_symbols else "carry_forward"),
            "rank_date": last_rank_day,
            "rank": int(rank["rank"]) if rank.get("rank") else None,
            "score": float(rank["score"]) if rank.get("score") else None,
            "close_rmb": quote.get("close_rmb") if quote else None,
            "close_cents": price_cents,
            "tradestatus": quote.get("tradestatus") if quote else None,
            "is_st": quote.get("is_st") if quote else None,
            "price_basis": quote.get("price_basis") if quote else None,
            "target_weight_estimate": (price_cents * quantity / terminal_equity_cents
                                        if price_cents is not None and terminal_equity_cents else None),
        })
        order_rows.append({
            "symbol": symbol,
            "side": "none",
            "qty": 0,
            "price_rmb": None,
            "status": "not_created",
            "reason": "carry_forward_no_new_month_end_signal",
            "execution_admitted": False,
        })
    provisional_prices = {symbol: quotes[symbol]["close_cents"] for symbol in provisional_symbols
                          if symbol in quotes and quotes[symbol].get("close_cents") is not None}
    provisional_targets = []
    provisional_orders = []
    provisional_missing_quotes = sorted(provisional_symbols - set(provisional_prices))
    if not provisional_missing_quotes and not is_fixed_daily:
        from ..portfolio_allocation import stock_lot_allocation
        from ..rules import execution_rule

        equity_for_sizing = terminal_equity_cents
        if not equity_for_sizing:
            raise ValueError("Terminal equity is required for provisional sizing")
        target_weight = execution_config.gross_limit / execution_config.holdings
        weights = {row["symbol"]: target_weight for row in selected_rank}
        rules = {symbol: execution_rule(symbol, cutoff) for symbol in weights}
        entries, _, _ = stock_lot_allocation(
            weights,
            equity_for_sizing,
            provisional_prices,
            rules,
            execution_config.single_limit,
            execution_config.gross_limit,
        )
        provisional_qty = {entry["symbol"]: entry["quantity"] for entry in entries}
        by_symbol = {row["symbol"]: row for row in selected_rank}
        for symbol in sorted(provisional_symbols | set(execution_positions)):
            rank = by_symbol.get(symbol, {})
            quote = quotes.get(symbol)
            current_qty = positions.get(symbol, 0)
            target_qty = provisional_qty.get(symbol, 0)
            delta = target_qty - current_qty
            side = "buy" if delta > 0 else "sell" if delta < 0 else "none"
            price_cents = quote.get("close_cents") if quote else None
            provisional_targets.append({
                "symbol": symbol,
                "current_qty": current_qty,
                "target_qty": target_qty,
                "delta_qty": delta,
                "target_weight": weights.get(symbol, 0),
                "realized_weight": (target_qty * price_cents / equity_for_sizing
                                    if price_cents is not None else None),
                "sizing_status": ("integer_lot_target" if target_qty else
                                   "below_minimum_lot_or_missing_quote" if symbol in provisional_symbols else
                                   "explicit_exit"),
                "rank": int(rank["rank"]) if rank.get("rank") else None,
                "score": float(rank["score"]) if rank.get("score") is not None else None,
                "close_rmb": quote.get("close_rmb") if quote else None,
                "price_basis": "research_raw_close_rmb" if quote else None,
            })
            provisional_orders.append({
                "symbol": symbol,
                "side": side,
                "qty": abs(delta),
                "price_rmb": quote.get("close_rmb") if quote else None,
                "estimated_notional_rmb": (abs(delta) * price_cents / 100 if price_cents is not None else None),
                "status": "paper_preview_not_created" if delta else "no_change",
                "reason": "terminal_cutoff_month_end_rank_preview",
                "execution_admitted": False,
            })
    trade_status = "NO_TRADE"
    if is_fixed_daily:
        target_rows, order_rows, provisional_missing_quotes, trade_status = _fixed_daily_rows(
            account, fixed_daily, positions, quotes, terminal_equity_cents, execution_config,
            cutoff, execution_day, sorted(set(scope_excluded_position_symbols) | set(scope_excluded_symbols)))
        # Existing consumers read either pair; they must see the same saved
        # decision, never a second rebalance reconstructed from an old rank.
        provisional_targets, provisional_orders = target_rows, order_rows
        missing_quotes = list(provisional_missing_quotes)
        marked_value_cents = (None if set(missing_quotes) & set(positions) else
                              sum(quotes[symbol]["close_cents"] * quantity
                                  for symbol, quantity in positions.items()))
        rank_carry_forward_allowed = False
        rank_usable_for_schedule = False
        release_state = "blocked_research_saved_account_only"
        release_blockers = ["saved_research_account_not_user_account", "broker_quote_missing",
                            "sellable_quantity_and_t1_status_missing", "manual_user_review_required"]
        if missing_quotes:
            release_blockers.append("exact_cutoff_research_quotes_missing")
    identifier = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output = ROOT / "outputs" / "paper_rebalance" / identifier
    data_output = DATA_ROOT / "research" / "paper_rebalance" / identifier
    output.mkdir(parents=True, exist_ok=False)
    data_output.mkdir(parents=True, exist_ok=False)
    files = [(target_rows, "target_holdings.csv"), (order_rows, "order_plan.csv"),
             (provisional_rank, "provisional_ranks.csv"), (provisional_targets, "provisional_target_holdings.csv"),
             (provisional_orders, "provisional_order_plan.csv")]
    for rows, name in files:
        with (data_output / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["symbol"])
            writer.writeheader()
            writer.writerows(rows)
        (output / name).write_bytes((data_output / name).read_bytes())
    adapter_orders = [
        {
            **row,
            "price_basis": "research_raw_close_rmb",
            "status": (row["status"] if is_fixed_daily and row.get("status", "").startswith("blocked_")
                       else "requires_current_account_observation"),
            "current_account_sellable_qty": None,
        }
        for row in provisional_orders
        if row.get("qty")
    ]
    rank_template = [
        {
            "symbol": row["symbol"],
            "rank": row.get("rank"),
            "score": row.get("score"),
            "date": row.get("date"),
        }
        for row in provisional_rank
    ]
    if is_fixed_daily:
        rank_template = []
    mainboard_symbols = [
        row["symbol"] for row in rank_template
        if re.fullmatch(r"(?:60\d{4}\.SSE|00\d{4}\.SZSE)", row["symbol"])
    ]
    execution_scope_summary = {
        "execution_scope": execution_scope,
        "candidate_count": len(rank_template),
        "mainboard_candidate_count": len(mainboard_symbols),
        "strategy_holdings": execution_config.holdings,
        "mainboard_only_capacity_sufficient": len(mainboard_symbols) >= execution_config.holdings,
        "scope_excluded_candidate_count": len(scope_excluded_symbols),
        "scope_excluded_candidate_symbols": scope_excluded_symbols,
        "scope_excluded_position_count": len(scope_excluded_position_symbols),
        "scope_excluded_position_symbols": scope_excluded_position_symbols,
        "basis": "same frozen score snapshot; account sizing still requires current account and quotes",
    }
    handoff = {
        "schema": "ths_manual_prefill_handoff_v1",
        "status": "research_preview_not_ready_for_prefill",
        "execution_admitted": False,
        "submission": "manual_only",
        "strategy_id": signal,
        "source_cutoff": cutoff,
        "execution_day": execution_day,
        "account_alias": None,
        "account_source": "research_replay_account_not_user_broker_account",
        "quote_basis": "research_raw_close_rmb",
        "adapter": {
            "name": "THSAdapter",
            "operation": "prepare_portfolio_then_prefill",
            "submit": False,
            "cancel": False,
        },
        "target_template": [
            {
                "symbol": row["symbol"],
                "target_qty": row["target_qty"],
                "target_weight": row.get("target_weight", 0),
                "price_basis": "research_raw_close_rmb",
            }
            for row in provisional_targets
        ],
        "rank_template": rank_template,
        "execution_scope_summary": execution_scope_summary,
        "release_control": {
            "state": release_state,
            "execution_release_allowed": False,
            "rank_is_current_for_execution_day": rank_is_current,
            "rank_carry_forward_allowed": rank_carry_forward_allowed,
            "rank_schedule": rank_cadence,
            "rank_day": provisional_rank_day,
            "cutoff": cutoff,
            "blockers": release_blockers,
        },
        "execution_scope": execution_scope,
        "scope_excluded_position_symbols": scope_excluded_position_symbols,
        "orders": adapter_orders,
        "requires_before_adapter": [
            "current THS account observation",
            "same-session raw RMB quotes",
            "sellable quantities and T+1 status",
            "manual user review",
        ],
        "why_not_ready": [
            "This handoff uses research replay positions, not a user broker snapshot.",
            "Prices are terminal-cutoff research closes, not same-session executable quotes.",
            "The THS physical client remains paused and no prefill was attempted.",
        ],
    }
    if is_fixed_daily:
        handoff.update({"task_method": "saved_fixed_account_next_session_v1",
            "trade_status": trade_status, "supported_sizing_modes": ["replay_shares"],
            "observed_account_sizing_supported": False,
            "observed_account_sizing_reason": "Saved pending/residual quantities cannot be reranked from an account observation.",
            "selection_enabled": False, "strategy_validated": False,
            "candidate_panel_only": True,
            "target_template": [{"symbol": row["symbol"], "target_qty": row["target_qty"],
                "target_weight": row["target_weight"], "price_basis": "research_raw_close_rmb"}
                for row in target_rows],
            "adapter": {"name": "THSAdapter", "operation": "saved_research_preview_only",
                        "submit": False, "cancel": False, "prefill": False}})
    if saved_intent is not None:
        if not is_fixed_daily:
            raise ValueError("Saved intent attachment requires its explicit fixed-account method")
        handoff.update(saved_account_intent=saved_intent, supported_sizing_modes=["saved_intent"],
            observed_account_sizing_reason="Saved targets remain research diagnostics; no rank reselection.")
    _write_json(data_output / "ths_manual_handoff.json", handoff)
    (output / "ths_manual_handoff.json").write_bytes((data_output / "ths_manual_handoff.json").read_bytes())
    result = {
        "status": "paper_preview_only",
        "execution_admitted": False,
        "trade_status": trade_status,
        "strategy_id": signal,
        "source_run": str(source_run),
        "source_artifacts": str(artifacts),
        "rank_source": ("alpha158_historical_scores" if is_alpha158 and rank_override_root is None else
                        "current_score_bundle" if rank_override_root is not None or source.get("score_only")
                        else "completed_replay_rankings"),
        "rank_override": str(rank_override_root) if rank_override_root is not None else None,
        "price_view": str(price_view),
        "supplemental_price_view": source.get("supplemental_raw_view") if is_stockdb_rolling else None,
        "source_cutoff": cutoff,
        "execution_day": execution_day,
        "last_replay_order_day": last_order_day,
        "last_replay_rank_day": last_rank_day,
        "provisional_rank_day": provisional_rank_day,
        "rank_is_current_for_execution_day": rank_is_current,
        "rank_carry_forward_allowed": rank_carry_forward_allowed,
        "rank_schedule": rank_cadence,
        "release_state": release_state,
        "release_blockers": release_blockers,
        "execution_config": execution_config.to_dict(),
        "execution_scope": execution_scope,
        "scope_excluded_position_symbols": scope_excluded_position_symbols,
        "account_source": "research_replay_account_not_user_broker_account",
        "terminal_equity_cents": terminal_equity_cents,
        "terminal_equity_rmb": float(Decimal(terminal["equity"])),
        "cash_cents": cash_cents,
        "cash_rmb": float(Decimal(cash_cents) / 100),
        "marked_value_cents": marked_value_cents,
        "marked_value_rmb": float(Decimal(marked_value_cents) / 100) if marked_value_cents is not None else None,
        "position_count": len(target_rows),
        "missing_quote_symbols": missing_quotes,
        "orders_created": 0,
        "provisional_rank_count": len(provisional_rank),
        "execution_scope_summary": execution_scope_summary,
        "provisional_target_count": len(provisional_targets),
        "provisional_order_count": sum(1 for row in provisional_orders if row.get("qty")),
        "adapter_handoff_status": handoff["status"],
        "adapter_handoff_order_count": len(adapter_orders),
        "provisional_missing_quote_symbols": provisional_missing_quotes,
         "source_limitations": (source_protocol.get("stock_manifest", {}).get("limitations", [])
                                 or source_protocol.get("limitations", [])
                                 or source.get("protocol", {}).get("stock_manifest", {}).get("limitations", [])
                                 or source.get("protocol", {}).get("limitations", [])
                                or source.get("protocol", {}).get("limits", [])
                                or source.get("limitations", [])),
        "why_no_order": ([
            "No user broker account observation or live quote was read; no prefill or client operation was attempted.",
        ] if rank_is_current else ([
            "The fixed monthly rank is being carried forward by its declared completed-session schedule; live account, quote and T+1 evidence are still missing.",
            "No user broker account observation or live quote was read; no prefill or client operation was attempted.",
        ] if rank_carry_forward_allowed else [
            "The fixed replay has no newly generated terminal-cutoff rank in this replay.",
            "The available rank tail is research data through the prior rebalance date, not a current EOD THS feed.",
            "No user broker account observation or live quote was read; no prefill or client operation was attempted.",
        ])),
        "artifacts": {
            "target_holdings": str(data_output / "target_holdings.csv"),
            "order_plan": str(data_output / "order_plan.csv"),
            "provisional_ranks": str(data_output / "provisional_ranks.csv"),
            "provisional_target_holdings": str(data_output / "provisional_target_holdings.csv"),
            "provisional_order_plan": str(data_output / "provisional_order_plan.csv"),
            "ths_manual_handoff": str(data_output / "ths_manual_handoff.json"),
        },
    }
    if is_fixed_daily:
        result.update({"task_method": "saved_fixed_account_next_session_v1",
            "selection_enabled": False, "strategy_validated": False, "candidate_panel_only": True,
            "full_exit_lifecycle": source_protocol["full_exit_lifecycle"],
            "month_end_schedule": source_protocol["month_end_schedule"],
            "pending_full_target_present": account.get("pending_targets") is not None,
            "position_count": len(positions),
            "saved_rank_display_only": True,
            "why_no_order": ["Preview consumes only saved pending targets, unfinished full exits and exact saved quantities.",
                "Research closes are not BrokerQuote; current account, sellable shares and executable quotes remain missing."]})
    _write_json(data_output / "result.json", result)
    _write_json(output / "result.json", result)
    report = [
        "# A-share next-session paper preview",
        "",
        f"- Strategy: `{signal}`",
        f"- Source cutoff: `{cutoff}`; intended next session: `{execution_day}`",
        f"- Status: `paper_preview_only`; `{trade_status}`",
        f"- Replay terminal equity: {float(Decimal(terminal['equity'])):.2f} CNY; cash: {float(Decimal(cash_cents) / 100):.2f} CNY",
        f"- Positions carried: {len(target_rows)}; created orders: 0",
        f"- Terminal-cutoff provisional rank: {len(provisional_rank)} candidates; provisional order lines: {sum(1 for row in provisional_orders if row.get('qty'))}",
        f"- THS manual handoff: `{handoff['status']}`; order payload lines: {len(adapter_orders)}; not saved to the order ledger",
        f"- Last saved replay rank date: `{last_rank_day}`; preview rank date: `{provisional_rank_day}`; last replay order date: `{last_order_day}`",
        f"- Release control: `{release_state}`; execution release allowed: `false`; blockers: `{', '.join(release_blockers)}`",
        "",
        ("This package carries the last completed Engine/Ledger paper account forward and consumes the current score bundle. It is not a user broker snapshot or a THS prefill request."
         if source.get("score_only") else
         "This package carries the last completed Engine/Ledger paper account forward for inspection. It is not a current score bundle, not a user broker snapshot, and not a THS prefill request."),
        "The carry-forward order plan is intentionally empty of executable orders. A separate provisional order plan compares the terminal-cutoff rank to the replay account, using research closes only; it is not executable.",
        "The THS manual handoff is a consumer payload only. It must be hydrated with a current account observation and same-session quotes before any THSAdapter plan can be prepared; it is not a prefill request.",
        "",
        f"Artifacts: `{data_output}`",
    ]
    if is_fixed_daily:
        report[10:] = ["", "The target and provisional files describe the same saved-account task.",
            "Saved rankings are display-only. NO_CHANGE retains exact shares; unfinished full exits retain zero-share targets.",
            "No Engine advance, broker observation, prefill or order submission occurred. Observed-account reranking is unsupported.",
            "Research closes do not establish current sellable quantities or executable broker prices.",
            f"Artifacts: `{data_output}`"]
    (data_output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--signal", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--rank-override", help="Directory containing a current-score rankings.csv")
    parser.add_argument("--execution-scope", choices=("strict", "mainboard_only"),
                        default="strict",
                        help="Keep the frozen universe or restrict new candidates to mainboard")
    args = parser.parse_args(argv)
    output, result = build(args.source_run, args.signal, args.cutoff, args.execution_day,
                           rank_override=args.rank_override,
                           execution_scope=args.execution_scope)
    print(json.dumps({"output": str(output), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
