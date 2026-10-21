"""Pinned mixed stock/fund diagnostic snapshot with calculated fields."""

import argparse
from ast import literal_eval
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import uuid
from xml.etree import ElementTree
from zipfile import ZipFile

import pandas as pd

from .etf_reconciliation import archived, cash_events, distribution_coverage
from .fund_dayk import load as load_dayk, number
from .experiment import replay
from ..config import StrategyConfig
from ..contracts import Price, PriceBasis, SignalFrame
from ..feed import HistoricalFeed
from ..market_dataset import write_json
from ..report import metrics as portfolio_metrics
from ..rules import (ETF_PROFILE, FUND_PROFILES, FUND_H1_2026_PROFILES,
                     FUND_Q3_2026_PROFILES, execution_rule)
from ..settings import DATA_ROOT


CODE, SYMBOL = "510300", "510300.SSE"
SCOPE = "mixed_calculated_fund_reference_historical_diagnostic_not_execution_admission"
FUND_SCOPE = "fund_only_calculated_reference_historical_diagnostic_not_execution_admission"
FIRST, LAST = "2023-01-03", "2025-12-31"
START = "2024-01-02"
RULE_REPLACEMENT_DATE = "2026-07-06"
RULE_NOTICE_URL = "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml"
RULE_2026_URL = "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/10816482/files/959da0158c65434daa8a43a6e32be7ba.docx"
RULE_DEFERRED_URL = "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/10816482/files/f6bd756cdd8248f49b7795f62e73237f.docx"
FUND_RULE_CLAUSES = {"2.4.2", "3.1.4", "3.1.5", "3.3.8", "3.3.9", "3.3.11", "3.3.13"}


def pinned_fund_calendar(folder, last):
    folder = Path(folder)
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if (receipt.get("query") != "query_trade_dates" or receipt.get("execution_admitted") is not False
            or not receipt["start"] <= FIRST <= last <= receipt["end"]):
        raise ValueError("Fund calendar requires the pinned provider trade-date query")
    provider = pd.read_csv(BytesIO(archived(folder, {"file": "provider_calendar.csv",
                                                  "sha256": receipt["provider_calendar_sha256"]})), dtype=str)
    calendar = pd.read_csv(BytesIO(archived(folder, {"file": "calendar.csv",
                                                  "sha256": receipt["calendar_sha256"]})), dtype=str).date.tolist()
    natural = pd.date_range(receipt["start"], receipt["end"]).strftime("%Y-%m-%d").tolist()
    if (provider.calendar_date.tolist() != natural or not provider.is_trading_day.isin(["0", "1"]).all()
            or calendar != provider.loc[provider.is_trading_day.eq("1"), "calendar_date"].tolist()
            or len(calendar) != receipt["sessions"] or not calendar
            or calendar[0] != receipt["first"] or calendar[-1] != receipt["last"]):
        raise ValueError("Fund calendar differs from the complete observed provider dates")
    return [day for day in calendar if FIRST <= day <= last], receipt


