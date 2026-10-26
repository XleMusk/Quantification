"""Run fixed rolling low-volatility comparisons through the shared Engine/Ledger.

The source is a read-only StockDB full-market inventory.  It is intentionally
kept separate from the PIT HistoricalFeed: raw provider prices, membership and
corporate actions are not sufficient for paper or live promotion.  The only
purpose of this module is to compare predefined rules on a longer path while
using the production cash/share accounting and order lifecycle.
"""

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..report import metrics as portfolio_metrics
from ..rules import stock_board
from ..settings import DATA_ROOT, ROOT
from ..symbols import normalize_symbol
from ..target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from .experiment import baseline_ledger_result, load_replay_checkpoint, write_json
from .full_market import verify_chunks
from .monthly_timing import month_ends
from .exchange_calendar import _closed_days


SCHEMA = "stockdb_rolling_low_volatility_ledger_v1"
VARIANTS = ("momentum_12_1_20", "low_volatility_252_20", "low_vol_half_momentum_20")
LOOKBACK = 252
SKIP = 21
HOLDINGS = 20
RETAIN_RANK = 30
MIN_AMOUNT = 20_000_000.0
SOURCE_COLUMNS = ("code", "date", "open", "high", "low", "close", "volume", "amount",
                  "is_st", "pre_close")
LEGACY_QUOTE_SEMANTICS = "volume_derived_v1"
QUOTE_SEMANTICS = "native_supplement_status_v2"
DERIVED_QUOTE_SEMANTICS = "stockdb_pre_close_derived_research_v1"
CALENDAR_MONTH_END_METHOD = "fixed_exchange_calendar_month_end_and_next_session_v1"


def portfolio(initial_cash=140000.0):
    return StrategyConfig(signal="equal_weight", lookback=LOOKBACK, skip=SKIP,
                          holdings=HOLDINGS, retain_rank=RETAIN_RANK,
                          rebalance_sessions=1, initial_cash=initial_cash,
                          min_amount=MIN_AMOUNT, min_listing_days=1).validate()


