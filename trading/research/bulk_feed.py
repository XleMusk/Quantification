"""Full source-covered stock data for diagnostic replay through the sole ledger."""
from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import date

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..feed import HistoricalFeed
from ..rules import holder_distribution_action, stock_board
from ..symbols import normalize_symbol
from .price_boundaries import canonicalize


BULK_SCOPE = "full_source_sh_sz_research_only"
RAW_FIELDS = ("open", "high", "low", "close", "preclose", "volume", "amount",
              "tradestatus", "isST")
AMOUNT_FIELDS = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
ADJUSTED_REFERENCE_TOLERANCE = 0.011


def read_result(folder):
    return json.loads((folder / "result.json").read_text(encoding="utf-8"))


def numeric(series):
    values = pd.to_numeric(series, errors="coerce")
    if (series.fillna("").astype(str).str.strip().ne("") & ~np.isfinite(values)).any():
        raise ValueError(f"Invalid numeric source values: {series.name}")
    return values


def adjusted_reference_delta(precloses, closes, day_index, symbol, day,
                             tolerance=ADJUSTED_REFERENCE_TOLERANCE):
    """Return a verified adjusted-panel continuity delta, or ``None``.

    This is a research diagnostic only: it does not reconstruct missing action
    terms and must never be used for cash/share accounting or order admission.
    """
    index = day_index.get(day)
    if index is None or index == 0 or symbol not in precloses.columns:
        return None
    current = precloses.iloc[index][symbol]
    previous = closes.iloc[index - 1][symbol]
    if not (np.isfinite(current) and np.isfinite(previous)):
        return None
    delta = float(current - previous)
    return delta if abs(delta) <= tolerance else None


def _load_listing_master(path):
    """Load either the legacy provider table or an instrument_master_v1 folder."""
    path = Path(path).resolve()
    candidate = path / "instrument_master.csv" if path.is_dir() else path
    if candidate.name == "instrument_master.csv":
        from .instrument_master import load_master
        rows = load_master(candidate)
        return pd.DataFrame([
            {
                "code": row["provider_code"],
                "code_name": row["name"],
                "ipoDate": row["provider_ipo_date"],
                "outDate": row["provider_out_date"],
                "type": "1",
                "status": "1" if row["observed_status"] == "active_observed" else "0",
                "symbol": row["canonical_symbol"],
                "board": row["board"],
                "listing_date_known": "True",
            }
            for row in rows
        ]), {"kind": "instrument_master_v1", "path": str(candidate)}
    return pd.read_csv(candidate, dtype=str, keep_default_na=False), {
        "kind": "legacy_provider_listing", "path": str(candidate)
    }


def _scope_mismatch_detail(candidate, missing):
    """Explain a deliberate master/source scope mismatch without admitting it."""
    candidate = Path(candidate).resolve()
    excluded = candidate.with_name("excluded_instruments.csv")
    if not excluded.exists():
        return None
    frame = pd.read_csv(excluded, dtype=str, keep_default_na=False)
    code_field = "code" if "code" in frame.columns else "provider_code"
    if code_field not in frame.columns:
        return None
    frame = frame[frame[code_field].isin(missing)]
    if frame.empty:
        return None
    fields = [field for field in (code_field, "reason", "source") if field in frame.columns]
    return frame[fields].rename(columns={code_field: "code"}).to_dict("records")


def _load_research_scope(path):
    """Load a dated research-only scope receipt and its included source codes."""
    path = Path(path).resolve()
    candidate = path / "research_scope.csv" if path.is_dir() else path
    if not candidate.exists():
        raise ValueError(f"Research scope file is missing: {candidate}")
    frame = pd.read_csv(candidate, dtype=str, keep_default_na=False)
    required = {"provider_code", "included_in_research_scope", "pit_status_known", "execution_admitted"}
    if not required <= set(frame.columns) or frame.provider_code.duplicated().any():
        raise ValueError("Invalid research_scope_v1 identity table")
    if not frame.pit_status_known.eq("false").all() or not frame.execution_admitted.eq("false").all():
        raise ValueError("Research scope cannot claim PIT or execution admission")
    included = set(frame.loc[frame.included_in_research_scope.eq("true"), "provider_code"])
    if not included:
        raise ValueError("Research scope contains no included source codes")
    run_path = candidate.with_name("run.json")
    metadata = json.loads(run_path.read_text(encoding="utf-8")) if run_path.exists() else {}
    if metadata and metadata.get("schema") != "research_scope_v1":
        raise ValueError("Research scope manifest schema differs from research_scope_v1")
    return included, {"kind": "research_scope_v1", "path": str(candidate), "manifest": metadata}


