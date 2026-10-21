"""Fixed annual-quality candidates for subsequent sessions through the sole ledger."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import PortfolioIntent, RunContext, SignalFrame, SignalUnavailable, TradingMode, decision_time
from ..engine import choose_targets, shares_for_target
from ..ledger import Ledger, new_state
from ..portfolio_allocation import LOT_ALLOCATION, OPEN_LOT_ALLOCATION, allocate_close_lots, allocate_open_lots
from ..rules import cents, execution_rule, fees, notional_cents, quote_value
from ..settings import DATA_ROOT, ROOT
from ..worker import lock, market_now
from .annual_quality_features import ID as FEATURE_ID
from .annual_quality_features import INVESTMENT_ID as INVESTMENT_FEATURE_ID, INVESTMENT_SOURCES
from .bulk_feed import BulkResearchFeed
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .low_risk import LowRiskFeed, VARIANTS, features, portfolio, rank
from .monthly_timing import month_ends
from .profitability_portfolios import rank_z
from .prospective_research import incoming_dates, publish_head, validate_extension


ID = "annual_quality_same_pool_prospective_v1"
INVESTMENT_ID = "annual_quality_investment_same_pool_prospective_v1"
CASES = ("annual_matched_momentum", "annual_matched_low_vol_half_momentum",
         "annual_quality_only", "annual_risk_momentum_quality")
QUALITY_FIELDS = ("revenue_cost_spread_assets", "operating_cashflow_assets")
FORMULAS = {
    "quality": "(rank_z(revenue_cost_spread_assets)+rank_z(operating_cashflow_assets))/2",
    CASES[0]: "momentum=P[t-21]/P[t-252]-1 in the common financial/price pool",
    CASES[1]: "lowest-volatility ceil(N/2) of that common pool, then rank momentum",
    CASES[2]: "quality",
    CASES[3]: "(rank_z(-volatility)+rank_z(momentum)+quality)/3",
}
INVESTMENT_CASES = (*CASES[:3], "annual_low_asset_growth", "annual_quality_low_asset_growth")
INVESTMENT_FORMULAS = {key: value for key, value in FORMULAS.items() if key != CASES[3]} | {
    "investment": "rank_z(-asset_growth); asset_growth=TOTAL_ASSETS[y]/TOTAL_ASSETS[y-1]-1",
    INVESTMENT_CASES[3]: "investment",
    INVESTMENT_CASES[4]: "(quality+investment)/2; two economic families, no weight fit",
}
LIMITS = [
    "Fixed financial retrieval version at declaration, not original historical PIT or complete QMJ",
    "Revenue-minus-cost/assets and net operating cashflow/assets are adaptations; no gross-margin double-counting",
    "Common pool uses the existing full source-covered SH/SZ stock rule, not all A-shares/ETF/BSE",
    "Latest-known-price preview is not a past signal, fresh quote, target order or account cash/position verification",
    "Only subsequent complete sessions may generate signals; initial session then confirmed month-ends",
    "20 names/rank30/80pct target gross, existing cash/shares/T1/lots/costs/actions through the sole Engine/Ledger",
    "Four unbound research hypotheses may have distinct research accounts; none is the user's THS account",
    "No historical annual-feature backfill, fit, threshold/weight search, default-strategy switch or live/broker operation",
    "Prospective diagnostic receipt-time research, not untouched/PIT admission, profitable strategy or tail protection",
]
INVESTMENT_LIMITS = [line for line in LIMITS if not line.startswith("Four unbound")] + [
    "Five fixed cases share the SAME quality-plus-investment pool, including all three simple controls",
    "Low asset growth follows the public I/A direction, NOT a complete q-factor model or expected-growth forecast",
    "No NYSE breakpoints, long-short/value weighting or annual June formation replication; fixed existing A-share monthly engine",
    "Financial-sector exclusion and cross-year restatement comparability are not verified; no industry-neutral or original-paper-reproduction claim",
    "Reject missing/late preceding-year assets; both years must have positive reported assets, no historical fallback",
]


def comparison_definition(identifier):
    if identifier == ID:
        return CASES, FORMULAS, False
    if identifier == INVESTMENT_ID:
        return INVESTMENT_CASES, INVESTMENT_FORMULAS, True
    raise ValueError("Unknown fixed annual comparison definition")


def declared_definition(protocol):
    cases, formulas, investment = comparison_definition(protocol["definition_id"])
    if protocol["signals"] != list(cases) or protocol["formulas"] != formulas:
        raise ValueError("Fixed annual comparison cases/formulas changed")
    return cases, investment


def quality_pool(price_pool, annual, *, include_investment=False):
    if annual.symbol.duplicated().any():
        raise ValueError("One fixed annual observation per source identity required")
    fields = (*QUALITY_FIELDS, "asset_growth") if include_investment else QUALITY_FIELDS
    columns = ["symbol", "report_day", *fields,
               *[field + "_status" for field in fields],
               *[field + "_received_at" for field in fields]]
    observed = np.isfinite(annual[list(fields)]).all(axis=1)
    for field in fields:
        observed &= annual[field + "_status"].eq("available_numeric") & annual[field + "_received_at"].notna()
    price = pd.DataFrame(price_pool, columns=["symbol", "momentum", "volatility", "median_amount"])
    if price.symbol.duplicated().any():
        raise ValueError("Distinct same-day market members required")
    frame = price.merge(annual.loc[observed, columns], on="symbol", how="inner", validate="one_to_one")
    frame = frame.set_index("symbol").sort_index()
    if len(frame):
        for field in fields:
            frame[field + "_received_at"] = frame[field + "_received_at"].map(lambda value: pd.Timestamp(value).isoformat())
        frame["quality"] = sum(rank_z(frame[field]) for field in QUALITY_FIELDS) / len(QUALITY_FIELDS)
        if include_investment:
            frame["investment"] = rank_z(-frame.asset_growth)
    return frame, {"price_pool": len(price), "annual_source_rows": len(annual),
                   "annual_numeric_rows": int(observed.sum()), "common_pool": len(frame)}


def ranked(frame, case):
    if case not in {*CASES, *INVESTMENT_CASES}:
        raise ValueError("Unknown fixed annual quality case")
    if case in CASES[:2]:
        return rank(frame.reset_index().to_dict("records"), VARIANTS[0] if case == CASES[0] else VARIANTS[2])
    current = frame.copy()
    if not len(current):
        return []
    if case == INVESTMENT_CASES[3]:
        current["score"] = current.investment
    elif case == INVESTMENT_CASES[4]:
        current["score"] = (current.quality + current.investment) / 2
    else:
        current["score"] = (current.quality if case == CASES[2] else
                            (rank_z(-current.volatility) + rank_z(current.momentum) + current.quality) / 3)
    return current.reset_index().sort_values(["score", "symbol"], ascending=[False, True], kind="stable").to_dict("records")


class AnnualQualityFeed:
    def __init__(self, base, values, annual, case, end, declared_at, feature_asof, include_investment=False):
        self.declared_at, self.feature_asof = pd.Timestamp(declared_at), pd.Timestamp(feature_asof)
        cases = INVESTMENT_CASES if include_investment else CASES
        self.fields = (*QUALITY_FIELDS, "asset_growth", "investment") if include_investment else QUALITY_FIELDS
        self.include_investment = include_investment
        if (case not in cases or any(point.tzinfo is None for point in (self.declared_at, self.feature_asof))
                or self.feature_asof > self.declared_at or annual.report_day.nunique() != 1):
            raise ValueError("One fixed annual period with known past availability and declaration required")
        for field in (*QUALITY_FIELDS, "asset_growth") if include_investment else QUALITY_FIELDS:
            received = pd.to_datetime(annual[field + "_received_at"], utc=True, errors="coerce")
            known = annual[field + "_status"].eq("available_numeric")
            if (known & (received.isna() | received.gt(self.feature_asof))).any():
                raise ValueError("Annual feature was not received by its frozen as-of time")
        self.base, self.annual, self.case, self.end = base, annual, case, end
        self.market = LowRiskFeed(base, values, VARIANTS[0], end, diagnostic_extension=True)
        future = [day for day in base.calendar if pd.Timestamp(decision_time(day)) >= self.declared_at]
        self.decision_days = set(month_ends(base.calendar, end)) & set(future)
        if future:
            self.decision_days.add(future[0])
        self.audit, self.ranking_rows = [], []

    def __getattr__(self, key):
        return getattr(self.base, key)

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY or config != portfolio(config.initial_cash):
            raise ValueError("Unbound replay with the unchanged fixed monthly portfolio required")
        if day not in self.base.day_index or day > self.end:
            raise ValueError("Annual quality decision outside its actual calendar/cutoff")
        if pd.Timestamp(decision_time(day)) < self.declared_at:
            return SignalUnavailable("annual_quality_strategy_not_declared_by_decision")
        if day not in self.decision_days:
            return SignalUnavailable("not_annual_quality_rebalance_day")
        market = self.market.pool(day, config)
        if isinstance(market, SignalUnavailable):
            return market
        pool, counts = quality_pool(market, self.annual, include_investment=self.include_investment)
        if len(pool) < config.retain_rank:
            self.audit.append({"date": day, "case": self.case, **counts, "intent": "NO_CHANGE"})
            return SignalUnavailable("annual_quality_common_pool_below_fixed_rank_buffer")
        candidates = ranked(pool, self.case)
        self.audit.append({"date": day, "case": self.case, **counts, "intent": "FULL_TARGET"})
        self.ranking_rows.extend({"date": day, "case": self.case, "rank": i, **row}
                                 for i, row in enumerate(candidates[:config.retain_rank], 1))
        # The signal contract carries numeric diagnostics; provenance stays in the saved ranking table.
        numeric = ("score", "momentum", "volatility", "median_amount", "quality", *self.fields)
        signals = [{"symbol": row["symbol"], **{field: float(row[field]) for field in numeric}} for row in candidates]
        return SignalFrame.from_legacy(signals, day, "research:" + self.case, context)


def load_quality(quality_run, report_day, include_investment=False):
    root = Path(quality_run).resolve()
    receipt = json.loads((root / "result.json").read_text(encoding="utf-8"))
    expected = INVESTMENT_FEATURE_ID if include_investment else FEATURE_ID
    if (receipt["definition_id"] != expected or receipt["status"] != "completed_annual_quality_features"
            or receipt["execution_admitted"] is not False):
        raise ValueError("Completed fixed unadmitted annual quality feature consumer required")
    annual = pd.read_parquet(root / "annual_quality_features.parquet")
    if len(annual) != receipt["rows"] or not annual["asof"].eq(receipt["asof"]).all():
        raise ValueError("Annual feature count or as-of differs from its frozen receipt")
    annual = annual.loc[annual.report_day.eq(report_day)].copy()
    if not len(annual) or annual.symbol.duplicated().any():
        raise ValueError("A present unique fixed annual report period required")
    return annual, receipt


def report(root, protocol, result):
    out = ROOT / "outputs/annual_quality_portfolios" / protocol["id"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "run.json", result | {"protocol": protocol, "artifacts": str(root)})
    lines = ["# Fixed Annual Quality To Portfolio", "", "NO_TRADE. " + result["status"],
             f"Declared {protocol['declared_at']}; financial as-of {protocol['feature_asof']}; fixed period {protocol['report_day']}.",
             f"Preview market date {protocol['preview_market_day']}; common pool {protocol['preview_counts']['common_pool']}.",
             f"Actual account sessions {result['sessions']}; no THS binding.", "",
             "Declared lot allocation: " + str(protocol.get("lot_allocation") or "original_exact_target") + ".",
             "Preview weights use no existing holdings; actual rank-buffer retention belongs to the existing engine.",
             "Preview rankings do not become historical signals or executable plans.", "",
             *["- " + key + ": " + value for key, value in protocol["formulas"].items()], "",
             *["- " + value for value in protocol["limits"]], "", "Artifacts: " + str(root)]
    if result.get("reason"):
        lines += ["", result["reason"]]
    for case in result.get("results", []):
        lines.append("\n" + case["signal"] + ": " + json.dumps(case))
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare(quality_run, source_run, report_day, current=None, *, allocation=None, comparison=ID):
    if allocation not in {None, OPEN_LOT_ALLOCATION}:
        raise ValueError("Declare the original target sizing or existing integer attempt-mark allocation")
    current = current or market_now()
    cases, formulas, investment = comparison_definition(comparison)
    limits = INVESTMENT_LIMITS if investment else LIMITS
    annual, quality = load_quality(quality_run, report_day, investment)
    if pd.Timestamp(quality["asof"]) > pd.Timestamp(current):
        raise ValueError("Cannot declare a strategy on future financial inputs")
    source_run = Path(source_run).resolve()
    source = json.loads(source_run.read_text(encoding="utf-8"))
    inputs = {key: source["protocol"]["stock_inputs"][key]
              for key in ("price_view", "event_view", "reference_view", "listing_master")}
    base = BulkResearchFeed(**inputs)
    try:
        day = base.calendar[-1]
        if day > current.date().isoformat() or (day == current.date().isoformat() and (current.hour, current.minute) < (16, 15)):
            raise ValueError("Known price snapshot needs a complete nonfuture market session")
        values = features(base, day, feature_days=[day])
        config = portfolio()
        feed = AnnualQualityFeed(base, values, annual, cases[0], day, current.isoformat(), quality["asof"], investment)
        price_pool = feed.market.pool(day, config)
        if isinstance(price_pool, SignalUnavailable):
            raise ValueError("Cannot form the actual known-market pool: " + price_pool.reason)
        pool, counts = quality_pool(price_pool, annual, include_investment=investment)
        if len(pool) < config.retain_rank:
            raise ValueError("Common preview pool is below the fixed rank buffer")
        rejected = feed.signal_frame(day, config, RunContext("annual_quality_no_account_preview", TradingMode.REPLAY))
        if not isinstance(rejected, SignalUnavailable):
            raise ValueError("Declare after the preview market session; preview cannot become a past signal")
        identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        root = DATA_ROOT / "research/annual_quality_portfolios" / identifier
        root.mkdir(parents=True, exist_ok=False)
        shutil.copy2(__file__, root / "implementation.py")
        pool.reset_index().to_parquet(root / "preview_common_pool.parquet", index=False)
        coverage = annual[["symbol", "report_day", *QUALITY_FIELDS, *(["asset_growth"] if investment else [])]].copy()
        coverage["in_source_market_identity"] = coverage.symbol.isin(base.symbols)
        coverage["in_known_market_pool"] = coverage.symbol.isin([row["symbol"] for row in price_pool])
        coverage["in_common_pool"] = coverage.symbol.isin(pool.index)
        coverage.to_csv(root / "preview_intersection.csv", index=False)
        ranking_rows, targets = [], {}
        for case in cases:
            candidates = ranked(pool, case)
            ranking_rows.extend({"market_day": day, "case": case, "rank": i, **row}
                                for i, row in enumerate(candidates[:config.retain_rank], 1))
            targets[case] = choose_targets(candidates, [], config)
        pd.DataFrame(ranking_rows).to_parquet(root / "preview_rankings.parquet", index=False)
        write_json(root / "preview_weights.json", {"mode": "known_market_preview_only", "held": [],
                                                  "account_created": False, "cases": targets})
        protocol = {"id": identifier, "definition_id": comparison, "declared_at": current.isoformat(),
            "feature_asof": quality["asof"], "quality_run": str(Path(quality_run).resolve()),
            "feature_id": quality["id"], "report_day": report_day, "signals": list(cases), "formulas": formulas,
            "portfolio": config.to_dict(), "source_run": str(source_run), "stock_inputs": inputs,
            "manifest": base.manifest, "preview_market_day": day, "preview_counts": counts,
            "rebalance": "First actual post-declaration complete session, then confirmed exchange month-ends",
            "small_pool": "NO_CHANGE if fewer than fixed rank30 members; no imputation or historical fallback",
            "financial_revisions": "Frozen retrieval version throughout this finite comparison",
            "end": "2026-12-31", "lot_allocation": allocation,
            "execution_admitted": False, "limits": limits}
        if investment:
            protocol["method_sources"] = INVESTMENT_SOURCES
        write_json(root / "protocol.json", protocol)
        result = {"id": identifier, "status": "prepared_annual_quality_prospective_candidates", "sessions": 0,
                  "preview_counts": counts, "preview_past_signal_rejected": rejected.reason,
                  "account_created": False, "execution_admitted": False, "lot_allocation": allocation,
                  "results": [{"signal": case, "completed_sessions": 0, "status": "not_started"} for case in cases]}
        publish_head(root, result)
        report(root, protocol, result)
        print(json.dumps(result), flush=True)
        print(str(root), flush=True)
        return root, result
    finally:
        base.close()


def capacity_rows(weights, quotes, day, config, *, quantities=None):
    """Saved-close sizing only; do not submit intents or assume next-open fills."""
    equity = cents(config.initial_cash)
    rows, summaries = [], []
    for case, targets in weights.items():
        if quantities is not None and set(quantities[case]) != set(targets):
            raise ValueError("Integer capacity quantities must preserve every original target identity")
        case_rows = []
        for symbol, weight in targets.items():
            if not isinstance(weight, (int, float)) or not np.isfinite(weight) or not 0 <= weight <= config.single_limit:
                raise ValueError("Capacity sizing requires finite nonnegative admitted weights")
            rule = execution_rule(symbol, day)
            row = {"case": case, "symbol": symbol, "market_day": day, "target_weight": weight,
                   "board": rule.board, "minimum_buy_quantity": rule.quantity.minimum,
                   "quantity_step": rule.quantity.step, "price_basis": "raw_rmb",
                   "within_existing_ths_instrument_scope": rule.board == "main",
                   "close_cents": None, "engine_quantity": None, "legal_quantity": None,
                   "notional_cents": None, "buy_fees_cents": None,
                   "minimum_commission_premium_cents": None, "rounded_weight": None,
                   "minimum_entry_capital_cents": None}
            quote = quotes.get(symbol)
            reason = None
            if quote is None:
                reason = "missing_saved_raw_quote"
            elif str(quote.get("adjustflag")) != "3":
                reason = "not_explicit_raw_price"
            elif quote.get("tradestatus") != 1 or quote.get("isST") != 0:
                reason = "saved_status_not_eligible"
            elif (not isinstance(quote.get("close"), (int, float, np.number))
                  or not np.isfinite(quote["close"]) or quote["close"] <= 0):
                reason = "unknown_or_invalid_raw_close"
            if reason is not None:
                row["status"] = reason
            else:
                price = quote_value(quote["close"], rule.price_unit)
                quantity = quantities[case][symbol] if quantities is not None else shares_for_target(equity, weight, price, rule)
                if type(quantity) is not int or quantity < 0:
                    raise ValueError("Capacity quantities must be nonnegative integers")
                legal = rule.quantity.rounded(quantity, "buy")
                value = notional_cents(price, legal, rule.price_unit)
                fee = fees(value, "buy", config)
                premium = (max(cents(config.minimum_commission), round(value * config.commission))
                           - round(value * config.commission)) if value else 0
                minimum_value = notional_cents(price, rule.quantity.minimum, rule.price_unit)
                row.update(close_cents=price, engine_quantity=quantity, legal_quantity=legal,
                           notional_cents=value, buy_fees_cents=fee,
                           minimum_commission_premium_cents=premium, rounded_weight=value / equity,
                           minimum_entry_capital_cents=int(np.ceil(minimum_value / weight)) if weight else None,
                           status="known_close_size" if legal else
                                  "below_minimum_entry_size" if weight else "explicit_zero_allocated_weight")
            case_rows.append(row)
        observed = [row for row in case_rows if row["legal_quantity"] is not None]
        unknown = len(case_rows) - len(observed)
        value = sum(row["notional_cents"] for row in observed)
        fee = sum(row["buy_fees_cents"] for row in observed)
        summaries.append({"case": case, "initial_cash_cents": equity, "target_count": len(targets),
                          "target_gross": sum(targets.values()), "unknown_targets": unknown,
                          "known_sized_names": sum(row["legal_quantity"] > 0 for row in observed),
                          "below_minimum_names": sum(row["legal_quantity"] == 0 for row in observed),
                          "targets_outside_existing_ths_scope": sum(row["board"] != "main" for row in case_rows),
                          "known_subtotal_notional_cents": value, "known_subtotal_buy_fees_cents": fee,
                          "minimum_commission_premium_cents": sum(row["minimum_commission_premium_cents"] for row in observed),
                          "rounded_gross": value / equity if not unknown else None,
                          "unallocated_target_weight": sum(targets.values()) - value / equity if not unknown else None,
                          "cash_after_close_cost_cents": equity - value - fee if not unknown else None,
                          "close_cost_fits_cash": value + fee <= equity if not unknown else None})
        rows.extend(case_rows)
    return pd.DataFrame(rows), summaries


def allocated_capacity_weights(weights, quotes, config, calculated_at):
    """Reuse the actual allocator on an unbound, nonpersisted capital scenario."""
    day = datetime.fromisoformat(calculated_at).date().isoformat()
    allocated, events = {}, []
    for case, targets in weights.items():
        state = new_state(config.initial_cash)
        context = RunContext("annual_quality_capacity_" + case, TradingMode.REPLAY)
        book = Ledger(state, config, context)
        intent = PortfolioIntent.full_target(targets, context=context, decided_at=calculated_at,
                                             source="saved_price_capacity_not_execution")
        result = allocate_close_lots(intent, book, day, quotes)
        if state["orders"] or state["fills"] or state["positions"] or state["cash"] != cents(config.initial_cash):
            raise ValueError("Capacity allocation cannot mutate cash/holdings or submit orders")
        allocated[case] = dict(result.weights)
        events.extend({"case": case, **event} for event in state["events"])
    return allocated, events


def integer_capacity_quantities(weights, quotes, config, calculated_at):
    """Existing attempt-mark allocator, using saved marks without simulating fills."""
    day = datetime.fromisoformat(calculated_at).date().isoformat()
    quantities, events = {}, []
    for case, targets in weights.items():
        state = new_state(config.initial_cash)
        context = RunContext("annual_quality_capacity_" + case, TradingMode.REPLAY)
        book = Ledger(state, config, context)
        for symbol in targets:
            book.mark_price(symbol, quote_value(quotes[symbol]["close"]), day)
        quantities[case] = allocate_open_lots(targets, book, day, quotes)
        if state["orders"] or state["fills"] or state["positions"] or state["cash"] != cents(config.initial_cash):
            raise ValueError("Integer capacity allocation cannot trade or mutate scenario cash/holdings")
        events.extend({"case": case, **event} for event in state["events"])
    return quantities, events


def capacity(prepared_run, *, allocation=None):
    if allocation not in {None, LOT_ALLOCATION, OPEN_LOT_ALLOCATION}:
        raise ValueError("Only the existing stock-lot capacity comparisons are supported")
    root = Path(prepared_run).resolve()
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    cases, _ = declared_definition(protocol)
    preview = json.loads((root / "preview_weights.json").read_text(encoding="utf-8"))
    config = StrategyConfig(**protocol["portfolio"]).validate()
    if (protocol["execution_admitted"] is not False
            or config != portfolio() or preview["mode"] != "known_market_preview_only"
            or preview["held"] != [] or preview["account_created"] is not False
            or list(preview["cases"]) != list(cases)):
        raise ValueError("Original fixed no-account annual quality preview required")
    rankings = pd.read_parquet(root / "preview_rankings.parquet")
    common_count = protocol["preview_counts"]["common_pool"]
    if type(common_count) is not int or common_count < config.retain_rank:
        raise ValueError("Original preview common pool is below the fixed rank buffer")
    for case in cases:
        candidates = rankings.loc[rankings.case.eq(case)].sort_values("rank")
        expected = min(config.retain_rank, (common_count + 1) // 2 if case == CASES[1] else common_count)
        if (len(candidates) != expected or candidates.symbol.duplicated().any()
                or not candidates.market_day.eq(protocol["preview_market_day"]).all()
                or candidates["rank"].tolist() != list(range(1, expected + 1))
                or choose_targets(candidates.to_dict("records"), [], config) != preview["cases"][case]):
            raise ValueError("Capacity preview must keep the original ranked targets")
    base = BulkResearchFeed(**protocol["stock_inputs"])
    allocated, quantities, events, control_summaries, calculated_at = None, None, [], None, None
    try:
        day = protocol["preview_market_day"]
        if day not in base.calendar:
            raise ValueError("Saved preview date is missing from the original price source")
        rows, summaries = capacity_rows(preview["cases"], base.rows.get(day, {}), day, config)
        if allocation is not None:
            if any(case["unknown_targets"] for case in summaries):
                raise ValueError("Unknown original raw prices/statuses prevent a matched allocation comparison")
            calculated_at = market_now().isoformat()
            if pd.Timestamp(calculated_at) < pd.Timestamp(protocol["declared_at"]):
                raise ValueError("Allocation comparison cannot precede its fixed financial/strategy declaration")
            control_summaries = summaries
            if allocation == OPEN_LOT_ALLOCATION:
                quantities, events = integer_capacity_quantities(preview["cases"], base.rows.get(day, {}), config, calculated_at)
                allocated = preview["cases"]
            else:
                allocated, events = allocated_capacity_weights(preview["cases"], base.rows.get(day, {}), config, calculated_at)
            rows, summaries = capacity_rows(allocated, base.rows.get(day, {}), day, config, quantities=quantities)
            rows["original_target_weight"] = [preview["cases"][row["case"]][row["symbol"]] for row in rows.to_dict("records")]
    finally:
        base.close()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    destination = root / "capacity_previews" / identifier
    destination.mkdir(parents=True, exist_ok=False)
    rows.to_csv(destination / "capacity.csv", index=False)
    if allocated is not None:
        write_json(destination / "allocated_weights.json", allocated)
        write_json(destination / "allocation_events.json", events)
        if quantities is not None:
            write_json(destination / "allocated_quantities.json", quantities)
    shutil.copy2(__file__, destination / "implementation.py")
    result = {"id": identifier, "prepared_run": str(root), "definition_id": protocol["definition_id"],
              "status": "completed_known_price_allocation_diagnostic" if allocation else "completed_known_price_capacity_diagnostic",
              "allocation_policy": allocation, "allocation_calculated_at": calculated_at,
              "control_cases": control_summaries, "transient_ledger_used": bool(allocation), "market_day": day,
              "declared_at": protocol["declared_at"], "financial_asof": protocol["feature_asof"],
              "portfolio": config.to_dict(), "cases": summaries, "account_created": False,
              "execution_admitted": False, "orders": 0, "fills": 0,
              "limitations": ["Frozen latest-known raw close, not a current quote or historical decision",
                              "Flat-holding capital scenario, not observed THS cash or positions",
                              "Engine target sizing, board minimum/steps and configured buy fees only",
                              "No next-open, slippage fill, volume queue, limit/status forecast or return claim",
                              "Original target identities/weights preserved; optional lot arm is separate, not a strategy switch",
                              "No target substitution, fee-band change, new persisted account or history replay",
                              "Existing THS main-board scope is not account permission or UI verification"]}
    if any(case["unknown_targets"] for case in summaries):
        result["status"] = "partial_known_price_capacity_diagnostic"
    write_json(destination / "result.json", result)
    out = ROOT / "outputs/annual_quality_portfolios" / protocol["id"] / "capacity" / identifier
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "run.json", result | {"artifacts": str(destination)})
    lines = ["# Saved-Price Capital Capacity", "", "NO_TRADE. " + result["status"],
             f"Raw close date: {day}. Scenario capital: {config.initial_cash:,.2f} CNY.", "",
             "| Case | Targets | Known sized | Below minimum | Unknown | Rounded gross | Buy fee CNY | Outside THS scope |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for case in summaries:
        gross = f"{case['rounded_gross']:.2%}" if case["rounded_gross"] is not None else "unknown"
        lines.append(f"|{case['case']}|{case['target_count']}|{case['known_sized_names']}|"
                     f"{case['below_minimum_names']}|{case['unknown_targets']}|{gross}|"
                     f"{case['known_subtotal_buy_fees_cents'] / 100:.2f}|{case['targets_outside_existing_ths_scope']}|")
    if allocation:
        lines += ["", "Allocation arm: " + allocation + "; calculation time: " + calculated_at,
                  "Saved September marks are inputs to a current diagnostic, NOT a September financial signal.",
                  ("Existing attempt-mark allocator's integer quantities; saved closes are NOT new session opens."
                   if allocation == OPEN_LOT_ALLOCATION else
                   "The allocator's close quantities are estimates; the original engine re-sizes its weights."), "",
                  "| Case | Original known sized | Allocated known sized | Original gross | Allocated gross |",
                  "|---|---:|---:|---:|---:|"]
        for control, case in zip(control_summaries, summaries):
            lines.append(f"|{case['case']}|{control['known_sized_names']}|{case['known_sized_names']}|"
                         f"{control['rounded_gross']:.2%}|{case['rounded_gross']:.2%}|")
    lines += ["", *["- " + line for line in result["limitations"]], "", "Artifacts: " + str(destination)]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)
    print(str(out / "REPORT.md"), flush=True)
    return destination, result


CALENDAR_CONFIRMATION_METHOD = "annual_quality_official_successor_without_future_raw_v1"


def confirm_session_successor(base, calendar_file):
    """Append one official session label after all physical source checks.

    Price rows, matrices, day_index and the source manifest remain unchanged.
    The following session confirms scheduling; it is never a synthetic bar.
    """
    from .leader_daily_signals import _calendar_view
    calendar_file = Path(calendar_file).resolve()
    data = calendar_file.read_bytes()
    payload = json.loads(data.decode("utf-8-sig"))
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    physical = list(base.calendar)
    if not physical or physical != sorted(set(physical)):
        raise ValueError("An ordered actual source calendar is required")
    cutoff = physical[-1]
    own_year = [day for day in physical if day[:4] == str(year)]
    if (status != "pinned_static_exchange_calendar" or cutoff[:4] != str(year)
            or not own_year or own_year != [day for day in sessions if own_year[0] <= day <= cutoff]
            or cutoff not in base.day_index or cutoff != base.manifest["end"]):
        raise ValueError("Official calendar must match every covered source session through the real cutoff")
    following = next((day for day in sessions if day > cutoff), None)
    if following is None or following in base.day_index:
        raise ValueError("An exact official successor label without a source bar is required")
    binding = {"method_id": CALENDAR_CONFIRMATION_METHOD,
        "calendar": {"path": str(calendar_file), "sha256": sha256(data).hexdigest()}}
    base.calendar = physical + [following]
    return binding, {"binding": binding, "physical_source_cutoff": cutoff,
        "successor_session": following, "successor_raw_bar_present": False,
        "source_manifest_changed": False, "price_rows_added": 0}


def advance(prepared_run, inputs=None, current=None, *, session_calendar=None):
    current = current or market_now()
    root = Path(prepared_run).resolve()
    with lock(root / "runner.lock"):
        protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
        head = json.loads((root / "head.json").read_text(encoding="utf-8"))
        if head.get("session_calendar_binding") is not None and session_calendar is None:
            raise ValueError("Continue the existing explicit session-calendar method; never silently disable it")
        cases, investment = declared_definition(protocol)
        if (protocol["execution_admitted"] is not False
                or head["execution_admitted"] is not False or head["id"] != protocol["id"]
                or protocol.get("lot_allocation") not in {None, OPEN_LOT_ALLOCATION}
                or head.get("lot_allocation") != protocol.get("lot_allocation")
                or [row["signal"] for row in head["results"]] != list(cases)):
            raise ValueError("Original unadmitted fixed annual quality comparison required")
        allocation_options = ({"lot_allocation": protocol["lot_allocation"]}
                              if protocol.get("lot_allocation") else {})
        config = StrategyConfig(**protocol["portfolio"]).validate()
        if config != portfolio():
            raise ValueError("Original portfolio/costs required")
        inputs = dict(inputs or protocol["stock_inputs"])
        price = json.loads((Path(inputs["price_view"]) / "result.json").read_text(encoding="utf-8"))
        if price["status"] != "research_reconstruction_completed" or price["cutoff"] > current.date().isoformat():
            raise ValueError("Actual completed nonfuture research price snapshot required")
        frozen_day = pd.Timestamp(protocol["declared_at"]).tz_convert("Asia/Hong_Kong").date().isoformat()
        if price["cutoff"] <= frozen_day:
            head.update(status="waiting_for_post_declaration_market_data", reason="Actual supplied market data ends " + price["cutoff"] + "; no subsequent sessions/orders/fills.")
            publish_head(root, head)
            report(root, protocol, head)
            return root, head
        base = BulkResearchFeed(**inputs)
        try:
            calendar_binding, calendar_confirmation = None, None
            if session_calendar is not None:
                calendar_binding, calendar_confirmation = confirm_session_successor(base, session_calendar)
                if head.get("session_calendar_binding", calendar_binding) != calendar_binding:
                    raise ValueError("The saved annual session-calendar binding changed")
            available = incoming_dates(base.calendar, protocol["preview_market_day"], frozen_day, current)
            if not available:
                head.update(status="waiting_for_complete_session_and_calendar", reason="No subsequent complete exchange session with following calendar confirmation.")
                publish_head(root, head)
                report(root, protocol, head)
                return root, head
            end = available[-1]
            if end > protocol["end"]:
                raise ValueError("Cannot extend beyond the original finite comparison horizon")
            dates = available
            if head["sessions"] >= len(dates):
                return root, head
            annual, quality = load_quality(protocol["quality_run"], protocol["report_day"], investment)
            if quality["id"] != protocol["feature_id"] or quality["asof"] != protocol["feature_asof"]:
                raise ValueError("Frozen financial inputs changed")
            validate_extension(protocol["manifest"], base.manifest, protocol["preview_market_day"])
            if calendar_binding is not None:
                head["session_calendar_binding"] = calendar_binding
            prior = head.get("last_batch")
            previous = {row["signal"]: row for row in head["results"]}
            recovered = {}
            if prior:
                before = json.loads((Path(prior) / "run.json").read_text(encoding="utf-8"))
                if before.get("lot_allocation") != protocol.get("lot_allocation"):
                    raise ValueError("Saved batch lot allocation differs from its declaration")
                validate_extension(before["manifest"], base.manifest, before["end"])
            pending = head.get("pending_case")
            if pending:
                if pending["signal"] not in cases:
                    raise ValueError("Unknown saved pending case")
                destination = Path(pending["destination"]).resolve()
                destination.relative_to(root / "batches")
                receipt = destination / "replay.json"
                if receipt.exists():
                    terminal = json.loads(receipt.read_text(encoding="utf-8"))
                    if terminal["status"] not in {"completed", "failed", "interrupted"}:
                        raise ValueError("Pending case is not authoritatively terminal; do not restart")
                    recovered[pending["signal"]] = load_replay_checkpoint(destination, config, dates, **allocation_options)
                    previous[pending["signal"]] = {"signal": pending["signal"], "checkpoint": str(destination),
                                                   "completed_sessions": terminal["completed_sessions"], "status": terminal["status"]}
                    head["account_created"] = True
                elif destination.exists():
                    raise ValueError("Pending case has files but no terminal receipt; reconciliation required")
                head.pop("pending_case")
                head["results"] = [previous[name] for name in cases]
                head["sessions"] = min(row["completed_sessions"] for row in head["results"])
                publish_head(root, head)
            decision_days = sorted({dates[0], *[day for day in month_ends(base.calendar, end) if day in dates]})
            values = features(base, end, feature_days=decision_days)
            batch = root / "batches" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            batch.mkdir(parents=True, exist_ok=False)
            shutil.copy2(__file__, batch / "implementation.py")
            record = {"status": "running", "received_at": current.isoformat(), "manifest": base.manifest,
                      "end": end, "results": [], "lot_allocation": protocol.get("lot_allocation"),
                      "execution_admitted": False}
            if calendar_confirmation is not None:
                record["calendar_confirmation"] = calendar_confirmation
            write_json(batch / "run.json", record)
            for case in cases:
                saved = previous.get(case, {})
                checkpoint_path = saved.get("checkpoint")
                checkpoint = (recovered[case] if case in recovered else
                              load_replay_checkpoint(Path(checkpoint_path), config, dates, **allocation_options) if checkpoint_path else None)
                feed = AnnualQualityFeed(base, values, annual, case, end, protocol["declared_at"], protocol["feature_asof"], investment)
                head.update(last_batch=str(batch), pending_case={"signal": case, "destination": str(batch / case)})
                publish_head(root, head)
                result = baseline_ledger_result(feed, config, dates, batch / case, checkpoint=checkpoint,
                                                **allocation_options)
                pd.DataFrame(feed.audit).to_csv(batch / (case + "_pool_counts.csv"), index=False)
                pd.DataFrame(feed.ranking_rows).to_parquet(batch / (case + "_rankings.parquet"), index=False)
                record["results"].append({"signal": case, "checkpoint": str(batch / case), **result})
                write_json(batch / "run.json", record)
                previous[case] = record["results"][-1]
                # Carry every unfinished case's old cursor if this batch is interrupted.
                carried = [previous.get(name, {"signal": name, "completed_sessions": 0, "status": "not_started"}) for name in cases]
                head.update(status="partial", last_batch=str(batch), results=carried,
                            sessions=min(row["completed_sessions"] for row in carried), account_created=True)
                head.pop("pending_case")
                publish_head(root, head)
            record["status"] = "completed" if all(row["status"] == "completed" for row in record["results"]) else "partial"
            write_json(batch / "run.json", record)
            head.update(status=record["status"], reason=None)
            publish_head(root, head)
            report(root, protocol, head)
            return root, head
        finally:
            base.close()


def _saved_annual_run(identifier):
    if not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", identifier):
        raise ValueError("A saved annual research identifier is required")
    parent = (DATA_ROOT / "research/annual_quality_portfolios").resolve()
    root = (parent / identifier).resolve()
    root.relative_to(parent)
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    head = json.loads((root / "head.json").read_text(encoding="utf-8"))
    cases, _ = declared_definition(protocol)
    if (protocol["id"] != identifier or head["id"] != identifier
            or protocol["execution_admitted"] is not False or head["execution_admitted"] is not False
            or [row["signal"] for row in head["results"]] != list(cases)
            or protocol.get("lot_allocation") != head.get("lot_allocation")
            or protocol.get("lot_allocation") not in {None, OPEN_LOT_ALLOCATION}):
        raise ValueError("Saved annual declaration/head mismatch")
    value = {key: protocol[key] for key in ("id", "declared_at", "feature_asof", "report_day", "preview_market_day", "preview_counts", "portfolio")}
    value.update({key: head[key] for key in ("status", "sessions", "account_created", "execution_admitted", "results")})
    value.update(lot_allocation=protocol.get("lot_allocation"), reason=head.get("reason"),
                 artifacts=str(root), definition_id=protocol["definition_id"], signals=list(cases))
    return root, value


def saved_catalog():
    parent = DATA_ROOT / "research/annual_quality_portfolios"
    if not parent.exists():
        return []
    return [_saved_annual_run(path.name)[1] for path in sorted(parent.iterdir(), reverse=True)
            if path.is_dir() and (path / "protocol.json").is_file() and (path / "head.json").is_file()]


def saved_detail(identifier, case):
    from .annual_exposures import saved_exposures
    root, value = _saved_annual_run(identifier)
    if case not in value["signals"]:
        raise ValueError("A declared annual research case is required")
    preview = json.loads((root / "preview_weights.json").read_text(encoding="utf-8"))
    if preview["mode"] != "known_market_preview_only" or preview["account_created"] is not False:
        raise ValueError("Saved targets are not a known-market preview")
    rankings = pd.read_parquet(root / "preview_rankings.parquet")
    rankings = rankings.loc[rankings.case.eq(case)].sort_values("rank")
    def records(frame):
        return frame.astype(object).where(pd.notna(frame), None).to_dict("records")
    capacities = []
    parent = root / "capacity_previews"
    if parent.exists():
        for path in sorted(parent.iterdir(), reverse=True):
            if not path.is_dir() or not (path / "result.json").is_file():
                continue
            path.resolve().relative_to(root)
            receipt = json.loads((path / "result.json").read_text(encoding="utf-8"))
            if (receipt["definition_id"] != value["definition_id"] or Path(receipt["prepared_run"]).resolve() != root
                    or receipt["account_created"] is not False or receipt["execution_admitted"] is not False
                    or receipt["orders"] != 0 or receipt["fills"] != 0):
                raise ValueError("Saved capacity is not this unbound diagnostic")
            summary = next(row for row in receipt["cases"] if row["case"] == case)
            slots = pd.read_csv(path / "capacity.csv")
            capacities.append({"id": receipt["id"], "status": receipt["status"],
                "allocation_policy": receipt.get("allocation_policy"),
                "calculated_at": receipt.get("allocation_calculated_at"),
                "summary": summary, "slots": records(slots.loc[slots.case.eq(case)])})
    return value | {"case": case, "weights": preview["cases"][case],
                    "rankings": records(rankings), "capacities": capacities,
                    "exposures": saved_exposures(root, case)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--quality-run", type=Path, required=True)
    prep.add_argument("--source-run", type=Path, required=True)
    prep.add_argument("--report-day", required=True)
    prep.add_argument("--allocation", choices=[OPEN_LOT_ALLOCATION])
    prep.add_argument("--comparison", choices=["quality", "quality-investment"], default="quality")
    replay = commands.add_parser("advance")
    replay.add_argument("--prepared-run", type=Path, required=True)
    replay.add_argument("--inputs", type=Path)
    replay.add_argument("--session-calendar", type=Path)
    sizing = commands.add_parser("capacity")
    sizing.add_argument("--prepared-run", type=Path, required=True)
    sizing.add_argument("--allocation", choices=[LOT_ALLOCATION, OPEN_LOT_ALLOCATION])
    exposure = commands.add_parser("exposures")
    exposure.add_argument("--prepared-run", type=Path, required=True)
    exposure.add_argument("--capacity-run", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(args.quality_run, args.source_run, args.report_day, allocation=args.allocation,
                comparison=INVESTMENT_ID if args.comparison == "quality-investment" else ID)
        return 0
    if args.command == "capacity":
        _, result = capacity(args.prepared_run, allocation=args.allocation)
        return 1 if result["status"].startswith("partial") else 0
    if args.command == "exposures":
        from .annual_exposures import build
        build(args.prepared_run, args.capacity_run)
        return 0
    inputs = json.loads(args.inputs.read_text(encoding="utf-8")) if args.inputs else None
    root, result = advance(args.prepared_run, inputs, session_calendar=args.session_calendar)
    print(json.dumps({"artifacts": str(root), "status": result["status"], "sessions": result["sessions"],
                      "account_created": result["account_created"]}), flush=True)
    return 1 if result["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