def _read_snapshot(snapshot, supplemental_raw_view=None, end=None,
                   *, allow_derived_execution_reference=False):
    snapshot = Path(snapshot).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("Rolling replay requires a complete, unadmitted StockDB inventory")
    verify_chunks(snapshot, manifest)
    paths = [snapshot / item["parquet_file"] for item in manifest["chunks"]]
    frames = [pd.read_parquet(path, columns=list(SOURCE_COLUMNS)) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"].astype(str), errors="raise").dt.strftime("%Y-%m-%d")
    for column in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["is_st"] = frame["is_st"].fillna(False).astype(bool)
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("StockDB inventory contains duplicate code/date rows")
    supplemental = {"path": None, "rows_added": 0, "basis": None,
                    "execution_reference_mode": "native_status_required"}
    if supplemental_raw_view:
        raw_path = Path(supplemental_raw_view).resolve()
        if raw_path.is_dir():
            raw_path = raw_path / "raw_daily.parquet"
        if not raw_path.is_file():
            raise ValueError(f"Supplemental raw view is missing: {raw_path}")
        raw_columns = ("date", "code", "open", "high", "low", "close", "preclose",
                       "volume", "amount", "tradestatus", "adjustflag", "isST")
        raw = pd.read_parquet(raw_path, columns=list(raw_columns))
        raw["code"] = raw["code"].astype(str).str.extract(r"(\d{6})$")[0]
        raw["date"] = pd.to_datetime(raw["date"].astype(str), errors="raise").dt.strftime("%Y-%m-%d")
        source_start = str(manifest.get("start") or "")
        if source_start:
            raw = raw.loc[raw["date"] >= source_start]
        if end:
            raw = raw.loc[raw["date"] <= end]
        known_codes = set(frame["code"])
        raw = raw.loc[raw["code"].isin(known_codes)].copy()
        for column in ("open", "high", "low", "close", "volume", "amount", "preclose"):
            raw[column] = pd.to_numeric(raw[column], errors="coerce")
        raw["is_st"] = raw["isST"].astype(str).str.strip().eq("1")
        if raw.duplicated(["code", "date"]).any():
            raise ValueError("Supplemental raw view contains duplicate code/date rows")
        derived_source = False
        result_path = raw_path.parent / "result.json"
        if allow_derived_execution_reference and result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            proofs = result.get("source_proofs") or []
            derived_source = (
                result.get("schema") == "lowvol_append_only_complete_source_supplement_v1"
                and result.get("status") == "completed_append_only_research_supplement"
                and result.get("missing_source_fields_policy") == "null_never_zero"
                and bool(proofs)
                and all(item.get("source_schema") == "stockdb_close_package_v1"
                        for item in proofs if isinstance(item, dict))
                and all(isinstance(item, dict) and item.get("source_schema") == "stockdb_close_package_v1"
                        for item in proofs)
            )
        status = raw.tradestatus.astype("string")
        adjustment = raw.adjustflag.astype("string")
        if derived_source:
            invalid = ((status.notna() & ~status.isin(["0", "1"]))
                       | (adjustment.notna() & ~adjustment.eq("3")))
            if (status.isna() ^ adjustment.isna()).any():
                raise ValueError("Derived StockDB quotes must leave tradestatus and adjustflag together unknown")
            if invalid.any():
                raise ValueError("Derived StockDB quotes require valid explicit status values when present")
        elif (not status.isin(["0", "1"]).all()
              or not adjustment.eq("3").fillna(False).all()):
            raise ValueError("Supplemental native quotes require explicit tradestatus 0 or 1 and adjustflag 3")
        keys = pd.MultiIndex.from_frame(frame[["code", "date"]])
        raw_keys = pd.MultiIndex.from_frame(raw[["code", "date"]])
        missing = ~raw_keys.isin(keys)
        # Keep StockDB's OHLCV and universe rows authoritative.  The raw view
        # is allowed to annotate overlapping keys with an explicit execution
        # status/adjustment so held positions can be checked against an
        # independent native stream on the next session.  Only missing keys
        # contribute price rows.
        overlap = raw.loc[~missing, ["code", "date", "tradestatus", "adjustflag"]]
        if not overlap.empty:
            frame = frame.merge(overlap, on=["code", "date"], how="left",
                                sort=False, validate="one_to_one")
        additions = raw.loc[missing, ["code", "date", "open", "high", "low", "close", "volume", "amount",
                                      "is_st", "preclose", "tradestatus", "adjustflag"]]
        additions = additions.rename(columns={"preclose": "pre_close"})
        frame = pd.concat([frame, additions], ignore_index=True)
        supplemental = {"path": str(raw_path), "rows_added": int(len(additions)),
                        "rows_annotated": int(len(overlap)),
                        "basis": "local raw_daily.parquet; fills missing StockDB keys and annotates overlapping execution status/adjustment only",
                        "execution_reference_mode": ("derived_stockdb_research_only"
                                                       if derived_source else "native_status_required")}

    symbols = {}
    keep = []
    for code in sorted(frame.code.unique()):
        symbol = normalize_symbol(code).vt_symbol
        try:
            stock_board(symbol)
        except ValueError:
            continue
        symbols[code] = symbol
        keep.append(code)
    frame = frame.loc[frame.code.isin(keep)].copy()
    frame["symbol"] = frame["code"].map(symbols)
    frame = frame.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    if frame.empty:
        raise ValueError("StockDB inventory has no supported ordinary SH/SZ equities")
    return snapshot, manifest, frame, supplemental


def _effective_quote_metadata(frame, semantics=QUOTE_SEMANTICS):
    """Keep StockDB's old rule; native rows retain their explicit raw status."""
    if semantics not in {LEGACY_QUOTE_SEMANTICS, QUOTE_SEMANTICS, DERIVED_QUOTE_SEMANTICS}:
        raise ValueError(f"Unknown rolling quote semantics: {semantics}")
    metadata = pd.DataFrame({"tradestatus": frame.volume.gt(0).astype(int),
                             "adjustflag": "3"}, index=frame.index)
    if semantics == LEGACY_QUOTE_SEMANTICS:
        return metadata
    status = frame.get("tradestatus", pd.Series(pd.NA, index=frame.index, dtype="string"))
    adjustment = frame.get("adjustflag", pd.Series(pd.NA, index=frame.index, dtype="string"))
    native = status.notna() | adjustment.notna()
    if (not status.loc[native].astype("string").isin(["0", "1"]).all()
            or not adjustment.loc[native].astype("string").eq("3").fillna(False).all()):
        raise ValueError("Native quotes require explicit tradestatus 0/1 and raw quote adjustflag 3")
    metadata.loc[native, "tradestatus"] = status.loc[native].astype(int)
    return metadata


class _DayRows:
    def __init__(self, frames):
        self.frames = frames
        self.cache = {}

    def get(self, day, default=None):
        if day not in self.frames:
            return default
        if day not in self.cache:
            frame = self.frames[day]
            self.cache[day] = frame.to_dict("index")
        return self.cache[day]


class RollingStockDBFeed:
    """Engine feed backed by raw StockDB bars and precomputed fixed signals."""

    def __init__(self, frame, end, *, variant, fixed_calendar=None,
                 quote_semantics=QUOTE_SEMANTICS,
                 allow_derived_execution_reference=False):
        if quote_semantics not in {QUOTE_SEMANTICS, DERIVED_QUOTE_SEMANTICS}:
            raise ValueError(f"Unsupported rolling quote semantics: {quote_semantics}")
        if type(allow_derived_execution_reference) is not bool:
            raise ValueError("Derived execution reference mode must be an explicit boolean")
        if allow_derived_execution_reference != (quote_semantics == DERIVED_QUOTE_SEMANTICS):
            raise ValueError("Derived execution reference mode and quote semantics disagree")
        self.variant = variant
        self.quote_semantics = quote_semantics
        self.allow_derived_execution_reference = allow_derived_execution_reference
        self.calendar = sorted(frame["date"].unique().tolist())
        if end not in self.calendar:
            raise ValueError(f"Replay end is not a StockDB session: {end}")
        self.calendar = [day for day in self.calendar if day <= end]
        self.raw_calendar = list(self.calendar)
        self._calendar_driven_month_ends = fixed_calendar is not None
        if fixed_calendar is not None:
            fixed_ends = month_ends(fixed_calendar, end)
            following = next((day for day in fixed_calendar if day > end), None)
            if end not in fixed_calendar or following is None:
                raise ValueError("Calendar-driven month ends require an exact fixed-calendar successor after the raw cutoff")
            # Only the date is known. No successor quote, feature or price row
            # is added; it exists solely for next-session intent/settlement.
            self.calendar = [*self.raw_calendar, following]
        self.day_index = {day: index for index, day in enumerate(self.calendar)}
        frame = frame.loc[frame.date.isin(self.raw_calendar)].copy()
        self.symbols = sorted(frame.symbol.unique())
        self.instruments = {symbol: {"ipoDate": "2000-01-01", "outDate": ""} for symbol in self.symbols}
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {
            "replay_only": True,
            "scope": "stockdb_raw_provider_inventory_ordinary_equities",
            "selection_date": "2000-01-01",
            "execution_admitted": False,
            "corporate_actions_verified": False,
            "listing_master_verified": False,
            "quote_semantics": quote_semantics,
            "execution_reference_mode": ("derived_stockdb_research_only"
                                           if allow_derived_execution_reference
                                           else "native_status_required"),
        }
        indexed = frame.set_index(["date", "symbol"])
        engine_frame = frame.copy()
        engine_frame["preclose"] = engine_frame["pre_close"]
        engine_frame["isST"] = engine_frame["is_st"].astype(int)
        # Identify original native rows before effective metadata can derive
        # StockDB status from volume. Derived status is not raw-reference proof.
        native_status = frame.get("tradestatus", pd.Series(pd.NA, index=frame.index, dtype="string"))
        native_adjustment = frame.get("adjustflag", pd.Series(pd.NA, index=frame.index, dtype="string"))
        native_reference = (native_status.notna() & native_adjustment.notna()
                            & native_status.astype("string").isin(["0", "1"])
                            & native_adjustment.astype("string").eq("3").fillna(False))
        engine_frame["execution_reference_basis"] = np.where(
            native_reference, "native_raw", "mixed_stockdb_pre_close")
        quote_metadata = _effective_quote_metadata(engine_frame, quote_semantics)
        engine_frame["tradestatus"] = quote_metadata.tradestatus
        engine_frame["adjustflag"] = quote_metadata.adjustflag
        self.rows = _DayRows({day: group.set_index("symbol") for day, group in engine_frame.groupby("date", sort=True)})
        self.volumes = frame.pivot(index="date", columns="symbol", values="volume").reindex(index=self.raw_calendar, columns=self.symbols)
        self.amounts = frame.pivot(index="date", columns="symbol", values="amount").reindex(index=self.raw_calendar, columns=self.symbols)
        self.closes = frame.pivot(index="date", columns="symbol", values="close").reindex(index=self.raw_calendar, columns=self.symbols)
        self._features = self._build_features()
        self.ends = (set(fixed_ends) & set(self.raw_calendar) if fixed_calendar is not None
                     else set(month_ends(self.calendar, end)))
        self.audit = []
        self.ranking_rows = []

    def _build_features(self):
        live = self.volumes.gt(0) & self.closes.gt(0) & np.isfinite(self.closes)
        closes = self.closes.where(live)
        returns = closes.pct_change(fill_method=None)
        volatility = returns.rolling(LOOKBACK, min_periods=LOOKBACK).std(ddof=1) * np.sqrt(252)
        momentum = closes.shift(SKIP) / closes.shift(LOOKBACK) - 1
        median_amount = self.amounts.rolling(20, min_periods=20).median()
        return {"volatility": volatility, "momentum": momentum, "median_amount": median_amount}

    def next_day(self, day):
        index = self.day_index[day]
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else day

    def validate_session(self, state, day):
        if self._calendar_driven_month_ends and day not in self.raw_calendar:
            raise ValueError("A known calendar date is not an observed raw execution session")
        if state is not None:
            required = {symbol for symbol, lots in state["positions"].items()
                        if sum(lot["qty"] for lot in lots) > 0}
            required.update(symbol for symbol, weight in (state.get("pending_targets") or {}).items()
                            if weight > 0)
            rows = self.rows.get(day, {})
            for symbol in sorted(required):
                basis = rows.get(symbol, {}).get("execution_reference_basis")
                allowed = (basis == "native_raw"
                           or (self.allow_derived_execution_reference
                               and basis == "mixed_stockdb_pre_close"))
                if not allowed:
                    raise ValueError(f"Mixed StockDB execution reference basis: {symbol} {day}; "
                                     "an independent native raw execution stream is required unless "
                                     "derived_stockdb_research_only is explicitly enabled")

    def _rank(self, rows):
        if self.variant == VARIANTS[1]:
            return sorted(rows, key=lambda row: (row["volatility"], row["symbol"]))
        if self.variant == VARIANTS[2]:
            rows = sorted(rows, key=lambda row: (row["volatility"], row["symbol"]))[:(len(rows) + 1) // 2]
        return sorted(rows, key=lambda row: (-row["momentum"], row["symbol"]))

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("StockDB rolling comparison is replay-only")
        if config != portfolio(config.initial_cash):
            raise ValueError("StockDB rolling comparison requires its fixed portfolio/cost config")
        self.validate_session(None, day)
        if day not in self.ends:
            self.audit.append({"date": day, "variant": self.variant, "decision": "not_month_end"})
            return SignalUnavailable("not_month_end")
        position = self.day_index[day]
        if position < LOOKBACK:
            self.audit.append({"date": day, "variant": self.variant, "decision": "252_session_warmup"})
            return SignalUnavailable("252_session_warmup")
        values = self._features
        rows = self.rows.get(day, {})
        candidates = []
        for symbol in self.symbols:
            row = rows.get(symbol)
            if not row or row.get("volume", 0) <= 0 or bool(row.get("is_st")):
                continue
            volatility = values["volatility"].at[day, symbol]
            momentum = values["momentum"].at[day, symbol]
            median_amount = values["median_amount"].at[day, symbol]
            if not all(np.isfinite(value) for value in (volatility, momentum, median_amount)):
                continue
            if volatility <= 0 or median_amount < MIN_AMOUNT:
                continue
            candidates.append({"symbol": symbol, "volatility": float(volatility),
                               "momentum": float(momentum), "median_amount": float(median_amount)})
        ranked = self._rank(candidates)
        for index, row in enumerate(ranked[:config.retain_rank], start=1):
            self.ranking_rows.append({"date": day, "variant": self.variant, "rank": index, **row})
        self.audit.append({"date": day, "variant": self.variant, "decision": "monthly_ranking",
                           "eligible_count": len(candidates), "candidate_count": len(ranked)})
        if not ranked:
            return SignalUnavailable("known_empty_eligible_pool")
        signal_rows = []
        for row in ranked[:config.retain_rank]:
            score = -row["volatility"] if self.variant == VARIANTS[1] else row["momentum"]
            signal_rows.append({"symbol": row["symbol"], "score": score,
                                "volatility": row["volatility"], "momentum": row["momentum"]})
        return SignalFrame.from_legacy(signal_rows, day, "research:" + self.variant, context)


def _write_report(out, record):
    lines = ["# StockDB Rolling Low-Volatility Shared Ledger Replay", "",
             "Status: `research_only`; execution admitted: `False`.",
             f"Source window: `{record['source_start']}..{record['source_end']}`; account replay: `{record['replay_start']}..{record['replay_end']}`.",
             f"Source ordinary equities: `{record['symbols']}`; sessions: `{record['sessions']}`; monthly rebalance; CNY{record['config']['initial_cash']:.0f}.",
        "All listed variants use the same StockDB snapshot, dates, Engine/Ledger, cash, lots, fees and slippage.",
             "Raw provider prices, membership and corporate actions remain research-only; no strategy is promoted.", "",
             "| Variant | Status | Sessions | Orders | Fills | Net return | Max drawdown | Sharpe | Fees CNY |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    if record.get("resume_run"):
        lines[7:7] = [f"Continuation: `{record['continuation_status']}`; source `{record['resume_run']}`.",
                      "Saved cash, positions, orders and fills are retained; only unprocessed sessions advance.", ""]
    verification = record.get("calendar_verification", {})
    lines[7:7] = [f"Fixed-calendar verified years: `{verification.get('verified_years', [])}`; "
                  f"unverified replay years: `{verification.get('unverified_replay_years', [])}`.", ""]
    for result in record["results"]:
        if result.get("status") == "completed":
            lines.append(f"| {result['signal']} | completed | {result['completed_sessions']} | {result['orders']} | {result['fills']} | "
                         f"{result['net_return']:.2%} | {result['max_drawdown']:.2%} | "
                         f"{result.get('sharpe_zero_rf_252')} | {result['fees']:.2f} |")
        else:
            lines.append(f"| {result['signal']} | {result.get('status')} | {result.get('completed_sessions', 0)} | -- | -- | unavailable | unavailable | unavailable | unavailable |")
            lines.append(f"Failure: {result.get('error')}")
    lines += ["", "## Boundaries", "",
              "- This is a longer same-rule diagnostic, not an untouched holdout: the StockDB period has already been inspected.",
              "- No point-in-time universe, verified corporate actions, dividend reinvestment or independently reconciled listing master.",
              "- The account is the production research Engine/Ledger in replay mode, not a broker account or live order path.",
              "- Missing or invalid held data stops the affected variant; it is never imputed to zero.", "",
              f"Artifacts: `{record['artifacts']}`."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_resume_prefix(before, after, previous_end, end, last_day, *,
                            previous_quote_semantics=LEGACY_QUOTE_SEMANTICS, engine_quote_start=None,
                            month_end_calendar=None):
    """Reject any revision that could change a processed rolling signal/account."""
    if previous_quote_semantics not in {LEGACY_QUOTE_SEMANTICS, QUOTE_SEMANTICS,
                                        DERIVED_QUOTE_SEMANTICS}:
        raise ValueError(f"Unknown saved rolling quote semantics: {previous_quote_semantics}")
    if last_day is None:
        return
    old_calendar = sorted(before.loc[before.date.le(previous_end), "date"].unique())
    new_calendar = sorted(after.loc[after.date.le(end), "date"].unique())
    if [day for day in old_calendar if day <= last_day] != [day for day in new_calendar if day <= last_day]:
        raise ValueError("Completed calendar prefix changed; full replay required")
    old_schedule = old_calendar if month_end_calendar is None else month_end_calendar
    new_schedule = new_calendar if month_end_calendar is None else month_end_calendar
    if ([day for day in month_ends(old_schedule, previous_end) if day <= last_day]
            != [day for day in month_ends(new_schedule, end) if day <= last_day]):
        raise ValueError("Completed month-end signal prefix changed; full replay required")
    columns = [*SOURCE_COLUMNS, "symbol"]
    old_full = before.loc[before.date.le(last_day)].sort_values(["date", "symbol"]).reset_index(drop=True)
    new_full = after.loc[after.date.le(last_day)].sort_values(["date", "symbol"]).reset_index(drop=True)
    old, new = old_full[columns], new_full[columns]
    try:
        pd.testing.assert_frame_equal(old, new, check_dtype=False, check_exact=True)
    except AssertionError as error:
        raise ValueError("Completed raw history prefix changed; full replay required") from error
    # V1 ignored native metadata during execution; it still cannot be rewritten
    # to disguise a different source status as the old volume-derived behavior.
    for field in ("tradestatus", "adjustflag"):
        if field in old_full:
            native = old_full[field].notna()
            old_values = old_full.loc[native, field].astype("string")
            new_values = new_full.get(field, pd.Series(pd.NA, index=new_full.index, dtype="string"))
            if not old_values.equals(new_values.loc[native].astype("string")):
                raise ValueError("Completed native quote semantics prefix changed; full replay required")
    # Warmup prices/volume still shape features and remain fully checked above.
    # Engine quote metadata is consumed only from the original fixed replay start.
    if engine_quote_start is not None:
        try:
            valid_start = (date.fromisoformat(engine_quote_start).isoformat() == engine_quote_start
                           and engine_quote_start <= last_day)
        except (TypeError, ValueError):
            valid_start = False
        if not valid_start:
            raise ValueError("Invalid fixed Engine quote prefix start")
        old_full = old_full.loc[old_full.date.ge(engine_quote_start)]
        new_full = new_full.loc[new_full.date.ge(engine_quote_start)]
    # Old v1 accounts actually used volume-derived status, even on native rows.
    # A missing version must compare that effective behavior, not new columns.
    old_quotes = _effective_quote_metadata(old_full, previous_quote_semantics)
    new_quotes = _effective_quote_metadata(new_full)
    try:
        pd.testing.assert_frame_equal(old_quotes, new_quotes, check_dtype=False, check_exact=True)
    except AssertionError as error:
        raise ValueError("Completed quote semantics prefix changed; full replay required") from error


def _calendar_verification(calendars, source, start, end, *, include_fixed_sessions=False):
    """Check only the explicit fixed-calendar years; never infer missing sessions."""
    files, years, fixed_sessions = [], set(), set()
    for calendar in calendars or ():
        path = Path(calendar).resolve()
        raw = path.read_bytes()
        payload = json.loads(raw)
        year = payload.get("year")
        if (payload.get("schema") != "cn_a_share_exchange_calendar_v1"
                or type(year) is not int or not 1 <= year <= 9999 or year in years
                or not isinstance(payload.get("closed_ranges"), list)
                or not isinstance(payload.get("open_sessions"), list)):
            raise ValueError("Expected one fixed exchange calendar per explicit year")
        years.add(year)
        first, last = date(year, 1, 1), date(year, 12, 31)
        for interval in payload["closed_ranges"]:
            left, right = (date.fromisoformat(value) for value in interval.split("/"))
            if left > right or left.year != year or right.year != year:
                raise ValueError("Fixed calendar closure must be an ordered range within its year")
        closed = _closed_days(payload)
        overrides = {date.fromisoformat(value) for value in payload.get("open_sessions", [])}
        if any(day.year != year for day in closed | overrides):
            raise ValueError("Fixed calendar dates must stay within its declared year")
        natural = [first + timedelta(days=index) for index in range((last - first).days + 1)]
        opened = {day.isoformat() for day in natural
                  if day in overrides or (day.weekday() < 5 and day not in closed)}
        fixed_sessions.update(opened)
        observed = set(source.loc[source.date.str.startswith(f"{year:04d}-")
                                  & source.date.le(end), "date"])
        unexpected = sorted(observed - opened)
        expected = sorted(day for day in opened if start <= day <= end)
        missing = sorted(set(expected) - observed)
        if missing or unexpected:
            raise ValueError(f"Fixed calendar/raw session mismatch for {year}: "
                             f"missing_open_sessions={missing}; noncalendar_raw_sessions={unexpected}")
        verified_start, verified_end = max(start, first.isoformat()), min(end, last.isoformat())
        if verified_start > verified_end:
            raise ValueError("Fixed calendar year does not intersect the replay window")
        files.append({"path": str(path), "sha256": sha256(raw).hexdigest(), "year": year,
                      "verified_start": verified_start, "verified_end": verified_end,
                      "expected_open_sessions": expected, "observed_year_sessions_checked": len(observed)})
    replay_years = set(range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1))
    result = {"status": "verified_explicit_years" if files else "legacy_raw_dates_unverified",
            "files": files, "verified_years": sorted(years),
            "unverified_replay_years": sorted(replay_years - years),
            "entire_replay_calendar_verified": bool(files) and replay_years <= years}
    if include_fixed_sessions:
        result["fixed_open_sessions"] = sorted(fixed_sessions)
    return result


def _resume_inputs(resume_run, source, start, end, config, variants, dates, *, resume_selected_variants=False,
                   resume_source_snapshot=None, month_end_schedule=None, month_end_calendar=None,
                   full_exit_lifecycle=None, allow_derived_execution_reference=False):
    path = Path(resume_run).resolve()
    if path.is_dir():
        path = path / "run.json"
    previous = json.loads(path.read_text(encoding="utf-8"))
    if (previous.get("schema") != SCHEMA
            or previous.get("status") not in {"completed_research_only", "blocked_research_only", "no_new_session"}
            or previous.get("research_only") is not True
            or previous.get("execution_admitted") is not False):
        raise ValueError("Resume requires a terminal unadmitted StockDB rolling run")
    original_variants = previous.get("variants")
    variants_match = original_variants == list(variants)
    if resume_selected_variants:
        variants_match = (isinstance(original_variants, list)
                          and len(set(original_variants)) == len(original_variants)
                          and set(variants) <= set(original_variants))
    if (previous.get("config") != config.to_dict() or not variants_match
            or previous.get("replay_start") != start or previous["replay_end"] > end):
        raise ValueError("Resume strategy, costs, variants or replay interval changed")
    old_artifacts = Path(previous["artifacts"]).resolve()
    protocol = json.loads((old_artifacts / "protocol.json").read_text(encoding="utf-8"))
    if (protocol.get("config") != config.to_dict() or protocol.get("variants") != original_variants
            or protocol.get("replay_start") != start or protocol.get("replay_end") != previous["replay_end"]
            or protocol.get("lookback") != LOOKBACK or protocol.get("skip") != SKIP
            or protocol.get("rank_cadence") != "monthly_completed_session_end"):
        raise ValueError("Resume fixed signal protocol changed")
    if (previous.get("month_end_schedule") != month_end_schedule
            or protocol.get("month_end_schedule") != month_end_schedule):
        raise ValueError("Resume month-end schedule mode or fixed calendar binding changed")
    if (previous.get("full_exit_lifecycle") != full_exit_lifecycle
            or protocol.get("full_exit_lifecycle") != full_exit_lifecycle):
        raise ValueError("Resume full-exit lifecycle changed; fresh replay required")
    original_snapshot = Path(protocol["snapshot"]).resolve()
    old_snapshot = Path(resume_source_snapshot).resolve() if resume_source_snapshot is not None else original_snapshot
    manifest_bytes = (old_snapshot / "manifest.json").read_bytes()
    old_manifest = json.loads(manifest_bytes)
    if old_manifest != protocol["snapshot_manifest"]:
        raise ValueError("Previously pinned StockDB manifest changed")
    source_proof = {"resume_original_snapshot": str(original_snapshot)}
    if resume_source_snapshot is not None:
        source_proof["explicit_reconstructed_resume_snapshot"] = {
            "path": str(old_snapshot), "manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "basis": "Explicit old-source reconstruction; full manifest equals the prior protocol snapshot_manifest; "
                     "original chunk hashes and completed raw/calendar/month-end prefixes remain required"}
    old_supplement = (protocol.get("supplemental_raw_view") or {}).get("path")
    if allow_derived_execution_reference:
        _, _, before, _ = _read_snapshot(
            old_snapshot, old_supplement, end=previous["replay_end"],
            allow_derived_execution_reference=True)
    else:
        # Keep the legacy seam callable by narrow test/source adapters that
        # implement the original three-argument loader contract.
        _, _, before, _ = _read_snapshot(
            old_snapshot, old_supplement, end=previous["replay_end"])
    checkpoints = {}
    for variant in variants:
        results = [row for row in previous.get("results", []) if row.get("signal") == variant]
        if len(results) != 1:
            raise ValueError("Every selected variant requires one saved result and checkpoint")
        checkpoint_options = ({"full_exit_lifecycle": full_exit_lifecycle}
                              if full_exit_lifecycle is not None else {})
        checkpoint = load_replay_checkpoint(old_artifacts / variant, config, dates, **checkpoint_options)
        if results[0].get("completed_sessions") != len(checkpoint["daily"]):
            raise ValueError("Selected variant result/checkpoint session count differs")
        _validate_resume_prefix(before, source, previous["replay_end"], end, checkpoint["state"]["day"],
                                previous_quote_semantics=protocol.get("quote_semantics", LEGACY_QUOTE_SEMANTICS),
                                engine_quote_start=protocol["replay_start"], month_end_calendar=month_end_calendar)
        checkpoints[variant] = checkpoint
    return path, previous, old_artifacts, checkpoints, source_proof


def _saved_rows(path, last_day):
    try:
        rows = pd.read_csv(path, dtype={"date": str, "symbol": str})
    except pd.errors.EmptyDataError:
        return []
    return rows.loc[rows.date.le(last_day)].to_dict("records") if last_day is not None else []


def run(snapshot, start, end, initial_cash=140000.0, variants=VARIANTS, supplemental_raw_view=None,
        *, resume_run=None, resume_selected_variants=False, calendars=None, resume_source_snapshot=None,
        calendar_driven_month_ends=False, persistent_full_exits=False,
        allow_derived_execution_reference=False):
    if type(calendar_driven_month_ends) is not bool:
        raise ValueError("Calendar-driven month ends must be an explicit boolean")
    if type(persistent_full_exits) is not bool:
        raise ValueError("Persistent full exits must be an explicit boolean")
    if type(allow_derived_execution_reference) is not bool:
        raise ValueError("Derived execution reference mode must be an explicit boolean")
    if persistent_full_exits and not calendar_driven_month_ends:
        raise ValueError("Persistent full exits require calendar-driven month ends")
    full_exit_lifecycle = FULL_EXIT_LIFECYCLE if persistent_full_exits else None
    lifecycle_fields = ({"full_exit_lifecycle": full_exit_lifecycle}
                        if full_exit_lifecycle is not None else {})
    if resume_source_snapshot is not None and resume_run is None:
        raise ValueError("--resume-source-snapshot requires --resume-run")
    quote_semantics = (DERIVED_QUOTE_SEMANTICS if allow_derived_execution_reference
                       else QUOTE_SEMANTICS)
    snapshot, manifest, source, supplemental = _read_snapshot(
        snapshot, supplemental_raw_view, end=end,
        allow_derived_execution_reference=allow_derived_execution_reference)
    if calendar_driven_month_ends:
        source = source.loc[source.date.le(end)].copy()
    config = portfolio(initial_cash)
    if not start <= end:
        raise ValueError("Replay start must not follow end")
    all_dates = sorted(source.date.unique())
    replay_dates = [day for day in all_dates if start <= day <= end]
    if not replay_dates:
        raise ValueError("No StockDB sessions in replay window")
    if not variants or len(set(variants)) != len(variants) or any(variant not in VARIANTS for variant in variants):
        raise ValueError("Expected unique fixed StockDB variants")
    if end not in replay_dates:
        raise ValueError(f"Replay end is not a StockDB session: {end}")
    if resume_selected_variants and resume_run is None:
        raise ValueError("Explicit variant selection requires --resume-run")
    calendar_verification = _calendar_verification(calendars, source, start, end,
        include_fixed_sessions=calendar_driven_month_ends)
    schedule_fields, fixed_calendar = {}, None
    if calendar_driven_month_ends:
        if not calendar_verification["entire_replay_calendar_verified"]:
            raise ValueError("Calendar-driven month ends require fixed calendars for every replay year")
        fixed_calendar = calendar_verification["fixed_open_sessions"]
        if not any(day > end for day in fixed_calendar):
            raise ValueError("Calendar-driven month ends require an exact fixed-calendar successor after the raw cutoff")
        schedule_fields = {"month_end_schedule": {"method_id": CALENDAR_MONTH_END_METHOD,
            "calendar_files": sorted([{key: item[key] for key in ("path", "sha256", "year")}
                for item in calendar_verification["files"]], key=lambda item: item["year"])}}
    resumed = (_resume_inputs(resume_run, source, start, end,
                              config, variants, replay_dates, resume_selected_variants=resume_selected_variants,
                              resume_source_snapshot=resume_source_snapshot,
                              month_end_schedule=schedule_fields.get("month_end_schedule"),
                              month_end_calendar=fixed_calendar,
                              allow_derived_execution_reference=allow_derived_execution_reference,
                              **lifecycle_fields)
               if resume_run is not None else None)
    resume_source_proof = resumed[4] if resumed is not None else {}
    resume_selection = ({"original_variants": list(resumed[1]["variants"]),
                         "resume_selected_variants": list(variants),
                         "unselected_variant_results": [deepcopy(row) for row in resumed[1]["results"]
                                                        if row["signal"] not in variants]}
                        if resume_selected_variants else {})
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    artifacts = DATA_ROOT / "research" / "stockdb_rolling_low_volatility" / identifier
    out = ROOT / "outputs" / "stockdb_rolling_low_volatility" / identifier
    out.mkdir(parents=True, exist_ok=False)
    if resumed is not None and all(len(checkpoint["daily"]) == len(replay_dates)
                                  for checkpoint in resumed[3].values()):
        path, previous, old_artifacts, checkpoints, _ = resumed
        record = deepcopy(previous)
        record["variants"] = list(variants)
        record["results"] = [row for row in record["results"] if row["signal"] in variants]
        record.update(status="no_new_session", continuation_status="no_new_session", resume_run=str(path),
                      artifacts=str(old_artifacts), reused_sessions_by_variant={
                          variant: len(checkpoints[variant]["daily"]) for variant in variants},
                      newly_completed_sessions=0, account_reset=False,
                      checked_at=datetime.now().astimezone().isoformat(),
                      calendar_verification=calendar_verification, **resume_selection, **resume_source_proof)
        for result in record["results"]:
            result.update(reused_sessions=len(checkpoints[result["signal"]]["daily"]), newly_completed_sessions=0)
        write_json(out / "run.json", record)
        protocol = json.loads((old_artifacts / "protocol.json").read_text(encoding="utf-8"))
        protocol.update(resume_run=str(path), variants=list(variants), account_reset=False,
                        calendar_verification=calendar_verification, **resume_selection, **resume_source_proof)
        write_json(out / "protocol.json", protocol)
        _write_report(out, record)
        return out, record
    artifacts.mkdir(parents=True, exist_ok=False)
    protocol = {
        "schema": SCHEMA, "snapshot": str(snapshot), "snapshot_manifest": manifest,
        "source_start": all_dates[0], "source_end": all_dates[-1], "replay_start": start, "replay_end": end,
        "symbols": int(source.symbol.nunique()), "sessions": len(replay_dates),
        "supplemental_raw_view": supplemental,
        "quote_semantics": quote_semantics,
        "execution_reference_mode": ("derived_stockdb_research_only"
                                       if allow_derived_execution_reference
                                       else "native_status_required"),
        "calendar_verification": calendar_verification, **resume_selection, **resume_source_proof,
        **schedule_fields,
        **lifecycle_fields,
        "variants": list(variants), "config": config.to_dict(), "lookback": LOOKBACK, "skip": SKIP,
        "selection": "monthly completed-session ends; raw 252-session volatility; 21-session skip momentum; 20 names/rank30",
        "rank_cadence": "monthly_completed_session_end",
        "eligibility": "ordinary SH/SZ boards, positive volume, non-ST, finite features, 20-session median amount >= 20m CNY; listing age unavailable",
        "source_semantics": "StockDB provider raw OHLC/volume/amount; no PIT or corporate-action promotion",
        "limitations": [
            "StockDB raw provider prices and retrieval-time universe remain research-only.",
            "Point-in-time membership, listing master and corporate actions are not verified.",
            "Supplemental raw_daily rows fill missing keys only and do not establish independent source agreement.",
            "This is a same-rule longer diagnostic, not an untouched holdout.",
        ],
        "research_only": True, "execution_admitted": False, "strategy_validated": False,
        "fresh_holdout_claimed": False, "account_backtest": True,
    }
    write_json(out / "protocol.json", protocol)
    write_json(artifacts / "protocol.json", protocol)
    record = {"schema": SCHEMA, "status": "running", "artifacts": str(artifacts),
              "source_start": all_dates[0], "source_end": all_dates[-1], "replay_start": start,
              "replay_end": end, "symbols": int(source.symbol.nunique()), "sessions": len(replay_dates),
              "supplemental_raw_view": supplemental,
              "quote_semantics": quote_semantics,
              "execution_reference_mode": ("derived_stockdb_research_only"
                                             if allow_derived_execution_reference
                                             else "native_status_required"),
              "calendar_verification": calendar_verification, **resume_selection, **resume_source_proof,
              **schedule_fields,
              **lifecycle_fields,
              "config": config.to_dict(), "variants": list(variants), "results": [],
              "research_only": True, "execution_admitted": False, "strategy_validated": False,
              "fresh_holdout_claimed": False}
    if resumed is not None:
        record.update(resume_run=str(resumed[0]), continuation_status="advancing_saved_replay",
                      account_reset=False, reused_sessions_by_variant={
                          variant: len(resumed[3][variant]["daily"]) for variant in variants})
        protocol.update(resume_run=str(resumed[0]), account_reset=False)
        write_json(out / "protocol.json", protocol)
        write_json(artifacts / "protocol.json", protocol)
    write_json(artifacts / "run.json", record)
    for variant in variants:
        feed = (RollingStockDBFeed(source, end, variant=variant, fixed_calendar=fixed_calendar,
                                   quote_semantics=quote_semantics,
                                   allow_derived_execution_reference=allow_derived_execution_reference)
                if calendar_driven_month_ends
                else RollingStockDBFeed(source, end, variant=variant,
                                        quote_semantics=quote_semantics,
                                        allow_derived_execution_reference=allow_derived_execution_reference))
        checkpoint = resumed[3][variant] if resumed is not None else None
        result = baseline_ledger_result(feed, config, replay_dates, artifacts / variant,
                                        checkpoint=checkpoint, **lifecycle_fields)
        result.update(signal=variant, label=variant)
        last_day = checkpoint["state"]["day"] if checkpoint else None
        old_audit = _saved_rows(resumed[2] / f"{variant}_decisions.csv", last_day) if checkpoint else []
        old_ranks = _saved_rows(resumed[2] / f"{variant}_ranks.csv", last_day) if checkpoint else []
        pd.DataFrame(old_audit + feed.audit).to_csv(artifacts / f"{variant}_decisions.csv", index=False)
        pd.DataFrame(old_ranks + feed.ranking_rows).to_csv(artifacts / f"{variant}_ranks.csv", index=False)
        record["results"].append(result)
        write_json(artifacts / "run.json", record)
    record["status"] = "completed_research_only" if all(item["status"] == "completed" for item in record["results"]) else "blocked_research_only"
    record["completed_at"] = datetime.now().astimezone().isoformat()
    if resumed is not None:
        record["continuation_status"] = "continued_research_only" if record["status"] == "completed_research_only" else "blocked_research_only"
    write_json(artifacts / "run.json", record)
    write_json(out / "run.json", record)
    pd.DataFrame(record["results"]).to_csv(out / "metrics.csv", index=False)
    _write_report(out, record)
    return out, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--start", default="2025-01-06")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--initial-cash", type=float, default=140000.0)
    parser.add_argument("--supplemental-raw-view", type=Path,
                        help="Optional local raw_daily.parquet used only to fill missing StockDB code/date rows")
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    parser.add_argument("--resume-run", type=Path,
                        help="Continue a terminal matching research account; preserve raw/calendar/month-end prefixes")
    parser.add_argument("--resume-selected-variants", action="store_true",
                        help="Explicitly resume only --variant selections from a saved multi-variant batch")
    parser.add_argument("--resume-source-snapshot", type=Path,
                        help="Explicit reconstructed old source; manifest must exactly equal the prior pinned manifest")
    parser.add_argument("--calendar", type=Path, action="append",
                        help="Fixed cn_a_share_exchange_calendar_v1 JSON; repeat for each verified year")
    parser.add_argument("--calendar-driven-month-ends", action="store_true",
                        help="Opt in to fixed-calendar month ends and exact next session; all replay years must be verified")
    parser.add_argument("--persistent-full-exits", action="store_true",
                        help="New research account only: continue blocked full exits until flat or replaced by a newer full target; requires --calendar-driven-month-ends")
    parser.add_argument("--allow-derived-execution-reference", action="store_true",
                        help="Research-only: allow StockDB rows without independent native execution status")
    args = parser.parse_args(argv)
    if args.resume_selected_variants and not args.variant:
        parser.error("--resume-selected-variants requires explicit --variant")
    out, record = run(args.snapshot, args.start, args.end, args.initial_cash,
                      tuple(args.variant) if args.variant else VARIANTS,
                      supplemental_raw_view=args.supplemental_raw_view, resume_run=args.resume_run,
                      resume_selected_variants=args.resume_selected_variants, calendars=args.calendar,
                      resume_source_snapshot=args.resume_source_snapshot,
                      calendar_driven_month_ends=args.calendar_driven_month_ends,
                      allow_derived_execution_reference=args.allow_derived_execution_reference,
                      **({"persistent_full_exits": True} if args.persistent_full_exits else {}))
    print(out, flush=True)
    return 0 if record["status"] in {"completed_research_only", "no_new_session"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