class DailyRows:
    """Read only the current/prior raw-price row groups, not a giant quote dict."""
    def __init__(self, feed, raw_file):
        self.feed = feed
        self.file = pq.ParquetFile(raw_file)
        if self.file.num_row_groups != len(feed.calendar):
            raise ValueError("Raw daily row groups do not match the pinned calendar")
        self.load = lru_cache(maxsize=3)(self.load)

    def get(self, day, default=None):
        if day not in self.feed.day_index:
            return default
        return self.load(day)

    def load(self, day):
        feed = self.feed
        i = feed.day_index[day]
        frame = self.file.read_row_group(i, columns=BAOSTOCK_FIELDS.split(",")).to_pandas()
        if not frame.date.eq(day).all() or frame.code.duplicated().any() or not frame.adjustflag.eq("3").all():
            raise ValueError(f"Invalid raw source date/key/price basis: {day}")
        expected = feed.codes[feed.day_members[i]]
        if getattr(feed, "research_scope", None) is not None:
            allowed = set(expected)
            if feed.rename:
                allowed.add(feed.rename["old"])
            frame = frame.loc[frame.code.isin(allowed)].copy()
        extra = set(frame.code) - set(expected)
        if extra:
            if not feed.rename or extra != {feed.rename["old"]}:
                raise ValueError(f"Unexpected raw-only security rows: {day} {sorted(extra)}")
            _, frame = canonicalize(frame, frame, feed.rename)
        frame = frame.loc[frame.code.isin(expected)].set_index("code").reindex(expected)
        if frame.date.isna().any():
            raise ValueError(f"Missing canonical raw rows: {day}")
        for field in RAW_FIELDS:
            frame[field] = numeric(frame[field])
        if (not frame.tradestatus.isin([0, 1]).all() or not frame.isST.isin([0, 1]).all()
                or (frame[["volume", "amount"]] < 0).any().any()
                or frame.loc[frame.tradestatus.eq(1), ["open", "high", "low", "close", "preclose"]].isna().any().any()
                or (frame.loc[frame.tradestatus.eq(1), ["open", "high", "low", "close", "preclose"]] <= 0).any().any()):
            raise ValueError(f"Invalid raw quote/status: {day}")
        # Reconstructed prices may drive signals, but source turnover/volume must agree.
        for field, matrix in (("amount", feed.amounts), ("volume", feed.volumes)):
            if not np.allclose(frame[field], matrix.iloc[i, feed.day_members[i]], equal_nan=True, rtol=0, atol=0):
                raise ValueError(f"Raw/research {field} alignment changed: {day}")
        frame.index = [feed.code_symbols[code] for code in frame.index]
        return frame.drop(index=list(feed.unsupported_instruments), errors="ignore").to_dict("index")

    def close(self):
        self.load.cache_clear()
        self.file.close()