def rule_document_text(content):
    with ZipFile(BytesIO(content)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return ["".join(part.text or "" for part in paragraph.iter(ns + "t"))
            for paragraph in root.iter(ns + "p")]


def reviewed_fund_rule(reports, review, symbol, last, profiles):
    """Review receipts gate dated profiles; cash coverage remains a separate requirement."""
    rule = execution_rule(symbol, START, profiles)
    execution_rule(symbol, last, profiles)
    if symbol != SYMBOL or review is not None:
        if (not isinstance(review, dict) or review.get("instrument_type") != rule.fund_type
                or review.get("profile") != profiles[symbol]
                or not archived(reports, {"file": review["source_file"],
                                         "sha256": review["source_sha256"]}).startswith(b"%PDF-")):
            raise ValueError("Fund identity/listing/profile requires its reviewed issuer report")
    if profiles[symbol] != FUND_PROFILES[symbol]:
        if not isinstance(review, dict):
            raise ValueError("Extended fund profiles require reviewed exchange-rule evidence")
        span = review.get("rule_span", [])
        notice = review.get("rule_replacement_notice", {})
        if (len(span) != 2 or not span[0] <= START <= last <= span[1]
                or notice.get("effective_date") != RULE_REPLACEMENT_DATE
                or not notice.get("source_url", "").startswith("https://www.sse.com.cn/")
                or not archived(reports, {"file": notice["source_file"], "sha256": notice["source_sha256"]})
                or not archived(reports, {"file": review["rule_source_file"],
                                          "sha256": review["rule_source_sha256"]}).startswith(b"%PDF-")):
            raise ValueError("Extended fund profiles require reviewed pre-replacement exchange-rule evidence")
        if profiles[symbol] == FUND_Q3_2026_PROFILES[symbol]:
            replacement = review.get("replacement_rule", {})
            required = FUND_RULE_CLAUSES | ({"6.6"} if symbol == "588000.SSE" else set())
            if (notice.get("source_url") != RULE_NOTICE_URL
                    or review.get("rule_eras") != [["2024-01-01", "2026-07-05"],
                                                   [RULE_REPLACEMENT_DATE, "2026-09-30"]]
                    or replacement.get("effective_date") != RULE_REPLACEMENT_DATE
                    or replacement.get("source_url") != RULE_2026_URL
                    or replacement.get("deferred_source_url") != RULE_DEFERRED_URL
                    or not required <= set(replacement.get("reviewed_clauses", []))
                    or replacement.get("reviewed_deferred_scope") != "none_of_reviewed_secondary_fund_auction_clauses"):
                raise ValueError("Q3 fund profiles require both dated rule eras and reviewed replacement/deferred clauses")
            paragraphs = rule_document_text(archived(reports, {
                "file": replacement["source_file"], "sha256": replacement["source_sha256"]}))
            if not all(any(text.startswith(clause + " ") or text.startswith(clause + "\u3000")
                           or text.startswith(clause + "\t") for text in paragraphs) for clause in required):
                raise ValueError("Replacement rule archive lacks the reviewed fund clauses")
            if not rule_document_text(archived(reports, {
                    "file": replacement["deferred_source_file"], "sha256": replacement["deferred_source_sha256"]})):
                raise ValueError("Deferred-rule archive is empty")
        elif last >= RULE_REPLACEMENT_DATE:
            raise ValueError("Pre-replacement fund profile must not extend beyond the old rule era")
    if rule.same_day_resale:
        if (not isinstance(review, dict) or not review.get("rule_source_file") or not review.get("rule_source_sha256")
                or not archived(reports, {"file": review["rule_source_file"],
                                          "sha256": review["rule_source_sha256"]}).startswith(b"%PDF-")):
            raise ValueError("Same-day resale requires its pinned historical exchange rule")
    return rule


def calculated_bars(frame, events, calendar, code=CODE):
    """Never relabel provider qfq/status: compute a separate diagnostic series."""
    if (calendar != sorted(set(calendar)) or not calendar
            or frame.empty or not frame.code.eq(code).all() or frame.date.duplicated().any()
            or events.ex_date.duplicated().any()):
        raise ValueError("Invalid fund source identity/calendar/actions")
    source = frame.sort_values("date").set_index("date")
    if not set(calendar) <= set(source.index):
        raise ValueError("A missing fund session must not be filled or skipped")
    preceding = source.loc[source.index < calendar[0]]
    if preceding.empty:
        raise ValueError("The first reference requires an observed preceding close")
    selected = source.loc[calendar]
    by_date = {row["ex_date"]: Decimal(row["cash_rmb_per_unit"])
               for row in events.to_dict("records") if calendar[0] <= row["ex_date"] <= calendar[-1]}
    if not set(by_date) <= set(calendar):
        raise ValueError("Cash ex-date is not in the pinned exchange calendar")
    prior, raw, factors = Decimal(preceding.iloc[-1]["close"]), [], {}
    for day, row in selected.iterrows():
        cash = by_date.get(day, Decimal(0))
        if not cash.is_finite() or cash < 0 or prior <= cash:
            raise ValueError("Invalid fund cash/reference calculation")
        reference = (prior - cash).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        volume, amount = number(row["volume"]), number(row["field7_amount_candidate"])
        prices = {field: float(Price.from_rmb(row[field], PriceBasis.RAW, "mill").rmb)
                  for field in ("open", "high", "low", "close")}
        if volume <= 0 or volume != int(volume) or amount <= 0:
            raise ValueError("Daily-activity diagnosis requires observed positive volume and amount")
        vwap = amount / volume
        if not Decimal(str(prices["low"])) <= vwap <= Decimal(str(prices["high"])):
            raise ValueError("Fund field7 amount candidate conflicts with observed OHLC/volume")
        raw.append({"date": day, "symbol": code + ".SSE", **prices, "preclose": float(reference),
                    "volume": int(volume), "amount": float(amount), "tradestatus": 1, "isST": 0,
                    "adjustflag": "3"})
        if cash:
            factors[day] = reference / prior
        prior = Decimal(str(row["close"]))
    raw = pd.DataFrame(raw)
    adjusted = raw.copy()
    factor = Decimal(1)
    factor_by_day = {}
    for day in reversed(calendar):
        factor_by_day[day] = factor
        factor *= factors.get(day, Decimal(1))
    for field in ("open", "high", "low", "close", "preclose"):
        adjusted[field] = [float(Decimal(str(value)) * factor_by_day[day])
                           for day, value in zip(raw.date, raw[field])]
    adjusted["adjustflag"] = "2"
    provenance = {"reference": "mill-rounded observed prior close minus reviewed per-unit cash",
                  "adjustment": "computed forward cash ex-reference ratio chain; not provider flag2",
                  "adjustment_factors": {day: str(value) for day, value in factors.items()},
                  "status": "tradestatus1 denotes observed positive daily activity; intraday halts unverified",
                  "isST": "zero is a non-ordinary-stock diagnostic marker, not normalization of provider isST1",
                  "amount": "SSE day-K field7 RMB candidate; only Sep30 named snapshot independently matched",
                  "first_preceding_date": preceding.index[-1],
                  "first_preceding_close": str(preceding.iloc[-1]["close"]),
                  "calculated_not_published_reference": True}
    return raw, adjusted, provenance


def build(stock, dayk, sina, reports, out=None, codes=None, *, end=None, profiles=None, calendar_source=None):
    dayk, sina, reports = map(Path, (dayk, sina, reports))
    if (stock is None) == (calendar_source is None):
        raise ValueError("Choose either the stock snapshot or the pinned fund-only calendar")
    base = HistoricalFeed(Path(stock)) if stock is not None else None
    codes = list(codes) if codes is not None else [CODE]
    symbols = [code + ".SSE" for code in codes]
    last = LAST if end is None else end
    if not isinstance(last, str) or date.fromisoformat(last).isoformat() != last or last < START:
        raise ValueError("Expected a canonical diagnostic cutoff after its fixed start")
    profiles = FUND_PROFILES if profiles is None else profiles
    if (not codes or len(codes) != len(set(codes)) or not set(symbols) <= set(FUND_PROFILES)):
        raise ValueError("Only explicitly reviewed fund profiles may enter this diagnostic")
    calendar_receipt = None
    if base is not None:
        if (getattr(base, "instrument_profiles", {}) or set(symbols) & set(base.instruments)
                or base.manifest["start"] > START or base.manifest["end"] < last):
            raise ValueError("Expected a stock-only snapshot covering the fixed diagnostic period")
        days = [day for day in base.calendar if FIRST <= day <= last]
    else:
        calendar_source = Path(calendar_source)
        days, calendar_receipt = pinned_fund_calendar(calendar_source, last)
    if not days or days[0] != FIRST or days[-1] != last or START not in days:
        raise ValueError("Pinned source calendar does not cover the declared diagnostic/warmup")
    official, receipt = load_dayk(dayk)
    reviews_path = reports / "instrument_reviews.json"
    reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {}
    funds, coverages, provenances, cash, instruments = [], {}, {}, [], []
    for code, symbol in zip(codes, symbols):
        if symbol not in profiles:
            raise ValueError("Each declared fund requires an explicit reviewed profile")
        review = reviews.get(code)
        rule = reviewed_fund_rule(reports, review, symbol, last, profiles)
        events = cash_events(sina, code)
        coverage = distribution_coverage(reports, events, FIRST, last, code)
        if not coverage["complete_for_requested_period"]:
            raise ValueError("Reviewed distributions do not cover the entire diagnostic/warmup period")
        ipo = review["ipoDate"] if review else "2012-05-28"
        raw, adjusted, provenance = calculated_bars(official.loc[official.code.eq(code)], events, days, code)
        funds.append((raw, adjusted))
        coverages[code], provenances[code] = coverage, provenance
        cash.extend(events.loc[events.ex_date.between(FIRST, last)].to_dict("records"))
        instruments.append({"symbol": symbol, "board": "", "instrument_type": rule.fund_type,
                            "ipoDate": ipo, "outDate": ""})
    fund_raw = pd.concat([pair[0] for pair in funds], ignore_index=True)
    fund_adjusted = pd.concat([pair[1] for pair in funds], ignore_index=True)
    actions = []
    kind = "mixed_fund_diagnostic" if base is not None else "fund_diagnostic"
    out = Path(out) if out else DATA_ROOT / f"datasets/{kind}/snapshots" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    stock_manifest = base.manifest if base is not None else {}
    manifest = deepcopy(stock_manifest)
    manifest.update(status="validating", folder=str(out.resolve()), scope=SCOPE if base is not None else FUND_SCOPE,
                    replay_only=True,
                    source=("pinned stock sources plus SSE fund day-K and explicitly calculated fields" if base is not None else
                            "pinned provider trading calendar plus SSE fund day-K and explicitly calculated fields"),
                    created_at=datetime.now().astimezone().isoformat(),
                    start=START, end=last, selection_date="2023-12-29", execution_admitted=False,
                    strategy_validated=False, live_ready=False, selection_is_point_in_time=False,
                    instrument_profiles={s: profiles[s] for s in symbols},
                    symbols=sorted((base.symbols if base is not None else []) + symbols),
                    fund_calculated_fields=provenances,
                    fund_sources={"dayk_folder": str(dayk.resolve()),
                                  "receipt_sha256": sha256((dayk / "result.json").read_bytes()).hexdigest(),
                                  "receipts": [item for item in receipt["receipts"] if item["code"] in codes],
                                  "cash_events_sha256": sha256((sina / "supplements/cash_events.csv").read_bytes()).hexdigest(),
                                  "distribution_coverage": coverages, "instrument_reviews": reviews},
                    limitations=stock_manifest.get("limitations", []) + [
                        "Historical diagnostic only; shared engine rejects paper/live mode",
                        "Explicit reviewed funds, not full ETF market or strategy eligibility",
                        "Fund reference and forward-adjustment are calculated, not published histories",
                        "Positive daily activity is not proof of no intraday halt or open-auction fill availability",
                        "Primary distribution coverage does not independently establish every fund share action",
                        "Observed SSE OHLC prices retained; field7 amount remains a historical candidate",
                        ("Stock sample is acquired non-PIT subset; no untouched validation period claimed" if base is not None else
                         "Fund-only snapshot has no stock prices/actions; not an all-market equity dataset"),
                        "Observed provider trading dates are not an independent exchange-calendar admission"])
    if base is not None:
        manifest["stock_source"] = {"folder": str(base.folder.resolve()),
                                    "manifest_sha256": sha256((base.folder / "manifest.json").read_bytes()).hexdigest()}
    else:
        manifest["calendar_source"] = {"folder": str(calendar_source.resolve()), "receipt": calendar_receipt,
                                       "receipt_sha256": sha256((calendar_source / "receipt.json").read_bytes()).hexdigest()}
    manifest["historical_price_adjustments"] = [row for row in stock_manifest.get("historical_price_adjustments", [])
                                                if row["date"] <= last]
    write_json(out / "manifest.json", manifest)
    try:
        if base is not None:
            for name in ("raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv"):
                manifest["stock_source"].setdefault("file_hashes", {})[name] = sha256((base.folder / name).read_bytes()).hexdigest()
            stock_raw = base.raw.loc[base.raw.date.between(FIRST, last)]
            stock_adjusted = base.adjusted.loc[base.adjusted.date.between(FIRST, last)]
            raw = pd.concat([stock_raw, fund_raw], ignore_index=True)
            adjusted = pd.concat([stock_adjusted, fund_adjusted], ignore_index=True)
            stock_instruments = pd.DataFrame.from_dict(base.instruments, orient="index").rename_axis("symbol").reset_index()
        else:
            stock_raw = pd.DataFrame()
            raw, adjusted = fund_raw, fund_adjusted
            stock_instruments = pd.DataFrame(columns=["symbol", "board", "instrument_type", "ipoDate", "outDate"])
            target = out / "calendar_evidence"
            target.mkdir()
            for name in ("provider_calendar.csv", "calendar.csv", "receipt.json"):
                shutil.copy2(calendar_source / name, target / name)
        raw.to_parquet(out / "raw.parquet", index=False)
        adjusted.to_parquet(out / "adjusted.parquet", index=False)
        pd.DataFrame({"date": days}).to_csv(out / "calendar.csv", index=False)
        pd.concat([stock_instruments, pd.DataFrame(instruments)], ignore_index=True).fillna("").to_csv(
            out / "instruments.csv", index=False)
        for row in cash:
            target = out / row["source_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archived(sina, {"file": row["source_file"], "sha256": row["source_sha256"]}))
            actions.append({"symbol": row["instrument_code"] + ".SSE", "dividRegistDate": row["record_date"],
                            "dividOperateDate": row["ex_date"], "dividPayDate": row["pay_date"],
                            "dividCashPsBeforeTax": row["cash_rmb_per_unit"], "dividStocksPs": "0",
                            "dividReserveToStockPs": "0", "source_file": row["source_file"],
                            "source_url": row["source_url"], "source_sha256": row["source_sha256"]})
        stock_actions = [row for row in base.actions if row["dividOperateDate"] <= last] if base is not None else []
        action_rows = stock_actions + actions
        columns = None if action_rows else ["symbol", "dividRegistDate", "dividOperateDate", "dividPayDate",
                                            "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
        pd.DataFrame(action_rows, columns=columns).fillna("").to_csv(out / "actions.csv", index=False)
        for row in stock_manifest.get("corporate_action_supplements", []) + manifest["historical_price_adjustments"]:
            for field in ("source_file", "holder_source_file"):
                if row.get(field):
                    target = out / row[field]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(base.folder / row[field], target)
        for coverage in coverages.values():
            for period in coverage["reviewed_periods"]:
                if period.get("source_kind") in {"gt_complete_cash_dividend_history_v1", "chinaamc_complete_cash_dividend_history_v1",
                                               "huaan_public_distribution_table_v1"}:
                    if period["source_kind"] == "gt_complete_cash_dividend_history_v1":
                        from .gt_fund_history import load as load_history
                    elif period["source_kind"] == "chinaamc_complete_cash_dividend_history_v1":
                        from .chinaamc_fund_history import load as load_history
                    else:
                        from .huaan_fund_history import load as load_history
                    folder = reports / period["history_folder"]
                    history = load_history(folder)
                    target = out / period["history_folder"]
                    target.mkdir(parents=True, exist_ok=True)
                    for receipt in history["receipts"].values():
                        (target / receipt["file"]).write_bytes(archived(folder, receipt))
                    shutil.copy2(folder / "result.json", target / "result.json")
                    continue
                target = out / "source_reports" / Path(period["source_file"]).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archived(reports, {"file": period["source_file"], "sha256": period["source_sha256"]}))
        for code in codes:
            review = reviews.get(code, {})
            if review.get("rule_source_file"):
                target = out / review["rule_source_file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archived(reports, {"file": review["rule_source_file"],
                                                    "sha256": review["rule_source_sha256"]}))
            notice = review.get("rule_replacement_notice")
            if notice:
                target = out / notice["source_file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archived(reports, {"file": notice["source_file"],
                                                    "sha256": notice["source_sha256"]}))
            replacement = review.get("replacement_rule")
            if replacement:
                for prefix in ("", "deferred_"):
                    target = out / replacement[prefix + "source_file"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archived(reports, {"file": replacement[prefix + "source_file"],
                                                        "sha256": replacement[prefix + "source_sha256"]}))
        manifest.update(status="ready", rows=len(stock_raw) + len(fund_raw),
                        fund_rows=len(fund_raw), action_rows=len(stock_actions) + len(actions),
                        fund_action_rows=len(actions), stock_symbols=len(base.symbols) if base is not None else 0,
                        fund_symbols=len(codes))
        manifest["file_hashes"] = {name: sha256((out / name).read_bytes()).hexdigest() for name in (
            "raw.parquet", "adjusted.parquet", "calendar.csv", "instruments.csv", "actions.csv")}
        write_json(out / "manifest.json", manifest)
        HistoricalFeed(out)
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(out / "manifest.json", manifest)
        raise
    return out, manifest


