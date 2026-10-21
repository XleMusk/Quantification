"""Same-source signal migration/dated-rank diagnostic through the shared Engine.

This is a retrospective diagnostic on an already inspected StockDB source.
The old global-batch ensemble is retained only to measure the ranking defect.
No model fitting, feature regeneration, account database or broker is used.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from numbers import Real
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, SignalUnavailable, TradingMode
from ..engine import advance
from ..ledger import Ledger, new_state
from ..report import metrics
from ..rules import stock_board
from ..symbols import normalize_symbol
from .exchange_calendar import DEFAULT_CALENDAR_PATH, _closed_days, load_calendar
from .native_history_source import NativeHistorySource
from .stockdb_rolling_low_volatility import SOURCE_COLUMNS, _DayRows


METHOD = "stockdb_same_source_rule_daylocal_lgbm_legacy_rank_diagnostic_v1"
ACTION_METHOD = "stockdb_same_source_rule_daylocal_lgbm_reported_action_accounting_diagnostic_v1"
NATIVE_ACTION_METHOD = "stockdb_frozen_signal_native_whole_quote_reported_actions_diagnostic_v1"
SEEDS = ["score_seed17", "score_seed29", "score_seed43"]
ARMS = ("risk_adjusted_momentum", "lgbm_day_local", "lgbm_legacy_global_diagnostic")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _hash(path):
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repair_ranks(scores):
    """Reuse saved raw predictions; never read labels or recompute a model."""
    frame = scores.copy()
    needed = {"date", "symbol", "rank_mean", *SEEDS}
    if not needed <= set(frame):
        raise ValueError("Saved raw seed predictions and original rank_mean required")
    frame["date"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate dated prediction identity")
    frame[SEEDS] = frame[SEEDS].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(frame[SEEDS].to_numpy()).all():
        raise ValueError("Nonfinite saved raw predictions")
    frame["rank_mean_day_local"] = frame.groupby("date", sort=False)[SEEDS].rank(pct=True).mean(axis=1)
    return frame


class ControlledFeed:
    """Only ranking differs; every arm shares this exact dated eligible pool."""
    def __init__(self, raw, closes, scores, instruments, calendar, config, *, arm):
        if arm not in ARMS:
            raise ValueError("Unknown fixed diagnostic arm")
        self.arm, self.calendar = arm, list(calendar)
        self.symbols = list(closes.columns)
        self.instruments = instruments
        self.actions, self.instrument_profiles = [], {}
        self.action_accounting = None
        self.manifest = {"replay_only": True, "scope": METHOD, "pit_verified": False}
        self.rows = _DayRows({d: g.set_index("symbol") for d, g in raw.groupby("date", sort=True)})
        self.volumes = raw.pivot(index="date", columns="symbol", values="volume").reindex(columns=self.symbols)
        amounts = raw.pivot(index="date", columns="symbol", values="amount").reindex(columns=self.symbols)
        self.liquidity = amounts.rolling(20, min_periods=20).median()
        self.momentum = closes.shift(config.skip) / closes.shift(config.lookback) - 1
        self.volatility = closes.pct_change(fill_method=None).rolling(
            config.volatility_window, min_periods=config.volatility_window).std() * np.sqrt(252)
        self.scores = {d: g.set_index("symbol") for d, g in scores.groupby("date", sort=True)}
        self.pool_cache, self.audit = {}, []

    def next_day(self, day):
        return self.calendar[self.calendar.index(day) + 1]

    def pool(self, day, config):
        if day in self.pool_cache:
            return self.pool_cache[day]
        rows, scores = self.rows.get(day, {}), self.scores.get(day)
        pool = []
        rejected = {}
        for symbol in self.symbols:
            row, info = rows.get(symbol), self.instruments.get(symbol, {})
            reason = None
            if (row is None or any(not isinstance(row.get(field), Real) or isinstance(row.get(field), bool)
                                   for field in ("tradestatus", "isST"))
                    or row.get("tradestatus") != 1 or row.get("isST") != 0):
                reason = "missing_or_inactive_or_st_current_raw"
            elif not info.get("ipoDate") or (pd.Timestamp(day) - pd.Timestamp(info["ipoDate"])).days < config.min_listing_days:
                reason = "listing_date_unknown_or_too_recent"
            elif info.get("outDate") and day >= info["outDate"]:
                reason = "outside_reported_listing_period"
            elif scores is None or symbol not in scores.index:
                reason = "same_day_seed_score_missing"
            else:
                m, v = self.momentum.at[day, symbol], self.volatility.at[day, symbol]
                amount = self.liquidity.at[day, symbol]
                if not np.isfinite([m, v, amount]).all() or v <= 0:
                    reason = "rule_history_or_liquidity_unknown"
                elif amount < config.min_amount:
                    reason = "below_fixed_liquidity_minimum"
                elif m <= 0:
                    reason = "nonpositive_fixed_momentum_gate"
                else:
                    pool.append({"symbol": symbol, "risk_adjusted_momentum": float(m / v),
                        "lgbm_day_local": float(scores.at[symbol, "rank_mean_day_local"]),
                        "lgbm_legacy_global_diagnostic": float(scores.at[symbol, "rank_mean"])})
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
        self.pool_cache[day] = pool
        self.audit.append({"date": day, "eligible": len(pool), "excluded": rejected})
        return pool

    def signal_frame(self, day, config, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("Retrospective replay only")
        if day not in self.scores:
            return SignalUnavailable("exact_dated_scores_missing")
        rows = [{"symbol": r["symbol"], "score": r[self.arm]} for r in self.pool(day, config)]
        rows.sort(key=lambda row: (-row["score"], row["symbol"]))
        return SignalFrame.from_legacy(rows, day, "research:" + METHOD + ":" + self.arm, context)

    def validate_session(self, state, day):
        required = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}
        required.update((state.get("pending_targets") or {}).keys())
        rows = self.rows.get(day, {})
        for symbol in required:
            row = rows.get(symbol)
            finite = lambda value: isinstance(value, Real) and not isinstance(value, bool) and np.isfinite(value)
            if (row is None or isinstance(row.get("tradestatus"), bool) or row.get("tradestatus") not in {0, 1}
                    or isinstance(row.get("isST"), bool) or row.get("isST") not in {0, 1}
                    or any(not finite(row.get(field)) or row[field] <= 0 for field in ("close", "preclose"))
                    or not finite(row.get("open")) or row["open"] < 0
                    or (row["open"] == 0 and row["tradestatus"] != 0)):
                raise ValueError(f"Unknown required raw quote/status: {symbol} {day}; no future-path screening")
        if self.action_accounting is not None:
            self.action_accounting.validate(state, day, self.rows)


def append_native_gaps(raw, native):
    """Fill absent date/identity keys only; retain blanks and explicit halt status."""
    keys = pd.MultiIndex.from_frame(raw[["date", "symbol"]])
    selected = native.loc[~pd.MultiIndex.from_frame(native[["date", "symbol"]]).isin(keys)].copy()
    if selected.duplicated(["date", "symbol"]).any():
        raise ValueError("Duplicate native supplemental key")
    if not selected.adjustflag.eq("3").all():
        raise ValueError("Native supplementation must use original raw quotes")
    if selected.empty:
        return raw, []
    originals = selected[["date", "symbol", "open", "high", "low", "close", "preclose", "volume",
                          "amount", "tradestatus", "isST", "adjustflag"]].to_dict("records")
    for column in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
    for field in ("tradestatus", "isST"):
        selected[field] = selected[field].map({"0": 0, "1": 1})
    # No blank volume/amount is turned into a valid zero, even for known halts.
    return pd.concat([raw, selected], ignore_index=True), originals


def raw_history_context(provider_calendar, start):
    """Keep 21 actual provider sessions before D, including prior-year context."""
    if provider_calendar != sorted(set(provider_calendar)) or start not in provider_calendar:
        raise ValueError("Unique ordered pinned provider calendar and exact score start required")
    index = provider_calendar.index(start)
    if index < 21:
        raise ValueError("Pinned provider calendar lacks the preceding 21 raw-history sessions")
    preceding = provider_calendar[index - 21:index]
    prior_years = sorted({int(day[:4]) for day in preceding if day[:4] != start[:4]})
    return preceding[0], {"raw_start": preceding[0], "score_start": start,
        "preceding_provider_sessions": preceding, "preceding_provider_session_count": 21,
        "context_calendar_basis": "already_pinned_provider_calendar_exact_previous_21_sessions",
        "execution_calendar_year": int(start[:4]), "execution_calendar_remains_current_year_only": True,
        "prior_year_context_years": prior_years,
        "prior_year_exchange_calendar_independently_verified": False if prior_years else None}


def append_native_sources(raw, views, symbols, requested_days):
    """Read explicit disjoint native days and append absent scope keys only."""
    values = [] if views is None else [views] if isinstance(views, (str, Path)) else list(views)
    paths = [Path(value).resolve() for value in values]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate explicit native-gap source")
    requested = set(requested_days)
    proofs, originals = [], []
    with ExitStack() as stack:
        sources, ownership = [], {}
        # Check source-day ownership before reading any price rows. Overlap
        # outside this requested context/replay window does not select a source.
        for path in paths:
            source = stack.enter_context(NativeHistorySource(path))
            days = [day for day in source.calendar if day in requested]
            for day in days:
                if day in ownership:
                    raise ValueError(f"Overlapping native-gap sources for requested day {day}")
                ownership[day] = str(path)
            sources.append((path, source, days))
        for path, source, days in sources:
            read_days, appended = [], 0
            for day in days:
                present = set(raw.loc[raw.date.eq(day), "symbol"])
                missing = [symbol for symbol in symbols if symbol not in present]
                if not missing:
                    continue
                native = source.frame(day, missing)
                if not native.empty and (not native.date.eq(day).all() or not native.symbol.isin(missing).all()):
                    raise ValueError("Native gap reader returned a date or symbol outside the exact missing keys")
                raw, additions = append_native_gaps(raw, native)
                read_days.append(day)
                originals.extend(additions)
                appended += len(additions)
            proofs.append({"source_view": str(path), "requested_days": days, "read_days": read_days,
                "appended_rows": appended, "source_proof": source.provenance})
    return raw, proofs, originals


def _sources(batch_path, calendar_path, listing_path, native_gap_view=None, *, execution_native_view=None):
    batch_path = Path(batch_path).resolve()
    if batch_path.is_dir():
        batch_path /= "result.json"
    batch = _json(batch_path)
    if batch.get("status") != "completed_research_only_dated_score_batch":
        raise ValueError("Completed frozen dated batch required")
    for flag in ("model_refit", "future_labels_used_for_score", "execution_admitted", "strategy_validated"):
        if batch.get(flag) is not False:
            raise ValueError(f"Unverified score flag: {flag}")
    native = _json(batch["native_run"])
    provider_path = Path(native["provider_view"]) / "result.json"
    provider = _json(provider_path)
    snapshot = Path(provider["snapshot"])
    manifest_path = snapshot / "manifest.json"
    if _hash(manifest_path) != provider["snapshot_manifest_sha256"]:
        raise ValueError("Native feature provider's raw snapshot manifest changed")
    manifest = _json(manifest_path)
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("Unadmitted StockDB inventory required")
    if not execution_native_view:
        raise ValueError("StockDB pre_close has mixed price basis and cannot be an execution reference; "
                         "an explicit independent whole-quote --execution-native-view is required")
    if native_gap_view:
        raise ValueError("Independent whole execution quotes cannot be mixed with StockDB gap supplementation")
    model = _json(batch["model_run"])
    config = StrategyConfig(**model["portfolio_config"]).validate()
    scores = repair_ranks(pd.read_parquet(batch["score_path"]))
    if len(scores) != batch["score_rows"] or sorted(scores.date.unique()) != batch["score_dates"]:
        raise ValueError("Saved score identities changed")
    start, end = scores.date.min(), scores.date.max()
    calendar_record = load_calendar(Path(calendar_path))
    if calendar_record is None or str(calendar_record["year"]) != start[:4] or start[:4] != end[:4]:
        raise ValueError("Exact pinned exchange calendar year required")
    closed = _closed_days(calendar_record)
    calendar = [d.strftime("%Y-%m-%d") for d in pd.date_range(start[:4] + "-01-01", start[:4] + "-12-31")
                if d.weekday() < 5 and d.date() not in closed]
    days = [d for d in calendar if start <= d <= end]
    if days != sorted(scores.date.unique()):
        raise ValueError("Score batch has missing or extra exchange sessions")
    if end not in calendar or calendar.index(end) + 1 >= len(calendar):
        raise ValueError("Exact calendar successor required")
    provider_calendar = [pd.to_datetime(str(d), format="%Y%m%d").strftime("%Y-%m-%d") for d in provider["calendar"]]
    if [d for d in provider_calendar if start[:4] + "-01-01" <= d <= end] != [d for d in calendar if d <= end]:
        raise ValueError("Provider's current-year feature calendar differs from pinned exchange calendar")
    # The provider result stores a count; native partitions retain the frozen
    # instrument identities.  Do not derive this cohort from later score hits.
    cohort = [s for part in native["partitions"] for s in part["symbols"]]
    if len(cohort) != len(set(cohort)) or len(cohort) != provider["symbols"] or len(cohort) != native["symbols"]:
        raise ValueError("Native/provider instrument cohort mismatch")
    symbols, unsupported = [], []
    for symbol in sorted(cohort):
        try:
            stock_board(symbol)
        except ValueError:
            unsupported.append(symbol)
        else:
            symbols.append(symbol)
    # Read only existing normalized close binaries, not Alpha158 or future labels.
    arrays, binary_hash = {}, sha256()
    for symbol in symbols:
        code = symbol.split(".")[0]
        qcode = ("sh" if symbol.endswith(".SSE") else "sz") + code
        file = Path(provider["provider_folder"]) / "features" / qcode / "close.day.bin"
        data = file.read_bytes()
        binary_hash.update(qcode.encode()); binary_hash.update(data)
        values = np.frombuffer(data, dtype="<f4")
        if not len(values) or not np.isfinite(values[0]) or values[0] != int(values[0]):
            raise ValueError("Invalid Qlib close binary offset")
        offset = int(values[0])
        if offset < 0 or offset + len(values) - 1 > len(provider_calendar):
            raise ValueError("Qlib close binary exceeds its pinned calendar")
        array = np.full(len(provider_calendar), np.nan)
        array[offset:offset + len(values) - 1] = values[1:]
        arrays[symbol] = array
    closes = pd.DataFrame(arrays, index=provider_calendar)
    # StockDB stays the frozen signal provider.  The execution stream is an
    # independent whole-bar source, never a replacement of its pre_close field.
    raw_start, raw_context = raw_history_context(provider_calendar, start)
    from .native_execution_quotes import load_native_execution_quotes
    raw, execution_provenance = load_native_execution_quotes(execution_native_view, symbols,
        [day for day in provider_calendar if raw_start <= day <= end])
    master = pd.read_csv(listing_path, dtype=str).fillna("")
    instruments = {}
    for row in master.to_dict("records"):
        if row.get("type") != "1":
            continue
        try:
            symbol = normalize_symbol(row["code"]).vt_symbol
        except ValueError:
            continue
        if symbol in symbols:
            value = {"ipoDate": row.get("ipoDate", ""), "outDate": row.get("outDate", "")}
            if symbol in instruments and instruments[symbol] != value:
                raise ValueError("Conflicting listing records")
            instruments[symbol] = value
    refs = {"batch": str(batch_path), "batch_sha256": _hash(batch_path), "raw_seed_scores": batch["score_path"],
        "raw_seed_scores_sha256": _hash(batch["score_path"]), "native_run": batch["native_run"],
        "provider": str(provider_path), "provider_sha256": _hash(provider_path), "snapshot": str(snapshot),
        "snapshot_manifest_sha256": _hash(manifest_path), "raw_chunks": [],
        "normalized_close_binary_aggregate_sha256": binary_hash.hexdigest(), "close_binary_count": len(symbols),
        "listing_master": str(Path(listing_path).resolve()), "listing_master_sha256": _hash(listing_path),
        "calendar": str(Path(calendar_path).resolve()), "calendar_sha256": _hash(calendar_path),
        "model_run": batch["model_run"], "unsupported_provider_symbols": unsupported,
        "native_gap_proof": None, "native_gap_rows": [], "native_gap_sources": [],
        "execution_native_quotes": execution_provenance,
        "stockdb_pre_close_used_for_execution": False,
        "raw_history_context": raw_context,
        "original_stockdb_values_replaced": False, "provider_features_recomputed": False}
    return raw, closes, scores, instruments, calendar, config, days, refs


def _resume_source_identity(sources):
    """Only native reader opened_at is a new observation, not a source change."""
    result = deepcopy(sources)
    if isinstance(result.get("native_gap_proof"), dict):
        result["native_gap_proof"].pop("opened_at", None)
    for item in result.get("native_gap_sources", []):
        item["source_proof"].pop("opened_at", None)
    return result


def _saved_daily(path, count):
    if count == 0:
        try:
            frame = pd.read_csv(path, dtype={"date": str})
        except pd.errors.EmptyDataError:
            return []
    else:
        frame = pd.read_csv(path, dtype={"date": str})
    for column in frame.columns:
        if column == "event_time_frontier":
            frame[column] = frame[column].astype(object)
        # Engine's nested receipts/events are Python container repr in CSV.
        # Literal parsing also ensures '[]' never counts as a stale valuation.
        for index, value in frame[column].items():
            if column == "event_time_frontier" and (value is None or isinstance(value, float) and np.isnan(value)):
                # Engine records None on a session with no new SignalFrame;
                # CSV preserves it as an empty cell, not an empty frontier.
                frame.at[index, column] = None
                continue
            if isinstance(value, str) and value.lstrip().startswith(("[", "{", "(")):
                try:
                    parsed = ast.literal_eval(value)
                except (ValueError, SyntaxError) as error:
                    raise ValueError("Saved daily container is not a valid literal") from error
                if not isinstance(parsed, (list, dict, tuple)):
                    raise ValueError("Saved daily container changed type")
                frame.at[index, column] = parsed
    daily = frame.to_dict("records")
    if "event_time_frontier" not in frame or any(not isinstance(row.get("stale_symbols"), list)
           or not all(isinstance(symbol, str) for symbol in row["stale_symbols"])
           or not isinstance(row.get("events"), list)
           or (row["event_time_frontier"] is not None and not isinstance(row["event_time_frontier"], dict)) for row in daily):
        raise ValueError("Saved daily stale/events/frontier containers are incomplete")
    return daily


def _resume_incomplete(original, out, method, days, config, arms):
    path = Path(original).resolve()
    path = path / "result.json" if path.is_dir() else path
    directory = path.parent
    if out == directory or directory in out.parents or out in directory.parents:
        raise ValueError("Resume output must be a new directory outside the original incomplete run")
    record, original_method = _json(path), _json(directory / "method.json")
    if (record.get("schema") != METHOD or record.get("status") != "incomplete"
            or record.get("method") != original_method
            or any(record.get(k) is not False for k in ("model_refit", "future_labels_used_for_score", "execution_admitted", "strategy_validated"))
            or original_method.get("method") != METHOD or list(original_method.get("arms", [])) != list(arms)
            or original_method.get("config") != config.to_dict()
            or original_method.get("start") != days[0] or original_method.get("end") != days[-1]
            or _resume_source_identity(original_method["sources"]) != _resume_source_identity(method["sources"])):
        raise ValueError("Incomplete resume method, config, arms, dates or exact source proofs changed")
    for key, value in original_method.items():
        if key not in {"declared_at", "sources"} and key in method and json.loads(json.dumps(method[key])) != value:
            raise ValueError("Incomplete resume changed an original diagnostic rule")
    if [row["arm"] for row in record["results"]] != list(arms):
        raise ValueError("Incomplete run case order differs from its original fixed arms")
    references = {str(file.resolve()): _hash(file) for file in (path, directory / "method.json")}
    cases = {}
    for result in record["results"]:
        arm = result["arm"]
        folder = directory / arm
        files = sorted(file for file in folder.rglob("*") if file.is_file())
        if not {"account.json", "result.json", "daily.csv", "orders.csv", "fills.csv"} <= {file.name for file in files}:
            raise ValueError("Incomplete run is missing its saved case files")
        references.update({str(file.resolve()): _hash(file) for file in files})
        if _json(folder / "result.json") != result:
            raise ValueError("Original case result no longer equals the incomplete run summary")
        state = _json(folder / "account.json")
        count = result["sessions"]
        if type(count) is not int or not 0 <= count <= len(days) or state.get("broker_binding"):
            raise ValueError("Invalid or broker-bound saved replay prefix")
        daily = _saved_daily(folder / "daily.csv", count)
        last = days[count-1] if count else None
        if (len(daily) != count or [row["date"] for row in daily] != days[:count] or state["day"] != last
                or len(state["orders"]) != result["orders"] or len(state["fills"]) != result["fills"]
                or state.get("pending_targets") != result.get("pending_targets")):
            raise ValueError("Saved account/daily/order prefix does not end at the last complete session")
        if count and (round(float(daily[-1]["equity"]) * 100) != Ledger(state, config).equity()
                      or round(float(daily[-1]["cash"]) * 100) != state["cash"]):
            raise ValueError("Saved daily balance differs from its original Ledger")
        if any(row.get("day") is not None and (last is None or row["day"] > last)
               for collection in (state["orders"], state["fills"]) for row in collection.values()):
            raise ValueError("Saved failed account contains activity after its completed daily prefix")
        if result["status"] == "completed":
            if count != len(days) or result.get("error") is not None:
                raise ValueError("Completed saved case does not contain the full exact window")
        elif result["status"] == "failed":
            error = result.get("error") or {}
            if (count >= len(days) or error.get("type") != "ValueError"
                    or not str(error.get("message", "")).startswith("Unknown required raw quote/status:")
                    or error.get("day") != days[count]):
                raise ValueError("Only the exact required-quote validation failure can resume")
        else:
            raise ValueError("Running or unknown cases cannot seed incomplete resume")
        cases[arm] = {"folder": folder, "state": state, "daily": daily, "result": result,
                      "complete": result["status"] == "completed"}
    return {"source_result": {"path": str(path), "sha256": references[str(path)]},
            "source_files": references, "cases": cases}


def run(batch_path, calendar_path, listing_path, out, *, native_gap_view=None, corrected_only=False, resume_incomplete=None,
        action_event_view=None, action_reference_view=None, execution_native_view=None):
    if type(corrected_only) is not bool:
        raise ValueError("Corrected-only diagnostic mode must be explicit boolean")
    action_accounting_requested = action_event_view is not None or action_reference_view is not None
    if action_accounting_requested and (action_event_view is None or action_reference_view is None):
        raise ValueError("Action accounting requires its explicit event/reference source pair")
    if action_accounting_requested and (not execution_native_view or not corrected_only or resume_incomplete is not None):
        raise ValueError("Action accounting requires new corrected-only accounts; actionless accounts cannot be upgraded")
    if execution_native_view and (not action_accounting_requested or native_gap_view):
        raise ValueError("Independent whole native execution quotes require the action source pair and no StockDB gap mixing")
    out = Path(out).resolve()
    if resume_incomplete is not None:
        original = Path(resume_incomplete).resolve()
        directory = original if original.is_dir() else original.parent
        if out == directory or directory in out.parents or out in directory.parents:
            raise ValueError("Resume output must be a new directory outside the original incomplete run")
    out.mkdir(parents=True, exist_ok=False)
    source_args = (batch_path, calendar_path, listing_path, native_gap_view)
    raw, closes, scores, instruments, calendar, config, days, refs = (
        _sources(*source_args, execution_native_view=execution_native_view) if execution_native_view else _sources(*source_args))
    arms = ARMS[:2] if corrected_only else ARMS
    accounting = None
    method_id = NATIVE_ACTION_METHOD if execution_native_view else METHOD
    if action_accounting_requested:
        from .stockdb_action_accounting import load_action_accounting
        accounting = load_action_accounting(action_event_view, action_reference_view,
            symbols=list(closes.columns), raw_days=refs["raw_history_context"]["preceding_provider_sessions"] + days,
            replay_days=days, price_source="independent_baostock_raw")
        refs["action_accounting"] = accounting.provenance
    method = {"method": method_id, "declared_at": datetime.now().astimezone().isoformat(), "arms": arms,
        "comparison_mode": "fixed_rule_and_day_local_only" if corrected_only else "fixed_rule_day_local_and_legacy_global_diagnostic",
        "legacy_global_diagnostic_enabled": not corrected_only,
        "config": config.to_dict(), "start": days[0], "end": days[-1], "sources": refs,
        "pool_rule": "same-D active non-ST; known reported listing age; positive126-21 momentum;60vol;20median amount; all saved seed scores",
        "rank_rule": "three raw-seed percentile ranks independently within exact D, then mean",
        "known_halt_zero_open_policy": ("native_explicit_status0_positive_close_preclose_zero_open_valuation_only_no_imputation"
            if execution_native_view else "status0_source_proxy_positive_close_preclose_zero_open_valuation_only_no_imputation"),
        "all_status0_rows_independently_verified": False,
        "forward_path_screening": False, "symbol_cap": None, "threshold_tuning": False}
    resumed = _resume_incomplete(resume_incomplete, out, method, days, config, arms) if resume_incomplete is not None else None
    (out / "method.json").write_text(json.dumps(method, ensure_ascii=False, indent=2), encoding="utf-8")
    scores.to_parquet(out / "saved_predictions_day_local_rank.parquet", index=False)
    base = ControlledFeed(raw, closes, scores, instruments, calendar, config, arm=ARMS[0])
    if accounting is not None:
        base.action_accounting = accounting
        base.actions = accounting.actions
        base.manifest.update({"scope": method_id, "start": days[0], "end": days[-1],
            "historical_price_adjustments": accounting.special_references})
    for day in days:
        base.pool(day, config)
    pd.DataFrame(base.audit).assign(excluded=lambda f: f.excluded.map(json.dumps)).to_csv(out / "eligibility_by_day.csv", index=False)
    pool_rows = [{"date": d, **r} for d in days for r in base.pool(d, config)]
    pd.DataFrame(pool_rows).to_parquet(out / "common_pool.parquet", index=False)
    impacts = []
    for day in ([] if corrected_only else days):
        pool = base.pool(day, config)
        order = lambda arm: [r["symbol"] for r in sorted(pool, key=lambda r: (-r[arm], r["symbol"]))]
        old, new = order(ARMS[2]), order(ARMS[1])
        impacts.append({"date": day, "common_eligible": len(pool), "top5_changed": old[:5] != new[:5],
            "top5_old": old[:5], "top5_corrected": new[:5], "rank_positions_changed": sum(a != b for a, b in zip(old, new))})
    results, reused_cases, new_sessions = [], [], {}
    for arm in arms:
        prior = resumed["cases"][arm] if resumed is not None else None
        if prior is not None and prior["complete"]:
            shutil.copytree(prior["folder"], out / arm)
            results.append(deepcopy(prior["result"]))
            reused_cases.append(arm)
            new_sessions[arm] = 0
            continue
        # Reuse exact input objects and precomputed common pool; only scorer changes.
        feed = object.__new__(ControlledFeed)
        feed.__dict__ = {**base.__dict__, "arm": arm}
        context = RunContext(method_id + ":" + arm, TradingMode.REPLAY)
        state = prior["state"] if prior is not None else new_state(config.initial_cash)
        daily, error = (prior["daily"] if prior is not None else []), None
        reused_sessions = len(daily)
        for index in range(reused_sessions, len(days)):
            day = days[index]
            try:
                daily.append(advance(state, feed, day, config, index, context=context))
            except Exception as exc:
                error = {"day": day, "type": type(exc).__name__, "message": str(exc)}
                break
        new_sessions[arm] = len(daily)-reused_sessions
        fills = list(state["fills"].values())
        result = {"arm": arm, "status": "failed" if error else "completed", "error": error,
            "sessions": len(daily), "orders": len(state["orders"]), "fills": len(fills),
            "metrics": metrics(daily, config.initial_cash) if not error and fills else None,
            "blocked": sum(e.get("kind") == "order_blocked" for e in state["events"]),
            "pending_targets": state.get("pending_targets"),
            "open_positions": {s: sum(lot["qty"] for lot in lots) for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots)}}
        folder = out / arm
        folder.mkdir()
        for name, value in (("account.json", state), ("result.json", result)):
            (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        pd.DataFrame(daily).to_csv(folder / "daily.csv", index=False)
        pd.DataFrame(state["orders"].values()).to_csv(folder / "orders.csv", index=False)
        pd.DataFrame(fills).to_csv(folder / "fills.csv", index=False)
        results.append(result)
    record = {"schema": method_id, "status": "completed" if all(r["status"] == "completed" for r in results) else "incomplete",
        "method": method, "rank_impact": impacts, "results": results, "model_refit": False,
        "future_labels_used_for_score": False, "future_dates_used_for_corrected_rank": False,
        "actual_historical_publication_verified": False, "new_untouched_test": False,
        "metric_basis": ("native_raw_close_with_reported_action_components_research_diagnostic" if accounting is not None
                         else "raw_close_mark_to_market_research_diagnostic"),
        "reported_action_accounting_attached": accounting is not None,
        "total_return_verified": False, "returns_reportable": False,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False, "broker_account_or_database_mutated": False,
        "limitations": ["Already inspected cross-lineage StockDB source migration diagnostic, not an untouched RULE/LGB comparison.",
            "Provider-close cumulative pct_chg proxy differs from original BaoStock model training lineage.",
            "Common positive-momentum gate and old fixed5-session portfolio are explicit diagnostic controls, not the original low-vol monthly strategy.",
            "Snapshot universe, listing knowledge time, raw volume-derived status, actions and total return are not independently verified.",
            "Actual model and input acquisition occurred after historical decisions; 15:05 SignalFrame is a retrospective timing hypothesis.",
            ("Legacy-global diagnostic was explicitly excluded before this run; no new global-rank comparison is claimed."
             if corrected_only else "Legacy-global arm intentionally retains future-distribution dependency and is never a tradable signal."),
            "Open positions and pending targets remain; no artificial terminal liquidation."]}
    if resumed is not None:
        if any(_hash(file) != digest for file, digest in resumed["source_files"].items()):
            raise ValueError("Original incomplete run changed during continuation")
        record["resume"] = {"source_result": resumed["source_result"], "source_files": resumed["source_files"],
            "completed_cases_reused_without_engine": reused_cases, "new_sessions_by_arm": new_sessions,
            "reused_sessions_by_arm": {arm: len(value["daily"])-new_sessions[arm]
                if not value["complete"] else len(value["daily"]) for arm, value in resumed["cases"].items()},
            "original_files_modified": False, "cash_positions_pending_reset": False,
            "native_source_opened_at_ignored_for_identity_only": True}
    if accounting is not None:
        pd.DataFrame(accounting.checks).to_csv(out / "action_reference_checks.csv", index=False)
        record["action_reference_checks"] = {
            "path": str(out / "action_reference_checks.csv"), "count": len(accounting.checks),
            "price_source": "independent_native_whole_quotes_and_exact_prior_session",
            "old_native_reference_match_reused": False}
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-run", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument("--listing-master", type=Path, required=True)
    parser.add_argument("--native-gap-view", type=Path, action="append",
        help="Repeat for explicit completed native sources; disjoint requested days, absent keys only")
    parser.add_argument("--corrected-only", action="store_true", help="Run fixed RULE and day-local LGBM only; exclude the already studied legacy-global diagnostic")
    parser.add_argument("--resume-incomplete", type=Path,
        help="Explicit original incomplete run; reuse complete cases and continue only its exact quote-validation prefix into a new out")
    parser.add_argument("--action-event-view", type=Path,
        help="Explicit posthoc reported-action source for new corrected-only research accounts")
    parser.add_argument("--action-reference-view", type=Path,
        help="Matching saved evidence pair; StockDB raw references are recomputed, never borrowed")
    parser.add_argument("--execution-native-view", type=Path, action="append",
        help="Repeat explicit disjoint whole raw quote sources; StockDB pre_close is never an execution reference")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record = run(args.batch_run, args.calendar, args.listing_master, args.out,
            native_gap_view=args.native_gap_view, corrected_only=args.corrected_only, resume_incomplete=args.resume_incomplete,
            action_event_view=args.action_event_view, action_reference_view=args.action_reference_view,
            execution_native_view=args.execution_native_view)
    except Exception as exc:
        record = {"status": "failed_input_or_command", "error": f"{type(exc).__name__}: {exc}", "execution_admitted": False}
        # Do not overwrite any earlier run when an existing output is rejected.
        if args.out.exists() and not any(args.out.iterdir()):
            (args.out / "failure.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False))
        return 1
    print(json.dumps({"out": str(args.out), "status": record["status"], "results": record["results"]}, ensure_ascii=False))
    return 0 if record["status"] == "completed" else 1