class BulkResearchFeed(HistoricalFeed):
    """Reuse rule signals; defer unresolved accounting to the affected session.

    This is NOT HistoricalFeed admission. All source members stay in the ranking
    universe; future event quality never filters a stock out of the strategy.
    """
    def __init__(self, price_view, event_view, reference_view, listing_master=None,
                 allow_adjusted_continuity=False, research_scope=None):
        self.allow_adjusted_continuity = bool(allow_adjusted_continuity)
        self.research_scope = None
        self.folder = Path(price_view).resolve()
        event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
        if any((folder / "INVALID.json").exists() for folder in (self.folder, event_view, reference_view)):
            raise ValueError("Invalidated research snapshot cannot enter a ledger")
        prices, events, references = read_result(self.folder), read_result(event_view), read_result(reference_view)
        if (prices["status"] != "research_reconstruction_completed"
                or events["status"] != "research_action_events_projected"
                or references["status"] != "research_reference_presence_diagnostic"
                or Path(events["raw_view"]).resolve() != Path(prices["raw_view"]).resolve()
                or Path(references["price_view"]).resolve() != self.folder
                or Path(references["event_view"]).resolve() != event_view
                or events["unqueried"] != 0):
            raise ValueError("Bulk replay needs aligned, completed price/action/reference views")
        raw_view = Path(prices["raw_view"])
        self.calendar = pd.read_csv(event_view / "source_input_calendar.csv", dtype=str).date.tolist()
        if sorted(set(self.calendar)) != self.calendar or self.calendar[-1] != prices["cutoff"]:
            raise ValueError("Bulk calendar must be unique, ascending and end at the price cutoff")
        self.day_index = {day: i for i, day in enumerate(self.calendar)}
        self.rename = prices.get("rename")
        universe = pd.read_csv(event_view / "source_input_universe.csv", dtype=str, keep_default_na=False)
        codes = set(universe.code)
        if len(codes) != len(universe) or len(codes) != events["expected_codes"]:
            raise ValueError("Bulk source universe is incomplete or duplicated")
        if self.rename:
            if not {self.rename["old"], self.rename["new"]} <= codes:
                raise ValueError("Reviewed alias lineage is not in the source universe")
            codes.remove(self.rename["old"])
        full_codes = set(codes)
        scope_meta = None
        if research_scope is not None:
            scope_codes, scope_meta = _load_research_scope(research_scope)
            if self.rename and self.rename["old"] in scope_codes:
                scope_codes = (scope_codes - {self.rename["old"]}) | {self.rename["new"]}
            unknown_scope = sorted(scope_codes - full_codes)
            if unknown_scope:
                raise ValueError(f"Research scope contains codes outside the canonical replay source: {unknown_scope}")
            codes &= scope_codes
            if not codes:
                raise ValueError("Research scope removes every canonical replay instrument")
            self.research_scope = scope_meta
        self.code_symbols = {code: normalize_symbol(code).vt_symbol for code in codes}
        self.codes = np.array(sorted(codes, key=self.code_symbols.get))
        self.symbols = [self.code_symbols[code] for code in self.codes]
        self.unsupported_instruments = {}
        for symbol in self.symbols:
            try:
                stock_board(symbol)
            except ValueError as error:
                self.unsupported_instruments[symbol] = str(error)
        if scope_meta is None and len(self.symbols) != prices["canonical_symbols"]:
            raise ValueError("Full canonical universe size does not match price reconstruction")
        master_path = Path(listing_master).resolve() if listing_master else Path(prices["factor_view"]) / "input_listing_master.csv"
        master, master_meta = _load_listing_master(master_path)
        if master.code.duplicated().any():
            raise ValueError("Listing master contains duplicate source keys")
        master = master.set_index("code")
        missing_listing = sorted(set(self.codes) - set(master.index))
        if missing_listing:
            detail = (_scope_mismatch_detail(master_meta["path"], missing_listing)
                      if master_meta["kind"] == "instrument_master_v1" else None)
            if detail:
                raise ValueError(
                    "Instrument master scope does not cover the full diagnostic source universe: "
                    f"missing={missing_listing}; excluded_by_master={detail}. "
                    "Use the pinned provider listing overlay for full-source replay; "
                    "do not widen instrument_master_v1 or treat this replay as execution-admitted."
                )
            raise ValueError(f"Listing metadata is missing for source-covered stocks: {missing_listing}")
        self.instruments = {self.code_symbols[code]: master.loc[code].to_dict() for code in self.codes}
        self.instrument_profiles = {}
        self.manifest = {
            "scope": BULK_SCOPE, "replay_only": True, "start": max("2024-01-01", self.calendar[0]),
            "end": self.calendar[-1], "selection_date": "2023-01-01",
            "price_view": str(self.folder), "event_view": str(event_view), "reference_view": str(reference_view),
            "raw_view": str(raw_view), "source_codes": len(universe),
            "full_canonical_codes": len(full_codes), "canonical_codes": len(self.symbols),
            "listing_master": str(master_path),
            "listing_master_kind": master_meta["kind"],
            "research_scope": scope_meta["path"] if scope_meta else None,
            "research_scope_kind": scope_meta["kind"] if scope_meta else None,
            "research_scope_excluded_codes": len(full_codes) - len(self.symbols),
            "unsupported_instruments": self.unsupported_instruments,
            "selection_is_point_in_time": False, "execution_admitted": False,
            "limitations": ["Source-covered SH/SZ stocks, not independently complete A/BSE/ETF coverage",
                            "Retrieval-time listing master and reconstructed prices, not PIT-verified inputs",
                            "Unresolved held actions/reference changes stop the account, never default to zero",
                            "Dated alias accounting/migration is not admitted; affected positions stop",
                            "No independent price/action coverage or untouched-test claim"],
            "historical_price_adjustments": json.loads((reference_view / "reviewed_special_references.json").read_text(encoding="utf-8"))}
        if self.allow_adjusted_continuity:
            self.manifest["limitations"].append(
                "Research-only adjusted-panel continuity override: permits only reference_mismatch rows "
                "with verified adjusted preclose continuity; no blank/missing terms and no execution admission")
        if events.get("secondary_reported_reserve_completions", 0):
            self.manifest["limitations"].append(
                f"{events['secondary_reported_reserve_completions']} opt-in secondary reported reserve zeros; "
                "matched primary cash/dates but NOT issuer-confirmed share terms or execution admission")
        if events.get("postevent_cash_confirmations", 0):
            self.manifest["limitations"].append(
                f"{events['postevent_cash_confirmations']} post-event issuer cash-only confirmations: "
                + events["postevent_cash_confirmation_basis"])
        panel = pq.ParquetFile(self.folder / "canonical_daily.parquet")
        if panel.num_row_groups != len(self.calendar):
            raise ValueError("Research daily row groups do not match the pinned calendar")
        arrays = {field: np.full((len(self.calendar), len(self.symbols)), np.nan)
                  for field in ("close", "preclose", "amount", "volume")}
        code_index = pd.Index(self.codes)
        self.day_members = []
        total_rows = 0
        for i, day in enumerate(self.calendar):
            frame = panel.read_row_group(
                i, columns=["date", "code", "adjustflag", "close", "preclose", "amount", "volume"]
            ).to_pandas()
            if (not frame.date.eq(day).all() or frame.code.duplicated().any()
                    or not frame.adjustflag.eq("2").all()):
                raise ValueError(f"Invalid research date/key/price basis: {day}")
            selected = frame.loc[frame.code.isin(code_index)]
            members = code_index.get_indexer(selected.code)
            self.day_members.append(members)
            total_rows += len(frame)
            for field in arrays:
                arrays[field][i, members] = numeric(selected[field])
        if total_rows != prices["canonical_rows"]:
            raise ValueError("Canonical price rows are incomplete")
        self.closes = pd.DataFrame(arrays["close"], index=self.calendar, columns=self.symbols)
        self.precloses = pd.DataFrame(arrays["preclose"], index=self.calendar, columns=self.symbols)
        self.amounts = pd.DataFrame(arrays["amount"], index=self.calendar, columns=self.symbols)
        self.volumes = pd.DataFrame(arrays["volume"], index=self.calendar, columns=self.symbols)
        self._features = {}
        self.rows = DailyRows(self, raw_view / "raw_daily.parquet")
        actions = pd.read_parquet(event_view / "canonical_actions.parquet").fillna("").to_dict("records")
        if scope_meta is not None:
            selected_symbols = set(self.symbols)
            actions = [item for item in actions
                       if normalize_symbol(item["code"]).vt_symbol in selected_symbols
                       or (self.rename and item["code"] in {self.rename["old"], self.rename["new"]})]
        if self.rename:
            for action in actions:
                if action["code"] == self.rename["old"] and self.rename["new"] in self.code_symbols:
                    action["symbol"] = self.code_symbols[self.rename["new"]]
        if scope_meta is not None:
            actions = [item for item in actions if item["symbol"] in set(self.symbols)]
        keys = {(item["symbol"], item["dividOperateDate"]) for item in actions}
        if len(keys) != len(actions):
            raise ValueError("Duplicate canonical actions cannot enter the ledger")
        for item in self.manifest["historical_price_adjustments"]:
            if item["kind"] == "restructuring_holder_distribution":
                key = item["symbol"], item["date"]
                if key in keys:
                    raise ValueError("Restricted distribution overlaps an ordinary action")
                actions.append(holder_distribution_action(item))
        self.actions = actions
        self.actions_by_ex = defaultdict(list)
        for item in actions:
            self.actions_by_ex[item["dividOperateDate"]].append(item)
        self.unresolved_references = defaultdict(list)
        self.adjusted_reference_continuity = {}
        for item in pd.read_parquet(reference_view / "raw_reference_changes.parquet").to_dict("records"):
            if item["reference_formula_status"] != "reference_match":
                symbol = normalize_symbol(item["code"]).vt_symbol
                if self.rename and item["code"] == self.rename["old"]:
                    symbol = self.code_symbols[self.rename["new"]]
                if symbol in set(self.symbols):
                    self.unresolved_references[item["date"]].append((symbol, item["reference_formula_status"]))
                if item["reference_formula_status"] == "reference_mismatch":
                    delta = adjusted_reference_delta(
                        self.precloses, self.closes, self.day_index, symbol, item["date"])
                    if delta is not None:
                        self.adjusted_reference_continuity[(symbol, item["date"])] = delta

    def close(self):
        self.rows.close()

    def validate_session(self, state, day):
        held = {symbol for symbol, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        pending = {symbol for symbol, weight in (state.get("pending_targets") or {}).items() if weight > 0}
        unsupported = (held | pending) & set(self.unsupported_instruments)
        if unsupported:
            raise ValueError(f"Unsupported instrument accounting: {sorted(unsupported)} {day}")
        if self.rename:
            alias = self.code_symbols[self.rename["new"]]
            if alias in held | pending and day < self.rename["effective_date"]:
                raise ValueError(f"Unsupported dated symbol mapping: {alias} {day}; tradable code={self.rename['old']}")
        for item in self.actions_by_ex.get(day, []):
            identifier = f"{item['symbol']}:{day}"
            if not state["entitlements"].get(identifier, 0):
                continue
            missing = []
            amounts = {}
            for field in AMOUNT_FIELDS:
                try:
                    value = Decimal(str(item.get(field, "")))
                    if not value.is_finite() or value < 0:
                        raise InvalidOperation
                    amounts[field] = value
                except InvalidOperation:
                    missing.append(field)
            if missing:
                raise ValueError(f"Unresolved held corporate action: {item['symbol']} {day}; missing={','.join(missing)}")
            if amounts["dividStocksPs"] > 0:
                try:
                    basis = Decimal(str(item.get("bonus_taxable_cash_ps", "")))
                    if not basis.is_finite() or basis <= 0:
                        raise InvalidOperation
                except InvalidOperation as error:
                    raise ValueError(f"Unresolved bonus taxable basis: {item['symbol']} {day}") from error
            for field, positive, label in (
                    ("dividPayDate", amounts["dividCashPsBeforeTax"] > 0, "dividend payment"),
                    ("dividStockMarketDate", amounts["dividStocksPs"] + amounts["dividReserveToStockPs"] > 0, "bonus listing")):
                if positive:
                    try:
                        parsed = date.fromisoformat(item.get(field, ""))
                        if parsed.isoformat() < day:
                            raise ValueError("Before ex date")
                    except ValueError as error:
                        raise ValueError(f"Unresolved {label} date: {item['symbol']} {day}") from error
        for symbol, reason in self.unresolved_references.get(day, []):
            if symbol in held | pending:
                if (reason == "reference_mismatch"
                        and getattr(self, "allow_adjusted_continuity", False)
                        and (symbol, day) in getattr(self, "adjusted_reference_continuity", {})):
                    continue
                raise ValueError(f"Unresolved held/execution price reference: {symbol} {day}; {reason}")