class FixedMixedProbeFeed:
    """Fixed component roles exercise both units, never rank the stock universe."""
    def __init__(self, feed, symbols=None):
        self.feed = feed
        self.components = tuple(symbols) if symbols is not None else ("600000.SSE", SYMBOL)

    def __getattr__(self, name):
        return getattr(self.feed, name)

    def signal_frame(self, day, config, context):
        rows = self.feed.rows.get(day, {})
        candidates = [{"symbol": symbol, "score": 1.0} for symbol in self.components
                      if symbol in rows and rows[symbol]["tradestatus"] == 1 and rows[symbol]["isST"] == 0]
        return SignalFrame.from_legacy(candidates, day, "fixed_mixed_unit_account_diagnostic", context)


def saved_probe_checks(folder, feed, config, metrics, symbols=None):
    folder = Path(folder)
    symbols = tuple(symbols) if symbols is not None else ("600000.SSE", SYMBOL)
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    receipt = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
    if receipt["status"] != "completed":
        raise ValueError("Ledger probe did not complete")
    # CSV list cells are text; treating even "[]" as truthy invents stale days.
    daily = pd.read_csv(folder / "daily.csv", converters={"stale_symbols": literal_eval})
    if not daily.stale_symbols.map(lambda value: isinstance(value, list) and all(isinstance(s, str) for s in value)).all():
        raise ValueError("Saved stale-valuation cells are not symbol lists")
    observed_metrics = portfolio_metrics(daily.to_dict("records"), config.initial_cash)
    if any(not math.isfinite(value) or not math.isfinite(metrics[key]) or abs(value - metrics[key]) > 1e-10
           for key, value in observed_metrics.items()):
        raise ValueError("Saved daily marks do not reproduce the reported metrics")
    fills = list(account["fills"].values())
    checks = [{"check": "saved_daily_metrics_recomputed", "passed": True, "metrics": observed_metrics}]
    cash = round(config.initial_cash * 100)
    for fill in fills:
        notional = fill.get("notional", fill["qty"] * fill["price"])
        cash += notional * (1 if fill["side"] == "sell" else -1) - fill["fees"]
    cash += sum(event["cash"] for event in account["events"] if event["kind"] == "dividend_paid")
    checks.append({"check": "cash_from_fills_fees_and_paid_dividends", "residual_cents": account["cash"] - cash})
    positions = sum(round(sum(lot["qty"] for lot in lots) * account["prices"][symbol] /
                          (10 if account.get("price_units", {}).get(symbol) == "mill" else 1))
                    for symbol, lots in account["positions"].items())
    equity = account["cash"] + positions + sum(account["receivables"].values())
    checks.append({"check": "final_cash_positions_receivables_identity", "residual_cents":
                   round(metrics["final_equity"] * 100) - equity})
    units = {symbol: sorted({fill.get("price_unit", "cent") for fill in fills if fill["symbol"] == symbol})
             for symbol in symbols}
    expected = {s: [execution_rule(s, feed.manifest["start"], feed.instrument_profiles).price_unit] for s in symbols}
    if units != expected:
        raise ValueError("Actual probe did not exercise every requested quote-unit profile")
    checks.append({"check": "requested_quote_profiles_exercised", "units": units, "passed": True})
    settled = {}
    for fill in fills:
        lots = settled.setdefault(fill["symbol"], [])
        rule = execution_rule(fill["symbol"], fill["day"], feed.instrument_profiles)
        available = fill["day"] if rule.same_day_resale else feed.next_day(fill["day"])
        if fill.get("available_day", available) != available:
            raise ValueError("Saved fill sellability does not match its declared profile")
        if fill["side"] == "buy":
            lots.append({"qty": fill["qty"], "available": available})
        else:
            remaining = fill["qty"]
            for lot in lots:
                if lot["available"] <= fill["day"]:
                    taken = min(lot["qty"], remaining)
                    lot["qty"] -= taken
                    remaining -= taken
            if remaining:
                raise ValueError("Saved sale exceeds prior settled purchases")
    if any(sum(lot["qty"] for lot in lots) != sum(lot["qty"] for lot in account["positions"].get(symbol, []))
           for symbol, lots in settled.items()):
        raise ValueError("Probe has non-fill share movements requiring independent reconciliation")
    checks.append({"check": "sales_and_final_quantities_from_profile_resale_rules", "passed": True,
                   "same_day_resale_fills": sum(execution_rule(f["symbol"], f["day"], feed.instrument_profiles).same_day_resale
                                                for f in fills)})
    distribution_checks = []
    for action in feed.actions:
        symbol = action["symbol"]
        if symbol not in symbols or symbol not in feed.instrument_profiles or not feed.manifest["start"] <= action["dividOperateDate"] <= feed.manifest["end"]:
            continue
        key = symbol + ":" + action["dividOperateDate"]
        quantity = account["entitlements"].get(key, 0)
        expected = int((Decimal(str(action["dividCashPsBeforeTax"])) * quantity * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        credits = [event for event in account["events"] if event["kind"] == "dividend_paid"
                   and event["symbol"] == symbol and event["day"] == action["dividPayDate"]]
        created = [event for event in account["events"] if event["kind"] == "corporate_action"
                   and event["symbol"] == symbol and event["day"] == action["dividOperateDate"]]
        if (quantity < 0 or (quantity and (len(credits) != 1 or len(created) != 1
                or credits[0]["cash"] != expected or created[0]["receivable"] != expected))
                or (not quantity and (credits or created))):
            raise ValueError("Actual registered fund holding did not reconcile ex-date receivable/payment cash")
        distribution_checks.append({"symbol": symbol, "registered_quantity": quantity, "ex_date": action["dividOperateDate"],
                                    "pay_date": action["dividPayDate"], "cash_cents": expected})
    checks.append({"check": "fund_registered_cash_ex_and_payment_dates", "events": distribution_checks, "passed": True})
    if any(item.get("residual_cents", 0) != 0 for item in checks):
        raise ValueError("Saved probe account identity failed")
    result = {"status": "passed_saved_account_diagnostic", "checks": checks,
              "fund_fills": sum(fill["symbol"] in feed.instrument_profiles for fill in fills),
              "stock_fills": sum(fill["symbol"] not in feed.instrument_profiles for fill in fills),
              "execution_admitted": False, "strategy_validated": False}
    write_json(folder / "saved_account_checks.json", result)
    return result


def probe(dataset, out=None, *, all_reviewed_funds=False):
    feed = HistoricalFeed(dataset)
    if (feed.manifest.get("scope") not in {SCOPE, FUND_SCOPE} or feed.manifest.get("replay_only") is not True
            or feed.manifest.get("execution_admitted") is not False):
        raise ValueError("Fixed unit probe requires the declared replay-only diagnostic snapshot")
    for name, digest in feed.manifest["file_hashes"].items():
        archived(feed.folder, {"file": name, "sha256": digest})
    symbols = sorted(feed.instrument_profiles) if all_reviewed_funds else ["600000.SSE", SYMBOL]
    if not symbols or not set(symbols) <= set(feed.symbols):
        raise ValueError("Both fixed stock/fund diagnostic components required")
    config = StrategyConfig(initial_cash=140000, signal="equal_weight", holdings=len(symbols), retain_rank=len(symbols),
                            single_limit=.8 / len(symbols), gross_limit=.8, rebalance_sessions=20).validate()
    dates = [day for day in feed.calendar if feed.manifest["start"] <= day <= feed.manifest["end"]]
    out = Path(out) if out else DATA_ROOT / "research/mixed_ledger_probe" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    protocol = {"status": "running", "purpose": "mixed_price_unit_corporate_action_account_diagnostic",
                "dataset": str(feed.folder.resolve()), "manifest_sha256": sha256((feed.folder / "manifest.json").read_bytes()).hexdigest(),
                "config": config.to_dict(), "start": dates[0], "end": dates[-1], "sessions": len(dates),
                "snapshot_stock_symbols": feed.manifest["stock_symbols"],
                "fixed_components": {s: .8 / len(symbols) for s in symbols}, "nominal_cash_weight": .2,
                "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                "strategy_validated": False, "execution_admitted": False,
                "limits": feed.manifest["limitations"] + [
                    "Predetermined profile components exercise the account path; not full-universe selection or a winning strategy",
                    "Open-next-session simulation, declared costs and participation; not broker fills",
                    "No parameter search or new2026 PnL/untouched validation claim"]}
    write_json(out / "result.json", protocol)
    try:
        metrics = replay(FixedMixedProbeFeed(feed, symbols), config, dates, out / "ledger")
        checked = saved_probe_checks(out / "ledger", feed, config, metrics, symbols)
        protocol.update(status="completed_real_price_mixed_ledger_diagnostic", metrics=metrics,
                        saved_account_checks=checked)
    except Exception as error:
        protocol.update(status="failed_real_price_mixed_ledger_diagnostic", error=f"{type(error).__name__}: {error}")
        write_json(out / "result.json", protocol)
        raise
    write_json(out / "result.json", protocol)
    return out, protocol


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--stock", type=Path, help="Existing stock-only snapshot for the mixed dataset")
    sources.add_argument("--calendar-source", type=Path,
                         help="Archived provider trade-date query for fund-only data; no stock bars added")
    parser.add_argument("--dayk", type=Path, required=True)
    parser.add_argument("--sina", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--codes", help="Comma-separated reviewed fund codes; defaults to510300")
    parser.add_argument("--end", help="Explicit exchange-session cutoff; distribution coverage is still required")
    profile_args = parser.add_mutually_exclusive_group()
    profile_args.add_argument("--profiles-2026-h1", action="store_true",
                              help="Explicit reviewed pre-July2026 rule profiles; old profiles are unchanged")
    profile_args.add_argument("--profiles-2026-q3", action="store_true",
                              help="Both rule eras through September2026; complete cash coverage still required")
    args = parser.parse_args(argv)
    out, result = build(args.stock, args.dayk, args.sina, args.reports, args.out,
                        args.codes.split(",") if args.codes else None, end=args.end,
                        calendar_source=args.calendar_source,
                        profiles=(FUND_Q3_2026_PROFILES if args.profiles_2026_q3 else
                                  FUND_H1_2026_PROFILES if args.profiles_2026_h1 else None))
    print(json.dumps({"folder": str(out), **{key: result[key] for key in (
        "status", "stock_symbols", "fund_symbols", "fund_rows", "replay_only", "execution_admitted")}}), flush=True)
    return 0


def probe_main(argv=None):
    parser = argparse.ArgumentParser(description="Fixed mixed-unit account diagnostic through the existing Engine/Ledger")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--all-reviewed-funds", action="store_true",
                        help="Exercise every pinned fund profile at equal fixed weights; diagnostic, not strategy selection")
    args = parser.parse_args(argv)
    out, result = probe(args.dataset, args.out, all_reviewed_funds=args.all_reviewed_funds)
    print(json.dumps({"folder": str(out), "status": result["status"], "sessions": result["sessions"],
                      "metrics": result["metrics"], "strategy_validated": False}), flush=True)
    return 0
